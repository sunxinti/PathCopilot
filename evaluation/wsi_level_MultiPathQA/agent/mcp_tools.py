#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP工具定义模块 (Model Context Protocol Tools)

按照Anthropic MCP协议标准定义PathCopilot的可调用工具。
这些工具可在Plan阶段由LLM自主决定是否调用。

MCP协议参考: https://modelcontextprotocol.io/
"""

from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field, asdict
import json
import logging

logger = logging.getLogger(__name__)


# ==================== MCP工具定义结构 ====================

@dataclass
class MCPToolParameter:
    """MCP工具参数定义"""
    name: str
    type: str  # string, array, number, boolean, object
    description: str
    required: bool = True
    items: Optional[Dict] = None  # 用于array类型
    enum: Optional[List] = None  # 枚举值


@dataclass
class MCPTool:
    """MCP工具定义（符合JSON Schema规范）"""
    name: str
    description: str
    parameters: List[MCPToolParameter] = field(default_factory=list)
    handler: Optional[Callable] = None  # 实际执行函数
    
    def to_json_schema(self) -> Dict:
        """转换为MCP标准JSON Schema格式"""
        properties = {}
        required = []
        
        for param in self.parameters:
            prop = {
                "type": param.type,
                "description": param.description
            }
            if param.items:
                prop["items"] = param.items
            if param.enum:
                prop["enum"] = param.enum
            
            properties[param.name] = prop
            
            if param.required:
                required.append(param.name)
        
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    
    def to_prompt_format(self) -> str:
        """转换为Prompt中的工具描述格式"""
        params_desc = []
        for param in self.parameters:
            req = "必填" if param.required else "可选"
            params_desc.append(f"    - {param.name} ({param.type}, {req}): {param.description}")
        
        params_str = "\n".join(params_desc) if params_desc else "    无参数"
        
        return f"""🔧 **{self.name}**
  描述: {self.description}
  参数:
{params_str}"""


# ==================== PathCopilot MCP工具定义 ====================

# 导入实际的处理函数（延迟导入避免循环依赖）
def _get_knowledge_handler():
    try:
        from .knowledge_retrieval import search_pubmed, get_knowledge_for_report, search_grading_criteria
        return search_pubmed, get_knowledge_for_report, search_grading_criteria
    except ImportError:
        from knowledge_retrieval import search_pubmed, get_knowledge_for_report, search_grading_criteria
        return search_pubmed, get_knowledge_for_report, search_grading_criteria


# 工具1: PubMed文献检索
TOOL_SEARCH_PUBMED = MCPTool(
    name="search_pubmed",
    description="检索PubMed医学文献数据库，获取与当前诊断相关的循证医学参考。适用于：诊断不确定时需要文献支持、遇到罕见形态学特征、需要最新研究证据。",
    parameters=[
        MCPToolParameter(
            name="keywords",
            type="array",
            description="检索关键词列表（英文），如 ['endometrioid adenocarcinoma', 'LVSI', 'prognosis']",
            items={"type": "string"}
        ),
        MCPToolParameter(
            name="max_results",
            type="number",
            description="最大返回文献数量",
            required=False
        )
    ]
)


# 工具2: WHO诊断标准查询
TOOL_GET_GRADING_CRITERIA = MCPTool(
    name="get_grading_criteria",
    description="获取WHO标准的肿瘤诊断和分级标准。适用于：确认FIGO分级标准、核实组织学分型标准、需要标准化诊断依据。当需要确定组织学分型或鉴别多种肿瘤类型时，请使用 'who_2020_classification' 获取完整的WHO 2020子宫内膜癌分类总览。",
    parameters=[
        MCPToolParameter(
            name="tumor_type",
            type="string",
            description="肿瘤类型，参照 WHO 2020 女性生殖道肿瘤分类，如 'endometrioid carcinoma', 'serous carcinoma', 'clear cell carcinoma', 'mesonephric-like adenocarcinoma', 'undifferentiated carcinoma' 等；不限于列举类型。当需要全面了解子宫内膜癌各组织学类型的定义和鉴别要点时，请选择 'who_2020_classification'。",
            enum=["endometrioid carcinoma", "serous carcinoma", "clear cell carcinoma", "mucinous carcinoma", "mesonephric-like adenocarcinoma", "carcinosarcoma", "undifferentiated carcinoma", "dedifferentiated carcinoma", "mixed carcinoma", "who_2020_classification", "other"]
        )
    ]
)


# 工具3: 邻近区域查询（空间搜索）
TOOL_GET_NEIGHBOR_CONTEXT = MCPTool(
    name="get_neighbor_context",
    description="获取当前ROI邻近区域的分析结果。适用于：判断肿瘤边界、评估浸润范围、需要空间上下文辅助诊断。",
    parameters=[
        MCPToolParameter(
            name="roi_id",
            type="string",
            description="当前ROI的ID，如 'R3' 或 'R2-B1'"
        ),
        MCPToolParameter(
            name="max_neighbors",
            type="number",
            description="最多返回几个邻居的结果",
            required=False
        )
    ]
)


# 工具4: 证据链查询
TOOL_GET_EVIDENCE_CHAIN = MCPTool(
    name="get_evidence_chain",
    description="获取当前已收集的所有证据链摘要。适用于：生成最终报告前回顾所有发现、检查证据完整性、审计诊断依据。",
    parameters=[
        MCPToolParameter(
            name="filter_type",
            type="string",
            description="筛选证据类型",
            required=False,
            enum=["all", "tumor", "lvsi", "necrosis", "normal"]
        )
    ]
)


# ==================== MCP工具注册表 ====================

class MCPToolRegistry:
    """MCP工具注册表 - 管理所有可用工具"""
    
    def __init__(self):
        self._tools: Dict[str, MCPTool] = {}
        self._handlers: Dict[str, Callable] = {}
    
    def register(self, tool: MCPTool, handler: Callable = None):
        """注册工具"""
        self._tools[tool.name] = tool
        if handler:
            self._handlers[tool.name] = handler
        logger.debug(f"🔧 [MCP] 注册工具: {tool.name}")
    
    def get_tool(self, name: str) -> Optional[MCPTool]:
        """获取工具定义"""
        return self._tools.get(name)
    
    def get_handler(self, name: str) -> Optional[Callable]:
        """获取工具处理函数"""
        return self._handlers.get(name)
    
    def list_tools(self) -> List[MCPTool]:
        """列出所有工具"""
        return list(self._tools.values())
    
    def to_json_schema_list(self) -> List[Dict]:
        """导出所有工具的JSON Schema（MCP标准格式）"""
        return [tool.to_json_schema() for tool in self._tools.values()]
    
    def to_prompt_format(self) -> str:
        """生成用于Prompt的工具列表描述"""
        tool_descriptions = [tool.to_prompt_format() for tool in self._tools.values()]
        return "\n\n".join(tool_descriptions)
    
    def execute(self, tool_name: str, arguments: Dict[str, Any], context: Dict = None) -> Dict[str, Any]:
        """
        执行工具调用
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            context: 执行上下文（如agent_state）
        
        Returns:
            Dict: {"success": bool, "result": Any, "error": str|None}
        """
        if tool_name not in self._tools:
            return {"success": False, "result": None, "error": f"未知工具: {tool_name}"}
        
        handler = self._handlers.get(tool_name)
        if not handler:
            return {"success": False, "result": None, "error": f"工具 {tool_name} 无处理函数"}
        
        try:
            # 传入context（如果handler支持）
            import inspect
            sig = inspect.signature(handler)
            if 'context' in sig.parameters or 'agent_state' in sig.parameters:
                result = handler(**arguments, context=context)
            else:
                result = handler(**arguments)
            
            logger.info(f"✅ [MCP] 工具 {tool_name} 执行成功")
            return {"success": True, "result": result, "error": None}
        
        except Exception as e:
            logger.error(f"❌ [MCP] 工具 {tool_name} 执行失败: {e}")
            return {"success": False, "result": None, "error": str(e)}


# ==================== 创建全局工具注册表 ====================

def create_pathcopilot_registry() -> MCPToolRegistry:
    """创建PathCopilot的MCP工具注册表"""
    registry = MCPToolRegistry()
    
    # 注册工具（处理函数在执行时动态绑定）
    registry.register(TOOL_SEARCH_PUBMED)
    registry.register(TOOL_GET_GRADING_CRITERIA)
    registry.register(TOOL_GET_NEIGHBOR_CONTEXT)
    registry.register(TOOL_GET_EVIDENCE_CHAIN)
    
    return registry


# 全局注册表实例
_global_registry: Optional[MCPToolRegistry] = None


def get_mcp_registry() -> MCPToolRegistry:
    """获取全局MCP工具注册表"""
    global _global_registry
    if _global_registry is None:
        _global_registry = create_pathcopilot_registry()
    return _global_registry


def bind_tool_handlers(registry: MCPToolRegistry, agent_state: Dict = None):
    """
    绑定工具处理函数（在运行时调用）
    
    Args:
        registry: MCP工具注册表
        agent_state: Agent状态（用于空间搜索等需要上下文的工具）
    """
    try:
        # 绑定知识检索工具
        from .knowledge_retrieval import search_pubmed
    except ImportError:
        from knowledge_retrieval import search_pubmed
    
    # search_pubmed
    def handle_search_pubmed(keywords: List[str], max_results: int = 3, **kwargs):
        results = search_pubmed(keywords, max_results=max_results)
        if results:
            from .knowledge_retrieval import format_knowledge_context
            return format_knowledge_context(results)
        return "未找到相关文献"
    
    registry._handlers["search_pubmed"] = handle_search_pubmed
    
    # get_grading_criteria
    def handle_get_grading_criteria(tumor_type: str, **kwargs):
        return "暂无结果"
    
    registry._handlers["get_grading_criteria"] = handle_get_grading_criteria
    
    # get_neighbor_context（需要agent_state）
    def handle_get_neighbor_context(roi_id: str, max_neighbors: int = 3, context: Dict = None, **kwargs):
        if not context:
            return "无可用上下文"
        # 从agent_loop导入函数
        try:
            from .agent_loop import get_neighbor_context
        except ImportError:
            from agent_loop import get_neighbor_context
        return get_neighbor_context(roi_id, context, max_neighbors=max_neighbors)
    
    registry._handlers["get_neighbor_context"] = handle_get_neighbor_context
    
    # get_evidence_chain（需要agent_state）
    def handle_get_evidence_chain(filter_type: str = "all", context: Dict = None, **kwargs):
        if not context:
            return "无可用证据池"
        evidence_pool = context.get('evidence_pool')
        if not evidence_pool:
            return "证据池为空"
        
        if hasattr(evidence_pool, 'generate_chain'):
            return evidence_pool.generate_chain()
        return "证据池格式不支持"
    
    registry._handlers["get_evidence_chain"] = handle_get_evidence_chain
    
    logger.info(f"🔧 [MCP] 已绑定 {len(registry._handlers)} 个工具处理函数")


# ==================== MCP工具调用解析 ====================

def parse_tool_call_from_response(response: str) -> Optional[Dict[str, Any]]:
    """
    从LLM响应中解析工具调用请求
    
    支持格式：
    ```tool_call
    TOOL: tool_name
    PARAM1: value1
    PARAM2: value2
    ```
    
    或JSON格式：
    ```json
    {"tool": "tool_name", "arguments": {...}}
    ```
    
    Returns:
        Dict: {"tool": str, "arguments": dict} 或 None
    """
    import re
    
    # 检查是否明确表示不需要工具
    if "NO_TOOL_NEEDED" in response.upper() or "不需要" in response:
        return None
    
    # 尝试解析 tool_call 格式
    tool_call_match = re.search(
        r'```tool_call\s*\n(.*?)```',
        response,
        re.DOTALL | re.IGNORECASE
    )
    
    if tool_call_match:
        content = tool_call_match.group(1)
        
        # 解析 TOOL: xxx
        tool_match = re.search(r'TOOL:\s*(\w+)', content, re.IGNORECASE)
        if tool_match:
            tool_name = tool_match.group(1).lower()
            arguments = {}
            
            # 解析 KEYWORDS: xxx 或其他参数
            keywords_match = re.search(r'KEYWORDS?:\s*(.+?)(?:\n|$)', content, re.IGNORECASE)
            if keywords_match:
                keywords_str = keywords_match.group(1).strip()
                arguments["keywords"] = [k.strip() for k in keywords_str.split(',')]
            
            # 解析 TUMOR_TYPE: xxx
            tumor_match = re.search(r'TUMOR_TYPE:\s*(.+?)(?:\n|$)', content, re.IGNORECASE)
            if tumor_match:
                arguments["tumor_type"] = tumor_match.group(1).strip()
            
            # 解析 ROI_ID: xxx
            roi_match = re.search(r'ROI_ID:\s*(.+?)(?:\n|$)', content, re.IGNORECASE)
            if roi_match:
                arguments["roi_id"] = roi_match.group(1).strip()
            
            return {"tool": tool_name, "arguments": arguments}
    
    # 尝试解析JSON格式
    json_match = re.search(r'```json\s*\n(.*?)```', response, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if "tool" in data:
                return {"tool": data["tool"], "arguments": data.get("arguments", {})}
        except json.JSONDecodeError:
            pass
    
    # 简化匹配：直接在文本中查找工具名
    for tool_name in ["search_pubmed", "get_grading_criteria", "get_neighbor_context", "get_evidence_chain"]:
        if tool_name in response.lower():
            # 尝试提取参数
            arguments = {}
            keywords_match = re.search(r'(?:keywords?|关键词)[:\s]*["\']?([^"\'}\n]+)["\']?', response, re.IGNORECASE)
            if keywords_match:
                arguments["keywords"] = [k.strip() for k in keywords_match.group(1).split(',')]
            
            return {"tool": tool_name, "arguments": arguments}
    
    return None


# ==================== MCP标准输出格式 ====================

def format_tool_result_for_llm(tool_name: str, result: Dict[str, Any]) -> str:
    """
    格式化工具调用结果，用于传回LLM
    
    Args:
        tool_name: 工具名称
        result: 工具执行结果
    
    Returns:
        str: 格式化的结果文本
    """
    if result["success"]:
        return f"""【MCP工具调用结果】
🔧 工具: {tool_name}
✅ 状态: 成功

{result["result"]}
"""
    else:
        return f"""【MCP工具调用结果】
🔧 工具: {tool_name}
❌ 状态: 失败
错误: {result["error"]}
"""
