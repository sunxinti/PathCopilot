"""
PathCopilot Agent Loop 提示词模板

PathCopilot 的核心提示词设计，采用 Reason-Plan-Action-Memory 四阶段循环。
每个 Sub-agent 在分析 ROI 时都会执行这个循环。

设计原则：
1. 结构清晰：AI 明确知道自己在执行 Agent Loop 的哪个阶段
2. 元认知能力：AI 可以自主选择动作（下钻/停止/回溯/检索）
3. 上下文继承：每个节点都能获得父级的完整 Reason+Plan+Action 信息
4. 目标导向：所有分析均围绕用户当前问题或器官无关的形态学描述任务
"""

from typing import Optional, Dict, List

GENERAL_PATHOLOGY_OBJECTIVE = """
围绕用户当前问题，仅依据可见的 WSI、ROI 和已记录证据进行病理学分析。
不得预设器官、肿瘤类型、分级体系、浸润方式或诊断结论。
输出：结论；形态学依据与 ROI 来源；主要鉴别及支持/反对证据；局限与建议；证据支持度 0–4。
用户未提供明确问题时，只客观描述组织学特征与尚缺信息，不得虚构诊断。
"""

# ==================== Plan 动作定义 ====================

PLAN_ACTIONS = {
    'A': {
        'name': '下钻放大',
        'description': '选择子区域继续放大观察',
        'when_to_use': '发现可疑病变，需要更高倍镜确认细节',
        'requires_params': True,
        'param_description': '选择的区域编号列表'
    },
    'B': {
        'name': '停止下钻',
        'description': '当前倍率已获得足够证据，无需继续放大',
        'when_to_use': '当前视野已能明确回答诊断问题',
        'requires_params': False,
        'param_description': None
    },
    'C': {
        'name': '回溯/重路由',
        'description': '当前视野无诊断价值，建议切换到其他区域',
        'when_to_use': '遇到大面积坏死(>50%)/严重伪影/空白区域/完全偏离目标',
        'requires_params': True,
        'param_description': '回溯原因和建议的下一步'
    },
    'D': {
        'name': '检索知识库',
        'description': '查询外部知识（WHO分级标准、鉴别诊断指南等）',
        'when_to_use': '遇到罕见形态或不确定的鉴别诊断',
        'requires_params': True,
        'param_description': '需要检索的具体问题'
    }
}


# ==================== 各倍率的 Reason 指导 ====================

REASON_GUIDANCE = {
    10: {
        'name': '低倍镜（10×）',
        'focus': '全局架构观察',
        'key_observations': [
            '组织类型、取材完整性与整体空间分布',
            '上皮、间质及其他组织成分的整体关系',
            '结构紊乱、坏死、炎症或伪影的区域',
            '病变与邻近正常/反应性组织的边界',
            '与用户当前问题相关且需要进一步放大的区域'
        ],
        'can_answer': ['全片架构、范围与取材代表性'],
        'needs_higher': ['细胞性质、精细结构与关键鉴别'],
        'constraints': '不得用低倍假设完成细胞谱系定性、细胞学分级或依赖高倍证据的最终诊断；10× 下的恶性解释只能作为等待 40×/160× 核验的结构性怀疑，不得仅凭低倍外观写成明确癌、明确恶性细胞或明确破坏性浸润。'
    },
    40: {
        'name': '中倍镜（40×）',
        'focus': '形态学分析',
        'key_observations': [
            '与当前问题相关的组织结构和生长模式',
            '结构的一致性与异质性',
            '病变边界及其与邻近组织的关系',
            '间质反应：促纤维增生、炎症浸润',
            '需要高倍确认的代表性、可疑或矛盾区域'
        ],
        'can_answer': ['中倍结构证据与主要鉴别方向'],
        'needs_higher': ['细胞学确认与细小结构鉴别'],
        'constraints': '深染、密集或实性外观本身不能证明癌巢、浸润或恶性细胞；应使用证据分级语言，且下级必须独立核验。'
    },
    160: {
        'name': '高倍镜（160×）',
        'focus': '细胞学分析',
        'key_observations': [
            '核异型性：核大小、形态、染色质、核仁',
            '核与细胞质形态、细胞排列和分化线索',
            '核分裂象及其他与当前问题相关的细节',
            '技术伪影、炎细胞或其他良性模拟的排除',
            '对上级可疑区域的独立确认或否定'
        ],
        'can_answer': ['局部细胞性质与精细形态'],
        'constraints': '先核验可评估性和正向谱系/分化线索，不得仅靠排除法反推；单个定向高倍视野不能外推全片比例、分级、范围或患者级结论。',
        'is_terminal': True  # 终端节点，不再下钻
    }
}


# ==================== Agent Loop 主提示词模板 ====================

def get_agent_loop_prompt(
    zoom_level: int,
    roi_id: str,
    parent_context: str = "",
    neighbor_context: str = "",
    num_child_regions: int = 16,
    organ: str = "未指定标本",
    diagnostic_objective: str = ""
) -> str:
    """
    生成 PathCopilot Agent Loop 提示词
    
    Args:
        zoom_level: 当前放大倍率 (10/40/160)
        roi_id: 当前 ROI 的 ID
        parent_context: 父级路径上下文（Reason+Plan+Action）
        neighbor_context: 邻近区域参考（空间搜索结果）
        num_child_regions: 子区域数量（用于下钻选择）
        organ: 器官类型
    
    Returns:
        str: 完整的 Agent Loop 提示词
    """
    
    guidance = REASON_GUIDANCE.get(zoom_level, REASON_GUIDANCE[40])
    is_terminal = guidance.get('is_terminal', False)
    
    # ===== 头部：角色和位置 =====
    prompt = f"""你是 PathCopilot 的子智能体，正在执行 **PathCopilot Agent Loop**（Reason-Plan-Action-Memory 循环）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 **当前位置**：{guidance['name']} | ROI: {roi_id} | 标本：{organ}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**当前诊断目标与回答契约**：
{diagnostic_objective.strip() or GENERAL_PATHOLOGY_OBJECTIVE}
"""

    # ===== 上下文部分 =====
    if parent_context:
        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📂 **路径上下文**（父级节点的分析结果）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{parent_context}
"""

    if neighbor_context:
        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🗺️ **邻近区域参考**（空间搜索结果）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{neighbor_context}
"""

    # ===== Reason 阶段 =====
    key_obs_text = '\n'.join([f"   - {obs}" for obs in guidance['key_observations']])
    can_answer_text = ', '.join(guidance['can_answer'])
    
    prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 🔬 第一步：Reason（观察与分析）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

请分析当前视野。在 **{guidance['name']}** 下，你应重点观察：
{key_obs_text}

在此倍率下，你应该能够回答或提供线索：**{can_answer_text}**

**尺度边界**：{guidance.get('constraints', '')}
"""

    # ===== Plan 阶段 =====
    if is_terminal:
        # 160x 是终端节点，Plan 选项不同
        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 📋 第二步：Plan（规划下一步）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

你正在 **最高倍率（160×）**，这是分析路径的终点。请选择：

**动作 B：完成分析** ✅
- 你已完成对该区域的细胞学分析
- 总结你对当前问题的证据贡献

**动作 C：回溯/重路由** ⏪
- 当前视野无诊断价值（坏死>50%/伪影/空白）
- 建议返回上级蓝框，选择其他绿框

**动作 D：检索知识库** 📚
- 遇到罕见形态，需要查询 WHO 分级标准或鉴别诊断指南
"""
    else:
        # 10x/40x 有下钻选项
        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 📋 第二步：Plan（规划下一步）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

基于你的 Reason 结果，请选择下一步动作：

**动作 A：下钻放大** 🔍
- 从 {num_child_regions} 个子区域中选择最有诊断价值的 Top-K（建议 3-6 个）
- 适用：发现可疑病变，需要更高倍镜确认细节
- 参数：选择的区域编号列表，如 [1, 5, 9, 12]

**动作 B：停止下钻** ✅
- 当前倍率已获得足够证据，无需继续放大
- 适用：当前视野已能明确回答诊断问题
- 注意：当前路径已足够即可停止，不必每层都下钻

**动作 C：回溯/重路由** ⏪
- 当前视野无诊断价值，建议切换到其他区域
- 触发条件：大面积坏死(>50%)/严重伪影干扰/空白区域/完全偏离诊断目标
- 参数：回溯原因 + 建议的下一步

**动作 D：检索知识库** 📚
- 查询外部知识（WHO分级标准、鉴别诊断指南等）
- 适用：遇到罕见形态或不确定的鉴别诊断
- 参数：需要检索的具体问题
"""

    # ===== 输出格式 =====
    prompt += """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 📝 输出格式（请严格按此格式输出）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【Reason】
（描述你在当前视野观察到的形态学特征，按上述重点观察项展开）

【Plan】
选择动作：A / B / C / D
动作理由：（简要说明为什么选择这个动作）
动作参数：
- 如果 A：选择的区域 = [编号列表]，选择理由：...
- 如果 C：回溯原因 = ...，建议下一步 = ...
- 如果 D：检索问题 = ...

【Evidence】
（本次观察获得的关键证据，用于更新路径上下文）
- evidence: ...（与当前目标相关的直接形态学证据及 ROI 来源）
- limitations_or_counterevidence: ...（反证、良性模拟、尺度或取材局限）
- confidence: N（0–4）
"""

    return prompt


# Backward-compatible import name for integrations built before the terminology
# was corrected. MCP remains the tool transport; the reasoning cycle is the
# PathCopilot Agent Loop.
get_mcp_loop_prompt = get_agent_loop_prompt


# ==================== 辅助函数：格式化上下文 ====================

def format_parent_context(
    parent_level: str,
    parent_id: str,
    reason: str,
    plan_action: str,
    plan_params: str,
    evidence: str
) -> str:
    """
    格式化父级上下文，用于传递给子节点
    
    Args:
        parent_level: 父级倍率名称（如 "红框 10×"）
        parent_id: 父级 ROI ID
        reason: 父级的 Reason 输出
        plan_action: 父级选择的动作
        plan_params: 动作参数
        evidence: 父级收集的证据
    
    Returns:
        str: 格式化的上下文文本
    """
    return f"""
=== {parent_level} | {parent_id} ===
【Reason】
{reason}

【Plan】
动作：{plan_action}
参数：{plan_params}

【Evidence】
{evidence}
"""


def format_full_path_context(path_nodes: List[Dict]) -> str:
    """
    格式化完整路径上下文（从根到当前节点的所有父级）
    
    Args:
        path_nodes: 路径节点列表，每个节点包含 level, id, reason, plan, evidence
    
    Returns:
        str: 格式化的完整路径上下文
    """
    if not path_nodes:
        return ""
    
    context_parts = []
    for node in path_nodes:
        level_name = {10: "红框 10×", 40: "蓝框 40×", 160: "绿框 160×"}.get(
            node.get('zoom_level', 0), 
            f"未知 {node.get('zoom_level', '?')}×"
        )
        
        part = f"""
───────────────────────────────────
📍 {level_name} | {node.get('id', '?')}
───────────────────────────────────
【Reason】
{node.get('reason', '暂无')}

【Plan】
{node.get('plan', '暂无')}

【Evidence】
{node.get('evidence', '暂无')}
"""
        context_parts.append(part)
    
    return "\n".join(context_parts)


# ==================== 解析 AI 输出 ====================

def _load_action_contract():
    """Load the shared parser without importing agent_loop.py."""
    try:
        from agent.action_contract import parse_mcp_response as _parse
        return _parse
    except ImportError:
        pass
    import importlib.util
    from pathlib import Path as _Path
    path = _Path(__file__).resolve().parent / "action_contract.py"
    spec = importlib.util.spec_from_file_location(
        "_pathcopilot_action_contract_standalone", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_mcp_response


def parse_agent_loop_response(response: str, valid_indices=None) -> Dict:
    """Thin wrapper around the shared action-first parser."""
    return _load_action_contract()(response, valid_indices=valid_indices)


# Backward-compatible alias; new code should use parse_agent_loop_response.
parse_mcp_response = parse_agent_loop_response


# ==================== 动作执行建议 ====================

def get_action_recommendation(parsed_response: Dict) -> str:
    """
    根据解析结果返回动作执行建议
    
    Args:
        parsed_response: parse_agent_loop_response 的返回值
    
    Returns:
        str: 动作执行建议
    """
    action = parsed_response.get('plan_action', '')
    params = parsed_response.get('plan_params', {})
    
    if action == 'A':
        regions = params.get('regions', [])
        return f"执行下钻：选择区域 {regions}，继续 Agent Loop"
    
    elif action == 'B':
        return "停止下钻：当前路径分析完成，收集证据到 EvidencePool"
    
    elif action == 'C':
        reason = params.get('reason', '未知原因')
        next_step = params.get('next_step', '返回上级')
        return f"执行回溯：{reason}，建议 {next_step}"
    
    elif action == 'D':
        query = params.get('query', '未指定')
        return f"执行知识检索：查询 '{query}'，结果将加入上下文后重新分析"
    
    else:
        return "未能解析动作，使用默认行为"
