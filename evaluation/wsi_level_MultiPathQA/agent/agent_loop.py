# -*- coding: utf-8 -*-
"""
Agent循环逻辑模块
实现WSI Agent的感知→行动→推理循环
模拟病理医生工作流：缩略图预筛选 → 多尺度ROI分析 → 病理学工具集成 → 迭代推理
"""

# IMPORTANT — legacy benchmark identifiers
# The internal values 10 / 40 / 160 (and the prompt labels 10× / 40× / 160×)
# are routing identifiers retained to reproduce the original evaluation.
# They do NOT denote optical-objective or image-acquisition magnifications.
#
# Paper FOV hierarchy  <->  this snapshot
#   FOV Level 0  = global thumbnail
#   FOV Level 1  = wider relative FOV     (legacy ID 10,  red)
#   FOV Level 2  = intermediate FOV       (legacy ID 40,  blue)
#   FOV Level 3  = narrower relative FOV  (legacy ID 160, green)
#
# Diagnostic ROIs are cropped from native WSI pixel space
# (OpenSlide pyramid level 0).  That OpenSlide level is unrelated to
# FOV Level 0.  No 160× optical image or super-resolution is generated.

import os
import time
import json
import ast
import uuid
import random
import re
import threading
import numpy as np
from datetime import datetime
from pathlib import Path
import openslide
from PIL import Image
# 禁用 Pillow 的像素限制（WSI 图像通常很大，超过默认的 1.78 亿像素限制）
Image.MAX_IMAGE_PIXELS = None
import logging
import io
import base64

logger = logging.getLogger(__name__)

# ⭐ 全局 OpenSlide 读取锁：序列化所有 read_region 调用，
# 防止 NDPI 文件在 WSL2 9P 文件系统上并发随机读取导致 "Corrupt JPEG data"。
# 使用 RLock（可重入锁）防止同一线程内嵌套调用死锁。
_openslide_lock = threading.RLock()

# Candidate-filter policy is part of the deterministic benchmark protocol and
# cache namespace.  Keep the primary 10x/40x/160x geometry; the 2.5x/5x
# profiles and their thresholds belong only to the field-of-view ablations.
VIEW_PROFILE_NAME = "thumbnail/10x/40x/160x"
_DEFAULT_TISSUE_MASK_POLICY = (
    "relaxed_gray_std_no_morphology_v3_tissue15_fail_closed"
)
TISSUE_MASK_POLICY = os.environ.get(
    "PATHCOPILOT_MASK_POLICY",
    _DEFAULT_TISSUE_MASK_POLICY,
).strip() or _DEFAULT_TISSUE_MASK_POLICY


def _read_tissue_content_threshold():
    """Read the tissue threshold, falling back to the validated 15%."""
    try:
        value = float(os.environ.get("PATHCOPILOT_TISSUE_THRESHOLD", "0.15"))
    except (TypeError, ValueError):
        return 0.15
    return value if 0.0 < value <= 1.0 else 0.15


TISSUE_CONTENT_THRESHOLD = _read_tissue_content_threshold()

# ⭐ 器官中立开关：MultiPathQA 为泛癌/多器官，不能假设乳腺。
# 由评测驱动通过环境变量 PATHCOPILOT_ORGAN 设定（默认中立词，让模型从图像自行判断器官）。
ORGAN_DEFAULT = os.environ.get('PATHCOPILOT_ORGAN', '乳腺标本')

def safe_read_region(slide, location, level, size):
    """线程安全的 OpenSlide read_region 封装"""
    with _openslide_lock:
        return slide.read_region(location, level, size)

def safe_get_slide_metadata(slide):
    """线程安全地读取当前 OpenSlide 对象的元数据。

    不按 ``id(slide)`` 跨对象缓存：OpenSlide 对象释放后 Python 可能复用
    对象 ID，进而把上一张 WSI 的尺寸误用于下一张 WSI。
    """
    with _openslide_lock:
        return {
            'dimensions': slide.dimensions,
            'level_count': slide.level_count,
            'level_dimensions': tuple(slide.level_dimensions),
            'level_downsamples': tuple(slide.level_downsamples),
        }

# 导入AI客户端
try:
    from .gpt_client import call_gpt_vision, call_gemini_mcp
    from .report_generator import generate_detailed_word_report, generate_report_with_images
    from .evidence_model import (
        EvidenceUnit, EvidencePool, parse_evidence_from_analysis, 
        aggregate_evidence, generate_evidence_chain, BACKTRACK_TRIGGERS
    )
    from .knowledge_retrieval import (
        get_knowledge_for_report, extract_diagnostic_keywords, search_pubmed,
        format_knowledge_context, get_diagnostic_criteria
    )
    from .mcp_tools import (
        get_mcp_registry, bind_tool_handlers, parse_tool_call_from_response,
        format_tool_result_for_llm, MCPToolRegistry
    )
except ImportError:
    try:
        from gpt_client import call_gpt_vision, call_gemini_mcp
        from report_generator import generate_detailed_word_report, generate_report_with_images
        from evidence_model import (
            EvidenceUnit, EvidencePool, parse_evidence_from_analysis, 
            aggregate_evidence, generate_evidence_chain, BACKTRACK_TRIGGERS
        )
        from knowledge_retrieval import (
            get_knowledge_for_report, extract_diagnostic_keywords, search_pubmed,
            format_knowledge_context, get_diagnostic_criteria
        )
        from mcp_tools import (
            get_mcp_registry, bind_tool_handlers, parse_tool_call_from_response,
            format_tool_result_for_llm, MCPToolRegistry
        )
    except ImportError:
        # 如果都失败，提供一个空的实现
        def call_gpt_vision(prompt, image_base64):
            logger.warning("Vision API不可用，返回空结果")
            return '{"selected_grids": []}'
        def call_gemini_mcp(messages, model=None, max_tokens=65536, max_retries=3):
            logger.warning("MCP API不可用，返回空结果")
            return None, "API不可用"
        # 证据模型占位
        EvidencePool = None
        parse_evidence_from_analysis = None
        BACKTRACK_TRIGGERS = {}
        # 知识检索占位
        get_knowledge_for_report = None
        get_diagnostic_criteria = None
        # MCP工具占位
        get_mcp_registry = None
        bind_tool_handlers = None
        parse_tool_call_from_response = None
        format_tool_result_for_llm = None
        MCPToolRegistry = None

try:
    from .font_utils import load_grid_label_font
except ImportError:
    from font_utils import load_grid_label_font

# ============== 后端进度消息双语字典 ==============
_BACKEND_MSGS = {
    'analyzing_thumbnail':  {'zh': '正在分析缩略图，识别候选ROI区域...', 'en': 'Analyzing thumbnail, identifying candidate ROI regions...'},
    'thumbnail_done':       {'zh': '识别到 {n} 个红框区域',              'en': '{n} red-box regions identified'},
    'main_path_progress':   {'zh': '主路径分析中 ({done}/{total})',       'en': 'Main path analysis ({done}/{total})'},
    'red_select_done':      {'zh': '🔴 红框选取完成: 已选择 {n} 个红框 (10×)', 'en': '🔴 Red-box selection complete: {n} regions selected (10×)'},
    'start_analyze_red':    {'zh': '🔴 开始分析红框 {roi_id} (10×)',       'en': '🔴 Analyzing red box {roi_id} (10×)'},
    'red_done':             {'zh': '红框 {roi_id} 分析完成，选中 {n} 个蓝框 ({done}/{total})', 'en': 'Red box {roi_id} done, selected {n} blue boxes ({done}/{total})'},
    'blue_done':            {'zh': '蓝框 {roi_id} 分析完成，选中 {n} 个绿框 ({done}/{total})', 'en': 'Blue box {roi_id} done, selected {n} green boxes ({done}/{total})'},
    'ai_analyzing_reds':    {'zh': '🤖 AI 正在分析 {n} 个红框区域... (约需15秒)', 'en': '🤖 AI analyzing {n} red-box regions... (~15s)'},
    'green_progress':       {'zh': '绿框分析中 ({done}/{total})',         'en': 'Green-box analysis ({done}/{total})'},
    'green_done_all':       {'zh': '✅ 绿框分析完成 ({n}个)',              'en': '✅ Green-box analysis complete ({n} boxes)'},
    'generating_report':    {'zh': '正在生成最终报告...',                  'en': 'Generating final report...'},
    'waiting_upload':       {'zh': '等待 {part} 上传完成...',              'en': 'Waiting for {part} to finish uploading...'},
    'slide_done':           {'zh': '已完成 {done}/{total} 张切片分析',    'en': 'Completed {done}/{total} slide analyses'},
    'analysis_done':        {'zh': '分析完成！',                          'en': 'Analysis complete!'},
    'blue_progress':        {'zh': '蓝框分析中 ({done}/{total})',         'en': 'Blue-box analysis ({done}/{total})'},
    'green_select_reason':  {'zh': '🟢 绿框选择原因',                      'en': '🟢 Green-box selection rationale'},
}

def _msg(key: str, lang: str = 'zh', **kwargs) -> str:
    """从双语字典取进度消息，支持 format 占位符"""
    d = _BACKEND_MSGS.get(key, {})
    tmpl = d.get(lang) or d.get('zh') or key
    try:
        return tmpl.format(**kwargs)
    except (KeyError, IndexError):
        return tmpl

# Agent Loop prompts use Reason → Plan → Action → Memory.  Action letters
# remain A/B/C/D/E.  Historical function names still contain "mcp".

CLINICAL_QUESTIONS = ""  # 默认置空；MultiPathQA 评测时由 run_multipathqa_eval 按题注入（器官中立，无结直肠默认）

CLINICAL_QUESTIONS_EN = ""  # 默认置空；MultiPathQA 评测时由 run_multipathqa_eval 按题注入（器官中立，无结直肠默认）

# ============== 输出目录路径管理 ==============
#
# outputs/{name}_{timestamp}_numN/auto/...
# Batch evaluation (PATHCOPILOT_OUTPUT_TAG):
#   outputs/{TAG}/{sample}/auto/...
#

def sanitize_filename(filename: str) -> str:
    """
    清理文件名，移除或替换不适合作为目录名的字符
    
    Args:
        filename: 原始文件名
    
    Returns:
        str: 清理后的文件名（不含扩展名）
    """
    import re
    # 去掉扩展名
    name = Path(filename).stem if filename else 'unknown'
    # 替换特殊字符为下划线（保留中文、英文、数字、下划线、连字符）
    name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', name)
    # 去除连续的下划线
    name = re.sub(r'_+', '_', name)
    # 去除首尾下划线
    name = name.strip('_')
    # 限制长度（避免路径过长）
    if len(name) > 50:
        name = name[:50]
    return name if name else 'unknown'


def get_output_paths(file_id: str, mode: str = 'auto', filename: str = None) -> dict:
    """
    获取统一的输出路径（根据模式和类型自动生成）
    
    命名格式：{原始文件名}_{分析时间戳}，例如：sample_001_20260211_143052
    
    Args:
        file_id: WSI 文件 ID
        mode: retained for call-site compatibility; this snapshot always writes 'auto'
        filename: 原始文件名（可选，用于生成可读的目录名）
    
    Returns:
        dict: {
            'output_dir': Path,
            'steps_dir': Path,
            'report_html': str,
            'report_txt': str,
            'mode_subdir': str,
            'dir_name': str,
        }
    """
    mode_subdir = 'auto'
    
    # 生成可读的目录名：文件名_时间戳_numX
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if filename:
        safe_name = sanitize_filename(filename)
    else:
        safe_name = f"{file_id[:8]}" if file_id else "unknown"
    
    # 统计同名文件已有的分析次数（扫描 outputs/ 下以 safe_name_ 开头的目录）
    outputs_root = Path("outputs")
    run_num = 1
    if outputs_root.exists():
        prefix = f"{safe_name}_"
        existing = [d.name for d in outputs_root.iterdir() if d.is_dir() and d.name.startswith(prefix)]
        run_num = len(existing) + 1
    
    dir_name = f"{safe_name}_{timestamp}_num{run_num}"
    
    # Batch evaluation layout via PATHCOPILOT_OUTPUT_TAG:
    #   outputs/{TAG}/{sample}/auto/...  (e.g. MPQA-tcga-PathCopilot).
    #   Without the env var, keep the interactive naming: {name}_{timestamp}_numN.
    _batch_tag = os.environ.get("PATHCOPILOT_OUTPUT_TAG", "").strip()
    if _batch_tag:
        base_dir = Path(f"outputs/{_batch_tag}/{safe_name}")
        output_dir = base_dir / mode_subdir
        report_html = f'/outputs/{_batch_tag}/{safe_name}/{mode_subdir}/pathology_report.html'
        report_txt = f'/outputs/{_batch_tag}/{safe_name}/{mode_subdir}/pathology_report.txt'
        steps_dir = output_dir / "steps"
        return {
            'output_dir': output_dir, 'steps_dir': steps_dir,
            'report_html': report_html, 'report_txt': report_txt,
            'mode_subdir': mode_subdir, 'base_dir': base_dir, 'dir_name': safe_name,
        }

    base_dir = Path(f"outputs/{dir_name}")
    output_dir = base_dir / mode_subdir
    report_html = f'/outputs/{dir_name}/{mode_subdir}/pathology_report.html'
    report_txt = f'/outputs/{dir_name}/{mode_subdir}/pathology_report.txt'
    steps_dir = output_dir / "steps"
    return {
        'output_dir': output_dir,
        'steps_dir': steps_dir,
        'report_html': report_html,
        'report_txt': report_txt,
        'mode_subdir': mode_subdir,
        'base_dir': base_dir,
        'dir_name': dir_name,
    }


# MCP Plan 动作定义（A/B/C/D/E 编号 + 中文标注，兼顾稳定性和可读性）
MCP_PLAN_ACTIONS = {
    'A': {
        'name': '下钻',
        'description': '选择子区域继续放大观察',
        'when': '发现可疑病变，需要更高倍镜确认细节',
        'requires': 'selected_regions'  # 需要选择区域列表
    },
    'B': {
        'name': '停止',
        'description': '当前证据已足够，无需继续放大',
        'when': '当前倍率已能明确回答诊断问题',
        'requires': 'stop_reason'  # 停止原因
    },
    'C': {
        'name': '回溯',
        'description': '当前视野无诊断价值，需要返回或切换',
        'when': '大面积坏死(>50%) / 严重伪影 / 空白区域 / 完全偏离目标',
        'requires': 'backtrack_reason'  # 回溯原因 + 建议切换位置
    },
    'D': {
        'name': '查库',
        'description': '查询外部知识库辅助判断',
        'when': '遇到罕见形态或不确定的鉴别诊断',
        'requires': 'search_query'  # 检索问题
    },
    'E': {
        'name': '查邻',
        'description': '搜索相邻区域参考',
        'when': '需要参考周围组织关系，如对照正常组织、确认浸润边界',
        'requires': 'neighbor_query'  # 空间关系描述
    }
}


def build_mcp_prompt(
    zoom_level: int,
    roi_id: str,
    num_candidates: int,
    parent_context: str = "",
    neighbor_context: str = "",
    organ: str = ORGAN_DEFAULT,
    is_terminal: bool = False,
    lang: str = 'zh'
) -> str:
    """
    构建 Agent Loop 提示词 (中英文双语)。函数名 build_mcp_prompt 为历史标识。
    
    Args:
        zoom_level: legacy FOV routing id (10/40/160); not an optical magnification
        roi_id: 当前 ROI 的 ID
        num_candidates: 子区域候选数量（用于下钻选择）
        parent_context: 路径上下文（父级分析结果）
        neighbor_context: 邻近区域参考（空间搜索结果）
        organ: 器官类型
        is_terminal: 是否为终点（160x 绿框）
        lang: 语言 ('zh' 或 'en')
    
    Returns:
        str: 规范化的 MCP Prompt
    """
    reason_guidance = _get_reason_guidance(zoom_level, lang)
    plan_options = _get_plan_options(zoom_level, num_candidates, is_terminal, lang)

    if lang == 'en':
        zoom_desc = {
            10: "10× Low-power (Red box) — Global architecture survey",
            40: "40× Medium-power (Blue box) — Morphological analysis",
            160: "160× High-power (Green box) — Cytological assessment"
        }.get(zoom_level, f"{zoom_level}× magnification")

        prompt = f"""You are **PathCopilot Sub-agent**, dispatched and supervised by the **Orchestrator**.

## System Architecture
- **Orchestrator**: handles task scheduling, evidence aggregation, and final diagnosis
- **Sub-agent (you)**: responsible for observing and analyzing the current field of view, and planning the next action via the MCP Loop

Your analysis will be collected by the Orchestrator and integrated into the final pathology diagnostic report.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current position: {zoom_desc} | ROI: {roi_id}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**[Diagnostic Objectives]** The Orchestrator requires you to collect evidence to answer:
{CLINICAL_QUESTIONS_EN}
"""
        if parent_context:
            prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**[Path Context]** (inherited from parent analysis)
{parent_context}
"""
        if neighbor_context:
            prompt += f"""
**[Adjacent Region Reference]** (results from your E(Neighbor) action)
{neighbor_context}
"""
        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Step 1: Reason (Observe & Analyze)

{reason_guidance}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Step 2: Plan (Choose Next Action)

Based on your Reason, select the most appropriate next action:

{plan_options}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Output Format (strictly follow this structure)

[Reason]
(Your morphological observations and analysis, focused on the current magnification level)

[Plan]
Selected action: A(Drill-down) / B(Stop) / C(Backtrack) / D(Knowledge) / E(Neighbor)
Action parameter:
- A(Drill-down): selected region indices, e.g. [1, 5, 9]
- B(Stop): explain why current evidence is sufficient
- C(Backtrack): backtrack reason + suggested new position
- D(Knowledge): specific query question
- E(Neighbor): description of spatial relationship needed
Rationale: (explain why you chose this action)

[Evidence] (for the Meta phase — evidence consolidation)
- evidence: (findings collected along the current analysis path down to this field)
- confidence: output as "N (label)" where N is one of:
  · 0 (insufficient evidence, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features are fairly characteristic)
  · 4 (definitive, features are typical and unambiguous)
  Example: "- confidence: 3 (probable, features are fairly characteristic)"
"""
    else:
        zoom_desc = {
            10: "10× 低倍镜（红框）- 全局架构观察",
            40: "40× 中倍镜（蓝框）- 形态学分析",
            160: "160× 高倍镜（绿框）- 细胞学判断"
        }.get(zoom_level, f"{zoom_level}× 倍镜")

        prompt = f"""你是 **PathCopilot Sub-agent（子智能体）**，由 **Orchestrator（编排器）** 调度执行。

## 系统架构
- **Orchestrator**：负责任务调度、证据整合、最终诊断
- **Sub-agent（你）**：负责当前视野的观察分析和路径规划，执行 MCP Loop

你的分析结果将被 Orchestrator 收集并整合，用于生成最终病理诊断报告。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前位置：{zoom_desc} | ROI: {roi_id}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【诊断目标】Orchestrator 需要你收集证据，帮助回答以下问题：
{CLINICAL_QUESTIONS}
"""
        if parent_context:
            prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【路径上下文】（继承自父级分析）
{parent_context}
"""
        if neighbor_context:
            prompt += f"""
【邻近区域参考】（你发起的 E(查邻) 动作的检索结果）
{neighbor_context}
"""
        prompt += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 第一步：Reason（观察与分析）

{reason_guidance}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 第二步：Plan（规划下一步动作）

基于你的 Reason 结果，请选择最合适的下一步动作：

{plan_options}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 输出格式（请严格遵守）

【Reason】
（你的形态学观察和分析，针对当前倍率的重点）

【Plan】
选择动作：A(下钻) / B(停止) / C(回溯) / D(查库) / E(查邻)
动作参数：
- A(下钻)：选择的区域编号，如 [1, 5, 9]
- B(停止)：说明当前证据已足够的原因
- C(回溯)：回溯原因 + 建议切换位置
- D(查库)：需要检索的具体问题
- E(查邻)：需要参考的空间关系
选择理由：（说明为什么做出该动作决策）

【Evidence】（为 Meta 阶段准备，用于证据固化）
- evidence: （当前路径下钻至当前视野下的证据发现）
- confidence: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例："- confidence: 3 (较确定，特征较明确)"
"""

    return prompt


def _get_reason_guidance(zoom_level: int, lang: str = 'zh') -> str:
    """根据倍率获取 Reason 阶段的简要指导（目标导向，不指定具体问题）"""

    if lang == 'en':
        if zoom_level == 10:
            return """You are observing under **10× low-power magnification**.

Low-power is ideal for assessing overall tissue architecture, spatial distribution, and the extent of lesions.
Apply your professional judgment to perform a thorough and systematic observation, collecting evidence to answer the diagnostic objectives."""
        elif zoom_level == 40:
            return """You are observing under **40× medium-power magnification**.

Medium-power is ideal for evaluating morphological features, growth patterns, and invasion patterns.
Apply your professional judgment to perform a thorough and systematic observation, collecting evidence to answer the diagnostic objectives."""
        else:  # 160x
            return """You are observing under **160× high-power magnification**.

High-power is ideal for assessing cytological features, nuclear atypia, and mitotic figures.
Apply your professional judgment to perform a thorough and systematic observation, collecting evidence to answer the diagnostic objectives."""
    else:
        if zoom_level == 10:
            return """你正在 **10× 低倍镜** 下观察。

低倍镜适合观察整体架构、组织分布和病变范围。
请基于你的专业判断进行细致的观察和分析，为回答问题收集证据。"""
        elif zoom_level == 40:
            return """你正在 **40× 中倍镜** 下观察。

中倍镜适合观察形态学特征、生长模式和浸润模式。
请基于你的专业判断进行细致的观察和分析，为回答问题收集证据。"""
        else:  # 160x
            return """你正在 **160× 高倍镜** 下观察。

高倍镜适合观察细胞学特征、核异型性和核分裂象。
请基于你的专业判断进行细致的观察和分析，为回答问题收集证据。"""


def _get_plan_options(zoom_level: int, num_candidates: int, is_terminal: bool, lang: str = 'zh') -> str:
    """根据倍率获取 Plan 阶段的动作选项"""

    options = []

    if lang == 'en':
        if not is_terminal and num_candidates > 0:
            next_zoom = {10: 40, 40: 160}.get(zoom_level, zoom_level * 4)
            next_frame = {10: "blue box", 40: "green box"}.get(zoom_level, "sub-region")
            options.append(f"""**A(Drill-down)** → Zoom to {next_zoom}×
- Select the most diagnostically valuable regions from {num_candidates} {next_frame} candidates
- Use when: suspicious lesion found that requires higher magnification to confirm
- ⚠️ Select only the regions truly necessary to answer the diagnostic objective — do NOT exhaustively cover all candidates.
- Parameter: list of selected region indices, e.g. [1, 5, 9]""")
        options.append(f"""**B(Stop)** → Current evidence is sufficient
- The current {zoom_level}× view already provides enough evidence to answer the diagnostic objective
- Use when: definitive features already visible; or region shows no abnormality; or current magnification is sufficient to answer the question
- ⚖️ Weigh the analysis cost: once you have collected enough evidence to answer the current question, STOP drilling down immediately — do NOT over-magnify in pursuit of completeness.
- Parameter: state the reason why evidence is sufficient""")
        options.append("""**C(Backtrack)** → Return or switch field of view
- Current view has a critical problem; abandon and switch
- Triggers: extensive necrosis (>50%) / severe artifact / blank region / off-target
- Parameter: reason for backtrack + suggested new position""")
        options.append("""**D(Knowledge)** → Query external knowledge base
- Encountered rare morphology or uncertain differential diagnosis
- Use when: need to look up WHO grading criteria or differential diagnosis guidelines
- Parameter: specific query question""")
        options.append("""**E(Neighbor)** → Search adjacent regions
- Need to reference surrounding tissue context
- Use when: compare with normal tissue / confirm invasion boundary / verify vascular continuity
- Parameter: description of the spatial relationship to reference""")
    else:
        if not is_terminal and num_candidates > 0:
            next_zoom = {10: 40, 40: 160}.get(zoom_level, zoom_level * 4)
            next_frame = {10: "蓝框", 40: "绿框"}.get(zoom_level, "子区域")
            options.append(f"""**A(下钻)** → 放大到 {next_zoom}×
- 从 {num_candidates} 个{next_frame}候选中选择最有诊断价值的区域
- 适用：发现可疑病变，需要更高倍镜确认细节
- ⚠️ 只选择对回答诊断目标**真正必要的**区域，不要穷举覆盖所有候选。
- 参数：选择的区域编号列表，如 [1, 5, 9]""")
        options.append(f"""**B(停止)** → 当前证据已足够
- 当前 {zoom_level}× 视野已能为诊断目标提供足够证据
- 适用：已见明确特征，无需更高倍镜；或当前区域无异常；或当前倍率已足以回答问题
- ⚖️ 请权衡分析成本——若已收集到足以回答当前问题的证据，应立即停止下钻，不要为追求完备而过度放大。
- 参数：说明证据充分的原因""")
        options.append("""**C(回溯)** → 返回或切换视野
- 当前视野存在严重问题，应放弃并切换
- 触发条件：大面积坏死(>50%) / 严重伪影 / 空白区域 / 偏离目标
- 参数：回溯原因 + 建议切换位置""")
        options.append("""**D(查库)** → 查询外部知识库
- 遇到罕见形态或不确定的鉴别诊断
- 适用：需要查询 WHO 分级标准、鉴别诊断指南
- 参数：需要检索的具体问题""")
        options.append("""**E(查邻)** → 搜索相邻区域
- 需要参考周围组织关系
- 适用：对照正常组织、确认浸润边界、验证脉管连续性
- 参数：需要参考的空间关系描述""")

    return "\n\n".join(options)


_MCP_NON_DRILL_ACTIONS = frozenset({'B', 'C', 'D', 'E'})
_MCP_ALL_ACTIONS = frozenset({'A', 'B', 'C', 'D', 'E'})
_MCP_CN_ACTION_NAMES = {
    '下钻': 'A',
    '停止': 'B',
    '回溯': 'C',
    '查库': 'D',
    '查邻': 'E',
}
_MCP_ACTION_LINE_RE = re.compile(
    r'(?:选择动作|Selected\s+action)\s*[：:]\s*"?(?P<letter>[ABCDE])\b',
    re.IGNORECASE,
)
_MCP_ACTION_JSON_RE = re.compile(
    r'"Selected action"\s*:\s*"(?P<letter>[ABCDE])',
    re.IGNORECASE,
)
_MCP_PLAN_JSON_SCALAR_RE = re.compile(
    r'"Plan"\s*:\s*"(?P<letter>[ABCDE])\s*[\(（]\s*'
    r'(?:Drill-down|Stop|Backtrack|Search|Neighbor|下钻|停止|回溯|查库|查邻)',
    re.IGNORECASE,
)
_MCP_NESTED_ACTION_JSON_RE = re.compile(
    r'"(?:action|Selected action)"\s*:\s*"(?P<letter>[ABCDE])\s*[\(（]\s*'
    r'(?:Drill-down|Stop|Backtrack|Search|Neighbor|下钻|停止|回溯|查库|查邻)',
    re.IGNORECASE,
)
_MCP_PLAN_SCALAR_RE = re.compile(
    r'^\s*(?P<letter>[ABCDE])\s*[\(（]\s*'
    r'(?:Drill-down|Stop|Backtrack|Search|Neighbor|下钻|停止|回溯|查库|查邻)'
    r'\s*[\)）]\s*$',
    re.IGNORECASE,
)
_MCP_CN_ACTION_NAME_RE = re.compile(
    r'选择动作[：:]\s*(?P<name>下钻|停止|回溯|查库|查邻)',
)
_MCP_INDEX_LIST_RE = re.compile(r'\[\s*(?:\d+(?:\s*,\s*\d+)*)?\s*\]')
_MCP_ACTION_PARAMETER_RE = re.compile(
    r'(?:动作参数|Action parameter)\s*[：:]\s*(.*?)'
    r'(?=\n\s*(?:选择理由|Rationale)\s*[：:]|\Z)',
    re.IGNORECASE | re.DOTALL,
)
_MCP_LABELED_INDEX_RES = (
    re.compile(
        r'(?:Action parameter|动作参数)\s*[：:]\s*"?(\[\s*(?:\d+(?:\s*,\s*\d+)*)?\s*\])"?',
        re.IGNORECASE,
    ),
    re.compile(
        r'Selected\s+(?:red|blue|green)\s+box\s+regions\s*[=:]\s*(\[\s*\d+(?:\s*,\s*\d+)*\s*\])',
        re.IGNORECASE,
    ),
    re.compile(
        r'"Selected\s+(?:red|blue|green)\s+box\s+regions"\s*:\s*(\[\s*\d+(?:\s*,\s*\d+)*\s*\])',
        re.IGNORECASE,
    ),
    re.compile(
        r'选择的?红框区域\s*[=：:]\s*(\[\s*\d+(?:\s*,\s*[、\d]+)*\s*\])',
    ),
    re.compile(
        r'(?:红框|蓝框|绿框)\s*[：:]\s*(\[\s*\d+(?:\s*,\s*[、\d]+)*\s*\])',
    ),
    re.compile(
        r'选中的(?:区域|子区域|微观区域)\s*[：:]\s*(\[\s*\d+(?:\s*,\s*[、\d]+)*\s*\])',
    ),
    re.compile(
        r'"selected_grids"\s*:\s*(\[\s*\d+(?:\s*,\s*\d+)*\s*\])',
        re.IGNORECASE,
    ),
    re.compile(
        r'"Action parameter"\s*:\s*(\[\s*(?:\d+(?:\s*,\s*\d+)*)?\s*\])',
        re.IGNORECASE,
    ),
    re.compile(
        r'"Action parameter"\s*:\s*"(\[\s*(?:\d+(?:\s*,\s*\d+)*)?\s*\])"',
        re.IGNORECASE,
    ),
)

_SELECTION_ABSENT = 'absent'
_SELECTION_EMPTY = 'explicit_empty'
_SELECTION_VALID = 'valid'
_SELECTION_INVALID = 'invalid'


def _loads_json_object(text):
    """Parse a JSON object without eval(). Return dict/list or None."""
    if not text or not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value


def _strip_wrapping_fence(text: str) -> str:
    stripped = (text or '').strip()
    match = re.match(r'^```(?:json)?\s*\n?([\s\S]*?)\n?```$', stripped, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped


def _is_mcp_json_dict(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = set(obj)
    if keys <= {'answer'} or (len(obj) == 1 and 'answer' in obj):
        return False
    return bool(keys & {'Reason', 'Plan', 'Evidence', 'Selected action'})


def _format_action_parameter_line(value) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if (
            len(stripped) >= 2
            and stripped[0] == stripped[-1]
            and stripped[0] in {'"', "'"}
        ):
            inner = stripped[1:-1].strip()
            if inner.startswith('[') and inner.endswith(']'):
                stripped = inner
        return f'Action parameter: {stripped}'
    if isinstance(value, (list, dict)):
        return f'Action parameter: {json.dumps(value, ensure_ascii=False)}'
    return f'Action parameter: {value}'


def _mcp_json_to_tagged(obj: dict) -> str:
    parts = []
    if 'Reason' in obj:
        reason = obj['Reason']
        if isinstance(reason, dict):
            reason = '\n'.join(f'{key}: {value}' for key, value in reason.items())
        parts.append(f'【Reason】\n{reason}')
    if 'Plan' in obj:
        plan = obj['Plan']
        if isinstance(plan, str) and _MCP_PLAN_SCALAR_RE.match(plan.strip()):
            lines = [f'Selected action: {plan.strip()}']
            if 'Action parameter' in obj:
                lines.append(_format_action_parameter_line(obj['Action parameter']))
            plan = '\n'.join(lines)
        elif isinstance(plan, dict):
            action_val = None
            for key in ('Selected action', 'action', 'Action'):
                cand = plan.get(key)
                if isinstance(cand, str) and _MCP_PLAN_SCALAR_RE.match(cand.strip()):
                    action_val = cand.strip()
                    break
            if action_val:
                lines = [f'Selected action: {action_val}']
                param = None
                if 'Action parameter' in obj:
                    param = obj['Action parameter']
                elif 'Action parameter' in plan:
                    param = plan['Action parameter']
                elif 'parameter' in plan:
                    param = plan['parameter']
                if param is not None:
                    lines.append(_format_action_parameter_line(param))
                plan = '\n'.join(lines)
            else:
                lines = []
                for key, value in plan.items():
                    if isinstance(value, (list, dict)):
                        lines.append(f'{key}: {json.dumps(value, ensure_ascii=False)}')
                    else:
                        lines.append(f'{key}: {value}')
                if 'Action parameter' in obj and 'Action parameter' not in plan:
                    lines.append(_format_action_parameter_line(obj['Action parameter']))
                plan = '\n'.join(lines)
        parts.append(f'【Plan】\n{plan}')
    if 'Evidence' in obj:
        evidence = obj['Evidence']
        if isinstance(evidence, dict):
            evidence = '\n'.join(f'- {key}: {value}' for key, value in evidence.items())
        elif isinstance(evidence, list):
            chunks = []
            for item in evidence:
                if isinstance(item, dict):
                    chunks.append('\n'.join(f'- {key}: {value}' for key, value in item.items()))
                else:
                    chunks.append(str(item))
            evidence = '\n'.join(chunks)
        parts.append(f'【Evidence】\n{evidence}')
    if 'Selected action' in obj and 'Plan' not in obj:
        lines = [f'Selected action: {obj["Selected action"]}']
        if 'Action parameter' in obj:
            lines.append(_format_action_parameter_line(obj['Action parameter']))
        parts.append('【Plan】\n' + '\n'.join(lines))
    return '\n\n'.join(parts)


def _coerce_mcp_json_to_text(response: str) -> str:
    """If the whole reply is an MCP JSON object, convert it to tagged text."""
    if not response:
        return ''
    stripped = _strip_wrapping_fence(response)
    obj = _loads_json_object(stripped)
    if _is_mcp_json_dict(obj):
        tagged = _mcp_json_to_tagged(obj)
        if tagged:
            return tagged
    return response


def _parse_index_list_literal(literal: str, valid_indices=None):
    if not literal:
        return []
    cleaned = literal.replace('、', ',').strip()
    value = _loads_json_object(cleaned)
    if value is None:
        try:
            value = ast.literal_eval(cleaned)
        except (ValueError, SyntaxError, TypeError, MemoryError):
            return []
    if not isinstance(value, list):
        return []
    selected = []
    for item in value:
        if isinstance(item, bool):
            continue
        number = None
        if isinstance(item, int):
            number = item
        elif isinstance(item, str) and item.strip().isdigit():
            number = int(item.strip())
        if number is None:
            continue
        if valid_indices is None or number in valid_indices:
            if number not in selected:
                selected.append(number)
    return selected


def _extract_tagged_section(text: str, name: str) -> str:
    match = re.search(
        rf'(?:【|\[){name}(?:】|\])\s*([\s\S]*?)(?=(?:【|\[)(?:Reason|Plan|Evidence)(?:】|\])|【证据摘要】|$)',
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ''


def _extract_explicit_action(text: str) -> str:
    if not text:
        return ''
    match = _MCP_ACTION_LINE_RE.search(text)
    if match:
        return match.group('letter').upper()
    match = _MCP_ACTION_JSON_RE.search(text)
    if match:
        return match.group('letter').upper()
    match = _MCP_PLAN_JSON_SCALAR_RE.search(text)
    if match:
        return match.group('letter').upper()
    match = _MCP_NESTED_ACTION_JSON_RE.search(text)
    if match:
        return match.group('letter').upper()
    match = _MCP_CN_ACTION_NAME_RE.search(text)
    if match:
        return _MCP_CN_ACTION_NAMES[match.group('name')]
    stripped = text.strip()
    match = _MCP_PLAN_SCALAR_RE.match(stripped)
    if match:
        return match.group('letter').upper()
    first_line = stripped.splitlines()[0].strip()
    match = _MCP_PLAN_SCALAR_RE.match(first_line)
    if match:
        return match.group('letter').upper()
    return ''


def _classify_index_literal(literal: str, valid_indices=None):
    """Distinguish explicit [], a usable list, and a non-empty list that filtered to empty."""
    if not literal:
        return [], _SELECTION_ABSENT
    stripped = literal.replace('、', ',').strip()
    if stripped == '[]':
        return [], _SELECTION_EMPTY
    raw = _parse_index_list_literal(stripped, valid_indices=None)
    if not raw:
        return [], _SELECTION_INVALID
    selected = _parse_index_list_literal(stripped, valid_indices)
    if selected:
        return selected, _SELECTION_VALID
    return [], _SELECTION_INVALID


def _extract_labeled_selection(text: str, valid_indices=None):
    if not text:
        return [], _SELECTION_ABSENT
    found_invalid = False
    for pattern in _MCP_LABELED_INDEX_RES:
        match = pattern.search(text)
        if not match:
            continue
        selected, status = _classify_index_literal(match.group(1), valid_indices)
        if status in (_SELECTION_VALID, _SELECTION_EMPTY):
            return selected, status
        if status == _SELECTION_INVALID:
            found_invalid = True
    if found_invalid:
        return [], _SELECTION_INVALID
    return [], _SELECTION_ABSENT


def _extract_labeled_indices(text: str, valid_indices=None):
    selected, status = _extract_labeled_selection(text, valid_indices)
    if status in (_SELECTION_VALID, _SELECTION_EMPTY):
        return selected
    return []


def _extract_bracket_selection(plan_text: str, valid_indices=None):
    if not plan_text:
        return [], _SELECTION_ABSENT
    match = _MCP_INDEX_LIST_RE.search(plan_text)
    if not match:
        return [], _SELECTION_ABSENT
    return _classify_index_literal(match.group(0), valid_indices)


def _extract_plan_bracket_indices(plan_text: str, valid_indices=None):
    selected, status = _extract_bracket_selection(plan_text, valid_indices)
    if status in (_SELECTION_VALID, _SELECTION_EMPTY):
        return selected
    return []


def _empty_mcp_decision(response: str = '') -> dict:
    return {
        'reason': '',
        'plan_action': '',
        'plan_params': None,
        'action_parameter': None,
        'evidence': {},
        'raw_response': response or '',
        'action': '',
        'selected_indices': [],
        'inferred_stop': False,
        'parse_status': 'failed',
    }


def _action_parameter_from_plan(action, selected_indices, plan_text, tagged):
    """Return the labeled Plan parameter only. Never fall back to the whole Plan."""
    if action == 'A':
        return list(selected_indices or [])
    blob = plan_text or tagged or ''
    match = _MCP_ACTION_PARAMETER_RE.search(blob)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value == '[]':
        return None
    return value


def _mcp_decision_is_complete(decision: dict) -> bool:
    """True when the model must not be retried and fallback must not run."""
    if not decision:
        return False
    if decision.get('parse_status') in {
        'invalid_selection',
        'missing_selection',
        'missing_parameter',
    }:
        return False
    action = (decision.get('action') or '').upper()
    return action in _MCP_ALL_ACTIONS


def parse_mcp_response(response: str, valid_indices=None) -> dict:
    """Parse one Sub-agent / Orchestrator reply into an action and its parameters.

    Contract: decide the action first. If the action is B/C/D/E, no number
    from Reason, Evidence, confidence, or the answer JSON may become a
    drill-down index.
    """
    result = _empty_mcp_decision(response)
    if not response:
        return result

    tagged = _coerce_mcp_json_to_text(response)
    result['reason'] = _extract_tagged_section(tagged, 'Reason')

    plan_text = _extract_tagged_section(tagged, 'Plan')
    evidence_text = _extract_tagged_section(tagged, 'Evidence')
    if not evidence_text:
        evidence_match = re.search(r'【证据摘要】\s*([\s\S]*?)$', tagged)
        if evidence_match:
            evidence_text = evidence_match.group(1).strip()

    action = _extract_explicit_action(plan_text) or _extract_explicit_action(tagged)
    if not action:
        action = _extract_explicit_action(response)

    selected_indices = []
    parse_status = 'failed'
    inferred_stop = False

    if action in _MCP_NON_DRILL_ACTIONS:
        selected_indices = []
        parse_status = 'ok'
    elif action == 'A':
        selected_indices, sel_status = _extract_labeled_selection(plan_text, valid_indices)
        if sel_status == _SELECTION_ABSENT:
            selected_indices, sel_status = _extract_bracket_selection(plan_text, valid_indices)
        if sel_status == _SELECTION_ABSENT:
            selected_indices, sel_status = _extract_labeled_selection(tagged, valid_indices)
        if sel_status == _SELECTION_ABSENT:
            selected_indices, sel_status = _extract_labeled_selection(response, valid_indices)
        if sel_status == _SELECTION_INVALID:
            selected_indices = []
            parse_status = 'invalid_selection'
        elif sel_status == _SELECTION_ABSENT:
            selected_indices = []
            parse_status = 'missing_selection'
        else:
            parse_status = 'ok'
    else:
        selected_indices = _extract_labeled_indices(tagged, valid_indices)
        if not selected_indices:
            selected_indices = _extract_labeled_indices(response, valid_indices)
        if selected_indices:
            action = 'A'
            parse_status = 'inferred_drill'
        elif _mcp_response_is_stop(tagged) or _mcp_response_is_stop(response):
            action = 'B'
            selected_indices = []
            inferred_stop = True
            parse_status = 'inferred_stop'
            logger.info("🛟 [MCP兜底] 未解析到动作但检测到停止语义 → 判定 B(Stop)")

    if action in _MCP_NON_DRILL_ACTIONS:
        selected_indices = []

    action_parameter = _action_parameter_from_plan(
        action, selected_indices, plan_text, tagged
    )
    result['action'] = action
    result['plan_action'] = action
    result['selected_indices'] = selected_indices
    result['action_parameter'] = action_parameter
    result['inferred_stop'] = inferred_stop
    result['parse_status'] = parse_status
    if action == 'A':
        result['plan_params'] = selected_indices
    elif action in ('B', 'C', 'D', 'E'):
        result['plan_params'] = action_parameter

    if evidence_text:
        for field in ['evidence', 'confidence', 'pathology_type', 'key_findings']:
            field_match = re.search(
                rf'[-•]\s*{field}[：:]\s*(.+?)(?=\n[-•]|$)',
                evidence_text,
                re.IGNORECASE,
            )
            if field_match:
                result['evidence'][field] = field_match.group(1).strip()
        for field in [
            'Q1_evidence', 'Q2_evidence', 'Q3_evidence', 'Q4_evidence',
            'Q5_evidence', 'Q6_evidence', 'Q7_evidence', 'Q8_evidence',
        ]:
            field_match = re.search(rf'{field}[：:]\s*(.+?)(?=\n|$)', evidence_text)
            if field_match:
                result['evidence'][field] = field_match.group(1).strip()

    return result


def _mcp_response_is_stop(response: str) -> bool:
    """Unlabeled stop inference only. Never overrides an explicit A/B/C/D/E."""
    if not response:
        return False
    rl = response.lower()
    if re.search(r'(?:选择动作|selected\s*action|action)\s*[：:]\s*[bcde]\b', rl):
        return True
    if re.search(r'\bb\s*[\(（]\s*(?:stop|停止)', rl):
        return True
    _cues = [
        '已足够', '足以回答', '已足以', '已能回答', '已可回答', '已经可以回答',
        '无需下钻', '无需进一步', '不需要下钻', '不需要进一步', '不必下钻',
        '已明确', '已可作答', '已可确定', '当前放大已', '当前倍率已', '已可诊断',
        'no need to drill', 'no further magnification', 'no further drill',
        'need not drill', 'no drill-down', 'no drill down', 'not necessary to drill',
        'already answered', 'already determined', 'already definitive',
        'definitively characteristic', 'definitively a', 'unambiguously',
        'sufficient to answer', 'evidence is sufficient', 'enough evidence to answer',
        'can be answered at this', 'answerable at this magnification',
        'current magnification is sufficient', 'this magnification is sufficient',
    ]
    return any(c in rl for c in _cues)

def should_execute_backtrack(parsed_response: dict) -> tuple:
    """
    判断是否应该执行回溯
    
    回溯是 Sub-agent 的自主决策，通过选择「回溯」动作触发，而非代码强制触发。
    
    Args:
        parsed_response: parse_mcp_response 的返回结果
    
    Returns:
        tuple: (should_backtrack: bool, reason: str)
    """
    # Sub-agent 选择 C(回溯) 动作
    if parsed_response.get('plan_action') == 'C':
        reason = parsed_response.get('plan_params', '视野质量问题')
        return True, f"Sub-agent 选择回溯: {reason[:100]}"
    
    # 不再使用关键词检测自动触发回溯
    # 回溯完全由 Sub-agent 自主决策
    
    return False, ''


def handle_mcp_action(
    mcp_result: dict,
    roi_info: dict,
    agent_state: dict,
    socketio=None,
    room=None,
    neighbor_context_override=None,
) -> dict:
    """
    统一处理 MCP Loop 动作
    
    根据 AI 选择的动作（下钻/停止/回溯/查库/查邻）执行相应的操作。
    
    Args:
        mcp_result: filter_rois_by_gpt_vision 的返回结果，包含:
            - selected_rois: 选中的 ROI 列表
            - analysis: AI 完整响应
            - mcp_action: 动作类型（下钻/停止/回溯/查库/查邻）
            - mcp_reason: 动作原因
        roi_info: 当前 ROI 信息
        agent_state: Agent 状态
        socketio: SocketIO 实例（用于发送消息）
        room: WebSocket 房间 ID
    
    Returns:
        dict: {
            'action': 'continue' | 'stop' | 'backtrack' | 'knowledge' | 'neighbor',
            'selected_rois': 选中的 ROI 列表,
            'reason': 动作原因,
            'search_result': 知识检索结果（如果动作是 D）
        }
    """
    mcp_action = mcp_result.get('mcp_action')
    mcp_reason = mcp_result.get('mcp_reason', '')
    selected_rois = list(mcp_result.get('selected_rois') or [])
    
    roi_id = roi_info.get('id', '?')
    
    cn_to_letter = {'下钻': 'A', '停止': 'B', '回溯': 'C', '查库': 'D', '查邻': 'E'}
    if isinstance(mcp_action, str):
        mcp_action = mcp_action.strip()
        if mcp_action in cn_to_letter:
            mcp_action = cn_to_letter[mcp_action]
        elif mcp_action.upper() in _MCP_ALL_ACTIONS:
            mcp_action = mcp_action.upper()
        else:
            mcp_action = ''
    else:
        mcp_action = ''

    if mcp_action in _MCP_NON_DRILL_ACTIONS:
        selected_rois = []

    # ===== A(下钻)：放大观察（正常流程）=====
    if mcp_action == 'A':
        logger.info(f"🔽 [A(下钻)] {roi_id}: 选中 {len(selected_rois)} 个子区域")
        return {
            'action': 'continue',
            'selected_rois': selected_rois,
            'reason': f'下钻到 {len(selected_rois)} 个子区域'
        }
    
    # ===== B(停止)：当前证据已足够 =====
    elif mcp_action == 'B':
        logger.info(f"⏹️ [B(停止)] {roi_id}: 当前证据已足够 - {mcp_reason[:50] if mcp_reason else ''}")
        
        # 发送停止消息到前端
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"⏹️ {roi_id}: 当前证据已足够，停止继续放大\n原因: {mcp_reason}",
                'level': 'info'
            }, to=room)
        
        return {
            'action': 'stop',
            'selected_rois': [],  # 不选择子区域
            'reason': mcp_reason or '当前证据已足够'
        }
    
    # ===== C(回溯)：返回或切换视野 =====
    elif mcp_action == 'C':
        logger.warning(f"⏪ [C(回溯)] {roi_id}: {mcp_reason[:50] if mcp_reason else '视野质量问题'}")
        
        # 记录回溯事件
        backtrack_record = {
            'roi_id': roi_id,
            'reason': mcp_reason,
            'timestamp': datetime.now().isoformat(),
            'trigger': 'AI_decision'  # 标记是 AI 主动决策
        }
        agent_state.setdefault('backtrack_history', []).append(backtrack_record)
        
        # 发送回溯消息到前端
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'warning',
                'content': f"⏪ {roi_id}: AI 判断当前视野无诊断价值，触发回溯\n原因: {mcp_reason}",
                'level': 'warning'
            }, to=room)
        
        return {
            'action': 'backtrack',
            'selected_rois': [],
            'reason': mcp_reason or '视野质量问题'
        }
    
    # D remains selectable in MultiPathQA, but retrieval is disabled.
    elif mcp_action == 'D':
        logger.info(f"🔍 [D(查库)] {roi_id}: 查库已关闭，暂无结果")
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"🔍 {roi_id}: 暂无结果",
                'level': 'info'
            }, to=room)

        return {
            'action': 'knowledge',
            'selected_rois': [],
            'reason': '暂无结果',
            'search_query': str(mcp_reason or '').strip(),
            'search_result': '暂无结果',
            'tool_status': 'disabled',
        }

    # ===== E(查邻)：搜索相邻区域 =====
    elif mcp_action == 'E':
        logger.info(f"🔎 [E(查邻)] {roi_id}: {mcp_reason[:50] if mcp_reason else '空间检索'}")
        
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"🔎 {roi_id}: AI 请求搜索相邻区域\n关系: {mcp_reason}",
                'level': 'info'
            }, to=room)
        
        if neighbor_context_override is None:
            neighbor_ctx = get_neighbor_context(roi_id, agent_state, max_neighbors=3)
        else:
            # 40× 使用当前红框内已完成的同级蓝框证据，不读写
            # 共享的待回注状态。空字符串也是有效的“暂无邻区证据”结果。
            neighbor_ctx = str(neighbor_context_override or '')
        if not neighbor_ctx:
            neighbor_ctx = f"【邻近区域参考】\n当前区域 {roi_id} 的邻近区域尚未分析完成，暂无可参考信息。\n"
            logger.info(f"🔎 [E(查邻)] {roi_id}: 邻近区域尚无已分析结果")
        else:
            logger.info(f"🔎 [E(查邻)] {roi_id}: 获取到邻近区域上下文 (长度={len(neighbor_ctx)})")
        
        return {
            'action': 'neighbor',
            'selected_rois': [],
            'reason': mcp_reason or '空间检索',
            'neighbor_query': mcp_reason,
            'neighbor_context': neighbor_ctx
        }
    
    else:
        logger.warning(
            f"⚠️ [MCP] 未知或缺失动作 '{mcp_action}'，fail-closed 停止，不推断下钻"
        )
        return {
            'action': 'stop',
            'selected_rois': [],
            'reason': mcp_reason or '未知动作，fail-closed 停止'
        }


def _effective_action_letter(action_result: dict) -> str:
    """Map the runtime outcome back to the public A/B/C action vocabulary."""
    return {
        'continue': 'A',
        'stop': 'B',
        'backtrack': 'C',
    }.get(action_result.get('action'), '')


def resolve_mcp_action_with_one_replan(
    initial_result: dict,
    roi_info: dict,
    agent_state: dict,
    replan_callback,
    socketio=None,
    room=None,
    neighbor_context_override=None,
):
    """Execute one A–E decision and, for D/E, replan the same view once.

    The model response remains the source of the Plan.  This helper only
    executes the typed action.  D/E are bounded to one tool/context round-trip;
    requesting either action again keeps the completed observation as a B leaf
    rather than inventing a drill-down selection.
    """
    action_result = handle_mcp_action(
        initial_result,
        roi_info,
        agent_state,
        socketio,
        room,
        neighbor_context_override=neighbor_context_override,
    )
    if action_result.get('action') not in {'knowledge', 'neighbor'}:
        return initial_result, action_result, []

    if action_result['action'] == 'knowledge':
        context_kind = 'knowledge'
        context_content = action_result.get('search_result', '')
        tool_trace = {
            'action': 'D',
            'query': action_result.get('search_query', ''),
            'status': action_result.get('tool_status', 'ok'),
            'result': context_content,
        }
    else:
        context_kind = 'neighbor'
        context_content = action_result.get('neighbor_context', '')
        tool_trace = {
            'action': 'E',
            'query': action_result.get('neighbor_query', ''),
            'status': 'ok' if context_content else 'no_context',
            'result': context_content,
        }

    replanning_context = {
        'kind': context_kind,
        'content': context_content,
        'query': tool_trace.get('query', ''),
    }
    replanned_result = replan_callback(replanning_context)
    replanned_action = str(replanned_result.get('mcp_action') or '').strip().upper()

    if replanned_action in {'D', 'E'}:
        logger.warning(
            f"🛑 [MCP] {roi_info.get('id', '?')}: 同一视野再次请求 "
            f"{replanned_action}，已达回注上限；保留当前层为叶节点"
        )
        bounded_stop = {
            'action': 'stop',
            'selected_rois': [],
            'reason': f'{replanned_action} 回注已达一次上限',
            'bounded_replan': True,
        }
        return replanned_result, bounded_stop, [tool_trace]

    final_action = handle_mcp_action(
        replanned_result,
        roi_info,
        agent_state,
        socketio,
        room,
        neighbor_context_override=neighbor_context_override,
    )
    return replanned_result, final_action, [tool_trace]


# ============== 红框选择提示词（Orchestrator 全局路径规划）==============
def get_red_selection_prompt_mcp(num_red_boxes, organ=ORGAN_DEFAULT, lang='zh'):
    """
    红框选择提示词 - Orchestrator 全局路径规划 (双语)
    
    Args:
        num_red_boxes: 红框候选数量
        organ: 器官类型
        lang: 语言 ('zh' 或 'en')
    """
    if lang == 'en':
        return f"""You are **PathCopilot Orchestrator**, performing global path planning.

## System Architecture
- **Orchestrator (you)**: responsible for global path planning, task scheduling, evidence aggregation, and final diagnosis
- **Sub-agent**: responsible for local analysis in each field of view, executing the MCP Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current phase: Global path planning (thumbnail → select red boxes)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Diagnostic Objectives

All your observations and decisions serve to collect evidence for the following questions:
{CLINICAL_QUESTIONS_EN}

## Current Task

You are viewing the **thumbnail** of the entire slide (global view).
The thumbnail has been divided into **{num_red_boxes} red box candidates** (numbered 1–{num_red_boxes}).

Like an experienced pathologist performing the first low-power scan after receiving a slide, conduct a systematic and thorough global survey:
1. Observe the thumbnail carefully, think deeply, describe all your findings, and identify regions valuable for answering the diagnostic questions above. The thumbnail is the ONLY global view that simultaneously shows the overall lesion contour, the tissue architecture, and the relationship between the lesion and the surrounding normal tissue — please form preliminary hypotheses directly tied to the diagnostic questions above (e.g. lesion location and extent, growth pattern, and which regions are most likely to carry the morphological evidence each question requires). These hypotheses will be verified or refuted by subsequent drill-down paths.
2. Select red boxes for drill-down analysis (selected regions will be examined in detail by Sub-agent assistants at higher magnification)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 Output Format

[Reason]
(Your detailed observations, analysis, and reasoning of the thumbnail)

[Plan]
Selected red box regions = [list of indices]
Rationale: (explain why you selected these regions)

[Evidence] (for the Meta phase — evidence consolidation)
- evidence: (findings observed in the current field. Include preliminary hypotheses from the thumbnail relevant to the diagnostic questions above; subsequent drill-downs will verify.)
- confidence: output as "N (label)" where N is one of:
  · 0 (insufficient evidence, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features are fairly characteristic)
  · 4 (definitive, features are typical and unambiguous)
  Example: "- confidence: 3 (probable, features are fairly characteristic)"

⚠️ Note: the selected regions must be a pure list of numbers, e.g. [1, 3, 5, 8]
"""
    else:
        return f"""你是 **PathCopilot Orchestrator（编排器）**，正在执行全局路径规划。

## 系统架构
- **Orchestrator（你）**：负责全局路径规划、任务调度、证据整合、最终诊断
- **Sub-agent**：负责各视野的局部分析，执行 MCP Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前阶段：全局路径规划（缩略图 → 选择红框）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 诊断目标

你的一切观察和决策都是为了收集证据，回答以下问题：
{CLINICAL_QUESTIONS}

## 当前任务

你正在查看整张切片的**缩略图**（全局视野）。
当前缩略图已被划分为 **{num_red_boxes} 个红框候选**（编号 1-{num_red_boxes}）。

请像一位经验丰富的病理医生拿到切片后的第一步那样，进行系统、充分的低倍镜全局扫视：
1. 观察缩略图，充分而深入地思考，描述你看到的所有发现，识别对于回答上述诊断目标有价值的区域。缩略图是唯一能同时看到病变整体轮廓、组织结构分布、以及病变与周围正常组织关系的全局视野，请据此围绕上述诊断目标给出初步假说——例如病变的部位与范围、生长方式，以及哪些区域最可能承载各问题所需的形态学证据。这些假说将由后续下钻路径验证或否定。
2. 选择需要下钻分析的红框（选择的区域将由 Sub-agent 助手放大进行详细分析）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 输出格式（严格按此格式输出）

【Reason】
（你对缩略图的详细的观察和分析推理过程）

【Plan】
选择的红框区域 = [编号列表]
选择理由：（说明为什么选择这些区域）

【Evidence】（为 Meta 阶段准备，用于证据固化）
- evidence: （当前视野下观察到的证据发现。请结合缩略图围绕上述诊断目标给出初步假说，后续下钻将验证。）
- confidence: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例："- confidence: 3 (较确定，特征较明确)"

⚠️ 注意：选择的区域必须是纯数字列表，如 [1, 3, 5, 8]
"""


# 保留旧版函数名作为别名，确保兼容性
def get_red_selection_prompt(total_boxes, organ=ORGAN_DEFAULT, lang='zh'):
    """红框提示词 - 旧版别名，调用 MCP 版本"""
    return get_red_selection_prompt_mcp(total_boxes, organ, lang)


def get_blue_selection_prompt_mcp(red_idx, num_blue_boxes, parent_context="", neighbor_context="", organ=ORGAN_DEFAULT, lang='zh'):
    """蓝框选择提示词 - MCP Loop 规范化版本 (双语)"""
    base_prompt = build_mcp_prompt(
        zoom_level=10,
        roi_id=f"R{red_idx}",
        num_candidates=num_blue_boxes,
        parent_context=parent_context,
        neighbor_context=neighbor_context,
        organ=organ,
        is_terminal=False,
        lang=lang
    )
    if lang == 'en':
        selection_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Note: Blue Box Selection

Red box R{red_idx} has been divided into **{num_blue_boxes} blue box candidates** (numbered 1–{num_blue_boxes}).

If you choose **A(Drill-down)**, specify the selected blue box indices in [Plan], e.g.: [1, 5, 9]

💡 Selection principle: within the computational budget, select the Top-N regions with the highest diagnostic value to maximize evidence collection efficiency.

⚠️ Consistency: regions flagged as "suspicious" or "needs further examination" in 【Reason】 should appear in the 【Plan】 action list. Avoid raising a concern in Reason but then dropping it from Plan.

⚠️ Format: the region list must contain only numbers, e.g. [1, 2, 3]
"""
    else:
        selection_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 特别说明：蓝框选择

当前红框 R{red_idx} 已被划分为 **{num_blue_boxes} 个蓝框候选**（编号 1-{num_blue_boxes}）。

如果选择 **A(下钻)**，请在【Plan】中指定选择的蓝框编号列表，如：[1, 5, 9]

💡 选择原则：在计算预算允许范围内，选择诊断价值最高的 Top-N 区域，最大化证据收集效率。

⚠️ 一致性约束：【Reason】中被思考标记为"需要进一步检查"或"可疑"的区域，需要出现在【Plan】的动作参数中。不要出现在 Reason 中提出怀疑需要放大却在 Plan 中遗漏。

⚠️ 格式要求：区域列表必须是纯数字，如 [1, 2, 3]
"""
    return base_prompt + selection_note


def get_green_selection_prompt_mcp(blue_idx, num_green_boxes, parent_context="", neighbor_context="", organ=ORGAN_DEFAULT, lang='zh'):
    """绿框选择提示词 - MCP Loop 规范化版本 (双语)"""
    base_prompt = build_mcp_prompt(
        zoom_level=40,
        roi_id=f"{blue_idx}",
        num_candidates=num_green_boxes,
        parent_context=parent_context,
        neighbor_context=neighbor_context,
        organ=organ,
        is_terminal=False,
        lang=lang
    )
    if lang == 'en':
        selection_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Note: Green Box Selection (40× → 160×)

Blue box {blue_idx} has been divided into **{num_green_boxes} green box candidates** (numbered 1–{num_green_boxes}).

**Key judgment**: Can the current 40× view already answer the diagnostic question?

Choose **A(Drill-down)** to 160× if:
- Mitotic figures or chromatin patterns need confirmation
- Suspected LVSI requires high-power verification
- Degree of nuclear atypia needs 160× clarification

Choose **B(Stop)** if:
- The current 40× evidence is sufficient to answer the diagnostic question

💡 Selection principle: within the computational budget, select the Top-N regions with the highest diagnostic value.

⚠️ Consistency: regions flagged as "suspicious" or "needs high-power verification" in 【Reason】 should appear in the 【Plan】 action list. Avoid raising a concern in Reason but then dropping it from Plan.

⚠️ Format: region list must contain only numbers, e.g. [1, 2, 3]; if choosing B(Stop), use []
"""
    else:
        selection_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 特别说明：绿框选择（40x → 160x）

当前蓝框 {blue_idx} 已被划分为 **{num_green_boxes} 个绿框候选**（编号 1-{num_green_boxes}）。

**关键判断**：当前 40x 视野是否已能回答诊断问题？

如果选择 **A(下钻)** 到 160x：
- 存在需要确认的核分裂象、染色质模式
- 疑似脉管内癌栓（LVSI）需要高倍确认
- 细胞异型性程度需要 160x 明确

如果选择 **B(停止)**：
- 当前 40x 证据已足够回答诊断问题，无需下钻到 160x

💡 选择原则：在计算预算允许范围内，选择诊断价值最高的 Top-N 区域，最大化证据收集效率。

⚠️ 一致性约束：【Reason】中被思考标记为"需要高倍确认"或"可疑"的区域，需要出现在【Plan】的动作参数中。不要出现在 Reason 中提出怀疑需要放大却在 Plan 中遗漏。

⚠️ 格式要求：区域列表必须是纯数字，如 [1, 2, 3]；如选 B(停止)，填 []
"""
    return base_prompt + selection_note




# Agent停止标志
agent_stop_flags = {}

# ⭐ 活跃的Agent状态存储（按room/session_id索引）
active_agent_states = {}

def get_agent_state(room):
    """获取指定room的agent_state（用于socket事件处理器）"""
    return active_agent_states.get(room)

def set_agent_state(room, agent_state):
    """设置指定room的agent_state"""
    active_agent_states[room] = agent_state

def clear_agent_state(room):
    """清理指定room的agent_state"""
    active_agent_states.pop(room, None)


# ==================== 证据原子化 ====================

def collect_evidence(agent_state, analysis_result: dict, roi_info: dict, level: str = 'unknown'):
    """
    收集分析结果中的证据并存入证据池
    
    Args:
        agent_state: Agent状态
        analysis_result: 分析结果字典
        roi_info: ROI信息
        level: 层级 (red/blue/green)
    """
    if not parse_evidence_from_analysis:
        return  # 证据模型未加载
    
    try:
        evidence_list = parse_evidence_from_analysis(analysis_result, roi_info, level)
        evidence_pool = agent_state.get('evidence_pool')
        
        # ⭐ V2修复: 用 is not None 判断, 否则空的 EvidencePool(__len__=0) 会被判为 False,
        #   导致第一条证据永远加不进去 → 证据池永远为空 → 整个 Memory 子系统失效。
        if evidence_pool is not None and hasattr(evidence_pool, 'add_batch'):
            evidence_pool.add_batch(evidence_list)
            logger.debug(f"📌 [证据收集] {roi_info.get('id', '?')}: 添加 {len(evidence_list)} 条证据 (池累计={len(evidence_pool)})")
        elif isinstance(evidence_pool, list):
            evidence_pool.extend([ev.to_dict() if hasattr(ev, 'to_dict') else ev for ev in evidence_list])
    except Exception as e:
        logger.warning(f"证据收集失败: {e}")


def _split_reason_plan(raw_text: str):
    """V2: 从节点原始 reasoning 文本中拆出 reason(形态学观察) 与 plan(下钻决策)。
    去掉给 Memory 用不上的导航噪声; reason 用于证据描述, plan 仅留作记录。"""
    if not raw_text:
        return '', ''
    reason, plan = '', ''
    m = re.search(r'(?:【Reason】|\[Reason\])\s*([\s\S]*?)(?=【Plan】|\[Plan\]|【Evidence】|\[Evidence\]|【路径证据汇总】|\[Path Evidence Summary\]|【诊断建议】|\[Diagnostic [Ii]mpression\]|$)', raw_text)
    if m:
        reason = m.group(1).strip()
    pm = re.search(r'(?:【Plan】|\[Plan\])\s*([\s\S]*?)(?=【Evidence】|\[Evidence\]|【路径证据汇总】|\[Path Evidence Summary\]|【诊断建议】|\[Diagnostic [Ii]mpression\]|$)', raw_text)
    if pm:
        plan = pm.group(1).strip()
    if not reason:
        reason = raw_text.strip()
    return reason, plan


def build_memory_tree(agent_state):
    """V2 统一 Memory 树：缩略图(根) → 红框 → 蓝框 → 绿框 的层级结构 (就像文件夹展开)。

    每个节点记录：坐标(rect) / reason(观察) / plan(决策) / 结构化 evidence{Q1..Q8} /
    每题置信度 q_confidence / 诊断建议 / 图像路径。
    同时把全树(含红框, findings 占位时回退到 reason)展开成扁平 EvidencePool, 供聚合与证据链。

    Returns: (memory_tree: dict, evidence_pool: EvidencePool|None)
    """
    pool = EvidencePool() if EvidencePool else None

    def _node_to_memory(node, level):
        a = node.get('analysis', {}) or {}
        raw = a.get('reasoning', '') or ''
        reason, plan = _split_reason_plan(raw)
        findings = a.get('findings', '') or ''
        # 结构化证据 / 每题置信度 / 诊断建议
        evidence = a.get('evidence', {}) or {}
        q_confidence = a.get('q_confidence', {}) or {}
        diag = a.get('diagnosis_suggestion', '') or ''
        # evidence 为空时，从 reasoning 原文重新解析路径证据汇总。
        if not evidence and raw and ('_evidence' in raw or '路径证据汇总' in raw or 'Path Evidence Summary' in raw):
            try:
                _p = parse_structured_response(raw, 160)
                evidence = _p.get('evidence', {}) or {}
                q_confidence = _p.get('q_confidence', {}) or {}
                if not diag:
                    diag = _p.get('diagnosis_suggestion', '') or ''
            except Exception as _e:
                logger.debug(f"[Memory树] reasoning 重解析失败: {_e}")
        node_id = node.get('roi_id', node.get('id', '?'))
        rect = node.get('rect') or a.get('rect', {}) or {}
        # 证据池描述源: 优先用结构化每题证据拼接, 否则 findings, 再否则 reason
        if evidence:
            pool_desc = ' | '.join(v for v in evidence.values() if v)
        else:
            pool_desc = findings if (findings and not findings.startswith('AI正在分析')) else reason[:500]
        if pool is not None and parse_evidence_from_analysis and pool_desc:
            try:
                units = parse_evidence_from_analysis(
                    {'findings': pool_desc, 'confidence': a.get('confidence', 50),
                     'evidence': evidence},
                    {'id': node_id, 'rect': rect}, level)
                if units:
                    pool.add_batch(units)
            except Exception as _e:
                logger.debug(f"[Memory树] 证据派生失败 {node_id}: {_e}")
        # ⭐ V2 全量节点(所见即所得, 对应 HTML 展示): 只省略真正为空的字段。
        #   path_tree.json 用全量; memory.json 再由 _strip_for_synthesis 去掉 rect/image_path。
        mem = {'id': node_id, 'level': level}
        if rect:
            mem['rect'] = rect
        if reason:
            mem['reason'] = reason            # 【Reason】形态学观察
        if plan:
            mem['plan'] = plan                # 【Plan】下钻决策(红/蓝有, 绿框天然无)
        if evidence:
            mem['evidence'] = evidence        # 【路径证据汇总】每题证据
            if q_confidence:
                mem['q_confidence'] = q_confidence
        # findings: 无结构化证据时的文本回退(绿框有 evidence 时省略, 避免与 evidence 重复)
        if (not evidence) and findings and not findings.startswith('AI正在分析'):
            mem['findings'] = findings
        if diag:
            mem['diagnosis_suggestion'] = diag  # 【诊断建议】
        _img = node.get('image_path') or a.get('image_path')
        if _img:
            mem['image_path'] = _img
        child_level = {'red': 'blue', 'blue': 'green'}.get(level, 'green')
        children = [_node_to_memory(ch, child_level) for ch in node.get('children', [])]
        if children:
            mem['children'] = children
        return mem

    ga = agent_state.get('global_analysis', '')
    if isinstance(ga, dict):
        ga = ga.get('reasoning', '') or ga.get('analysis', '')
    elif isinstance(ga, list):
        ga = '\n'.join(str(x) for x in ga)
    root = {
        'id': 'THUMBNAIL',
        'level': 'thumbnail',
        'organ': agent_state.get('organ', ''),
        'filename': agent_state.get('filename', ''),
        'global_analysis': ga or '',
        'children': [_node_to_memory(n, 'red') for n in agent_state.get('tree_nodes', [])]
    }
    return root, pool


def strip_for_synthesis(node, drop_keys=('rect', 'image_path')):
    """V2: 由全量 path_tree 派生"喂汇总"的精简 memory —— 仅去掉对文本汇总无意义的
    机器字段(默认 rect 像素坐标 / image_path 图路径), 其余(reason/plan/evidence/
    q_confidence/diagnosis_suggestion)全部保留, ROI id 作为可追溯锚点保留。
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in drop_keys:
                continue
            if k == 'children':
                out[k] = [strip_for_synthesis(c, drop_keys) for c in v]
            else:
                out[k] = strip_for_synthesis(v, drop_keys) if isinstance(v, (dict, list)) else v
        return out
    if isinstance(node, list):
        return [strip_for_synthesis(x, drop_keys) for x in node]
    return node


# ==================== 空间搜索（Spatial Search） ====================

def build_spatial_index(candidate_rois: list, agent_state: dict) -> dict:
    """
    构建空间索引：计算每个ROI的邻居关系
    
    基于ROI的空间位置（rect坐标），计算哪些ROI彼此相邻。
    用于实现Plan阶段的空间搜索功能。
    
    Args:
        candidate_rois: 候选ROI列表，每个ROI需包含 'id' 和 'rect' 字段
        agent_state: Agent状态字典
    
    Returns:
        dict: 空间索引 {roi_id: {'rect': {...}, 'neighbors': [...]}, ...}
    """
    spatial_index = {}
    
    if not candidate_rois:
        return spatial_index
    
    # 计算每个ROI的中心点
    roi_centers = []
    for roi in candidate_rois:
        roi_id = roi.get('id', 'unknown')
        rect = roi.get('rect', {})
        
        center_x = rect.get('x', 0) + rect.get('w', 0) / 2
        center_y = rect.get('y', 0) + rect.get('h', 0) / 2
        
        roi_centers.append({
            'id': roi_id,
            'rect': rect,
            'center': (center_x, center_y),
            'width': rect.get('w', 0),
            'height': rect.get('h', 0)
        })
        
        # 初始化索引条目
        spatial_index[roi_id] = {
            'rect': rect,
            'neighbors': [],
            'findings': ''  # 分析完成后更新
        }
    
    # 计算邻居关系（基于距离和重叠）
    # 如果两个ROI的中心距离小于它们平均尺寸的1.5倍，则认为是邻居
    for i, roi_a in enumerate(roi_centers):
        for j, roi_b in enumerate(roi_centers):
            if i >= j:
                continue
            
            # 计算中心距离
            dist = ((roi_a['center'][0] - roi_b['center'][0]) ** 2 + 
                    (roi_a['center'][1] - roi_b['center'][1]) ** 2) ** 0.5
            
            # 计算阈值（平均尺寸的1.5倍）
            avg_size = (roi_a['width'] + roi_a['height'] + roi_b['width'] + roi_b['height']) / 4
            threshold = avg_size * 1.5
            
            if dist < threshold:
                # 互为邻居
                spatial_index[roi_a['id']]['neighbors'].append(roi_b['id'])
                spatial_index[roi_b['id']]['neighbors'].append(roi_a['id'])
    
    # 保存到agent_state
    agent_state['spatial_index'] = spatial_index
    logger.info(f"🗺️ [空间索引] 构建完成: {len(spatial_index)} 个ROI, 平均 {sum(len(v['neighbors']) for v in spatial_index.values()) / max(len(spatial_index), 1):.1f} 个邻居")
    
    return spatial_index


def update_spatial_index_findings(roi_id: str, findings: str, agent_state: dict):
    """
    更新空间索引中某个ROI的分析结果
    
    Args:
        roi_id: ROI的ID
        findings: 分析结果文本
        agent_state: Agent状态字典
    """
    spatial_lock = agent_state.get('spatial_index_lock')
    if spatial_lock:
        with spatial_lock:
            spatial_index = agent_state.get('spatial_index', {})
            if roi_id in spatial_index:
                spatial_index[roi_id]['findings'] = findings
    else:
        spatial_index = agent_state.get('spatial_index', {})
        if roi_id in spatial_index:
            spatial_index[roi_id]['findings'] = findings


def get_neighbor_context(roi_id: str, agent_state: dict, max_neighbors: int = 3) -> str:
    """
    获取邻近区域的分析结果摘要
    
    用于Plan阶段的空间搜索：当分析某个区域时，可以参考邻近区域的发现。
    
    Args:
        roi_id: 当前ROI的ID
        agent_state: Agent状态字典
        max_neighbors: 最多返回几个邻居的结果
    
    Returns:
        str: 邻近区域上下文文本
    """
    spatial_lock = agent_state.get('spatial_index_lock')
    if spatial_lock:
        with spatial_lock:
            spatial_index = agent_state.get('spatial_index', {})
            if roi_id not in spatial_index:
                return ""
            neighbors = list(spatial_index[roi_id].get('neighbors', []))
            findings_by_id = {
                neighbor_id: spatial_index.get(neighbor_id, {}).get('findings', '')
                for neighbor_id in neighbors[:max_neighbors * 2]
            }
    else:
        spatial_index = agent_state.get('spatial_index', {})
        if roi_id not in spatial_index:
            return ""
        neighbors = list(spatial_index[roi_id].get('neighbors', []))
        findings_by_id = {
            neighbor_id: spatial_index.get(neighbor_id, {}).get('findings', '')
            for neighbor_id in neighbors[:max_neighbors * 2]
        }

    if not neighbors:
        return ""

    context_parts = []
    for neighbor_id in neighbors[:max_neighbors * 2]:
        findings = findings_by_id.get(neighbor_id, '')
        if findings:
            context_parts.append(f"- {neighbor_id}: {findings}")
            if len(context_parts) >= max_neighbors:
                break
    
    if not context_parts:
        return ""
    
    return f"【邻近区域参考】\n当前区域 {roi_id} 的邻近区域分析如下：\n" + "\n".join(context_parts) + "\n"


def get_completed_sibling_context(
    current_roi: dict,
    sibling_rois: list,
    completed_analyses: list,
    max_neighbors: int = 3,
) -> str:
    """Return same-level evidence already completed within the current parent.

    This is the 40× E(neighbor) source.  It is deliberately local to one red
    path, so parallel red paths cannot overwrite one another's replanning
    context.  Only adjacent siblings with completed findings are included.
    """
    current_rect = current_roi.get('rect', {})
    current_id = current_roi.get('id', '?')
    current_center = (
        current_rect.get('x', 0) + current_rect.get('w', 0) / 2,
        current_rect.get('y', 0) + current_rect.get('h', 0) / 2,
    )
    completed = {
        item.get('roi_id'): item.get('reasoning') or item.get('findings', '')
        for item in completed_analyses
        if item.get('roi_id')
    }
    candidates = []
    for sibling in sibling_rois:
        sibling_id = sibling.get('id')
        findings = completed.get(sibling_id, '')
        if not sibling_id or sibling_id == current_id or not findings:
            continue
        rect = sibling.get('rect', {})
        sibling_center = (
            rect.get('x', 0) + rect.get('w', 0) / 2,
            rect.get('y', 0) + rect.get('h', 0) / 2,
        )
        distance = (
            (current_center[0] - sibling_center[0]) ** 2
            + (current_center[1] - sibling_center[1]) ** 2
        ) ** 0.5
        avg_size = (
            current_rect.get('w', 0) + current_rect.get('h', 0)
            + rect.get('w', 0) + rect.get('h', 0)
        ) / 4
        if avg_size > 0 and distance < avg_size * 1.5:
            candidates.append((distance, sibling_id, findings))

    candidates.sort(key=lambda item: (item[0], item[1]))
    context_parts = [
        f"- {sibling_id}: {findings}"
        for _distance, sibling_id, findings in candidates[:max_neighbors]
    ]
    if not context_parts:
        return ""
    return (
        f"【邻近区域参考】\n当前区域 {current_id} 的同级邻区分析如下：\n"
        + "\n".join(context_parts)
        + "\n"
    )


def get_spatial_context_for_boundary(roi_id: str, agent_state: dict) -> str:
    """
    获取用于边界判断的空间上下文
    
    当检测到当前区域可能处于肿瘤边缘时，提供邻近区域的信息帮助判断。
    
    Args:
        roi_id: 当前ROI的ID
        agent_state: Agent状态字典
    
    Returns:
        str: 边界判断上下文文本
    """
    neighbor_context = get_neighbor_context(roi_id, agent_state, max_neighbors=4)
    
    if not neighbor_context:
        return ""
    
    return f"""
【边界判断辅助信息】
您正在分析的区域 {roi_id} 可能位于肿瘤与正常组织的交界处。
以下是邻近区域的分析结果，请结合参考：

{neighbor_context}

请在分析时考虑：
1. 与邻近区域的病变是否连续
2. 浸润边界的特征
3. 是否存在局部扩散征象
"""


# ==================== 回溯/重新路由（Backtrack/Reroute） ====================
#
# 回溯是 Sub-agent 的自主决策，通过选择「回溯」动作触发，而非代码强制触发。
#
# 回溯触发流程：
# 1. Sub-agent 在 Reason 阶段发现视野质量问题（坏死/伪影/空白等）
# 2. Sub-agent 在 Plan 阶段选择「回溯」动作
# 3. handle_mcp_action() 函数处理回溯动作，执行回溯
#
# detect_backtrack_trigger() 函数仅用于日志监控，不触发回溯。

def detect_backtrack_trigger(analysis_result: dict) -> tuple:
    """
    检测分析结果中是否存在潜在的回溯信号（仅用于日志监控）
    
    注意：此函数不触发回溯！回溯由 Sub-agent 通过选择「回溯」动作自主决策。
    此函数仅用于监控和统计，帮助评估 Sub-agent 的决策质量。
    
    Args:
        analysis_result: 分析结果字典，包含 'findings' 字段
    
    Returns:
        tuple: (has_warning_signal: bool, signal_description: str, signal_type: str)
    """
    findings = analysis_result.get('findings', '')
    if not findings:
        return False, '', ''
    
    findings_lower = findings.lower()
    
    # 检测潜在问题信号（用于日志监控，不触发回溯）
    warning_signals = []
    
    for trigger_type, keywords in BACKTRACK_TRIGGERS.items():
        for keyword in keywords:
            if keyword.lower() in findings_lower:
                warning_signals.append((trigger_type, keyword))
    
    # 检测置信度等级信号
    confidence_level = analysis_result.get('confidence_level', 2)
    if confidence_level <= 1:
        level_desc = {0: '证据不足', 1: '不确定'}.get(confidence_level, '低')
        warning_signals.append(('low_confidence', f"Level {confidence_level}: {level_desc}"))
    
    if warning_signals:
        # 返回警告信号（仅用于监控，不触发回溯）
        signal_type, signal_desc = warning_signals[0]
        return True, f"[监控] 检测到潜在问题: {signal_desc}", signal_type
    
    return False, '', ''


def execute_backtrack(agent_state: dict, current_roi: dict, candidate_queue: list, reason: str = '') -> dict:
    """
    执行回溯：跳过当前ROI，返回下一个候选
    
    Args:
        agent_state: Agent状态
        current_roi: 当前ROI信息
        candidate_queue: 剩余候选ROI队列
        reason: 回溯原因
    
    Returns:
        dict: {
            'action': 'skip' | 'retry' | 'continue',
            'next_roi': 下一个ROI或None,
            'message': 回溯消息
        }
    """
    roi_id = current_roi.get('id', 'unknown')
    
    # 记录回溯历史
    backtrack_record = {
        'roi_id': roi_id,
        'reason': reason,
        'timestamp': datetime.now().isoformat()
    }
    agent_state.setdefault('backtrack_history', []).append(backtrack_record)
    
    logger.warning(f"⏪ [回溯] 跳过 {roi_id}: {reason}")
    
    # 检查是否还有候选
    if candidate_queue:
        next_roi = candidate_queue[0]
        return {
            'action': 'skip',
            'next_roi': next_roi,
            'message': f"回溯: 跳过 {roi_id} (原因: {reason})，切换到 {next_roi.get('id', 'unknown')}"
        }
    else:
        return {
            'action': 'continue',
            'next_roi': None,
            'message': f"回溯: 跳过 {roi_id} (原因: {reason})，无更多候选"
        }


def should_skip_roi(analysis_result: dict, roi_info: dict, agent_state: dict) -> tuple:
    """
    综合判断是否应该跳过当前ROI
    
    结合回溯检测和其他条件判断。
    
    Args:
        analysis_result: 分析结果
        roi_info: ROI信息
        agent_state: Agent状态
    
    Returns:
        tuple: (should_skip: bool, reason: str)
    """
    # 检查回溯触发
    should_backtrack, reason, trigger_type = detect_backtrack_trigger(analysis_result)
    
    if should_backtrack:
        return True, reason
    
    # 检查是否已经分析过（避免重复）
    analyzed_ids = [r.get('id') for r in agent_state.get('analyzed_rois', [])]
    if roi_info.get('id') in analyzed_ids:
        return True, "已分析过"
    
    return False, ''


def log_backtrack_event(agent_state: dict, roi_id: str, reason: str, action: str):
    """记录回溯事件（用于审计和调试）"""
    event = {
        'timestamp': datetime.now().isoformat(),
        'roi_id': roi_id,
        'reason': reason,
        'action': action
    }
    agent_state.setdefault('backtrack_history', []).append(event)
    logger.info(f"📝 [回溯事件] {roi_id}: {reason} → {action}")


# ==================== ROI命名简化系统 ====================
def assign_simple_roi_ids(rois, prefix, agent_state, parent_id=None):
    """
    为选中的ROI分配简单的顺序ID
    
    红框: 使用缩略图中的 grid_id（如 R2, R5, R9）保持与缩略图一致
    蓝框: 使用 {父红框ID}-B{N} 格式（如 R2-B1, R2-B2）
    绿框: 使用 {父蓝框ID}-G{N} 格式（如 R2-B1-G1）
    
    Args:
        rois: ROI列表
        prefix: ID前缀 ('R' for red, 'B' for blue, 'G' for green)
        agent_state: Agent状态字典（用于维护计数器）
        parent_id: 父级ROI的ID（蓝框需要红框ID，绿框可从ROI的parent_id属性获取）
    
    Returns:
        list: 更新后的ROI列表（原地修改）
    """
    import re
    counter_key = f'{prefix.lower()}_roi_counter'
    if counter_key not in agent_state:
        agent_state[counter_key] = 0
    
    for roi in rois:
        current_id = roi.get('id', '')
        
        # 检查是否已经有正确格式的ID
        if prefix == 'R' and re.match(r'^R\d+$', current_id):
            continue
        elif prefix == 'B' and re.match(r'^R\d+-B\d+$', current_id):
            continue
        elif prefix == 'G' and re.match(r'^R\d+-B\d+-G\d+$', current_id):
            continue
        
        roi['original_id'] = current_id  # 保留原始ID用于调试
        
        if prefix == 'R':
            # 红框：使用 grid_id 保持与缩略图一致
            grid_id = roi.get('grid_id')
            if grid_id:
                roi['id'] = f'R{grid_id}'
            else:
                # 如果没有 grid_id，使用顺序编号
                agent_state[counter_key] += 1
                roi['id'] = f'R{agent_state[counter_key]}'
        elif prefix == 'B':
            # 蓝框：使用 {父红框ID}-B{grid_id} 格式，保留原始网格编号
            actual_parent = parent_id or roi.get('parent_id', '')
            grid_id = roi.get('grid_id')  # ⭐ 优先使用原始网格编号
            if actual_parent and grid_id:
                # 使用原始网格编号，不重新计数
                roi['id'] = f'{actual_parent}-B{grid_id}'
            elif actual_parent:
                # 没有grid_id时才使用顺序编号
                parent_counter_key = f'{actual_parent}_b_counter'
                if parent_counter_key not in agent_state:
                    agent_state[parent_counter_key] = 0
                agent_state[parent_counter_key] += 1
                roi['id'] = f'{actual_parent}-B{agent_state[parent_counter_key]}'
            else:
                agent_state[counter_key] += 1
                roi['id'] = f'B{agent_state[counter_key]}'
        elif prefix == 'G':
            # 绿框：使用 {父蓝框ID}-G{grid_id} 格式，保留原始网格编号
            actual_parent = parent_id or roi.get('parent_id', '')
            grid_id = roi.get('grid_id')  # ⭐ 优先使用原始网格编号
            if actual_parent and grid_id:
                # 使用原始网格编号，不重新计数
                roi['id'] = f'{actual_parent}-G{grid_id}'
            elif actual_parent:
                # 没有grid_id时才使用顺序编号
                parent_counter_key = f'{actual_parent}_g_counter'
                if parent_counter_key not in agent_state:
                    agent_state[parent_counter_key] = 0
                agent_state[parent_counter_key] += 1
                roi['id'] = f'{actual_parent}-G{agent_state[parent_counter_key]}'
            else:
                agent_state[counter_key] += 1
                roi['id'] = f'G{agent_state[counter_key]}'
        else:
            # 默认：顺序编号
            agent_state[counter_key] += 1
            roi['id'] = f'{prefix}{agent_state[counter_key]}'
    
    return rois

def get_simple_roi_label(roi_id):
    """获取ROI的简单标签（用于显示）"""
    import re
    # 如果已经是简单ID，直接返回
    if re.match(r'^[RBG]\d+$', roi_id):
        return roi_id
    # 否则返回原ID
    return roi_id

def update_green_parent_ids(green_rois, blue_rois):
    """
    更新绿框的parent_id，使其指向蓝框的新ID
    
    在蓝框重命名后调用，确保绿框能正确获取其父蓝框的新ID
    
    Args:
        green_rois: 绿框列表
        blue_rois: 蓝框列表（已重命名）
    """
    # 创建旧ID到新ID的映射
    old_to_new = {}
    for blue in blue_rois:
        old_id = blue.get('original_id', '')
        new_id = blue.get('id', '')
        if old_id and new_id:
            old_to_new[old_id] = new_id
    
    # 更新绿框的parent_id
    for green in green_rois:
        old_parent_id = green.get('parent_id', '')
        if old_parent_id in old_to_new:
            green['parent_id'] = old_to_new[old_parent_id]

def init_roi_counters(agent_state):
    """初始化ROI计数器"""
    agent_state['r_roi_counter'] = 0
    agent_state['b_roi_counter'] = 0
    agent_state['g_roi_counter'] = 0

# ==================== 父ROI图像缓存系统（性能优化） ====================
import math

# 全局缓存
_parent_roi_cache = {}

def get_parent_roi_context(slide, parent_roi, target_size=2000):
    """
    获取父ROI的图像上下文（带缓存、自动Level选择、自动Resize）
    返回一个字典，包含图像和坐标映射所需的参数
    
    Args:
        slide: OpenSlide对象
        parent_roi: 父ROI信息字典
        target_size: 目标图像尺寸（默认2000）
    
    Returns:
        dict: 包含 image, scale, origin, roi_id 的上下文字典
    """
    roi_id = parent_roi['id']
    
    # 1. 命中缓存直接返回
    if roi_id in _parent_roi_cache:
        logger.debug(f"🚀 [Cache Hit] 复用父ROI图像: {roi_id}")
        return _parent_roi_cache[roi_id]

    # 2. 准备参数
    x, y = parent_roi['rect']['x'], parent_roi['rect']['y']  # Level 0 坐标
    w, h = parent_roi['rect']['w'], parent_roi['rect']['h']  # Level 0 尺寸
    max_side = max(w, h)

    # 3. 智能选择 Level (保证读取尺寸 >= target_size)
    meta = safe_get_slide_metadata(slide)
    best_level = 0
    for level in range(meta['level_count']):
        ds = meta['level_downsamples'][level]
        if (max_side / ds) >= target_size:
            best_level = level
        else:
            break
    
    # 4. 计算读取尺寸 (使用 ceil 防黑边)
    level_ds = meta['level_downsamples'][best_level]
    read_w = int(math.ceil(w / level_ds))
    read_h = int(math.ceil(h / level_ds))

    logger.info(f"📥 [IO Read] 父ROI {roi_id}: Level {best_level} (DS={level_ds:.1f}) | 原始 {w}x{h} -> 读取 {read_w}x{read_h}")

    # 5. 读取 WSI (耗时操作，通过全局锁序列化防止并发损坏)
    region_img = safe_read_region(slide, (x, y), best_level, (read_w, read_h)).convert("RGB")

    # 6. 统一 Resize 到 target_size (为了省内存和统一输入)
    # 保持长宽比，计算目标尺寸
    aspect_ratio = read_w / read_h
    if aspect_ratio > 1:
        final_w = target_size
        final_h = int(target_size / aspect_ratio)
    else:
        final_h = target_size
        final_w = int(target_size * aspect_ratio)
    
    if region_img.size != (final_w, final_h):
        region_img = region_img.resize((final_w, final_h), Image.Resampling.LANCZOS)
        logger.info(f"📐 [Resize] {read_w}x{read_h} -> {final_w}x{final_h}")
    
    # 计算全局缩放因子 (非常重要！用于后续把红框坐标映射到这张图上)
    scale_x = final_w / w
    scale_y = final_h / h

    # 7. 存入缓存结构
    context = {
        'image': region_img,            # Resize后的图
        'scale': (scale_x, scale_y),    # 坐标缩放因子
        'origin': (x, y),               # Level 0 起点
        'original_size': (w, h),        # Level 0 原始尺寸
        'final_size': (final_w, final_h),  # 最终图像尺寸
        'roi_id': roi_id
    }
    
    _parent_roi_cache[roi_id] = context
    logger.info(f"💾 [Cache Store] 已缓存父ROI {roi_id}, 当前缓存数: {len(_parent_roi_cache)}")
    return context

def map_coord_to_cached_image(roi_rect, parent_context):
    """
    工具函数：将 Level 0 的ROI坐标，映射到缓存图像上的像素坐标
    
    Args:
        roi_rect: ROI的rect字典 {'x', 'y', 'w', 'h'}
        parent_context: 父ROI上下文（来自get_parent_roi_context）
    
    Returns:
        tuple: (new_x, new_y, new_w, new_h) 在缓存图像上的坐标
    """
    scale_x, scale_y = parent_context['scale']
    parent_x, parent_y = parent_context['origin']

    # 1. 算出相对于父区域左上角的偏移
    rel_x = roi_rect['x'] - parent_x
    rel_y = roi_rect['y'] - parent_y

    # 2. 缩放
    new_x = int(rel_x * scale_x)
    new_y = int(rel_y * scale_y)
    new_w = int(roi_rect['w'] * scale_x)
    new_h = int(roi_rect['h'] * scale_y)

    return (new_x, new_y, new_w, new_h)

def clear_roi_cache(roi_id=None):
    """
    清理ROI缓存
    
    Args:
        roi_id: 指定要清理的ROI ID，为None则清理全部
    """
    global _parent_roi_cache
    if roi_id:
        if roi_id in _parent_roi_cache:
            del _parent_roi_cache[roi_id]
            logger.info(f"🧹 [Cache Clear] 已清理父ROI缓存: {roi_id}")
    else:
        cache_count = len(_parent_roi_cache)
        _parent_roi_cache.clear()
        import gc
        gc.collect()
        logger.info(f"🧹 [Cache Clear] 已清理全部缓存 ({cache_count}个)")

def generate_html_visual_report(agent_state, file_id, output_dir, final_report_text, total_red, total_blue, total_green, report_title=None, lang=None):
    """
    生成HTML可视化病理报告 - 完全按照 3.Report_Generator.py 格式
    
    Args:
        agent_state: Agent状态字典
        file_id: 文件ID
        output_dir: 输出目录
        final_report_text: 最终报告文本（Q1-Q8格式）
        total_red, total_blue, total_green: 框数量统计
        report_title: 报告标题（可选）
        lang: 语言设置 'zh' 或 'en'（默认从 agent_state 读取）
    
    Returns:
        str: HTML报告文件路径
    """
    # ⭐ 语言设置
    if lang is None:
        lang = agent_state.get('lang', 'zh')
    is_en = (lang == 'en')

    # ⭐ 报告 UI 文本国际化
    _t = {
        'report_title_default': 'Final Pathology Diagnostic Report' if is_en else '最终病理诊断报告',
        'html_title':           'Pathology Report' if is_en else '病理诊断报告',
        'header_h1':            '🔬 Pathology Diagnostic Report' if is_en else '🔬 病理诊断报告',
        'label_sample':         'Sample' if is_en else '样本文件',
        'label_organ':          'Tissue Origin' if is_en else '组织来源',
        'label_time':           'Generated' if is_en else '生成时间',
        'section_stats':        '📊 Analysis Statistics' if is_en else '📊 分析统计',
        'label_red':            '🔴 Red ROI (10×)' if is_en else '🔴 红框 (10x)',
        'label_blue':           '🔵 Blue ROI (40×)' if is_en else '🔵 蓝框 (40x)',
        'label_green':          '🟢 Green ROI (160×)' if is_en else '🟢 绿框 (160x)',
        'section_step1':        '📊 Step 1: Global Screening (Thumbnail Overview)' if is_en else '📊 第一步：全局筛查（全片缩略图）',
        'caption_global':       'Global view - 10× low-power (red ROI numbers labeled)' if is_en else '全局视野 - 10倍低倍镜 (红框编号已标注)',
        'label_task':           'Task' if is_en else '任务',
        'task_desc':            'Select the most diagnostically valuable regions from the panoramic view' if is_en else '在全景视野下选择最具诊断价值的区域',
        'label_selected_red':   'Selected Red ROIs' if is_en else '选中红框',
        'label_global_obs':     '💭 AI Global Observation [Reason] - Red ROI selection rationale (click to expand/collapse)' if is_en else '💭 AI全局观察【Reason】- 选择红框理由 (点击展开/收起)',
        'section_step2':        '🔍 Step 2: Multi-scale Analysis' if is_en else '🔍 第二步：多倍镜逐级分析',
        'label_path':           'Path' if is_en else '路径',
        'label_red_box':        'Red ROI' if is_en else '红框',
        'label_red_obs':        '💭 AI Observation (40×) - Blue ROI selection rationale (click to expand/collapse)' if is_en else '💭 AI观察 (40x) - 选择蓝框理由 (点击展开/收起)',
        'label_blue_box':       'Blue ROI' if is_en else '蓝框',
        'label_blue_obs':       '💭 Blue ROI Analysis (click to expand/collapse)' if is_en else '💭 蓝框分析 (点击展开/收起)',
        'label_green_select':   '🟢 Green ROI Selection Rationale (click to expand/collapse)' if is_en else '🟢 绿框选择原因 (点击展开/收起)',
        'label_green_box':      'Green ROI' if is_en else '绿框',
        'label_green_obs':      'Analysis (click to expand/collapse)' if is_en else '分析 (点击展开/收起)',
        'section_step3':        'Step 3' if is_en else '第三步',
        'footer_disclaimer':    'This report is generated with PathCopilot AI assistance for reference only. Final diagnosis must be confirmed by a qualified pathologist.' if is_en else '本报告由 PathCopilot AI 辅助生成，仅供参考，最终诊断需由专业病理医生确认',
        'time_format':          '%Y-%m-%d %H:%M:%S',
    }

    # ⭐ 设置默认标题
    if report_title is None:
        report_title = _t['report_title_default']
    from datetime import datetime
    from pathlib import Path
    import base64
    import glob
    
    def encode_image_to_base64(image_path):
        """将图像文件编码为base64"""
        try:
            if image_path and Path(image_path).exists():
                with open(image_path, 'rb') as f:
                    return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logger.warning(f"无法编码图像 {image_path}: {e}")
        return None
    
    try:
        filename = agent_state.get('filename', '未知文件')
        organ = agent_state.get('organ', ORGAN_DEFAULT)
        sample_name = Path(filename).stem if filename else file_id

        # ⭐ 英文模式下翻译器官名称
        if is_en:
            _organ_map = {
                '肺': 'Lung',
                '子宫内膜': 'Endometrium',
                '子宫': 'Uterus',
                '宫颈': 'Cervix',
                '卵巢': 'Ovary',
                '输卵管': 'Fallopian tube',
                '子宫体': 'Uterine body',
                '子宫颈': 'Uterine cervix',
            }
            organ_display = _organ_map.get(organ, organ)
        else:
            organ_display = organ
        
        # ⭐ 从 tree_nodes 获取实际分析的红框数量（而非候选框数量）
        tree_nodes = agent_state.get('tree_nodes', [])
        actual_red_count = len(tree_nodes)
        actual_blue_count = sum(len(node.get('children', [])) for node in tree_nodes)
        actual_green_count = sum(
            len(blue.get('children', [])) 
            for red in tree_nodes 
            for blue in red.get('children', [])
        )
        
        # 获取选中的红框ID列表
        selected_red_ids = [node.get('roi_id', f'R{i+1}') for i, node in enumerate(tree_nodes)]
        
        # ⭐ 查找全局缩略图（带网格编号）
        global_thumb_b64 = None
        
        # ⭐ 优先使用 agent_state 中记录的持久化路径
        _global_thumb_path = agent_state.get('global_thumbnail_path')
        if _global_thumb_path and Path(_global_thumb_path).exists():
            global_thumb_b64 = encode_image_to_base64(_global_thumb_path)
            if global_thumb_b64:
                logger.info(f"✓ 使用持久化全局缩略图: {_global_thumb_path}")
        
        # 回退：尝试多个可能的位置
        if not global_thumb_b64:
            thumb_search_paths = [
                # 1. steps 目录（持久化保存）
                output_dir / "steps",
                # 2. 标准位置：debug_images/{sample_name}/
                Path(f"debug_images/{sample_name}"),
                # 3. 输出目录本身
                output_dir
            ]
            
            for search_dir in thumb_search_paths:
                if search_dir.exists():
                    patterns = [
                        "global_thumbnail.jpg",  # ⭐ 新命名规则优先
                        "debug_gpt_global_*.jpg",
                        "全局_网格_带编号.jpg",
                        "step_0_thumbnail.jpg"
                    ]
                    for pattern in patterns:
                        matches = list(search_dir.glob(pattern))
                        if matches:
                            matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                            global_thumb_b64 = encode_image_to_base64(matches[0])
                            if global_thumb_b64:
                                logger.info(f"✓ 找到全局缩略图(回退): {matches[0]}")
                                break
                    if global_thumb_b64:
                        break
        
        if not global_thumb_b64:
            logger.warning(f"⚠ 未找到全局缩略图")
        
        # ==================== 构建 HTML（完全按照 3.Report_Generator.py 格式）====================
        
        # CSS 样式（与 3.Report_Generator.py 一致）
        html_content = f"""<!DOCTYPE html>
<html lang="{'en' if is_en else 'zh-CN'}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_t['html_title']} - {filename}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: "Microsoft YaHei", "SimHei", Arial, sans-serif; line-height: 1.8; color: #333; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; box-shadow: 0 0 20px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 40px; text-align: center; }}
        .header h1 {{ font-size: 32px; margin-bottom: 10px; }}
        .header .meta {{ font-size: 14px; opacity: 0.9; }}
        .content {{ padding: 40px; }}
        .section {{ margin-bottom: 40px; }}
        .section-title {{ font-size: 24px; color: #667eea; border-left: 4px solid #667eea; padding-left: 15px; margin-bottom: 20px; }}
        
        /* 全局视图样式 */
        .global-view {{ text-align: center; margin: 30px 0; }}
        .global-view img {{ max-width: 100%; border: 2px solid #ddd; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
        .global-view .caption {{ margin-top: 10px; font-size: 14px; color: #666; }}
        
        /* 信息框样式 */
        .info-box {{ background: #f8f9fa; border-left: 4px solid #667eea; padding: 15px 20px; margin: 15px 0; border-radius: 4px; }}
        .info-box strong {{ color: #667eea; }}
        
        /* 统计网格 */
        .stats-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: linear-gradient(135deg, #f5f7fa 0%, #e4e8eb 100%); padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-card .number {{ font-size: 36px; font-weight: bold; }}
        .stat-card .label {{ color: #666; margin-top: 5px; }}
        .stat-card.red .number {{ color: #f44336; }}
        .stat-card.blue .number {{ color: #2196F3; }}
        .stat-card.green .number {{ color: #4CAF50; }}
        
        /* 路径卡片样式 */
        .path-card {{ border: 1px solid #e0e0e0; border-radius: 8px; padding: 25px; margin: 20px 0; background: #fafafa; }}
        .path-card .path-header {{ background: linear-gradient(90deg, #667eea 0%, #764ba2 100%); color: white; padding: 12px 20px; border-radius: 6px; font-size: 18px; font-weight: bold; margin: -25px -25px 20px -25px; cursor: pointer; user-select: none; position: relative; padding-left: 50px; }}
        .path-card .path-header::before {{ content: '▶'; position: absolute; left: 20px; transition: transform 0.3s; }}
        .path-card .path-header.active::before {{ transform: rotate(90deg); }}
        .path-card .path-body {{ display: none; overflow: hidden; }}
        .path-card .path-body.show {{ display: block; }}
        
        /* ROI层级样式 */
        .roi-level {{ margin: 20px 0; padding: 15px; background: white; border-radius: 6px; border-left: 3px solid #ddd; }}
        .roi-level.red {{ border-left-color: #f44336; }}
        .roi-level.blue {{ border-left-color: #2196F3; border: 1px solid #e3f2fd; background: #fafefe; }}
        .roi-level.green {{ border-left-color: #4CAF50; }}
        .roi-level .level-badge {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: bold; margin-bottom: 10px; }}
        .roi-level.red .level-badge {{ background: #ffebee; color: #c62828; }}
        .roi-level.blue .level-badge {{ background: #e3f2fd; color: #1565c0; cursor: pointer; user-select: none; position: relative; padding-left: 30px; }}
        .roi-level.blue .level-badge::before {{ content: '▶'; position: absolute; left: 10px; transition: transform 0.3s; }}
        .roi-level.blue .level-badge.active::before {{ transform: rotate(90deg); }}
        .roi-level.green .level-badge {{ background: #e8f5e9; color: #2e7d32; }}
        
        /* 蓝框内容折叠 */
        .blue-box-content {{ display: none; overflow: hidden; }}
        .blue-box-content.show {{ display: block; }}
        
        /* ROI图像网格 */
        .roi-images {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 15px; margin-top: 15px; }}
        .roi-images img {{ width: 100%; border: 1px solid #ddd; border-radius: 4px; }}
        
        /* 思考过程（可折叠）*/
        .thinking {{ background: #fff3e0; border-left: 4px solid #ff9800; padding: 15px; margin: 15px 0; border-radius: 4px; }}
        .thinking .label {{ font-weight: bold; color: #e65100; margin-bottom: 8px; cursor: pointer; user-select: none; position: relative; padding-left: 25px; }}
        .thinking .label::before {{ content: '▶'; position: absolute; left: 0; transition: transform 0.3s; }}
        .thinking .label.active::before {{ transform: rotate(90deg); }}
        .thinking pre {{ white-space: pre-wrap; word-wrap: break-word; font-family: inherit; margin: 0; line-height: 1.8; }}
        
        /* 折叠内容通用 */
        .collapsible {{ cursor: pointer; user-select: none; position: relative; padding-left: 25px; }}
        .collapsible::before {{ content: '▶'; position: absolute; left: 0; transition: transform 0.3s; }}
        .collapsible.active::before {{ transform: rotate(90deg); }}
        .collapsible-content {{ display: none; overflow: hidden; }}
        .collapsible-content.show {{ display: block; }}
        
        /* 最终报告 */
        .final-report {{ background: #fff; border: 3px solid #667eea; border-radius: 8px; padding: 30px; }}
        .final-report pre {{ white-space: pre-wrap; word-wrap: break-word; font-family: inherit; line-height: 1.8; margin: 0; }}
        
        /* 修改记录 */
        .modification-record {{
            border: 2px solid #ef4444;
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
            background: linear-gradient(135deg, #fff5f5 0%, #fef2f2 100%);
            position: relative;
        }}
        .modification-record::before {{
            content: '🤝';
            position: absolute;
            top: -14px;
            left: 20px;
            background: white;
            padding: 0 8px;
            font-size: 20px;
        }}
        .modification-record .mod-title {{
            font-size: 16px;
            font-weight: bold;
            color: #dc2626;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid #fecaca;
        }}
        .modification-record .mod-change {{
            background: white;
            border-radius: 6px;
            padding: 12px 16px;
            margin-bottom: 10px;
            border-left: 3px solid #ef4444;
        }}
        .modification-record .mod-change .change-label {{
            font-weight: bold;
            color: #991b1b;
            margin-bottom: 4px;
        }}
        .modification-record .mod-user {{
            background: #eff6ff;
            border-radius: 6px;
            padding: 12px 16px;
            margin-bottom: 10px;
            border-left: 3px solid #3b82f6;
        }}
        .modification-record .mod-user .user-icon {{ color: #2563eb; }}
        .modification-record .mod-ai {{
            background: #f0fdf4;
            border-radius: 6px;
            padding: 12px 16px;
            margin-bottom: 6px;
            border-left: 3px solid #22c55e;
        }}
        .modification-record .mod-ai .ai-icon {{ color: #16a34a; }}
        .modification-record .mod-ai.rejected {{
            background: #fefce8;
            border-left-color: #f59e0b;
        }}
        .modification-record .mod-ai.rejected .ai-icon {{ color: #d97706; }}
        
        /* 页脚 */
        .footer {{ background: #f5f5f5; padding: 20px; text-align: center; color: #666; font-size: 14px; }}
        
        @media print {{
            body {{ background: white; }}
            .container {{ box-shadow: none; }}
            .collapsible-content {{ display: block !important; }}
            .path-body {{ display: block !important; }}
            .blue-box-content {{ display: block !important; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- 报告头部 -->
        <div class="header">
            <h1>{_t['header_h1']}</h1>
            <div class="meta">
                <p>{_t['label_sample']}: {filename}</p>
                <p>{_t['label_organ']}: {organ_display}</p>
                <p>{_t['label_time']}: {datetime.now().strftime(_t['time_format'])}</p>
            </div>
        </div>
        
        <div class="content">
            <!-- 统计概览 -->
            <div class="section">
                <h2 class="section-title">{_t['section_stats']}</h2>
                <div class="stats-grid">
                    <div class="stat-card red">
                        <div class="number">{actual_red_count}</div>
                        <div class="label">{_t['label_red']}</div>
                    </div>
                    <div class="stat-card blue">
                        <div class="number">{actual_blue_count}</div>
                        <div class="label">{_t['label_blue']}</div>
                    </div>
                    <div class="stat-card green">
                        <div class="number">{actual_green_count}</div>
                        <div class="label">{_t['label_green']}</div>
                    </div>
                </div>
            </div>
"""
        
        # ==================== 第1部分：全局筛查 ====================
        html_content += f"""
            <!-- 全局筛查 -->
            <div class="section">
                <h2 class="section-title">{_t['section_step1']}</h2>
"""
        
        # 插入全局缩略图
        if global_thumb_b64:
            html_content += f"""
                <div class="global-view">
                    <img src="data:image/jpeg;base64,{global_thumb_b64}" alt="global thumbnail">
                    <p class="caption">{_t['caption_global']}</p>
                </div>
"""
        
        # ⭐ 获取全局分析的AI思考过程
        global_analysis_raw = agent_state.get('global_analysis', '')
        
        # ⭐ 修复：处理 global_analysis 可能是列表的情况
        if isinstance(global_analysis_raw, list):
            # 如果是列表，将其转换为字符串
            global_analysis = '\n'.join([str(item) for item in global_analysis_raw])
            logger.info(f"📋 [HTML报告] global_analysis 是列表 (长度: {len(global_analysis_raw)}), 已转换为字符串")
        elif isinstance(global_analysis_raw, dict):
            # 如果是字典，尝试提取有意义的内容
            global_analysis = global_analysis_raw.get('reasoning', '') or global_analysis_raw.get('analysis', '') or str(global_analysis_raw)
            logger.info(f"📋 [HTML报告] global_analysis 是字典, 已提取内容")
        else:
            global_analysis = str(global_analysis_raw) if global_analysis_raw else ''
        
        # ⭐ 调试：打印全局分析内容
        logger.info(f"📋 [HTML报告] global_analysis 最终长度: {len(global_analysis)}")
        if global_analysis:
            # 安全地截取前200字符
            preview = global_analysis[:200] if len(global_analysis) > 200 else global_analysis
            logger.info(f"📋 [HTML报告] global_analysis 前200字符: {preview}...")
        else:
            logger.warning(f"⚠️ [HTML报告] global_analysis 为空！检查 agent_state 键: {list(agent_state.keys())}")
        
        html_content += f"""
                <div class="info-box">
                    <p><strong>{_t['label_task']}：</strong>{_t['task_desc']}</p>
                    <p><strong>{_t['label_selected_red']}：</strong>{', '.join(selected_red_ids)}</p>
                </div>
"""
        
        # ⭐ 显示AI的完整思考过程【Reason】- 无论是否为空都生成（调试用）
        if global_analysis:
            html_content += f"""
                <div class="thinking">
                    <div class="label collapsible active">{_t['label_global_obs']}</div>
                    <div class="collapsible-content show">
                        <pre>{global_analysis}</pre>
                    </div>
                </div>
"""
        else:
            # ⭐ 如果 global_analysis 为空，尝试从第一个红框的分析中获取
            if tree_nodes and len(tree_nodes) > 0:
                first_red_analysis = tree_nodes[0].get('analysis', {})
                fallback_reasoning = first_red_analysis.get('reasoning', '') or first_red_analysis.get('selection_reasoning', '')
                if fallback_reasoning:
                    logger.info(f"📋 [HTML报告] 使用第一个红框的 reasoning 作为 fallback (长度: {len(fallback_reasoning)})")
                    html_content += f"""
                <div class="thinking">
                    <div class="label collapsible active">{_t['label_global_obs']}</div>
                    <div class="collapsible-content show">
                        <pre>{fallback_reasoning}</pre>
                    </div>
                </div>
"""
        
        html_content += """
            </div>
"""
        
        # ==================== 第2部分：多倍镜逐级分析（与3.Report_Generator.py一致） ====================
        html_content += f"""
            <!-- 路径分析 -->
            <div class="section">
                <h2 class="section-title">{_t['section_step2']}</h2>
"""
        
        # 遍历每个红框（路径）
        for path_idx, red_node in enumerate(tree_nodes, 1):
            red_id = red_node.get('roi_id', f'R{path_idx}')
            # ⭐ 优先使用 reasoning（完整思考过程），其次使用 findings（摘要）
            red_analysis = red_node.get('analysis', {})
            
            # ⭐ 增强获取逻辑：从多个字段尝试获取完整AI输出
            red_reasoning = (
                red_analysis.get('reasoning', '') or 
                red_analysis.get('selection_reasoning', '') or 
                red_analysis.get('thinking', '') or
                red_analysis.get('observation', '') or
                ''
            )
            
            # 如果还是为空，尝试从 red_node 直接获取
            if not red_reasoning:
                red_reasoning = red_node.get('reasoning', '') or red_node.get('observation', '')
            
            # 最终 fallback 到 findings
            red_findings = red_reasoning if red_reasoning else (
                red_analysis.get('findings', '') or 
                red_node.get('findings', '') or 
                ('No analysis result' if is_en else '无分析结果')
            )
            
            # ⭐ 调试日志
            logger.info(f"📋 [HTML报告] 红框 {red_id} - reasoning长度: {len(red_reasoning)}, findings长度: {len(red_findings)}")
            red_image_path = red_analysis.get('image_path') or red_node.get('image_path')
            
            # ⭐ 如果没有图片路径，尝试从 debug_images 目录查找红框的蓝框网格图
            if not red_image_path:
                debug_dir = Path(f"debug_images/{sample_name}")
                if debug_dir.exists():
                    # ⭐⭐⭐ 修复：只使用精确匹配，避免 R13 匹配到 R13-B4 等蓝框图片 ⭐⭐⭐
                    # 旧代码 debug_gpt_*{red_id}*_*.jpg 会导致红框R13匹配到蓝框R13-B8的图片
                    patterns = [
                        f"debug_gpt_{red_id}_*candidates_*.jpg",  # 例如 debug_gpt_R12_16candidates_12345.jpg
                    ]
                    for pattern in patterns:
                        matches = list(debug_dir.glob(pattern))
                        if matches:
                            matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                            red_image_path = str(matches[0])
                            logger.info(f"找到红框图片: {red_image_path}")
                            break
            
            html_content += f"""
                <div class="path-card">
                    <div class="path-header" onclick="togglePath(this)">
                        🔬 {_t['label_path']} {path_idx}：{_t['label_red_box']} {red_id}
                    </div>
                    <div class="path-body">
"""
            
            # 红框图片（显示的是40x下的蓝框网格图，与3.Report_Generator.py一致）
            if red_image_path:
                red_img_b64 = encode_image_to_base64(red_image_path)
                if red_img_b64:
                    html_content += f"""
                        <div class="roi-level red">
                            <span class="level-badge">🔴 {_t['label_red_box']} {red_id} - 40×</span>
                            <div class="roi-images">
                                <img src="data:image/jpeg;base64,{red_img_b64}" alt="{red_id}">
                            </div>
                        </div>
"""
            
            # 红框层面的AI分析（40x下观察蓝框网格并选择）- 与3.Report_Generator.py一致
            if red_findings:
                html_content += f"""
                        <div class="thinking">
                            <div class="label collapsible active">{_t['label_red_obs']}</div>
                            <div class="collapsible-content show">
                                <pre>{red_findings}</pre>
                            </div>
                        </div>
"""
            
            # 遍历蓝框
            for blue_idx, blue_node in enumerate(red_node.get('children', []), 1):
                blue_id = blue_node.get('roi_id', f'B{blue_idx}')
                # ⭐ 优先使用 reasoning（完整思考过程），其次使用 findings（摘要）
                blue_analysis = blue_node.get('analysis', {})
                blue_reasoning = blue_analysis.get('reasoning', '') or blue_analysis.get('green_selection_reasoning', '')
                blue_findings = blue_reasoning or blue_analysis.get('findings', '') or blue_node.get('findings', '') or ('No analysis result' if is_en else '无分析结果')
                blue_image_path = blue_analysis.get('image_path') or blue_node.get('image_path')
                
                # ⭐ 回退搜索 steps 目录
                if not blue_image_path:
                    _steps_dir = output_dir / "steps"
                    if _steps_dir.exists():
                        _candidate = _steps_dir / f"blue_{blue_id}.jpg"
                        if _candidate.exists():
                            blue_image_path = str(_candidate)
                
                html_content += f"""
                        <div class="roi-level blue">
                            <span class="level-badge" onclick="toggleBlueBox(this)">🔵 {_t['label_blue_box']} {blue_id} (40×)</span>
                            <div class="blue-box-content">
"""
                
                # 蓝框图片
                if blue_image_path:
                    blue_img_b64 = encode_image_to_base64(blue_image_path)
                    if blue_img_b64:
                        html_content += f"""
                                <div class="roi-images">
                                    <img src="data:image/jpeg;base64,{blue_img_b64}" alt="{blue_id}">
                                </div>
"""
                
                # 蓝框分析结果
                if blue_findings:
                    html_content += f"""
                                <div class="thinking">
                                    <div class="label collapsible active">{_t['label_blue_obs']}</div>
                                    <div class="collapsible-content show">
                                        <pre>{blue_findings}</pre>
                                    </div>
                                </div>
"""
                
                # ⭐ 新增：绿框选择原因（解释为什么选择这些绿框进行高倍放大）
                green_selection_reason = blue_node.get('analysis', {}).get('green_selection_reasoning', '')
                if green_selection_reason:
                    html_content += f"""
                                <div class="thinking" style="background: #e8f5e9; border-left-color: #4CAF50;">
                                    <div class="label collapsible active">{_t['label_green_select']}</div>
                                    <div class="collapsible-content show">
                                        <pre>{green_selection_reason}</pre>
                                    </div>
                                </div>
"""
                
                # 遍历绿框
                green_children = blue_node.get('children', [])
                if green_children:
                    green_ids = [g.get('roi_id', f'G{i+1}') for i, g in enumerate(green_children)]
                    html_content += f"""
                                <div class="roi-level green">
                                    <span class="level-badge">🟢 {_t['label_green_box']} {', '.join(green_ids)} (160×)</span>
                                    <div class="roi-images">
"""
                    
                    for green_node in green_children:
                        green_image_path = green_node.get('analysis', {}).get('image_path') or green_node.get('image_path')
                        if green_image_path:
                            green_img_b64 = encode_image_to_base64(green_image_path)
                            if green_img_b64:
                                green_id = green_node.get('roi_id', 'G?')
                                html_content += f'<img src="data:image/jpeg;base64,{green_img_b64}" alt="{green_id}">'
                    
                    html_content += """
                                    </div>
"""
                    
                    # 绿框分析结果
                    for green_node in green_children:
                        green_id = green_node.get('roi_id', 'G?')
                        # ⭐ 优先使用 reasoning（完整思考过程），其次使用 findings（摘要）
                        green_analysis = green_node.get('analysis', {})
                        green_reasoning = green_analysis.get('reasoning', '')
                        green_findings = green_reasoning or green_analysis.get('findings', '') or green_node.get('findings', '')
                        if green_findings:
                            html_content += f"""
                                    <div class="thinking">
                                        <div class="label collapsible active">💭 {green_id} {_t['label_green_obs']}</div>
                                        <div class="collapsible-content show">
                                            <pre>{green_findings}</pre>
                                        </div>
                                    </div>
"""
                    
                    html_content += """
                                </div>
"""
                
                html_content += """
                            </div>
                        </div>
"""
            
            html_content += """
                    </div>
                </div>
"""
        
        html_content += """
            </div>
"""
        
        # ==================== 第3部分：最终诊断报告 ====================
        html_content += f"""
            <!-- 最终诊断报告 -->
            <div class="section">
                <h2 class="section-title">📋 {_t['section_step3']}{'：' if not is_en else ': '}{report_title}</h2>
                <div class="final-report">
                    <pre>{final_report_text}</pre>
                </div>
            </div>
        </div>
        
        <div class="footer">
            <p>{_t['footer_disclaimer']}</p>
            <p>{_t['label_time']}: {datetime.now().strftime(_t['time_format'])}</p>
        </div>
    </div>
    
    <script>
        // 路径卡片折叠/展开功能
        function togglePath(header) {{
            header.classList.toggle('active');
            const pathBody = header.nextElementSibling;
            if (pathBody && pathBody.classList.contains('path-body')) {{
                pathBody.classList.toggle('show');
            }}
        }}
        
        // 蓝框折叠/展开功能
        function toggleBlueBox(badge) {{
            badge.classList.toggle('active');
            const blueBoxContent = badge.nextElementSibling;
            if (blueBoxContent && blueBoxContent.classList.contains('blue-box-content')) {{
                blueBoxContent.classList.toggle('show');
            }}
        }}
        
        // 内容折叠/展开功能
        document.addEventListener('DOMContentLoaded', function() {{
            const collapsibles = document.querySelectorAll('.collapsible');
            
            collapsibles.forEach(function(collapsible) {{
                collapsible.addEventListener('click', function() {{
                    this.classList.toggle('active');
                    const content = this.nextElementSibling;
                    if (content && content.classList.contains('collapsible-content')) {{
                        content.classList.toggle('show');
                    }}
                }});
            }});
            
            // 默认展开第一个路径
            const firstPathHeader = document.querySelector('.path-header');
            if (firstPathHeader) {{
                togglePath(firstPathHeader);
            }}
            
            // 默认展开第一个蓝框
            const firstBlueBadge = document.querySelector('.roi-level.blue .level-badge');
            if (firstBlueBadge) {{
                toggleBlueBox(firstBlueBadge);
            }}
        }});
    </script>
</body>
</html>"""
        
        # 保存HTML文件
        html_path = output_dir / "pathology_report.html"
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        return html_path
        
    except Exception as e:
        logger.error(f"生成HTML报告失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def generate_final_pathology_report(agent_state, file_id, output_dir, socketio=None, room=None, report_title=None):
    """
    聚合所有层级分析结果，生成最终病理报告
    
    Args:
        agent_state: Agent状态字典
        file_id: 文件ID
        output_dir: 输出目录
        socketio: SocketIO实例（可选，用于实时推送）
        room: WebSocket房间ID（可选）
        report_title: 报告标题（可选，默认"最终病理诊断报告"）
    
    Returns:
        dict: 最终报告
    """
    lang = agent_state.get('lang', 'zh')
    # ⭐ 设置默认标题
    if report_title is None:
        report_title = "Final Pathology Diagnostic Report" if lang == 'en' else "最终病理诊断报告"
    try:
        # 统计信息
        total_red = len(agent_state['tree_nodes'])
        total_blue = sum(len(node['children']) for node in agent_state['tree_nodes'])
        total_green = sum(
            len(blue_node['children']) 
            for red_node in agent_state['tree_nodes'] 
            for blue_node in red_node['children']
        )
        
        logger.info(f"\n{'='*80}")
        logger.info(f"📊 最终汇总病理报告如下：")
        logger.info(f"{'='*80}")
        logger.info(f"分析统计: {total_red}个红框, {total_blue}个蓝框, {total_green}个绿框")
        
        # 构建树状结构字符串
        tree_text = f"\n{'─'*80}\n🌳 可视化树状图如下：\n{'─'*80}\n"
        for red_idx, red_node in enumerate(agent_state['tree_nodes'], 1):
            tree_text += f"🔴 红框{red_idx}: {red_node['roi_id']}\n"
            logger.info(f"🔴 红框{red_idx}: {red_node['roi_id']}")
            for blue_idx, blue_node in enumerate(red_node['children'], 1):
                tree_text += f"  ├─ 🔵 蓝框{blue_idx}: {blue_node['roi_id']}\n"
                logger.info(f"  ├─ 🔵 蓝框{blue_idx}: {blue_node['roi_id']}")
                for green_idx, green_node in enumerate(blue_node['children'], 1):
                    is_last = (green_idx == len(blue_node['children']))
                    connector = "└─" if is_last else "├─"
                    tree_text += f"  │  {connector} 🟢 绿框{green_idx}: {green_node['roi_id']}\n"
                    logger.info(f"  │  {connector} 🟢 绿框{green_idx}: {green_node['roi_id']}")
        tree_text += f"{'─'*80}\n"
        logger.info(f"{'─'*80}\n")
        
        # ⭐ 移除：不再发送冗余的树状图到前端
        # 树状图信息已在日志中显示，不需要在聊天面板重复显示
        
        # ⭐ 收集所有分析结果（带 confidence 加权）
        all_red_data = []
        all_blue_data = []
        all_green_data = []
        
        for red_node in agent_state['tree_nodes']:
            analysis = red_node['analysis']
            all_red_data.append({
                'findings': analysis.get('findings', ''),
                'confidence': analysis.get('confidence', 50),
                'pathology_type': analysis.get('pathology_type', 'uncertain'),
                'key_features': analysis.get('key_features', [])
            })
            
            for blue_node in red_node['children']:
                analysis = blue_node['analysis']
                all_blue_data.append({
                    'findings': analysis.get('findings', ''),
                    'confidence': analysis.get('confidence', 50),
                    'pathology_type': analysis.get('pathology_type', 'uncertain'),
                    'key_features': analysis.get('key_features', [])
                })
                
                for green_node in blue_node['children']:
                    analysis = green_node['analysis']
                    all_green_data.append({
                        'findings': analysis.get('findings', ''),
                        'confidence': analysis.get('confidence', 50),
                        'pathology_type': analysis.get('pathology_type', 'uncertain'),
                        'key_features': analysis.get('key_features', [])
                    })
        
        # ⭐ 聚合统计：计算恶性/良性区域数量
        def count_pathology_types(data_list):
            malignant = sum(1 for d in data_list if d['pathology_type'] == 'malignant')
            benign = sum(1 for d in data_list if d['pathology_type'] == 'benign')
            uncertain = sum(1 for d in data_list if d['pathology_type'] == 'uncertain')
            avg_conf = sum(d['confidence'] for d in data_list) / len(data_list) if data_list else 0
            return malignant, benign, uncertain, avg_conf
        
        red_mal, red_ben, red_unc, red_conf = count_pathology_types(all_red_data)
        blue_mal, blue_ben, blue_unc, blue_conf = count_pathology_types(all_blue_data)
        green_mal, green_ben, green_unc, green_conf = count_pathology_types(all_green_data)
        
        _is_en = lang == 'en'
        
        logger.info(f"📊 ROI统计（全部传入Phase5）: 红框 {len(all_red_data)} 个 | 蓝框 {len(all_blue_data)} 个 | 绿框 {len(all_green_data)} 个")
        
        # ⭐ 构建路径发现总结 — 使用完整 reasoning + evidence（Gemini 100万 token 上下文）
        path_summaries = []
        for red_node in agent_state['tree_nodes']:
            red_id = red_node.get('id', red_node.get('roi_id', 'R?'))
            red_text = red_node['analysis'].get('reasoning', '') or red_node['analysis'].get('findings', 'No findings' if _is_en else '无发现')
            _r_label = f"Red Box {red_id} (10×)" if _is_en else f"红框 {red_id} (10×)"
            summary_parts = [f"🔴 {_r_label}:\n{red_text}"]
            
            for blue_node in red_node.get('children', []):
                blue_id = blue_node.get('id', blue_node.get('roi_id', 'B?'))
                blue_text = blue_node['analysis'].get('reasoning', '') or blue_node['analysis'].get('findings', '')
                _b_label = f"Blue Box {blue_id} (40×)" if _is_en else f"蓝框 {blue_id} (40×)"
                summary_parts.append(f"  └ 🔵 {_b_label}:\n  {blue_text}")
                
                for green_node in blue_node.get('children', []):
                    green_id = green_node.get('id', green_node.get('roi_id', 'G?'))
                    green_analysis = green_node['analysis']
                    green_text = green_analysis.get('reasoning', '') or green_analysis.get('findings', '')
                    conf = green_analysis.get('confidence', '?')
                    
                    evidence = green_analysis.get('evidence', {})
                    evidence_parts = []
                    for key in ['Q1_evidence', 'Q2_evidence', 'Q3_evidence', 'Q4_evidence', 'Q5_evidence', 'Q6_evidence', 'Q7_evidence', 'Q8_evidence']:
                        val = evidence.get(key, '')
                        if val:
                            evidence_parts.append(f"      - {key}: {val}")
                    
                    suggestion = green_analysis.get('diagnosis_suggestion', '')
                    
                    _g_label = f"Green Box {green_id} (160×)" if _is_en else f"绿框 {green_id} (160×)"
                    _conf_label = "confidence" if _is_en else "置信度"
                    green_block = f"    └ 🟢 {_g_label} [{_conf_label}: {conf}]:\n    {green_text}"
                    if evidence_parts:
                        _ev_label = "Evidence" if _is_en else "证据"
                        green_block += f"\n    [{_ev_label}]:\n" + "\n".join(evidence_parts)
                    if suggestion:
                        _imp_label = "Diagnostic Impression" if _is_en else "诊断印象"
                        green_block += f"\n    [{_imp_label}]: {suggestion}"
                    
                    summary_parts.append(green_block)
            
            path_summaries.append("\n".join(summary_parts))
        
        all_summaries = "\n\n".join(path_summaries)
        
        # ⭐⭐⭐ 注入全局缩略图分析（Orchestrator 的全局架构观察）
        # 缩略图是唯一能看到全切片架构的视角，对 Q3（浸润深度）等宏观判断至关重要
        global_analysis_text = agent_state.get('global_analysis', '')
        if isinstance(global_analysis_text, dict):
            global_analysis_text = global_analysis_text.get('reasoning', '') or global_analysis_text.get('analysis', '')
        elif isinstance(global_analysis_text, list):
            global_analysis_text = '\n'.join([str(item) for item in global_analysis_text])
        if global_analysis_text:
            _ga_label = "Thumbnail Global Analysis (Orchestrator — full slide overview)" if _is_en else "缩略图全局分析（Orchestrator — 全切片架构观察）"
            all_summaries = f"🔭 **{_ga_label}**:\n{global_analysis_text}\n\n{'─' * 60}\n\n{all_summaries}"
        
        # Synthesis uses the raw structured Memory tree (coordinates/images
        # stripped).  Do not pre-aggregate by counting or majority vote;
        # the summary agent resolves conflicts.  Fall back to narrative if this fails.
        try:
            import json as _json_v2
            _mtree_v2, _mpool_v2 = build_memory_tree(agent_state)
            _mem_v2 = strip_for_synthesis(_mtree_v2, drop_keys=("rect", "image_path", "filename"))
            if _is_en:
                _reason_rules = (
                    "⚠️ Reasoning & conflict-resolution rules (this is NOT a summation of slide-level outputs — "
                    "you must actively integrate and reconcile evidence across ROIs):\n"
                    "1. Prioritize each node's structured evidence (Q1-Q8) and q_confidence; treat reason as background.\n"
                    "2. When ROIs conflict on the same dimension, resolve by reasoning — weigh by q_confidence, "
                    "completeness of the evidence chain (higher-magnification confirmation), and specific morphological "
                    "counter-evidence; do NOT decide by simple majority/voting.\n"
                    "3. Focal findings (LVSI / special differentiation): a single high-confidence (q_confidence>=3) "
                    "positive path MUST be reflected and must NOT be diluted or overturned by many negative paths, "
                    "unless a negative path provides specific morphological refutation of that exact finding.\n")
                _treehdr = "[Structured Memory Tree — raw multi-scale, traceable, per-ROI evidence (conflicts preserved)]"
            else:
                _reason_rules = (
                    "⚠️ 推理与冲突裁决规则（这不是对切片级输出的简单相加——你必须主动对各 ROI 证据进行整合与冲突消解）：\n"
                    "1. 优先依据每个节点的结构化 evidence(Q1–Q8)与 q_confidence 作答；reason 仅作背景参考。\n"
                    "2. 当不同 ROI 在同一维度结论冲突时，须以推理裁决——按 q_confidence、证据链完整度（是否有更高倍确认）"
                    "与具体形态学反证来权衡，不得用简单多数票/计数决定。\n"
                    "3. 局灶发现(LVSI/特殊分化)：单条高置信(q_confidence≥3)阳性路径必须被体现，"
                    "不得被大量阴性路径稀释或否决，除非某阴性路径对该具体发现提供了针对性的形态学反证。\n")
                _treehdr = "【结构化记忆树 —— 各 ROI 原始、多尺度、可追溯证据(保留相互冲突,未做任何预聚合)】"
            all_summaries = (f"{_reason_rules}\n{'─' * 60}\n{_treehdr}\n"
                             + _json_v2.dumps(_mem_v2, ensure_ascii=False, indent=1))
            logger.info(f"🧠 [V2] 汇总证据源=Memory树(原始,无预聚合)+冲突裁决规则 (证据 {len(all_summaries)//1024}KB)")
        except Exception as _e_v2:
            logger.warning(f"[V2] Memory 证据源构建失败, 回退旧叙述式 all_summaries: {_e_v2}")
        
        # ⭐⭐⭐ MCP Protocol: Plan阶段 - LLM自主决定是否需要工具
        # 使用标准MCP接口实现 Plan→Action→Reason→Meta 工作流
        organ = agent_state.get('organ', ORGAN_DEFAULT)
        if _is_en:
            _organ_map = {
                '乳腺标本': 'Breast specimen', '乳腺': 'Breast',
                '子宫内膜': 'Endometrium', '子宫': 'Uterus', '宫颈': 'Cervix',
                '卵巢': 'Ovary', '输卵管': 'Fallopian tube', '子宫体': 'Uterine body',
                '子宫颈': 'Uterine cervix',
            }
            organ = _organ_map.get(organ, organ)
        
        if lang == 'en':
            # ⭐ English single-WSI report (Q1-Q8 format)
            report_prompt = f"""You are a senior pathologist writing a final pathology diagnostic report.

You have completed multi-point sampling of the entire slide. Below are the findings summary from each analysis path:

{all_summaries}

---

📊 **AI Analysis Summary**:
- 10× macro: {total_red} red-box regions
- 40× meso:  {total_blue} blue-box regions
- 160× micro: {total_green} green-box regions

---

**[Reason Phase — Synthesis]**

Please integrate evidence from all observation paths and produce a final pathology diagnostic report.

Notes:
- Base diagnosis primarily on morphological features
- Each conclusion must be traceable to a specific path and ROI
- If tool-call results are available, cite them as supporting evidence
- **Finding-type–specific evidence integration:**
  · Some findings (lymphovascular invasion, perineural invasion, and other focal lesions) may appear in only a few paths. When a sub-agent reports a positive finding with confidence ≥3, do NOT downgrade its weight merely because other paths did not see that lesion — a negative path only proves "not seen in that area" and cannot negate a positive finding.
  · GRADE / STAGE / DEGREE-OF-DIFFERENTIATION are assessed on the DOMINANT/representative pattern across the slide, per their standard definitions (e.g. proportion of higher-grade architecture, representative depth of invasion). A higher grade/stage therefore requires the higher-grade component to be substantial and representative, not a single isolated focus.
  · When paths conflict on the SAME dimension, reconcile by weighing confidence and higher-magnification confirmation against specific morphological counter-evidence — never by simple majority across paths.
- **Consistency**: Keep your answers internally consistent — e.g. the histologic grade must agree with the differentiation/architecture you describe, and any "present" conclusion for a focal lesion must be supported by at least one positive path. When a question provides candidate options, your final answer MUST be exactly one of the given options.

Output strictly in the following format:

---
### Pathology Diagnostic Report

Answer each question carefully, stating your reasoning and the source path:

If a question lists candidate options, you MUST pick the single best-matching option as your final answer; for open-ended questions, give a concise conclusion. For each question, first write "Answer: <conclusion / chosen option verbatim>", then "Evidence: <morphological basis + source path>".

{CLINICAL_QUESTIONS_EN}

**Diagnostic Confidence & Limitations**
* **Confidence**: (High / Medium / Low)
* **Notes**: (If any area is ambiguous or uncertain, please explain)
---

**Begin report generation:**
"""
        else:
            # ⭐ 单张 WSI 任务：Q1-Q8 格式
            report_prompt = f"""你是一位资深病理学专家，正在撰写最终的病理诊断报告。

现在，你已经完成了对整张切片的多点采样观察。以下是各条路径的发现总结：

{all_summaries}

---

📊 **AI分析汇总统计**：
- 10x宏观: {total_red}个红框区域
- 40x中观: {total_blue}个蓝框区域
- 160x微观: {total_green}个绿框区域

---

**【Reason阶段 - 综合推理】**

请综合以上所有观察路径的证据，生成一份最终病理诊断报告。

注意：
- 主要基于形态学特征进行描述和诊断
- 每个结论需追溯到具体的路径和ROI
- 如有工具调用结果，可适当引用支持诊断

请严格按照以下格式输出：

---
### 病理诊断报告

请逐一回答**下列每一个问题**，并严格遵守以下全局作答规则：

> **全局作答规则：**
> 1. 所有结论必须附**形态学依据**，不得只给结论不解释。
> 2. 所有证据必须**可溯源**：标注来自哪条分析路径（如 R3-B6-G7）。
> 3. 证据不足时可输出"无法判断"，但**仍须基于现有证据给出最可能的答案**。
> 4. **若问题附带候选选项（A./B./C./D. 或括号内列出），必须从给定选项中选择最符合的一项作为最终答案**，并简述理由；若为开放式问题（无选项），直接给出简明结论。
> 5. 每题作答格式：先写 **答案：<结论/所选选项原文>**，再写"依据：<形态学依据 + 路径>"。

---

{CLINICAL_QUESTIONS}

---

**诊断置信度与局限性**
* **置信度**：（高 / 中 / 低）
* **备注**：（如某区域存在歧义或不确定，请说明）

---

**开始生成报告：**
"""
        
        # 调用AI生成报告
        logger.info(f"\n{'='*80}")
        logger.info("📝 正在调用AI生成最终病理报告...")
        logger.info(f"{'='*80}")
        from agent.gpt_client import call_gpt_vision
        
        # 由于是纯文本，不需要图像
        final_report_text = call_gpt_vision(report_prompt, None)
        
        logger.info(f"\n{'='*80}")
        logger.info(f"✅ 最终病理报告生成完成！")
        logger.info(f"{'='*80}")
        logger.info(final_report_text)
        logger.info(f"{'='*80}\n")
        
        # 发送最终报告到前端
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'assistant',
                'content': f"✅ {'Final Pathology Report:' if lang == 'en' else '最终病理报告：'}\n\n{final_report_text}",
                'metadata': {
                    'message_type': 'final_report',
                    'report': final_report_text
                }
            }, to=room)
        
        # ⭐ V2 统一 Memory: 从全树(缩略图→红→蓝→绿)构建层级记忆 + 派生证据池(含红框)
        evidence_chain_text = ""
        evidence_aggregation = {}
        memory_tree = None
        try:
            memory_tree, evidence_pool = build_memory_tree(agent_state)
        except Exception as e:
            logger.warning(f"[V2 Memory] 构建 Memory 树失败, 回退到 live 证据池: {e}")
            evidence_pool = agent_state.get('evidence_pool')
        if evidence_pool is not None and hasattr(evidence_pool, 'generate_chain'):
            try:
                evidence_chain_text = evidence_pool.generate_chain()
                evidence_aggregation = evidence_pool.aggregate()
                logger.info(f"📌 证据池统计: {evidence_aggregation.get('total_evidence', 0)} 条证据, 全局置信度: {evidence_aggregation.get('global_confidence', 0):.0%}")
            except Exception as e:
                logger.warning(f"证据链生成失败: {e}")
        
        # 保存报告到文件（目录名已包含文件信息，无需在文件名中重复）
        report_path = output_dir / "pathology_report.txt"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(f"病理报告 - {agent_state['filename']}\n")
            f.write("=" * 60 + "\n\n")
            f.write(final_report_text)
            f.write("\n\n" + "=" * 60 + "\n")
            f.write(f"分析统计:\n")
            f.write(f"- 10× 红框: {total_red}个\n")
            f.write(f"- 40× 蓝框: {total_blue}个\n")
            f.write(f"- 160× 绿框: {total_green}个\n")

        logger.info(f"💾 报告已保存至: {report_path}")
        
        # ⭐ V2: 证据链 / 聚合结果 落盘为独立产物 (可追溯证据链, 供审计与复核)
        try:
            import json as _json
            if memory_tree is not None:
                # 全量(所见即所得, 含坐标/图路径): path_tree.json
                with open(output_dir / "path_tree.json", 'w', encoding='utf-8') as _mf:
                    _json.dump(memory_tree, _mf, ensure_ascii=False, indent=2)
                # 精简(去坐标/图路径, 喂最终汇总): memory.json
                with open(output_dir / "memory.json", 'w', encoding='utf-8') as _sf:
                    _json.dump(strip_for_synthesis(memory_tree), _sf, ensure_ascii=False, indent=2)
            if evidence_pool is not None and hasattr(evidence_pool, 'to_json'):
                with open(output_dir / "evidence_pool.json", 'w', encoding='utf-8') as _ef:
                    _ef.write(evidence_pool.to_json())
            if evidence_aggregation:
                with open(output_dir / "evidence_aggregation.json", 'w', encoding='utf-8') as _af:
                    _json.dump(evidence_aggregation, _af, ensure_ascii=False, indent=2)
            if evidence_chain_text:
                with open(output_dir / "evidence_chain.txt", 'w', encoding='utf-8') as _cf:
                    _cf.write(evidence_chain_text)
            logger.info(f"💾 [V2 Memory] 记忆产物已保存: path_tree.json(全量) / memory.json(喂汇总) / evidence_pool.json / evidence_aggregation.json / evidence_chain.txt")
        except Exception as _e:
            logger.warning(f"[V2 Memory] 记忆产物保存失败: {_e}")
        
        # ⭐ 生成HTML可视化报告（使用传入的 report_title）
        html_report_path = generate_html_visual_report(
            agent_state, file_id, output_dir, final_report_text, total_red, total_blue, total_green,
            report_title=report_title
        )
        logger.info(f"📄 HTML报告已生成: {html_report_path}")
        
        # 发送HTML报告路径到前端（使用 output_paths 中的正确路径）
        output_paths = agent_state.get('output_paths', {})
        html_path = output_paths.get('report_html', f'/outputs/{file_id}/auto/pathology_report.html')
        txt_path = output_paths.get('report_txt', f'/outputs/{file_id}/auto/pathology_report.txt')
        if socketio and room:
            socketio.emit('agent.report_ready', {
                'html_path': html_path,
                'txt_path': txt_path,
                'type': 'html'
            }, to=room)
        
        return {
            'report_text': final_report_text,
            'report_path': str(report_path),
            'html_report_path': str(html_report_path) if html_report_path else None,
            'statistics': {
                'red_frames': total_red,
                'blue_frames': total_blue,
                'green_frames': total_green
            },
            'tree_structure': agent_state['tree_nodes'],
            # Evidence aggregation results
            'evidence_aggregation': evidence_aggregation,
            'evidence_chain': evidence_chain_text
        }
        
    except Exception as e:
        logger.error(f"生成最终报告失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'report_text': f"报告生成失败: {str(e)}",
            'error': str(e)
        }


# ============== 规范化路径报告生成 ==============
# 
# 功能：将并行分析的混乱输出按路径整理为规范化文档
# 输出：
#   1. path_report.md - 按路径组织的规范化 Markdown 报告
#   2. analysis_tree.json - 完整树结构的 JSON 数据
#

def generate_standardized_path_report(agent_state, file_id, output_dir, socketio=None, room=None):
    """
    生成高度规范化的路径报告
    
    按路径整理所有分析结果，生成清晰的树状文档
    
    Args:
        agent_state: Agent状态字典（包含 tree_nodes）
        file_id: 文件ID
        output_dir: 输出目录
        socketio: SocketIO实例（可选，用于实时推送）
        room: WebSocket房间ID（可选）
    
    Returns:
        dict: 报告路径信息
    """
    try:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        tree_nodes = agent_state.get('tree_nodes', [])
        filename = agent_state.get('filename', '未知文件')
        organ = agent_state.get('organ', '未知器官')
        
        # 统计信息
        total_red = len(tree_nodes)
        total_blue = sum(len(node.get('children', [])) for node in tree_nodes)
        total_green = sum(
            len(blue.get('children', []))
            for red in tree_nodes
            for blue in red.get('children', [])
        )
        
        # 计算总路径数（每个绿框是一条完整路径的终点）
        total_paths = total_green if total_green > 0 else total_blue if total_blue > 0 else total_red
        
        logger.info(f"\n{'='*80}")
        logger.info(f"📝 生成规范化路径报告")
        logger.info(f"{'='*80}")
        logger.info(f"统计: {total_red}红框 → {total_blue}蓝框 → {total_green}绿框 = {total_paths}条完整路径")
        
        # ============== 构建规范化 Markdown 报告 ==============
        report_lines = []
        
        # 报告头部
        report_lines.append("# 🔬 MCP Loop 路径分析报告")
        report_lines.append("")
        report_lines.append("---")
        report_lines.append("")
        report_lines.append("## 📋 基本信息")
        report_lines.append("")
        report_lines.append(f"| 项目 | 内容 |")
        report_lines.append(f"|------|------|")
        report_lines.append(f"| **文件名** | {filename} |")
        report_lines.append(f"| **器官类型** | {organ} |")
        report_lines.append(f"| **分析时间** | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
        report_lines.append(f"| **文件ID** | {file_id} |")
        report_lines.append("")
        
        # 分析统计
        report_lines.append("## 📊 分析统计")
        report_lines.append("")
        report_lines.append("| 层级 | 倍率 | 数量 | 说明 |")
        report_lines.append("|------|------|------|------|")
        report_lines.append(f"| 🔴 红框 | 10× | {total_red} | 低倍镜全景筛查 |")
        report_lines.append(f"| 🔵 蓝框 | 40× | {total_blue} | 中倍镜区域聚焦 |")
        report_lines.append(f"| 🟢 绿框 | 160× | {total_green} | 高倍镜细胞分析 |")
        report_lines.append(f"| **完整路径** | - | **{total_paths}** | 从低倍到高倍的完整推理链 |")
        report_lines.append("")
        
        # 树状结构可视化
        report_lines.append("## 🌳 分析树结构")
        report_lines.append("")
        report_lines.append("```")
        for red_idx, red_node in enumerate(tree_nodes, 1):
            red_id = red_node.get('roi_id', f'R{red_idx}')
            report_lines.append(f"🔴 红框 {red_id} (10×)")
            
            blue_children = red_node.get('children', [])
            for blue_idx, blue_node in enumerate(blue_children, 1):
                blue_id = blue_node.get('roi_id', f'B{blue_idx}')
                is_last_blue = (blue_idx == len(blue_children))
                blue_prefix = "└──" if is_last_blue else "├──"
                report_lines.append(f"    {blue_prefix} 🔵 蓝框 {blue_id} (40×)")
                
                green_children = blue_node.get('children', [])
                for green_idx, green_node in enumerate(green_children, 1):
                    green_id = green_node.get('roi_id', f'G{green_idx}')
                    is_last_green = (green_idx == len(green_children))
                    green_prefix = "└──" if is_last_green else "├──"
                    indent = "        " if is_last_blue else "    │   "
                    report_lines.append(f"{indent}{green_prefix} 🟢 绿框 {green_id} (160×)")
        report_lines.append("```")
        report_lines.append("")
        
        # ============== 详细路径分析 ==============
        report_lines.append("---")
        report_lines.append("")
        report_lines.append("## 📍 详细路径分析")
        report_lines.append("")
        
        path_count = 0
        for red_idx, red_node in enumerate(tree_nodes, 1):
            red_id = red_node.get('roi_id', f'R{red_idx}')
            red_analysis = red_node.get('analysis', {})
            
            report_lines.append(f"### 🔴 红框 {red_id} (10× 低倍镜)")
            report_lines.append("")
            
            # Red-box analysis fields
            report_lines.append("#### 【Reason 阶段】观察与推理")
            report_lines.append("")
            red_findings = red_analysis.get('findings', '无记录')
            red_reasoning = red_analysis.get('reasoning', red_findings)
            # 格式化输出，保留换行
            for line in str(red_reasoning).split('\n'):
                if line.strip():
                    report_lines.append(f"> {line}")
            report_lines.append("")
            
            report_lines.append("#### 【Plan 阶段】决策")
            report_lines.append("")
            red_plan = red_analysis.get('plan_action') or '未记录'
            red_selected = red_analysis.get('selected_indices', [])
            report_lines.append(f"- **选择动作**: {red_plan}")
            red_runtime_action = red_analysis.get('runtime_action', '')
            if red_runtime_action and red_runtime_action != red_plan:
                report_lines.append(f"- **Runtime 有效动作**: {red_runtime_action}")
            if red_selected:
                report_lines.append(f"- **选择区域**: {red_selected}")
            report_lines.append("")
            
            # 置信度和病理类型
            red_confidence = red_analysis.get('confidence', 0)
            red_confidence_level = red_analysis.get('confidence_level', 2)
            red_pathology_type = red_analysis.get('pathology_type', 'uncertain')
            type_emoji = "⚠️" if red_pathology_type == 'malignant' else "✅" if red_pathology_type == 'benign' else "❓"
            level_desc = {0: '证据不足', 1: '不确定', 2: '可能', 3: '较确定', 4: '确定'}.get(red_confidence_level, '未知')
            report_lines.append("")
            
            # 蓝框子节点
            blue_children = red_node.get('children', [])
            if not blue_children:
                report_lines.append("*（无下钻蓝框）*")
                report_lines.append("")
                continue
            
            for blue_idx, blue_node in enumerate(blue_children, 1):
                blue_id = blue_node.get('roi_id', f'B{blue_idx}')
                blue_analysis = blue_node.get('analysis', {})
                
                report_lines.append(f"#### 🔵 蓝框 {blue_id} (40× 中倍镜)")
                report_lines.append("")
                
                # Blue-box analysis fields
                report_lines.append("##### 【Reason 阶段】观察与推理")
                report_lines.append("")
                blue_findings = blue_analysis.get('findings', '无记录')
                blue_reasoning = blue_analysis.get('reasoning', blue_findings)
                for line in str(blue_reasoning).split('\n'):
                    if line.strip():
                        report_lines.append(f"> {line}")
                report_lines.append("")
                
                report_lines.append("##### 【Plan 阶段】决策")
                report_lines.append("")
                blue_plan = blue_analysis.get('plan_action') or '未记录'
                blue_selected = blue_analysis.get('selected_indices', [])
                report_lines.append(f"- **选择动作**: {blue_plan}")
                blue_runtime_action = blue_analysis.get('runtime_action', '')
                if blue_runtime_action and blue_runtime_action != blue_plan:
                    report_lines.append(f"- **Runtime 有效动作**: {blue_runtime_action}")
                if blue_selected:
                    report_lines.append(f"- **选择区域**: {blue_selected}")
                report_lines.append("")
                
                # 置信度
                blue_confidence = blue_analysis.get('confidence', 0)
                blue_confidence_level = blue_analysis.get('confidence_level', 2)
                blue_pathology_type = blue_analysis.get('pathology_type', 'uncertain')
                type_emoji = "⚠️" if blue_pathology_type == 'malignant' else "✅" if blue_pathology_type == 'benign' else "❓"
                level_desc = {0: '证据不足', 1: '不确定', 2: '可能', 3: '较确定', 4: '确定'}.get(blue_confidence_level, '未知')
                report_lines.append("")
                
                # 绿框子节点
                green_children = blue_node.get('children', [])
                if not green_children:
                    report_lines.append("*（无下钻绿框）*")
                    report_lines.append("")
                    continue
                
                for green_idx, green_node in enumerate(green_children, 1):
                    path_count += 1
                    green_id = green_node.get('roi_id', f'G{green_idx}')
                    green_analysis = green_node.get('analysis', {})
                    
                    report_lines.append(f"##### 🟢 绿框 {green_id} (160× 高倍镜) - 路径 #{path_count}")
                    report_lines.append("")
                    
                    # Green-box analysis fields
                    report_lines.append("###### 【Reason 阶段】细胞学分析")
                    report_lines.append("")
                    green_findings = green_analysis.get('findings', '无记录')
                    green_reasoning = green_analysis.get('reasoning', green_findings)
                    for line in str(green_reasoning).split('\n'):
                        if line.strip():
                            report_lines.append(f"> {line}")
                    report_lines.append("")
                    
                    # 最终结论
                    report_lines.append("###### 【Action 阶段】诊断结论")
                    report_lines.append("")
                    green_confidence = green_analysis.get('confidence', 0)
                    green_confidence_level = green_analysis.get('confidence_level', 2)
                    green_pathology_type = green_analysis.get('pathology_type', 'uncertain')
                    green_key_features = green_analysis.get('key_features', [])
                    
                    type_emoji = "⚠️" if green_pathology_type == 'malignant' else "✅" if green_pathology_type == 'benign' else "❓"
                    level_desc = {0: '证据不足', 1: '不确定', 2: '可能', 3: '较确定', 4: '确定'}.get(green_confidence_level, '未知')
                    report_lines.append(f"- **最终置信度**: Level {green_confidence_level} ({level_desc})")
                    if green_key_features:
                        report_lines.append(f"- **关键特征**: {', '.join(green_key_features)}")
                    report_lines.append("")
                    
                    # 完整路径回顾
                    report_lines.append(f"###### 【Meta 阶段】路径 #{path_count} 完整链条")
                    report_lines.append("")
                    report_lines.append(f"```")
                    report_lines.append(f"🔴 {red_id} (10×) → 🔵 {blue_id} (40×) → 🟢 {green_id} (160×)")
                    report_lines.append(f"```")
                    report_lines.append("")
                
                report_lines.append("")
            
            report_lines.append("---")
            report_lines.append("")
        
        # 报告尾部
        report_lines.append("---")
        report_lines.append("")
        report_lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        report_lines.append("")
        report_lines.append("*本报告由 PathCopilot MCP Loop 系统自动生成，仅供参考，最终诊断请以专业病理医师意见为准。*")
        
        # 写入 Markdown 文件
        report_content = "\n".join(report_lines)
        report_path = output_dir / "path_report.md"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content)
        
        logger.info(f"✅ 规范化路径报告已生成: {report_path}")
        
        # ============== 保存完整树结构 JSON ==============
        tree_json_path = output_dir / "analysis_tree.json"
        
        # ⭐ 获取全局分析和缩略图路径（用于根节点显示）
        global_analysis = agent_state.get('global_analysis', '')
        global_thumbnail_path = agent_state.get('global_thumbnail_path', '')
        
        # ⭐ 调试日志：检查 global_analysis 是否正确获取
        logger.info(f"📋 [路径报告] global_analysis 长度: {len(global_analysis)}")
        if global_analysis:
            logger.info(f"📋 [路径报告] global_analysis 前100字符: {global_analysis[:100]}...")
        else:
            logger.warning(f"⚠️ [路径报告] global_analysis 为空！agent_state 键: {list(agent_state.keys())}")
        
        # ⭐ 构建缩略图URL（相对路径转API路径）
        thumbnail_url = ''
        if global_thumbnail_path:
            # 例如 debug_images/sample_name/debug_gpt_global_123.jpg -> /debug_images/sample_name/debug_gpt_global_123.jpg
            thumbnail_url = '/' + global_thumbnail_path.replace('\\', '/')
            logger.info(f"📷 全局缩略图URL: {thumbnail_url}")
        
        # 构建简化的树结构用于导出
        export_tree = {
            'metadata': {
                'filename': filename,
                'organ': organ,
                'file_id': file_id,
                'analysis_time': datetime.now().isoformat(),
                'statistics': {
                    'total_red': total_red,
                    'total_blue': total_blue,
                    'total_green': total_green,
                    'total_paths': total_paths
                },
                # ⭐ 新增：全局分析和缩略图（用于树可视化根节点）
                'global_analysis': global_analysis,  # Phase 1 缩略图分析【Reason】
                'thumbnail_url': thumbnail_url,      # 带切割框的缩略图
                'wsi_summary': agent_state.get('_wsi_summary_phase5', ''),  # Phase 5 结构化证据摘要
            },
            'tree_nodes': []
        }
        
        # ⭐ 构建 roi_id → rect 映射表（从 analyzed_rois / candidate_rois 获取红框坐标）
        _roi_rect_map = {}
        for roi in agent_state.get('analyzed_rois', []) + agent_state.get('candidate_rois', []) + agent_state.get('all_candidate_rois', []):
            rid = roi.get('id', roi.get('roi_id', ''))
            if rid and roi.get('rect'):
                _roi_rect_map[rid] = roi['rect']
        
        for red_node in tree_nodes:
            # ⭐ Phase 1.1: 添加 image_path 和 rect 用于前端树可视化
            red_image_path = red_node.get('image_path') or red_node.get('analysis', {}).get('image_path')
            red_roi_id = red_node.get('roi_id') or red_node.get('id', '')
            red_rect = red_node.get('rect') or red_node.get('analysis', {}).get('rect') or _roi_rect_map.get(red_roi_id)
            
            export_red = {
                'roi_id': red_node.get('roi_id'),
                'level': 'red',
                'zoom': '10×',
                'image_path': red_image_path,  # ⭐ 新增：图片路径
                'rect': red_rect,  # ⭐ 新增：区域坐标
                'analysis': {
                    'findings': red_node.get('analysis', {}).get('findings', ''),
                    'reasoning': red_node.get('analysis', {}).get('reasoning', ''),
                    'plan_action': red_node.get('analysis', {}).get('plan_action', ''),
                    'selected_indices': red_node.get('analysis', {}).get('selected_indices', []),
                    'runtime_action': red_node.get('analysis', {}).get('runtime_action', ''),
                    'mcp_tool_trace': red_node.get('analysis', {}).get('mcp_tool_trace', []),
                    'confidence': red_node.get('analysis', {}).get('confidence', 0),
                    'pathology_type': red_node.get('analysis', {}).get('pathology_type', 'uncertain')
                },
                'children': []
            }
            
            for blue_node in red_node.get('children', []):
                # ⭐ Phase 1.1: 添加 image_path 和 rect
                blue_image_path = blue_node.get('image_path') or blue_node.get('analysis', {}).get('image_path')
                blue_rect = blue_node.get('rect') or blue_node.get('analysis', {}).get('rect')
                
                export_blue = {
                    'roi_id': blue_node.get('roi_id'),
                    'level': 'blue',
                    'zoom': '40×',
                    'image_path': blue_image_path,  # ⭐ 新增：图片路径
                    'rect': blue_rect,  # ⭐ 新增：区域坐标
                    'analysis': {
                        'findings': blue_node.get('analysis', {}).get('findings', ''),
                        'reasoning': blue_node.get('analysis', {}).get('reasoning', ''),
                        'plan_action': blue_node.get('analysis', {}).get('plan_action', ''),
                        'selected_indices': blue_node.get('analysis', {}).get('selected_indices', []),
                        'runtime_action': blue_node.get('analysis', {}).get('runtime_action', ''),
                        'mcp_tool_trace': blue_node.get('analysis', {}).get('mcp_tool_trace', []),
                        'confidence': blue_node.get('analysis', {}).get('confidence', 0),
                        'pathology_type': blue_node.get('analysis', {}).get('pathology_type', 'uncertain')
                    },
                    'children': []
                }
                
                for green_node in blue_node.get('children', []):
                    # ⭐ Phase 1.1: 添加 image_path 和 rect
                    green_image_path = green_node.get('image_path') or green_node.get('analysis', {}).get('image_path')
                    green_rect = green_node.get('rect') or green_node.get('analysis', {}).get('rect')
                    
                    export_green = {
                        'roi_id': green_node.get('roi_id'),
                        'level': 'green',
                        'zoom': '160×',
                        'image_path': green_image_path,  # ⭐ 新增：图片路径
                        'rect': green_rect,  # ⭐ 新增：区域坐标
                        'analysis': {
                            'findings': green_node.get('analysis', {}).get('findings', ''),
                            'reasoning': green_node.get('analysis', {}).get('reasoning', ''),
                            'plan_action': green_node.get('analysis', {}).get('plan_action', ''),
                            'confidence': green_node.get('analysis', {}).get('confidence', 0),
                            'pathology_type': green_node.get('analysis', {}).get('pathology_type', 'uncertain'),
                            'key_features': green_node.get('analysis', {}).get('key_features', [])
                        }
                    }
                    export_blue['children'].append(export_green)
                
                export_red['children'].append(export_blue)
            
            export_tree['tree_nodes'].append(export_red)
        
        with open(tree_json_path, 'w', encoding='utf-8') as f:
            json.dump(export_tree, f, ensure_ascii=False, indent=2)
        
        logger.info(f"✅ 分析树结构已导出: {tree_json_path}")
        logger.info(f"   └─ metadata.global_analysis 长度: {len(export_tree['metadata'].get('global_analysis', ''))}")
        logger.info(f"   └─ metadata.thumbnail_url: {export_tree['metadata'].get('thumbnail_url', '无')}")
        
        # 不再发送到聊天框（避免冗余消息）
        
        return {
            'success': True,
            'path_report': str(report_path),
            'tree_json': str(tree_json_path),
            'statistics': {
                'total_red': total_red,
                'total_blue': total_blue,
                'total_green': total_green,
                'total_paths': total_paths
            }
        }
        
    except Exception as e:
        logger.error(f"生成规范化路径报告失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'error': str(e)
        }


def run_agent(socketio, room, file_id: str, max_steps: int = 12, strategy: str = "default", mode: str = "ai",
              lang: str = 'zh') -> dict:
    """
    运行WSI Agent循环 - 模拟病理医生工作流
    步骤0：缩略图级别ROI识别
    步骤1-N：多尺度ROI分析循环
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID
        file_id: WSI文件ID
        max_steps: 最大步数
        strategy: 分析策略
        mode: retained for call-site compatibility; forced to AI-only
        lang: 语言设置 ('zh' 中文, 'en' 英文)
    """
    try:
        mode = 'ai'
        logger.info(f"启动病理Agent (纯AI模式): file_id={file_id}, room={room}, max_steps={max_steps}")
        
        # ⭐ 启动 LLM 日志会话
        from .llm_logger import start_log_session, end_log_session
        from .gpt_client import set_current_session, clear_current_session
        
        log_metadata = {
            "mode": mode,
            "strategy": strategy,
            "max_steps": max_steps,
            "room": room
        }
        start_log_session(file_id, log_metadata)
        set_current_session(file_id)  # 设置当前会话，让 gpt_client 自动记录日志
        logger.info(f"📝 LLM日志会话已启动: {file_id}")
        
        # ⭐ 每样本独立分析日志：记录完整终端输出，便于用户溯源
        _sample_log_handler = None
        try:
            import logging as _logging
            _sample_log_dir = Path('logs')
            _sample_log_dir.mkdir(exist_ok=True)
            _wsi_name_safe = file_id[:50].replace('/', '_')
            _sample_log_path = _sample_log_dir / f"{_wsi_name_safe}_{file_id[:8]}_analysis.log"
            _sample_log_handler = _logging.FileHandler(str(_sample_log_path), encoding='utf-8')
            _sample_log_handler.setLevel(_logging.INFO)
            _sample_log_handler.setFormatter(_logging.Formatter(
                '%(asctime)s [%(name)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
            ))
            _logging.getLogger().addHandler(_sample_log_handler)
            logger.info(f"📝 样本分析日志已启动: {_sample_log_path}")
        except Exception as _log_err:
            logger.warning(f"⚠️ 样本日志启动失败: {_log_err}")
        
        # 重置停止标志
        agent_stop_flags[room] = False
        
        # 获取WSI文件路径（需要先获取文件名用于命名输出目录）
        file_path = get_wsi_file_path(file_id)
        if not file_path:
            emit_error(socketio, room, f"WSI文件不存在: {file_id}")
            return
        
        # 提取原始文件名（从 uploaded_files 获取用户上传时的原始名称）
        try:
            from app import uploaded_files
            original_filename = uploaded_files.get(file_id, {}).get('filename', '')
        except:
            original_filename = ''
        # 如果拿不到原始文件名，用磁盘文件名兜底
        filename = original_filename if original_filename else os.path.basename(file_path)
        
        # ⭐⭐⭐ 创建输出目录（使用文件名+时间戳命名，更直观可读）⭐⭐⭐
        paths = get_output_paths(file_id, mode, filename=filename)
        output_dir = paths['output_dir']
        steps_dir = paths['steps_dir']
        steps_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 输出目录: {output_dir} (目录名: {paths['dir_name']}, 模式: {paths['mode_subdir']})")
        
        # 打开WSI文件
        slide = openslide.OpenSlide(file_path)
        dimensions = safe_get_slide_metadata(slide)['dimensions']

        # Agent状态初始化
        agent_state = {
            'current_step': 0,
            'file_id': file_id,  # ⭐ 文件ID，用于输出路径等
            'filename': filename,
            'organ': ORGAN_DEFAULT,  # ⭐ 标本部位; MultiPathQA为泛癌/多器官,由 PATHCOPILOT_ORGAN 设为中立词
            'dimensions': dimensions,
            'candidate_rois': [],
            'analyzed_rois': [],
            'pathology_findings': [],
            'tool_results': [],
            'confidence_scores': [],
            'next_action_suggestions': [],
            'tree_nodes': [],  # 存储所有树结构
            'all_overlays': [],  # 累积所有已显示的overlays（红蓝绿框）
            'mode': 'ai',
            'room': room,
            'lang': lang,
            # ⭐ ROI计数器（用于简化命名: R1, R2, B1, B2, G1, G2）
            'r_roi_counter': 0,
            'b_roi_counter': 0,
            'g_roi_counter': 0,
            # Evidence pool, spatial index, and backtrack history.
            'evidence_pool': EvidencePool() if EvidencePool else [],  # 证据原子化存储
            'spatial_index': {},  # 空间搜索索引
            'spatial_index_lock': threading.Lock(),  # 并行红框的邻区证据读写保护
            'backtrack_history': [],  # 回溯历史记录
            # ⭐ 输出路径信息（用于分离存储）
            'output_paths': paths,
        }
        
        set_agent_state(room, agent_state)
        
        # === 步骤0：缩略图级别ROI识别 ===
        socketio.emit('agent.progress', {
            'type': 'progress',
            'step': 0,
            'total_steps': max_steps,
            'percentage': 0,
            'status': 'thumbnail_analysis',
            'message': _msg('analyzing_thumbnail', agent_state.get('lang', 'zh'))
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送消息
        
        # 获取缩略图并进行GPT分析
        candidate_rois = get_candidate_rois_from_thumbnail(socketio, room, slide, file_id, steps_dir, wsi_name=filename, agent_state=agent_state)
        if not candidate_rois:
            emit_error(socketio, room, "缩略图ROI识别失败")
            slide.close()
            return
        
        # ⭐ 为选中的红框分配简单ID (R1, R2, R3...)
        assign_simple_roi_ids(candidate_rois, 'R', agent_state)
        logger.info(f"🔴 红框ID分配: {[roi['id'] for roi in candidate_rois]}")
        
        # 发送简化ID后的红框列表 + 全局分析理由
        # 注意：必须在 assign_simple_roi_ids 之后发射，确保 ID 与后续 trace 事件一致
        socketio.emit('agent.trace', {
            'type': 'trace',
            'step': 0,
            'selected_red_rois': [{'id': roi.get('id'), 'rect': roi.get('rect', {})} 
                                   for roi in candidate_rois],
            'global_reasoning': agent_state.get('global_analysis', '')  # AI 全局选框理由
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送消息
        
        # ⭐ 发送进度更新：缩略图分析完成
        socketio.emit('agent.progress', {
            'type': 'progress',
            'percentage': 5,
            'status': 'thumbnail_done',
            'message': _msg('thumbnail_done', agent_state.get('lang', 'zh'), n=len(candidate_rois))
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送消息
        
        # Build spatial index for neighbor search.
        build_spatial_index(candidate_rois, agent_state)
            
        agent_state['candidate_rois'] = candidate_rois
        
        # ⭐ 初始化全局 overlays（10 个红框）- 累积显示所有框
        agent_state['all_overlays'] = [create_roi_overlay(roi) for roi in candidate_rois]
        
        # ⭐ 初始化线程锁（用于保护 all_overlays 的并发访问）
        agent_state['overlays_lock'] = threading.Lock()
        
        logger.info(f"✅ 从缩略图识别出 {len(candidate_rois)} 个10X红框，已初始化全局overlays")
        logger.info(f"📊 分析架构: 红框 → 每框16蓝框候选(AI选) → 每蓝16绿框候选(AI选)")
        
        # === 步骤1-N：真正的并行分析所有ROI ===
        logger.info(f"🚀 开始并行分析 {len(candidate_rois)} 个红框...")
        
        import concurrent.futures
        
        # ⭐ 发送初始进度：分析开始
        socketio.emit('agent.progress', {
            'type': 'progress',
            'percentage': 5,
            'status': 'analysis_start',
            'message': _msg('main_path_progress', agent_state.get('lang', 'zh'), done=0, total=len(candidate_rois))
        }, to=room)
        
        # 定义单个红框分析函数（用于并行执行）
        def analyze_single_red_roi(roi_index, current_roi):
            try:
                step = roi_index + 1
                logger.info(f"📍 [线程{roi_index+1}] 开始分析红框: {current_roi['id']}")
                
                # 注意：红框是并行分析的，这里不发送进度（会导致瞬间跳到6/6）
                # 进度在红框完成时由 concurrent.futures.as_completed 回调更新
                
                # 简化：不发送"开始分析"消息，只在完成时发送结果
                
                # ⭐ 线程安全修复：NDPI 使用 JPEG 压缩，底层 libjpeg 不是线程安全的。
                # 多线程共享同一 slide 对象会导致 "Corrupt JPEG data" 错误。
                # 每个线程独立打开自己的 OpenSlide 实例，用完即关。
                _thread_slide = openslide.OpenSlide(str(file_path))
                try:
                    # 多尺度ROI分析（每个红框内部是并行的：同时分析红/蓝/绿框）
                    roi_analysis_result = execute_multiscale_roi_analysis(
                        socketio, room, _thread_slide, current_roi, step, file_id, steps_dir, agent_state
                    )
                finally:
                    _thread_slide.close()
                
                logger.info(f"✅ [线程{roi_index+1}] 完成红框 {current_roi['id']} 分析")
                
                if roi_analysis_result and 'finding' in roi_analysis_result:
                    socketio.emit('agent.message', {
                        'type': 'assistant',
                        'content': f"✅ {current_roi['id']} 分析完成\n\n{roi_analysis_result['finding']}",
                        'level': 'red',
                        'roi_id': current_roi['id'],
                        'is_result': True,  # 只在左侧显示
                        'metadata': {'roi_id': current_roi['id'], 'step': step}
                    }, to=room)
                
                return {
                    'roi': current_roi,
                    'result': roi_analysis_result,
                    'index': roi_index
                }
            except Exception as e:
                logger.error(f"❌ [线程{roi_index+1}] 红框分析失败: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return {
                    'roi': current_roi,
                    'result': None,
                    'index': roi_index,
                    'error': str(e)
                }
        
        # 使用线程池并行处理所有红框（不限并发数）
        max_workers = len(candidate_rois)
        logger.info(f"🔧 线程池配置: {max_workers} 个工作线程处理 {len(candidate_rois)} 个红框")
        
        all_results = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            # 提交所有任务
            futures = [
                executor.submit(analyze_single_red_roi, idx, roi)
                for idx, roi in enumerate(candidate_rois)
            ]
            
            # 收集结果（按完成顺序）
            completed_count = 0
            for future in concurrent.futures.as_completed(futures):
                # 检查停止标志
                if agent_stop_flags.get(room, False):
                    logger.info(f"Agent被用户停止: {room}")
                    executor.shutdown(wait=False)
                    emit_done(socketio, room, file_id, agent_state, status='stopped')
                    slide.close()
                    return
                
                completed_count += 1
                result_data = future.result()
                all_results.append(result_data)
                
                if result_data.get('result'):
                    roi_analysis_result = result_data['result']
                    current_roi = result_data['roi']
                    
                    # 更新Agent状态
                    agent_state['analyzed_rois'].append(current_roi)
                    agent_state['pathology_findings'].append(roi_analysis_result.get('finding', ''))
                    agent_state['tool_results'].append(roi_analysis_result.get('tools_results', []))
                    _roi_confidence = roi_analysis_result.get('confidence')
                    if isinstance(_roi_confidence, (int, float)):
                        agent_state['confidence_scores'].append(_roi_confidence)
                    agent_state['next_action_suggestions'].append(roi_analysis_result.get('next_action', ''))
                    
                    # 收集树结构
                    if 'tree_nodes' in roi_analysis_result and roi_analysis_result['tree_nodes']:
                        agent_state['tree_nodes'].extend(roi_analysis_result['tree_nodes'])
                
                # 发送进度更新（统一格式）
                # 统一显示主路径进度
                progress_msg = _msg('main_path_progress', agent_state.get('lang', 'zh'), done=completed_count, total=len(candidate_rois))
                
                socketio.emit('agent.progress', {
                    'type': 'progress',
                    'step': completed_count,
                    'total_steps': len(candidate_rois),
                    'percentage': int((completed_count / len(candidate_rois)) * 90),
                    'status': 'roi_analysis',
                    'message': progress_msg
                }, to=room)
        
        logger.info(f"✅ 所有 {len(candidate_rois)} 个红框并行分析完成！")
        
        logger.info(f"✅ {len(all_results)} 个红框分析结果已在并行循环中处理完毕")
        
        slide.close()
        
        # ⭐ 清理父ROI缓存
        clear_roi_cache()
        
        # 完成分析
        if not agent_stop_flags.get(room, False):
            # 生成最终病理报告
            logger.info("="*60)
            logger.info("开始生成最终病理报告...")
            
            # ⭐ 发送进度更新：正在生成最终报告
            socketio.emit('agent.progress', {
                'type': 'progress',
                'percentage': 95,
                'status': 'generating_report',
                'message': _msg('generating_report', agent_state.get('lang', 'zh'))
            }, to=room)
            
            final_report = generate_final_pathology_report(agent_state, file_id, output_dir, socketio, room)
            agent_state['final_report'] = final_report
            stats = final_report.get('statistics', {})
            logger.info(f"✅ 病理报告生成完成！")
            logger.info(f"📈 分析统计: {stats.get('red_frames', 0)}红框 + {stats.get('blue_frames', 0)}蓝框 + {stats.get('green_frames', 0)}绿框")
            
            # Generate the path analysis report.
            logger.info("📝 开始生成规范化路径报告...")
            try:
                path_report_result = generate_standardized_path_report(
                    agent_state, file_id, output_dir, socketio, room
                )
                if path_report_result.get('success'):
                    logger.info(f"✅ 规范化路径报告已生成")
                    agent_state['path_report'] = path_report_result.get('path_report')
                    agent_state['tree_json'] = path_report_result.get('tree_json')
                else:
                    logger.warning(f"规范化路径报告生成失败: {path_report_result.get('error')}")
            except Exception as e:
                logger.warning(f"规范化路径报告生成失败（不影响主流程）: {e}")
            
            logger.info("="*60)
            
            emit_done(socketio, room, file_id, agent_state, status='completed')
            
    except Exception as e:
        logger.error(f"Agent执行失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        emit_error(socketio, room, f"Agent执行失败: {str(e)}")
    finally:
        # 清理停止标志
        if room in agent_stop_flags:
            del agent_stop_flags[room]
        clear_agent_state(room)
        
        # ⭐ 结束 LLM 日志会话
        try:
            summary = {
                "total_rois": len(agent_state.get('analyzed_rois', [])) if 'agent_state' in dir() else 0,
                "mode": mode,
                "strategy": strategy
            }
            end_log_session(file_id, summary)
            clear_current_session()
            logger.info(f"📝 LLM日志会话已结束: {file_id}")
        except Exception as log_error:
            logger.warning(f"结束日志会话失败: {log_error}")
        
        # ⭐ 关闭样本独立日志
        try:
            if _sample_log_handler:
                logger.info(f"📝 样本分析日志已保存完毕")
                import logging as _logging
                _logging.getLogger().removeHandler(_sample_log_handler)
                _sample_log_handler.close()
        except Exception:
            pass



def get_wsi_file_path(file_id):
    """
    获取WSI文件路径
    """
    try:
        from app import uploaded_files
        
        if file_id in uploaded_files:
            file_path = uploaded_files[file_id]['file_path']
            if os.path.exists(file_path):
                return file_path
        
        # 尝试从uploads目录按 file_id 前缀精确匹配
        uploads_dir = Path("uploads")
        if uploads_dir.exists():
            for file_path in uploads_dir.glob(f"{file_id}.*"):
                if file_path.is_file() and file_path.suffix.lower() in ['.svs', '.tiff', '.tif', '.ndpi']:
                    return str(file_path)
        
        return None
    except Exception as e:
        logger.error(f"获取文件路径失败: {e}")
        return None

def sample_uniform_centers_in_tissue(tissue_mask, num_centers):
    """
    在组织掩膜内均匀采样指定数量的中心点
    
    Args:
        tissue_mask: 组织区域的二值掩膜
        num_centers: 需要采样的中心点数量
    
    Returns:
        centers: [(x, y), ...] 采样的中心点坐标列表
    """
    # 获取所有组织区域的像素坐标
    tissue_coords = np.argwhere(tissue_mask)  # 返回 [[y, x], ...]
    
    if len(tissue_coords) == 0:
        return []
    
    # 转换为 (x, y) 格式
    tissue_points = tissue_coords[:, [1, 0]]  # [[x, y], ...]
    
    # 使用简单的均匀采样：将组织区域划分为网格，从每个网格中选择中心
    y_coords = tissue_coords[:, 0]
    x_coords = tissue_coords[:, 1]
    
    y_min, y_max = y_coords.min(), y_coords.max()
    x_min, x_max = x_coords.min(), x_coords.max()
    
    # 根据需要的中心点数量，计算网格布局
    # 对于6个红框，优先使用2x3或3x2布局，确保均匀分布
    aspect_ratio = (x_max - x_min) / max(1, (y_max - y_min))
    
    if num_centers == 6:
        # 固定使用2x3或3x2布局
        if aspect_ratio > 1.2:  # 宽型，使用3x2
            cols, rows = 3, 2
        else:  # 高型或正方形，使用2x3
            cols, rows = 2, 3
    elif aspect_ratio > 1.5:  # 宽型
        cols = int(np.ceil(np.sqrt(num_centers * aspect_ratio)))
        rows = int(np.ceil(num_centers / cols))
    elif aspect_ratio < 0.67:  # 高型
        rows = int(np.ceil(np.sqrt(num_centers / aspect_ratio)))
        cols = int(np.ceil(num_centers / rows))
    else:  # 接近正方形
        cols = rows = int(np.ceil(np.sqrt(num_centers)))
    
    # 在网格中采样中心点
    centers = []
    cell_width = (x_max - x_min) / cols
    cell_height = (y_max - y_min) / rows
    
    for row in range(rows):
        for col in range(cols):
            if len(centers) >= num_centers:
                break
            
            # 计算网格单元的中心
            center_x = int(x_min + (col + 0.5) * cell_width)
            center_y = int(y_min + (row + 0.5) * cell_height)
            
            # 检查该点是否在组织内
            if (0 <= center_y < tissue_mask.shape[0] and 
                0 <= center_x < tissue_mask.shape[1] and
                tissue_mask[center_y, center_x]):
                centers.append((center_x, center_y))
            else:
                # 如果网格中心不在组织内，在该网格区域内找最近的组织点
                cell_y_min = int(y_min + row * cell_height)
                cell_y_max = int(y_min + (row + 1) * cell_height)
                cell_x_min = int(x_min + col * cell_width)
                cell_x_max = int(x_min + (col + 1) * cell_width)
                
                # 在该网格内查找组织点
                cell_tissue = tissue_mask[cell_y_min:cell_y_max, cell_x_min:cell_x_max]
                if np.any(cell_tissue):
                    cell_coords = np.argwhere(cell_tissue)
                    # 选择最接近网格中心的点
                    rel_center_y = center_y - cell_y_min
                    rel_center_x = center_x - cell_x_min
                    distances = np.sqrt((cell_coords[:, 0] - rel_center_y)**2 + 
                                      (cell_coords[:, 1] - rel_center_x)**2)
                    closest_idx = np.argmin(distances)
                    actual_y = cell_y_min + cell_coords[closest_idx, 0]
                    actual_x = cell_x_min + cell_coords[closest_idx, 1]
                    centers.append((actual_x, actual_y))
        
        if len(centers) >= num_centers:
            break
    
    return centers[:num_centers]

def parse_mcp_selection(text, valid_indices=None):
    """Thin wrapper over parse_mcp_response. Returns indices only.

    B/C/D/E always yield []. Do not parse indices from this function
    and then re-detect the action with a second regex.
    """
    decision = parse_mcp_response(text, valid_indices=valid_indices)
    action = (decision.get('action') or '').upper()
    if action in _MCP_NON_DRILL_ACTIONS:
        return []
    return list(decision.get('selected_indices') or [])


def filter_rois_by_gpt_vision(
    slide,
    rois,
    thumbnail_size=(2000, 2000),
    max_select=None,
    parent_roi=None,
    wsi_name=None,
    return_analysis=False,
    agent_state=None,
    replanning_context=None,
):
    """
    使用AI-Vision从概览图中筛选出包含有效病理组织的网格
    
    ⭐ MCP关键函数：AI的回复同时包含【思考过程】（分析）和【选择结果】
    当 return_analysis=True 时，返回完整的AI分析（用于红框分析+蓝框选择合并）
    
    Args:
        slide: OpenSlide对象
        rois: 候选ROI列表（包含rect信息）
        thumbnail_size: 用于生成概览图的缩略图尺寸
        max_select: 最大选择数量
        parent_roi: 父ROI信息（用于生成局部上下文概览图），如果为None则使用全片缩略图
        wsi_name: WSI文件名（用于创建调试图片子目录）
        return_analysis: 是否返回完整的AI分析响应（MCP模式需要）
        agent_state: Agent状态
        
    Returns:
        当 return_analysis=False（默认）: list - 经过筛选后的ROI列表
        当 return_analysis=True: dict - {
            'selected_rois': 筛选后的ROI列表,
            'analysis': AI完整响应（包含思考过程）,
            'selection_indices': 选中的网格编号列表
        }
    """
    import time
    _filter_start_time = time.time()
    
    # ⭐ 提前初始化，避免异常发生在赋值前导致 UnboundLocalError
    persistent_image_path = None
    debug_path = None
    _slide_dims = None
    
    try:
        logger.info(f"⏱️ [计时] 进入 filter_rois_by_gpt_vision，候选ROI数量: {len(rois)}")
        
        # 线程安全地获取 slide 元数据（缓存后续复用）
        _meta = safe_get_slide_metadata(slide)
        _slide_dims = _meta['dimensions']
        _slide_level_count = _meta['level_count']
        _slide_level_dims = _meta['level_dimensions']
        _slide_level_ds = _meta['level_downsamples']
        
        logger.info(f"🔍 [Level调试] WSI尺寸: {_slide_dims}")
        logger.info(f"🔍 [Level调试] Level数量: {_slide_level_count}")
        logger.info(f"🔍 [Level调试] Level尺寸: {_slide_level_dims}")
        logger.info(f"🔍 [Level调试] Level倍率: {_slide_level_ds}")
        
        if parent_roi:
            # === 局部模式：使用父ROI图像（带缓存优化）===
            logger.info(f"使用局部上下文筛选 (父ROI: {parent_roi['id']})")
            
            # ⭐ 使用缓存系统获取父ROI图像
            parent_context = get_parent_roi_context(slide, parent_roi, target_size=2000)
            thumbnail = parent_context['image'].copy()  # ⭐ 复制图像，避免在缓存原图上绘制导致多次标注叠加
            
            x, y = parent_context['origin']
            w, h = parent_context['original_size']
            
            logger.info(f"父ROI最终图像大小: {thumbnail.size}")
            
            # 坐标映射函数：使用缓存中的缩放因子
            def map_rect(r):
                return map_coord_to_cached_image(r, parent_context)
                
        else:
            # === 全局模式：使用全片缩略图 ===
            logger.info("使用全局上下文筛选 (全片缩略图)")
            _thumb_start = time.time()
            
            # ⭐⭐⭐ 优化：尝试读取缓存的缩略图，避免重复生成 ⭐⭐⭐
            thumbnail = None
            # 从 wsi_name 或 agent_state 提取 file_id
            cache_file_id = None
            if agent_state:
                cache_file_id = agent_state.get('file_id')
            if not cache_file_id and wsi_name:
                # wsi_name 可能是 "xxx.ndpi" 或 "xxx.svs" 格式
                cache_file_id = Path(wsi_name).stem  # 去掉扩展名
            
            # 缩略图缓存统一放在 outputs/{file_id}/ 目录（与 grid_rois_cache.json 同级）
            if cache_file_id:
                # 尝试读取缓存的 2000×2000 缩略图
                cached_thumb_path = Path(f"outputs/{cache_file_id}/cached_thumbnail_2000.jpg")
                if cached_thumb_path.exists():
                    try:
                        thumbnail = Image.open(cached_thumb_path).convert('RGB')
                        logger.info(f"✅ [缓存] 使用缓存的缩略图: {cached_thumb_path}")
                    except Exception as e:
                        logger.warning(f"⚠️ 读取缓存缩略图失败: {e}，将重新生成")
                        thumbnail = None
            
            if thumbnail is None:
                # 没有缓存，重新生成
                with _openslide_lock:
                    thumbnail = slide.get_thumbnail(thumbnail_size).convert('RGB')
                
                # 保存缓存供下次使用
                if cache_file_id:
                    try:
                        cache_dir = Path(f"outputs/{cache_file_id}")
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        cached_thumb_path = cache_dir / "cached_thumbnail_2000.jpg"
                        thumbnail.save(cached_thumb_path, 'JPEG', quality=90)
                        logger.info(f"💾 [缓存] 已保存缩略图缓存: {cached_thumb_path}")
                    except Exception as e:
                        logger.warning(f"⚠️ 保存缩略图缓存失败: {e}")
            
            _thumb_elapsed = time.time() - _thumb_start
            logger.info(f"⏱️ [计时] 缩略图获取耗时: {_thumb_elapsed:.2f}秒")
            
            # 坐标映射函数
            scale_x = thumbnail.size[0] / _slide_dims[0]
            scale_y = thumbnail.size[1] / _slide_dims[1]
            
            def map_rect(r):
                return (
                    int(r['x'] * scale_x),
                    int(r['y'] * scale_y),
                    int(r['w'] * scale_x),
                    int(r['h'] * scale_y)
                )

        from PIL import ImageDraw
        draw = ImageDraw.Draw(thumbnail)
        
        # 加载字体 - ⭐ 自适应字体大小
        # 缩略图（无 parent_roi）用较小系数 ~1.0%，钻取图（有 parent_roi）用 ~1.8%
        _img_max_dim = max(thumbnail.size[0], thumbnail.size[1])
        if parent_roi:
            _font_size = max(20, int(_img_max_dim * 0.018))  # 钻取图：大字体，LLM 能看清
        else:
            _font_size = max(18, int(_img_max_dim * 0.010))  # 缩略图：小字体，不遮挡组织
        font, font_source = load_grid_label_font(_font_size)
        logger.info(
            f"🔤 [字体] 画布={thumbnail.size[0]}×{thumbnail.size[1]}, "
            f"请求字号={_font_size}px, 来源={font_source}"
        )
            
        # 生成组织掩膜用于预过滤（对所有模式都启用）
        tissue_mask = None
        # Global and local candidates use the same relaxed mask.  It only
        # rejects obvious background and deliberately performs no morphology,
        # which could erase thin, pale or edge tissue.
        try:
            _tissue_start = time.time()
            thumb_arr = np.array(thumbnail)
            tissue_mask = generate_relaxed_candidate_tissue_mask(thumb_arr)
            _tissue_elapsed = time.time() - _tissue_start
            mask_h, mask_w = tissue_mask.shape
            mode_name = "局部模式" if parent_roi else "全局模式"
            logger.info(
                f"⏱️ [计时] 宽松组织预过滤耗时: {_tissue_elapsed:.2f}秒"
                f"（{mode_name}; policy={TISSUE_MASK_POLICY}）"
            )
        except Exception as e:
            logger.error(f"组织掩膜生成失败: {e}")
            raise RuntimeError(
                "Candidate tissue-mask generation failed; "
                "unfiltered ROIs were not restored."
            ) from e

        # 在缩略图上绘制每个ROI的编号
        roi_map = {}  # 编号 -> ROI映射
        
        for idx, roi in enumerate(rois):
            grid_id = idx + 1  # 使用1-based编号
            
            # 计算位置
            x_draw, y_draw, w_draw, h_draw = map_rect(roi['rect'])
            
            # 预过滤：检查是否为空白区域
            y1 = max(0, y_draw)
            y2 = min(mask_h, y_draw + h_draw)
            x1 = max(0, x_draw)
            x2 = min(mask_w, x_draw + w_draw)
            
            if y2 > y1 and x2 > x1:
                roi_mask = tissue_mask[y1:y2, x1:x2]
                tissue_ratio = float(np.mean(roi_mask))
                threshold = TISSUE_CONTENT_THRESHOLD
                
                # 如果组织占比低于阈值，视为无效区域，不参与GPT筛选
                if tissue_ratio < threshold:
                    logger.info(f"🚫 过滤空白网格 #{grid_id}: 组织占比 {tissue_ratio:.1%} < {threshold:.0%} → 不编号")
                    continue
                logger.info(f"✅ 保留有效网格 #{grid_id}: 组织占比 {tissue_ratio:.1%} ≥ {threshold:.0%}")
            else:
                logger.info(f"🚫 过滤无效网格 #{grid_id}: 坐标范围无效")
                continue
            
            # 只有通过预过滤的ROI才会被记录和编号
            roi_map[grid_id] = roi
            
            # 绘制矩形框（红色）
            draw.rectangle([x_draw, y_draw, x_draw+w_draw, y_draw+h_draw], outline='red', width=2)
            
            # 绘制编号
            text = str(grid_id)
            try:
                text_bbox = draw.textbbox((0, 0), text, font=font)
                text_w = text_bbox[2] - text_bbox[0]
                text_h = text_bbox[3] - text_bbox[1]
            except:
                text_w, text_h = 15, 15  # 默认大小（配合 20px 字体）
            
            # 编号居中
            text_x = x_draw + (w_draw - text_w) // 2
            text_y = y_draw + (h_draw - text_h) // 2
            
            # 确保编号在图片内
            text_x = max(0, min(text_x, thumbnail.size[0] - text_w))
            text_y = max(0, min(text_y, thumbnail.size[1] - text_h))
            
            # 绘制文字背景和文字 - ⭐ 缩小背景框，减少遮挡
            draw.rectangle([text_x-2, text_y-2, text_x+text_w+2, text_y+text_h+2], fill='white', outline='red')
            draw.text((text_x, text_y), text, fill='black', font=font)
            
        # 准备发送给GPT的图像
        buffer = io.BytesIO()
        thumbnail.save(buffer, format='JPEG', quality=85)
        
        # 保存调试图像到专门的目录
        import time
        timestamp = int(time.time() * 1000) % 100000  # 最后5位毫秒
        parent_name = parent_roi['id'] if parent_roi else 'global'
        
        # 创建调试图片目录：debug_images/{样本名}/
        debug_base_dir = Path("debug_images")
        if wsi_name:
            # 去掉文件扩展名作为目录名
            sample_name = Path(wsi_name).stem
            debug_dir = debug_base_dir / sample_name
        else:
            debug_dir = debug_base_dir / "unknown_sample"
        debug_dir.mkdir(parents=True, exist_ok=True)
        
        debug_filename = f"debug_gpt_{parent_name}_{len(rois)}candidates_{timestamp}.jpg"
        debug_path = debug_dir / debug_filename
        thumbnail.save(debug_path)
        logger.info(f"已保存AI筛选调试图像: {debug_path}")
        
        # ⭐⭐⭐ 新增：同步保存到 outputs/steps/ 持久化目录 ⭐⭐⭐
        # 确保 HTML 报告能可靠找到图片，不依赖 debug_images 目录
        persistent_image_path = str(debug_path)  # 默认使用 debug 路径
        if agent_state:
            _steps_dir = agent_state.get('output_paths', {}).get('steps_dir')
            if _steps_dir:
                _steps_dir = Path(_steps_dir)
                _steps_dir.mkdir(parents=True, exist_ok=True)
                # 命名规则与绿框对齐：red_R13.jpg / blue_R13-B8.jpg / global_thumbnail.jpg
                if parent_roi:
                    _level = 'blue' if '-' in parent_name else 'red'
                    _persistent_filename = f"{_level}_{parent_name}.jpg"
                else:
                    _persistent_filename = f"global_thumbnail.jpg"
                _persistent_path = _steps_dir / _persistent_filename
                thumbnail.save(_persistent_path, 'JPEG', quality=90)
                persistent_image_path = str(_persistent_path)
                logger.info(f"💾 [持久化] 已保存分析图到 steps: {_persistent_path}")
        
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        # 构建Prompt - 使用规范化MCP提示词
        valid_indices = list(roi_map.keys())
        
        # If every candidate is still below threshold, stop this drill-down.
        # Never restore blank candidates after filtering.
        if not valid_indices:
            logger.warning(
                f"[组织过滤-安全终止] {len(rois)} 个候选均低于"
                f"{TISSUE_CONTENT_THRESHOLD:.0%}组织占比；"
                "不恢复空白候选，也不调用模型。"
            )
            if return_analysis:
                return {
                    'selected_rois': [],
                    'analysis': '',
                    'selection_indices': [],
                    'mcp_action': 'C',
                    'mcp_reason': (
                        "No candidate ROI met the "
                        f"{TISSUE_CONTENT_THRESHOLD:.0%} tissue-content threshold."
                    ),
                    'image_path': persistent_image_path,
                    'tissue_prefilter_empty': True,
                }
            return []
        
        # 获取器官信息（从agent_state或默认值）
        organ = agent_state.get('organ', ORGAN_DEFAULT) if agent_state else ORGAN_DEFAULT
        
        # 根据frame_type使用对应的规范化提示词函数
        frame_type = parent_roi.get('frame_type', 'red') if parent_roi else 'red'
        # ⭐ 从 agent_state 获取语言设置
        _lang = agent_state.get('lang', 'zh') if agent_state else 'zh'

        if frame_type == 'red' or not parent_roi:
            # 使用规范化红框提示词
            prompt = get_red_selection_prompt(len(valid_indices), organ, _lang)
            # (有效编号列表在下方统一追加)
        elif frame_type == 'blue':
            parent_id = parent_roi.get('id', '?')
            
            # Blue-box prompt: include thumbnail global analysis as path context.
            parent_context = ""
            _is_en = (_lang == 'en')
            if agent_state:
                global_analysis = agent_state.get('global_analysis', '')
                _ga_text = ''
                if isinstance(global_analysis, str) and global_analysis:
                    _ga_text = global_analysis
                elif isinstance(global_analysis, dict):
                    _ga_text = global_analysis.get('reasoning', '') or global_analysis.get('analysis', '')
                if _ga_text:
                    _label = f"Thumbnail global analysis (Orchestrator)" if _is_en else "缩略图全局分析（Orchestrator）"
                    parent_context = f"[{_label}]\n{_ga_text}"
                
            _replan = replanning_context if isinstance(replanning_context, dict) else {}
            _replan_kind = _replan.get('kind', '')
            _replan_content = str(_replan.get('content', '') or '')
            _neighbor_ctx = _replan_content if _replan_kind == 'neighbor' else ''
            if _replan_kind == 'knowledge':
                _label = "Local knowledge reference (not image evidence)" if _is_en else "本地知识参考（非图像证据）"
                _knowledge_block = f"[{_label}]\n{_replan_content}"
                parent_context = f"{parent_context}\n\n{_knowledge_block}".strip()
            prompt = get_blue_selection_prompt_mcp(
                red_idx=parent_id,
                num_blue_boxes=len(valid_indices),
                parent_context=parent_context,
                neighbor_context=_neighbor_ctx,
                organ=organ,
                lang=_lang
            )
            logger.info(f"🔵 [MCP Loop] 使用规范化蓝框提示词 (红框 {parent_id}, 上下文长度={len(parent_context)}, 邻近上下文={'有' if _neighbor_ctx else '无'}, lang={_lang})")
        else:
            # 绿框选择（frame_type == 'green'）
            parent_id = parent_roi.get('id', '?')
            
            # Green-box prompt: include thumbnail + red-box path context.
            parent_context = ""
            _is_en = (_lang == 'en')
            blue_parent_red_id = ''
            if agent_state:
                # ⭐ 1. 缩略图全局分析（始终包含，提供全切片架构视角）
                global_analysis = agent_state.get('global_analysis', '')
                _ga_text = ''
                if isinstance(global_analysis, str) and global_analysis:
                    _ga_text = global_analysis
                elif isinstance(global_analysis, dict):
                    _ga_text = global_analysis.get('reasoning', '') or global_analysis.get('analysis', '')
                if _ga_text:
                    _lbl = "Thumbnail global analysis (Orchestrator)" if _is_en else "缩略图全局分析（Orchestrator）"
                    parent_context += f"[{_lbl}]\n{_ga_text}\n\n"

                # ⭐ 2. 从蓝框 ID 解析出父红框 ID（R5-B3 → R5），精确匹配红框分析
                blue_parent_red_id = parent_roi.get('parent_id', '') if parent_roi else ''
                if not blue_parent_red_id and '-B' in parent_id:
                    blue_parent_red_id = parent_id.split('-B')[0]

                if blue_parent_red_id:
                    for ra in agent_state.get('red_analyses', []):
                        if ra.get('roi_id') == blue_parent_red_id:
                            red_reasoning = ra.get('reasoning', ra.get('findings', ''))
                            if red_reasoning:
                                _lbl = f"Red box {blue_parent_red_id} (10× observation)" if _is_en else f"红框 {blue_parent_red_id}（10× 全局观察）"
                                parent_context += f"[{_lbl}]\n{red_reasoning}\n\n"
                            break
                
            _replan = replanning_context if isinstance(replanning_context, dict) else {}
            _replan_kind = _replan.get('kind', '')
            _replan_content = str(_replan.get('content', '') or '')
            _neighbor_ctx = _replan_content if _replan_kind == 'neighbor' else ''
            if _replan_kind == 'knowledge':
                _label = "Local knowledge reference (not image evidence)" if _is_en else "本地知识参考（非图像证据）"
                _knowledge_block = f"[{_label}]\n{_replan_content}"
                parent_context = f"{parent_context}\n\n{_knowledge_block}".strip()
            prompt = get_green_selection_prompt_mcp(
                blue_idx=parent_id,
                num_green_boxes=len(valid_indices),
                parent_context=parent_context,
                neighbor_context=_neighbor_ctx,
                organ=organ,
                lang=_lang
            )
            logger.info(f"🟢 [MCP Loop] 使用规范化绿框提示词 (蓝框 {parent_id}, 父红框={blue_parent_red_id if agent_state else '?'}, 上下文长度={len(parent_context)}, 邻近上下文={'有' if _neighbor_ctx else '无'}, lang={_lang})")

        # ⭐⭐⭐ 双重保险：在提示词末尾明确列出有效编号列表，防止 AI 幻觉选出不存在的编号 ⭐⭐⭐
        valid_ids_sorted = sorted(valid_indices)
        valid_ids_str = ", ".join(str(i) for i in valid_ids_sorted)
        if len(valid_indices) < len(rois):
            # 有过滤 → 编号不连续，强警告
            prompt += (
                f"\n\n⚠️ 【重要约束】部分空白/无组织区域已被自动过滤，编号不连续。"
                f"\n图中仅标注了以下有效编号：【{valid_ids_str}】（共 {len(valid_indices)} 个，原始 {len(rois)} 个）。"
                f"\n🚫 严禁选择未在上述列表中的编号！如果你在图中看不到某个编号的标签，说明该区域已被过滤，不可选择。"
            )
            logger.info(f"📋 [双重保险] 有效编号（已过滤 {len(rois)-len(valid_indices)} 个）: {valid_ids_str}")
        else:
            # 无过滤 → 编号连续，简要提示
            prompt += f"\n\n📋 有效编号范围：【{valid_ids_str}】（共 {len(valid_indices)} 个）。请只从这些编号中选择。"
            logger.info(f"📋 [双重保险] 有效编号（无过滤）: {valid_ids_str}")

        # 支持重试的 AI 调用。动作一旦解析完成（含 B 和 A+[]）不得重试。
        max_retries = 2
        response = None
        selected_indices = []
        decision = _empty_mcp_decision('')

        for attempt in range(max_retries):
            if attempt == 0:
                _prep_elapsed = time.time() - _filter_start_time
                logger.info(f"⏱️ [计时] 准备工作完成，耗时: {_prep_elapsed:.2f}秒，即将调用 API...")
                logger.info("正在请求AI进行网格筛选...")
                response = call_gpt_vision(prompt, image_base64)
            else:
                if _mcp_decision_is_complete(decision):
                    break
                logger.warning(f"🔄 第{attempt+1}次重试AI筛选（上次解析失败）...")
                retry_prompt = prompt + "\n\n⚠️ 重要提醒：请确保【选择结果】格式正确，必须是 蓝框：[1, 2, 3] 这样的纯数字列表！"
                response = call_gpt_vision(retry_prompt, image_base64)

            if not response:
                logger.warning("⚠️ API 返回空响应（completion=0），跳过本次重试")
                continue
            logger.info(f"AI筛选响应: {response[:500]}...")

            decision = parse_mcp_response(response, valid_indices=valid_indices)
            action = (decision.get('action') or '').upper()
            selected_indices = list(decision.get('selected_indices') or [])
            if action in _MCP_NON_DRILL_ACTIONS:
                selected_indices = []
                decision['selected_indices'] = []
            if _mcp_decision_is_complete(decision):
                logger.info(f"✅ AI 选择了 {action}，选区={selected_indices}")
                break
            logger.warning(f"⚠️ 第{attempt+1}次解析失败，将重试...")

        action = (decision.get('action') or '').upper()
        selected_indices = list(decision.get('selected_indices') or [])
        if action in _MCP_NON_DRILL_ACTIONS:
            selected_indices = []

        logger.info(f"🔍 [筛选调试] AI选择编号: {selected_indices}")
        logger.info(f"🔍 [筛选调试] roi_map中的有效编号: {list(roi_map.keys())}")
        logger.info(f"🔍 [筛选调试] valid_indices: {valid_indices}")

        final_rois = []
        selected_ids_str = []
        skipped_ids = []

        for grid_id in selected_indices:
            if grid_id in roi_map:
                roi = roi_map[grid_id]
                roi['selection_source'] = 'ai_screening'
                roi['grid_id'] = grid_id
                final_rois.append(roi)
                selected_ids_str.append(str(grid_id))
            else:
                skipped_ids.append(str(grid_id))
                logger.warning(f"⚠️ 编号{grid_id}不在roi_map中！可能被组织检测过滤掉了")

        if skipped_ids:
            logger.error(f"❌ [严重] AI选择了{len(skipped_ids)}个无效编号: {skipped_ids}")

        if action in _MCP_NON_DRILL_ACTIONS:
            final_rois = []
            selected_indices = []
            selected_ids_str = []

        logger.info(f"AI筛选完成: 保留 {len(final_rois)}/{len(rois)} 个网格 (编号: {', '.join(selected_ids_str)})")

        for roi in final_rois:
            grid_num = roi.get('grid_number', roi.get('grid_id', '?'))
            rect = roi['rect']
            logger.debug(f"  选中Grid#{grid_num}: x={rect['x']}, y={rect['y']}, w={rect['w']}, h={rect['h']}")

        if max_select and len(final_rois) > max_select:
            logger.info(f"限制返回数量: {len(final_rois)} → {max_select}")
            final_rois = final_rois[:max_select]

        if return_analysis:
            mcp_action = (decision.get('action') or '').upper()
            action_parameter = decision.get('action_parameter')
            if mcp_action == 'A' or not isinstance(action_parameter, str):
                mcp_reason = ''
            else:
                mcp_reason = action_parameter
            logger.info(f"🔵 [MCP Loop] AI 选择 {mcp_action or '(none)'}，选中 {len(final_rois)} 个区域")

            if mcp_action in _MCP_NON_DRILL_ACTIONS:
                final_rois = []
                selected_indices = []

            return {
                'selected_rois': final_rois,
                'analysis': response,
                'selection_indices': selected_indices,
                'mcp_action': mcp_action,
                'mcp_reason': mcp_reason,
                'mcp_action_source': 'model',
                'image_path': persistent_image_path,
            }

        return final_rois

    except Exception as e:
        logger.error(f"AI网格筛选过程出错: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        # 筛选异常 fail-closed：不得把全部候选当作已选中。
        if return_analysis:
            return {
                'selected_rois': [],
                'analysis': '',
                'selection_indices': [],
                'mcp_action': '',
                'mcp_reason': 'filter_exception_fail_closed',
                'mcp_action_source': 'system_fail_closed',
                'image_path': persistent_image_path or (str(debug_path) if debug_path else None),
            }
        return []

def get_candidate_rois_from_thumbnail(socketio, room, slide, file_id, steps_dir, wsi_name=None, agent_state=None):
    """
    步骤0：生成完整规则网格红框，覆盖整个WSI并过滤空白区域
    策略：
    1. 生成规则网格（如4×6）覆盖整个WSI
    2. 检测每个网格块的组织占比
    3. 过滤掉空白区域（组织占比<15%）
    4. 保留有组织的网格块作为10X红框
    
    ⭐ 优化：缓存确定性步骤（网格划分+组织检测），AI筛选每次都重新运行
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID
        slide: OpenSlide对象
        file_id: 文件ID
        steps_dir: 步骤输出目录
        wsi_name: WSI文件名（用于创建调试图片子目录）
    """
    _slide_dims = safe_get_slide_metadata(slide)['dimensions']
    try:
        logger.info("开始生成完整规则网格红框")
        
        # ⭐⭐⭐ 缓存机制：检查是否存在有效的网格缓存 ⭐⭐⭐
        import json
        from datetime import datetime
        
        # 从 agent_state 获取正确的输出目录
        # 用 output_dir（WSI 级别）而非 base_dir，避免不同 WSI 的缓存互相覆盖
        output_paths = agent_state.get('output_paths', {}) if agent_state else {}
        cache_dir = output_paths.get('output_dir', output_paths.get('base_dir', Path(f"outputs/{file_id}")))
        if not isinstance(cache_dir, Path):
            cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "grid_rois_cache.json"
        
        grid_rois = None
        all_candidates_with_ids = None
        thumbnail_path = None
        grid_info = None
        use_cache = False
        
        # Cache identity includes the primary field-of-view geometry, candidate
        # count, mask policy and threshold.  Older grids remain on disk for
        # auditability but cannot be silently reused by this protocol.
        CACHE_VERSION = (
            "v14_primary_10x40x160x_TN16_"
            + TISSUE_MASK_POLICY
            + f"_t{TISSUE_CONTENT_THRESHOLD:.4f}"
        )
        
        # 检查缓存是否存在且有效
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                
                # Validate dimensions and the complete deterministic policy.
                cached_dimensions = cache_data.get('dimensions', [])
                current_dimensions = list(_slide_dims)
                cached_version = cache_data.get('cache_version', '')
                
                if (
                    cached_dimensions == current_dimensions
                    and cached_version == CACHE_VERSION
                    and cache_data.get('view_profile') == VIEW_PROFILE_NAME
                    and cache_data.get('tissue_mask_policy') == TISSUE_MASK_POLICY
                    and cache_data.get('tissue_content_threshold')
                    == TISSUE_CONTENT_THRESHOLD
                ):
                    grid_rois = cache_data.get('grid_rois', [])
                    all_candidates_with_ids = cache_data.get('all_candidates_with_ids', [])
                    thumbnail_path = cache_data.get('thumbnail_path', '')
                    grid_info = cache_data.get('grid_info', {})
                    
                    if grid_rois and len(grid_rois) > 0:
                        use_cache = True
                        logger.info(f"✅ [缓存命中] 复用网格缓存: {len(grid_rois)} 个红框候选 (跳过网格生成+组织检测)")
                    else:
                        logger.info(f"⚠️ [缓存无效] 缓存中无有效红框，重新生成")
                elif cached_dimensions != current_dimensions:
                    logger.info(f"⚠️ [缓存失效] WSI尺寸不匹配: 缓存{cached_dimensions} vs 当前{current_dimensions}")
                else:
                    logger.info(f"⚠️ [缓存版本过期] '{cached_version}' → 需要'{CACHE_VERSION}'，重新生成网格")
            except Exception as cache_err:
                logger.warning(f"⚠️ [缓存读取失败] {cache_err}，将重新生成网格")
        else:
            logger.info(f"📝 [无缓存] 首次分析，尝试从历史 run 搜索同名 WSI 缓存...")
        
        # ⭐ 当前目录无缓存时，搜索同一 WSI 的历史缓存并复制过来
        if not use_cache and wsi_name:
            try:
                _wsi_stem = Path(wsi_name).stem if wsi_name else ''
                if _wsi_stem:
                    outputs_root = Path("outputs")
                    if outputs_root.exists():
                        _found_cache = None
                        for candidate in outputs_root.glob(f"**/grid_rois_cache.json"):
                            if candidate == cache_file:
                                continue
                            try:
                                with open(candidate, 'r', encoding='utf-8') as _cf:
                                    _cd = json.load(_cf)
                                # 三重校验：尺寸 + 缓存版本 + 目录名含 WSI 文件名
                                _dir_name = candidate.parent.name
                                _name_in_dir = _wsi_stem in _dir_name
                                if (_cd.get('dimensions') == list(_slide_dims)
                                    and _cd.get('cache_version') == CACHE_VERSION
                                    and _cd.get('view_profile') == VIEW_PROFILE_NAME
                                    and _cd.get('tissue_mask_policy') == TISSUE_MASK_POLICY
                                    and _cd.get('tissue_content_threshold')
                                    == TISSUE_CONTENT_THRESHOLD
                                    and _cd.get('grid_rois')
                                    and _name_in_dir):
                                    _found_cache = candidate
                                    break
                            except Exception:
                                continue
                        
                        if _found_cache:
                            import shutil
                            shutil.copy2(str(_found_cache), str(cache_file))
                            logger.info(f"✅ [历史缓存复用] 从 {_found_cache.parent.name} 复制网格缓存")
                            with open(cache_file, 'r', encoding='utf-8') as f:
                                cache_data = json.load(f)
                            grid_rois = cache_data.get('grid_rois', [])
                            all_candidates_with_ids = cache_data.get('all_candidates_with_ids', [])
                            thumbnail_path = cache_data.get('thumbnail_path', '')
                            grid_info = cache_data.get('grid_info', {})
                            if grid_rois and len(grid_rois) > 0:
                                use_cache = True
            except Exception as _search_err:
                logger.warning(f"⚠️ [历史缓存搜索失败] {_search_err}，将重新生成网格")
        
        # ⭐⭐⭐ 如果缓存命中，跳过网格生成步骤 ⭐⭐⭐
        if use_cache:
            # 从缓存恢复到 agent_state
            if agent_state and all_candidates_with_ids:
                agent_state['all_candidate_rois'] = all_candidates_with_ids
                logger.info(f"✅ 从缓存恢复 all_candidate_rois: {len(all_candidates_with_ids)} 个")
            
            initial_roi_count = len(grid_rois)
            grid_rows = grid_info.get('rows', 4)
            grid_cols = grid_info.get('cols', 4)
            cell_size = grid_info.get('cell_size', 0)
            cell_width = cell_size
            cell_height = cell_size
            tissue_threshold = TISSUE_CONTENT_THRESHOLD
            
            # 确保 thumbnail_path 是 Path 对象
            if thumbnail_path:
                thumbnail_path = Path(thumbnail_path)
            else:
                thumbnail_path = steps_dir / "step_0_thumbnail.jpg"
            
            import time
            _cache_load_time = time.time()
            logger.info(f"📦 [缓存加载完成] {grid_rows}×{grid_cols} 网格, {initial_roi_count} 个红框候选")
            logger.info(f"⏱️ [计时] 缓存加载完成，时间戳: {_cache_load_time:.2f}，即将进入 AI 筛选...")
            # 缓存命中：跳过网格生成，直接进入 AI 筛选步骤
        if not use_cache:
            # ⭐⭐⭐ 正常流程：生成网格（将被缓存）⭐⭐⭐
            # 获取缩略图
            with _openslide_lock:
                thumbnail = slide.get_thumbnail((2000, 2000))
            thumbnail = thumbnail.convert('RGB')
            thumbnail_array = np.array(thumbnail)
            
            # 保存缩略图
            thumbnail_path = steps_dir / "step_0_thumbnail.jpg"
            thumbnail.save(thumbnail_path, 'JPEG', quality=90)
            
            # Global bounding box and per-grid ratios use exactly the same
            # relaxed, morphology-free candidate mask.
            candidate_tissue_mask = generate_relaxed_candidate_tissue_mask(
                thumbnail_array
            )
            
            # === 2. 计算组织的外接矩形（用宽松掩膜）===
            rows = np.any(candidate_tissue_mask, axis=1)
            cols = np.any(candidate_tissue_mask, axis=0)
            y_indices = np.where(rows)[0]
            x_indices = np.where(cols)[0]
            
            if len(x_indices) == 0 or len(y_indices) == 0:
                raise RuntimeError(
                    "No tissue bounding box was detected; "
                    "geometric-centre fallback is disabled."
                )
            
            x_min, x_max = x_indices[0], x_indices[-1]
            y_min, y_max = y_indices[0], y_indices[-1]
            tissue_width = x_max - x_min + 1
            tissue_height = y_max - y_min + 1
            logger.info(f"📐 外接矩形: x=[{x_min},{x_max}] y=[{y_min},{y_max}] 尺寸={tissue_width}×{tissue_height} 缩略图={thumbnail_array.shape[1]}×{thumbnail_array.shape[0]}")
            
            # === 3. 自适应正方形网格（方法3）===
            # 核心思想：根据组织面积自动计算正方形格子边长，不依赖固定预设
            import math
            TARGET_N = 16  # 目标红框候选数（固定为 16）
            
            # 核心公式：cell_size = sqrt(组织面积 / 目标数量)
            tissue_area = tissue_width * tissue_height
            cell_size_initial = math.sqrt(tissue_area / TARGET_N)
            
            # 用 ceil 向上取整，确保网格完全覆盖组织区域（宁多不少）
            # round 在非正方形组织上会丢失边缘（如 round(4.47)=4，右侧空一大块）
            # Extra blank cells from ceil() are dropped by the 15% tissue threshold.
            grid_cols = max(1, math.ceil(tissue_width / cell_size_initial))
            grid_rows = max(1, math.ceil(tissue_height / cell_size_initial))
            
            # 重新计算 cell_size：取 max 确保覆盖完整，格子保持正方形
            cell_size = max(tissue_width / grid_cols, tissue_height / grid_rows)
            
            total_grids = grid_rows * grid_cols
            logger.info(f"自适应网格: cell_size={int(cell_size)}x{int(cell_size)}px, "
                        f"{grid_rows}行x{grid_cols}列（共{total_grids}格）, TARGET_N={TARGET_N}")
            
            grid_rois = []
            all_red_overlays = []
            tissue_threshold = TISSUE_CONTENT_THRESHOLD
            thumb_width, thumb_height = thumbnail.size
            
            for row in range(grid_rows):
                for col in range(grid_cols):
                    # 计算网格块在缩略图上的位置
                    x_start = int(x_min + col * cell_size)
                    y_start = int(y_min + row * cell_size)
                    cell_w = int(cell_size)
                    cell_h = int(cell_size)
                    
                    # ⭐ 超出缩略图边界时裁切（不丢弃，保留边缘有意义的组织）
                    if x_start + cell_w > thumb_width:
                        cell_w = thumb_width - x_start
                    if y_start + cell_h > thumb_height:
                        cell_h = thumb_height - y_start
                    if cell_w <= 0 or cell_h <= 0:
                        continue
                    
                    # Slice the same candidate mask used for the bounding box.
                    patch_mask = candidate_tissue_mask[
                        y_start:y_start + cell_h,
                        x_start:x_start + cell_w,
                    ]
                    if patch_mask.size == 0:
                        continue
                    tissue_ratio = float(np.mean(patch_mask))
                    
                    if tissue_ratio >= tissue_threshold:
                        _grid_lang = agent_state.get('lang', 'zh') if agent_state else 'zh'
                        _reason = f'Grid[{row+1},{col+1}] tissue {tissue_ratio:.1%}' if _grid_lang == 'en' else f'网格[{row+1},{col+1}] 组织占比{tissue_ratio:.1%}'
                        roi = map_roi_to_original_dimensions({
                            'rect': {'x': x_start, 'y': y_start, 'w': cell_w, 'h': cell_h},
                            'id': f'roi_10x_r{row}_c{col}',
                            'zoom_level': 10,
                            'priority': 5,
                            'reason': _reason,
                            'suspected_pathology': 'pending' if _grid_lang == 'en' else '待分析',
                            'tissue_coverage': tissue_ratio,
                            'grid_position': {'row': row, 'col': col}
                        }, _slide_dims, thumbnail.size)
                        
                        grid_rois.append(roi)
                        all_red_overlays.append(create_roi_overlay(roi))
                        logger.info(f"✅ 保留网格[{row+1},{col+1}]: 组织占比{tissue_ratio:.1%}")
                    else:
                        logger.info(f"🚫 跳过网格[{row+1},{col+1}]: 组织占比{tissue_ratio:.1%} < {tissue_threshold:.1%}")
            
            # ⭐ 保存调试图：过滤前的全部网格（含被丢弃/跳过的）
            try:
                from PIL import ImageDraw
                debug_all_grid_img = thumbnail.copy()
                debug_draw = ImageDraw.Draw(debug_all_grid_img)
                grid_idx = 0
                for r in range(grid_rows):
                    for c in range(grid_cols):
                        grid_idx += 1
                        gx = int(x_min + c * cell_size)
                        gy = int(y_min + r * cell_size)
                        gw = int(cell_size)
                        gh = int(cell_size)
                        # 判断是否被保留（在 grid_rois 中）
                        is_kept = any(
                            roi.get('grid_position', {}).get('row') == r and roi.get('grid_position', {}).get('col') == c
                            for roi in grid_rois
                        )
                        color = 'red' if is_kept else 'gray'
                        debug_draw.rectangle([gx, gy, gx+gw, gy+gh], outline=color, width=2)
                        debug_draw.text((gx+5, gy+5), str(grid_idx), fill=color)
                debug_all_path = steps_dir / "debug_all_grids_before_filter.jpg"
                debug_all_grid_img.save(str(debug_all_path), quality=90)
                logger.info(f"💾 调试图已保存（过滤前全部{grid_rows}×{grid_cols}={grid_rows*grid_cols}格）: {debug_all_path}")
            except Exception as debug_err:
                logger.warning(f"调试图保存失败: {debug_err}")
            
            if len(grid_rois) == 0:
                raise RuntimeError(
                    "No first-level ROI met the tissue-content threshold; "
                    "geometric-centre fallback is disabled."
                )
            
            initial_roi_count = len(grid_rois)
            logger.info(f"成功生成 {initial_roi_count} 个有组织的红框（过滤掉 {grid_rows * grid_cols - initial_roi_count} 个空白）")
            
            # ⭐⭐⭐ 保存完整候选框列表（AI筛选前） ⭐⭐⭐
            if agent_state:
                # 为每个候选框分配临时ID（用于后续引用）
                all_candidates_with_ids = []
                for idx, roi in enumerate(grid_rois):
                    roi_copy = roi.copy()
                    roi_copy['candidate_index'] = idx + 1  # 1-based index
                    all_candidates_with_ids.append(roi_copy)
                agent_state['all_candidate_rois'] = all_candidates_with_ids
                logger.info(f"✅ 保存完整候选框列表到 agent_state['all_candidate_rois']: {initial_roi_count} 个")
            
            # ⭐⭐⭐ 保存网格缓存（确定性步骤）⭐⭐⭐
            try:
                cache_data = {
                    'cache_version': CACHE_VERSION,  # ⭐ 版本标记，旧缓存自动失效
                    'view_profile': VIEW_PROFILE_NAME,
                    'tissue_mask_policy': TISSUE_MASK_POLICY,
                    'tissue_content_threshold': TISSUE_CONTENT_THRESHOLD,
                    'file_id': file_id,
                    'dimensions': list(_slide_dims),
                    'thumbnail_size': [2000, 2000],
                    'grid_rois': grid_rois,
                    'all_candidates_with_ids': all_candidates_with_ids if agent_state else grid_rois,
                    'thumbnail_path': str(thumbnail_path),
                    'grid_info': {
                        'rows': grid_rows,
                        'cols': grid_cols,
                        'initial_count': initial_roi_count,
                        'cell_size': int(cell_size)
                    },
                    'created_at': datetime.now().isoformat()
                }
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)
                logger.info(f"💾 [缓存保存] 网格缓存已保存: {cache_file}")
            except Exception as save_err:
                logger.warning(f"⚠️ [缓存保存失败] {save_err}（不影响分析流程）")
        
        # ⭐⭐⭐ AI 筛选步骤：无论缓存命中与否都执行（非确定性）⭐⭐⭐
        # === 5. 用AI筛选红框（按规范：AI自己决定选择数量）===
        import time
        _ai_filter_start = time.time()
        logger.info(f"⏱️ [计时] 开始调用 AI 筛选，时间戳: {_ai_filter_start:.2f}")
        logger.info(f"启动AI筛选红框（候选{len(grid_rois)}个），AI将根据切片复杂度决定选择数量...")
        
        # 使用AI筛选最有价值的红框（不限制数量，让AI决定）
        # ⭐ 使用return_analysis=True获取AI的完整思考过程
        gpt_result = filter_rois_by_gpt_vision(
            slide, grid_rois, 
            thumbnail_size=(2000, 2000), 
            max_select=None,  # ⭐ 不限制数量，让AI根据切片复杂度决定（单题聚焦仅靠提示词引导少选/早停）
            parent_roi=None,  # 全局筛选
            wsi_name=wsi_name,
            return_analysis=True,  # ⭐ 获取AI的完整响应（包含【思考过程】）
            agent_state=agent_state
        )
        
        # 提取结果
        gpt_filtered_rois = gpt_result.get('selected_rois', gpt_result) if isinstance(gpt_result, dict) else gpt_result
        red_selection_reasoning = gpt_result.get('analysis', '') if isinstance(gpt_result, dict) else ''
        
        # ⭐ 保存全局分析到 agent_state（用于树可视化和报告）
        if agent_state:
            agent_state['global_analysis'] = red_selection_reasoning
            logger.info(f"✅ 保存 global_analysis 到 agent_state (长度: {len(red_selection_reasoning)})")
            if red_selection_reasoning:
                logger.info(f"   前200字符: {red_selection_reasoning[:200]}...")
            else:
                logger.warning(f"⚠️ red_selection_reasoning 为空！gpt_result类型={type(gpt_result)}")
            # ⭐ 优先使用 filter_rois_by_gpt_vision 返回的持久化路径
            _global_img_path = gpt_result.get('image_path') if isinstance(gpt_result, dict) else None
            if _global_img_path:
                agent_state['global_thumbnail_path'] = _global_img_path
                logger.info(f"✅ 保存全局缩略图路径(持久化): {_global_img_path}")
            else:
                # 回退：从 debug_images 搜索
                sample_name = wsi_name.replace('.svs', '').replace('.ndpi', '') if wsi_name else file_id
                debug_dir = Path(f"debug_images/{sample_name}")
                if debug_dir.exists():
                    thumb_matches = list(debug_dir.glob("debug_gpt_global_*.jpg"))
                    if thumb_matches:
                        thumb_matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                        agent_state['global_thumbnail_path'] = str(thumb_matches[0])
                        logger.info(f"✅ 保存全局缩略图路径(回退glob): {thumb_matches[0]}")
        
        logger.info(f"AI筛选完成: {len(grid_rois)} → {len(gpt_filtered_rois)} 个红框")
        
        # 更新为筛选后的ROI
        grid_rois = gpt_filtered_rois
        
        # 重新生成overlays（基于筛选后的ROI）
        all_red_overlays = []
        for roi in grid_rois:
            all_red_overlays.append(create_roi_overlay(roi))
        
        # === 6. 一次性发送所有红框（不改变视口缩放）===
        socketio.emit('agent.viewport', {
            'type': 'viewport',
            'step': 0,
            'center_px': {'x': _slide_dims[0] // 2, 'y': _slide_dims[1] // 2},
            # 不设置zoom_level，保持当前视图不变
            'overlays': all_red_overlays,
            'why': f'完整规则网格：{grid_rows}×{grid_cols}，保留{len(grid_rois)}个有组织区域',
            'thumbnail_path': str(thumbnail_path)
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 WebSocket 消息
        
        # 发送Trace事件
        total_grids = grid_rows * grid_cols
        blank_filtered_count = total_grids - initial_roi_count
        final_count = len(grid_rois)
        
        # ⭐ 发送红框选中消息（包含AI的完整思考过程）
        # 使用"已选择 X 个"格式触发前端的 isProgressMessage 判断，使其显示在左侧面板
        red_ids = [roi.get('id', f'红框{i+1}') for i, roi in enumerate(grid_rois)]
        logger.info(f"🔴 AI选中 {len(grid_rois)} 个红框进行分析: {red_ids}")
        socketio.emit('agent.message', {
            'type': 'assistant',
            'content': _msg('red_select_done', agent_state.get('lang', 'zh'), n=len(grid_rois)),  # ⭐ "选取完成"+"已选择 X 个"触发左侧面板显示
            'reasoning': red_selection_reasoning,  # ⭐ AI选择红框的完整思考过程
            'level': 'red',
            'roi_id': 'AI-Red-Selection',  # 特殊ID标识全局红框选择
            'metadata': {'action': 'selected', 'count': len(grid_rois)}
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 WebSocket 消息
        
        socketio.emit('agent.trace', {
            'type': 'trace',
            'step': 0,
            'observation': f"AI根据切片复杂度，从{initial_roi_count}个有效区域中选出{final_count}个最有诊断价值的红框。",
            'tools_called': ['grid_generation', 'tissue_detection'],
            'results': {
                'total_grids': total_grids,
                'after_blank_filter': initial_roi_count,
                'final_selected': final_count,
                'blank_filtered': blank_filtered_count,
                'grid_layout': f'{grid_rows}×{grid_cols}',
                'cell_size': f'{int(cell_size)}×{int(cell_size)}',
                'tissue_threshold': f'{tissue_threshold:.1%}',
                'analysis_strategy': f'{final_count}个红框；下级区域数量由 Subagent 按需决定，并行分析'
            },
            'decision': f"AI根据切片复杂度选择了{final_count}个区域进行多尺度分析。",
            'next_action': {'action': 'start_parallel_multiscale_analysis'},
            'evidence': [{'thumb': str(thumbnail_path), 'roi_id': 'grid_overview'}]
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 WebSocket 消息
        
        return grid_rois
        
    except Exception as e:
        logger.error(f"智能ROI划分失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise RuntimeError(
            "First-level ROI generation failed; no geometric-centre fallback "
            "was used."
        ) from e



def generate_relaxed_candidate_tissue_mask(image_array):
    """Generate the morphology-free mask used only for candidate filtering.

    Candidate filtering should reject obvious blank background, not perform
    precise tissue segmentation.  Otsu/HSV/morphology can erase thin, pale,
    fragmented or edge tissue and therefore create false-negative candidates.
    """
    rgb = np.asarray(image_array)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError(
            f"Candidate tissue mask expects an RGB image, got shape={rgb.shape}"
        )
    rgb = rgb[:, :, :3]
    gray = np.mean(rgb, axis=2)
    color_std = np.std(rgb, axis=2)
    return (gray < 220) & (color_std > 10)


def generate_tissue_mask(thumbnail_array):
    """
    生成组织掩膜，使用Otsu阈值和颜色过滤
    
    Args:
        thumbnail_array: RGB图像数组
        
    Returns:
        tissue_mask: 二值掩膜，True为组织区域
    """
    try:
        # 转换为灰度
        gray = np.mean(thumbnail_array, axis=2)
        
        # 方法1: Otsu阈值（如果可用）
        try:
            from skimage.filters import threshold_otsu
            threshold = threshold_otsu(gray)
            otsu_mask = gray < threshold
        except ImportError:
            # 备选方法：简单阈值
            threshold = 230  # 假设背景是白色
            otsu_mask = gray < threshold
        
        # 方法2: 颜色过滤（排除白色和极浅色背景）
        # 计算颜色强度和饱和度
        color_intensity = np.mean(thumbnail_array, axis=2)
        color_std = np.std(thumbnail_array, axis=2)
        
        # 组织区域通常有更多颜色变化
        color_mask = (color_intensity < 240) & (color_std > 10)
        
        # 方法3: HSV颜色空间过滤（病理切片通常有粉色/紫色）
        from PIL import Image
        pil_image = Image.fromarray(thumbnail_array.astype(np.uint8))
        hsv_array = np.array(pil_image.convert('HSV'))
        
        # 饱和度过滤（组织区域饱和度更高）
        saturation = hsv_array[:, :, 1]
        saturation_mask = saturation > 20
        
        # 组合所有掩膜
        tissue_mask = otsu_mask & color_mask & saturation_mask
        
        # 形态学操作去除噪声
        from scipy.ndimage import binary_opening, binary_closing
        tissue_mask = binary_opening(tissue_mask, structure=np.ones((5, 5)))
        tissue_mask = binary_closing(tissue_mask, structure=np.ones((10, 10)))
        
        return tissue_mask
        
    except Exception as e:
        logger.error(f"组织掩膜生成失败: {e}")
        # 备选简单方法
        gray = np.mean(thumbnail_array, axis=2)
        return gray < 230

def filter_and_adjust_roi_by_tissue_mask(roi, tissue_mask, thumbnail_size):
    """
    基于组织掩膜过滤和调整ROI
    
    Args:
        roi: ROI字典
        tissue_mask: 组织掩膜
        thumbnail_size: 缩略图尺寸
        
    Returns:
        adjusted_roi: 调整后的ROI，如果无效则返回None
    """
    try:
        x, y, w, h = roi['rect']['x'], roi['rect']['y'], roi['rect']['w'], roi['rect']['h']
        
        # 确保ROI在图像范围内
        x = max(0, min(x, thumbnail_size[0] - w))
        y = max(0, min(y, thumbnail_size[1] - h))
        w = min(w, thumbnail_size[0] - x)
        h = min(h, thumbnail_size[1] - y)
        
        # 提取ROI区域的组织掩膜
        roi_mask = tissue_mask[y:y+h, x:x+w]
        
        if roi_mask.size == 0:
            return None
            
        # 计算组织覆盖率
        tissue_coverage = np.sum(roi_mask) / roi_mask.size
        
        # 如果覆盖率太低，丢弃ROI
        if tissue_coverage < 0.6:
            logger.debug(f"ROI {roi.get('id', 'unknown')} 覆盖率 {tissue_coverage:.2f} < 0.6，丢弃")
            return None
        
        # 如果覆盖率在0.6-0.8之间，尝试收缩ROI
        if tissue_coverage < 0.8:
            adjusted_roi = shrink_roi_to_tissue(roi, tissue_mask, thumbnail_size)
            if adjusted_roi:
                return adjusted_roi
        
        # 更新ROI坐标
        roi['rect']['x'] = x
        roi['rect']['y'] = y
        roi['rect']['w'] = w
        roi['rect']['h'] = h
        roi['center_px']['x'] = x + w // 2
        roi['center_px']['y'] = y + h // 2
        roi['tissue_coverage'] = tissue_coverage
        
        return roi
        
    except Exception as e:
        logger.error(f"ROI过滤调整失败: {e}")
        return None

def shrink_roi_to_tissue(roi, tissue_mask, thumbnail_size):
    """
    收缩ROI到组织密集区域
    """
    try:
        x, y, w, h = roi['rect']['x'], roi['rect']['y'], roi['rect']['w'], roi['rect']['h']
        
        # 提取ROI区域
        roi_mask = tissue_mask[y:y+h, x:x+w]
        
        # 找到组织区域的边界
        tissue_coords = np.where(roi_mask)
        if len(tissue_coords[0]) == 0:
            return None
            
        min_y_rel, min_x_rel = np.min(tissue_coords, axis=1)
        max_y_rel, max_x_rel = np.max(tissue_coords, axis=1)
        
        # 计算新的ROI边界
        new_x = x + min_x_rel
        new_y = y + min_y_rel
        new_w = max_x_rel - min_x_rel + 1
        new_h = max_y_rel - min_y_rel + 1
        
        # 确保最小尺寸
        min_size = 50
        if new_w < min_size or new_h < min_size:
            return None
        
        # 重新计算覆盖率
        new_roi_mask = tissue_mask[new_y:new_y+new_h, new_x:new_x+new_w]
        new_coverage = np.sum(new_roi_mask) / new_roi_mask.size
        
        if new_coverage >= 0.8:
            roi['rect']['x'] = new_x
            roi['rect']['y'] = new_y
            roi['rect']['w'] = new_w
            roi['rect']['h'] = new_h
            roi['center_px']['x'] = new_x + new_w // 2
            roi['center_px']['y'] = new_y + new_h // 2
            roi['tissue_coverage'] = new_coverage
            roi['reason'] += ' (收缩优化)'
            return roi
        
        return None
        
    except Exception as e:
        logger.error(f"ROI收缩失败: {e}")
        return None

def generate_supplementary_rois(tissue_mask, thumbnail_array, num_needed):
    """
    基于组织掩膜自动生成补充ROI
    """
    try:
        supplementary_rois = []
        
        # 寻找组织密集区域
        h, w = tissue_mask.shape
        roi_size = min(w, h) // 8  # ROI大小
        
        # 计算每个可能位置的组织密度
        step_size = roi_size // 2
        best_positions = []
        
        for y in range(0, h - roi_size, step_size):
            for x in range(0, w - roi_size, step_size):
                roi_mask = tissue_mask[y:y+roi_size, x:x+roi_size]
                coverage = np.sum(roi_mask) / roi_mask.size
                
                if coverage >= 0.8:  # 只考虑高覆盖率区域
                    # 计算该区域的颜色复杂度（作为细胞密度的代理）
                    roi_region = thumbnail_array[y:y+roi_size, x:x+roi_size]
                    color_complexity = np.std(roi_region)
                    
                    best_positions.append({
                        'x': x, 'y': y, 'w': roi_size, 'h': roi_size,
                        'coverage': coverage,
                        'complexity': color_complexity,
                        'score': coverage * 0.7 + (color_complexity / 255) * 0.3
                    })
        
        # 按评分排序
        best_positions.sort(key=lambda x: x['score'], reverse=True)
        
        # 生成补充ROI，避免重叠
        used_positions = []
        for i, pos in enumerate(best_positions):
            if len(supplementary_rois) >= num_needed:
                break
                
            # 检查是否与已有ROI重叠
            overlap = False
            for used_pos in used_positions:
                if (abs(pos['x'] - used_pos['x']) < roi_size * 0.5 and 
                    abs(pos['y'] - used_pos['y']) < roi_size * 0.5):
                    overlap = True
                    break
            
            if not overlap:
                roi = {
                    'id': f'supplementary_roi_{len(supplementary_rois)+1}',
                    'center_px': {
                        'x': pos['x'] + pos['w'] // 2,
                        'y': pos['y'] + pos['h'] // 2
                    },
                    'rect': {
                        'x': pos['x'], 'y': pos['y'],
                        'w': pos['w'], 'h': pos['h']
                    },
                    'zoom_level': 10,
                    'priority': 4 - len(supplementary_rois),  # 递减优先级
                    'reason': f'组织密集区域补充 (覆盖率: {pos["coverage"]:.2f})',
                    'suspected_pathology': '自动识别的高密度区域',
                    'analysis_stages': ['10x', '20x', '40x'],
                    'tissue_coverage': pos['coverage'],
                    'source': 'tissue_mask_supplement'
                }
                supplementary_rois.append(roi)
                used_positions.append(pos)
        
        return supplementary_rois
        
    except Exception as e:
        logger.error(f"补充ROI生成失败: {e}")
        return []

def map_roi_to_original_dimensions(roi, slide_dimensions, thumbnail_size):
    """
    将ROI坐标从缩略图映射到原始WSI尺寸
    """
    try:
        # 计算缩放比例
        scale_x = slide_dimensions[0] / thumbnail_size[0]
        scale_y = slide_dimensions[1] / thumbnail_size[1]
        
        # 映射坐标
        original_x = int(roi['rect']['x'] * scale_x)
        original_y = int(roi['rect']['y'] * scale_y)
        original_w = int(roi['rect']['w'] * scale_x)
        original_h = int(roi['rect']['h'] * scale_y)
        
        # 边界检查
        original_x = max(0, min(original_x, slide_dimensions[0] - original_w))
        original_y = max(0, min(original_y, slide_dimensions[1] - original_h))
        original_w = min(original_w, slide_dimensions[0] - original_x)
        original_h = min(original_h, slide_dimensions[1] - original_y)
        
        # 更新ROI
        mapped_roi = roi.copy()
        mapped_roi['rect'] = {
            'x': original_x, 'y': original_y,
            'w': original_w, 'h': original_h
        }
        mapped_roi['center_px'] = {
            'x': original_x + original_w // 2,
            'y': original_y + original_h // 2
        }
        
        return mapped_roi
        
    except Exception as e:
        logger.error(f"ROI坐标映射失败: {e}")
        return roi

def create_roi_overlay(roi):
    """
    为前端创建ROI覆盖层（包含完整字段）
    """
    # 获取倍率，默认10x
    magnification = roi.get('zoom_level', 10)
    
    # 根据倍率设置颜色
    if magnification == 10:
        color = '#FF0000'  # 红色
        level_name = '宏观结构'
    elif magnification == 40:
        color = '#0066FF'  # 蓝色
        level_name = '病灶特征'
    elif magnification == 160:
        color = '#00CC00'  # 绿色
        level_name = '细胞证据'
    else:
        color = get_roi_color_by_priority(roi.get('priority', 3))
        level_name = f'{magnification}倍镜'
    
    # 获取图像路径（如果有）
    image_path = roi.get('image_path', None)
    if not image_path and 'evidence' in roi and roi['evidence']:
        # 尝试从evidence中获取
        if isinstance(roi['evidence'], list) and len(roi['evidence']) > 0:
            image_path = roi['evidence'][0].get('thumb', None)
        elif isinstance(roi['evidence'], dict):
            image_path = roi['evidence'].get('thumb', None)
    
    overlay_data = {
        'id': roi['id'],
        'rect': roi['rect'],
        'label': '',  # ⭐ 删除标签文字，保持界面干净
        'confidence': roi.get('tissue_coverage', roi.get('confidence', 0.8)),
        'color': color,
        'magnification': magnification,
        'level_name': level_name
    }
    
    # 如果有图像路径，添加到overlay数据中
    if image_path:
        overlay_data['image_path'] = image_path
    
    return overlay_data

def get_roi_color_by_priority(priority):
    """
    根据优先级返回ROI颜色
    """
    color_map = {
        5: '#ff0000',  # 红色 - 最高优先级
        4: '#ff8800',  # 橙色
        3: '#ffff00',  # 黄色
        2: '#88ff00',  # 浅绿色
        1: '#00ff00'   # 绿色 - 最低优先级
    }
    return color_map.get(priority, '#0088ff')



def validate_and_adjust_roi(roi, slide_dimensions, tissue_bounds=None):
    """
    验证并调整ROI位置，确保在组织区域内
    """
    width, height = slide_dimensions
    
    # 如果没有组织边界信息，使用默认验证
    if tissue_bounds is None:
        # 确保ROI在图像边界内
        roi['rect']['x'] = max(0, min(roi['rect']['x'], width - roi['rect']['w']))
        roi['rect']['y'] = max(0, min(roi['rect']['y'], height - roi['rect']['h']))
        roi['center_px']['x'] = roi['rect']['x'] + roi['rect']['w'] // 2
        roi['center_px']['y'] = roi['rect']['y'] + roi['rect']['h'] // 2
        return roi
    
    # 计算缩放比例（从缩略图到原始图像）
    scale_x = width / 1000  # 假设缩略图是1000x1000
    scale_y = height / 1000
    
    # 将组织边界转换到原始图像尺寸
    tissue_min_x = int(tissue_bounds['min_x'] * scale_x)
    tissue_max_x = int(tissue_bounds['max_x'] * scale_x)
    tissue_min_y = int(tissue_bounds['min_y'] * scale_y)
    tissue_max_y = int(tissue_bounds['max_y'] * scale_y)
    
    # 调整ROI位置到组织区域内
    roi_w, roi_h = roi['rect']['w'], roi['rect']['h']
    
    # 确保ROI完全在组织区域内
    new_x = max(tissue_min_x, min(roi['rect']['x'], tissue_max_x - roi_w))
    new_y = max(tissue_min_y, min(roi['rect']['y'], tissue_max_y - roi_h))
    
    # 如果调整后ROI仍然超出边界，缩小ROI
    if new_x + roi_w > tissue_max_x:
        roi_w = tissue_max_x - new_x
    if new_y + roi_h > tissue_max_y:
        roi_h = tissue_max_y - new_y
    
    # 更新ROI信息
    roi['rect']['x'] = new_x
    roi['rect']['y'] = new_y
    roi['rect']['w'] = roi_w
    roi['rect']['h'] = roi_h
    roi['center_px']['x'] = new_x + roi_w // 2
    roi['center_px']['y'] = new_y + roi_h // 2
    
    return roi

def subdivide_roi_into_grid(parent_roi, grid_rows=2, grid_cols=2, child_zoom_level=40):
    """
    将父ROI细分成子网格
    
    ⭐⭐⭐ 修复：使用与自动模式 create_blue_grid 一致的逻辑 ⭐⭐⭐
    之前的问题：使用 min(cell_width, cell_height) 作为正方形边长，导致非正方形父ROI时
    所有子框挤在左上角，而不是均匀覆盖整个父框区域，与 AI 看到的图像编号不对应。
    
    Args:
        parent_roi: 父ROI信息
        grid_rows: 网格行数
        grid_cols: 网格列数
        child_zoom_level: 子ROI的倍率
        
    Returns:
        list: 子ROI列表
    """
    parent_rect = parent_roi.get('rect', {})
    parent_id = parent_roi.get('id', 'unknown')
    
    # ⭐⭐⭐ 验证 rect 是否有效 ⭐⭐⭐
    if not parent_rect or not all(k in parent_rect for k in ['x', 'y', 'w', 'h']):
        logger.error(f"⚠️ [subdivide_roi_into_grid] 无效的 rect: parent_id={parent_id}, rect={parent_rect}")
        return []  # 返回空列表，避免崩溃
    
    # ⭐⭐⭐ 修复：使用实际的列宽和行高，与 create_blue_grid 保持一致 ⭐⭐⭐
    # 确保子框能够均匀覆盖整个父框区域
    cell_width = parent_rect['w'] // grid_cols
    cell_height = parent_rect['h'] // grid_rows
    
    child_rois = []
    grid_idx = 1  # GPT看到的编号（1-based）
    for row in range(grid_rows):
        for col in range(grid_cols):
            # ⭐ 修复：使用 cell_width 和 cell_height 分别计算 x 和 y
            x = parent_rect['x'] + col * cell_width
            y = parent_rect['y'] + row * cell_height
            
            child_roi = {
                'id': f"{parent_id}_sub_r{row}_c{col}",
                'rect': {'x': x, 'y': y, 'w': cell_width, 'h': cell_height},
                'zoom_level': child_zoom_level,
                'priority': 5,
                'reason': f'{parent_id} sub-region [{row},{col}]',
                'suspected_pathology': 'pending',
                'parent_id': parent_id,
                'grid_position': {'row': row, 'col': col},
                'grid_number': grid_idx,  # 记录GPT编号用于调试
                'grid_id': grid_idx  # ⭐ 添加 grid_id 与 filter_rois_by_gpt_vision 保持一致
            }
            child_rois.append(child_roi)
            grid_idx += 1
            
    return child_rois


def _extract_findings_from_reasoning(reasoning_text):
    """
    从 AI 分析输出中提取 findings 摘要（直接显示，不做格式转换）
    
    Args:
        reasoning_text: 完整的分析文本（可能是【思考过程】或【Reason】格式）
    
    Returns:
        str: 提取的 findings 摘要（直接显示 AI 原始输出，与单张 WSI 自动模式一致）
    """
    if not reasoning_text:
        return "40x 分析进行中..."
    
    import re
    
    # 提取中英文 Reason + Evidence（直接显示，不改写模型原文）
    reason_match = re.search(
        r'(?:【Reason】|\[Reason\])\s*([\s\S]*?)(?=【Plan】|\[Plan\]|$)',
        reasoning_text,
        re.IGNORECASE,
    )
    if reason_match:
        reason_text = reason_match.group(1).strip()
        
        # 同时提取 Evidence 部分（如果有）- 直接显示，不做格式转换
        evidence_match = re.search(
            r'(?:【Evidence】|\[Evidence\])\s*([\s\S]*?)(?=【|\[[A-Za-z ]+\]|$)',
            reasoning_text,
            re.IGNORECASE,
        )
        evidence_text = ""
        if evidence_match:
            evidence_text = evidence_match.group(1).strip()
        
        # 组合 Reason 和 Evidence
        result_parts = []
        if reason_text and len(reason_text) > 20:
            result_parts.append(reason_text)
        if evidence_text:
            result_parts.append("\n【Evidence】\n" + evidence_text)
        
        if result_parts:
            result = '\n'.join(result_parts)
            return result[:800] + ('...' if len(result) > 800 else '')
    
    # ⭐ 旧格式：提取【思考过程】部分（单张 WSI 模式使用）
    thinking_match = re.search(r'【思考过程】\s*([\s\S]*?)(?=【|$)', reasoning_text)
    if thinking_match:
        thinking_text = thinking_match.group(1).strip()
        # 取第一段（到第一个换行或200字符）
        first_para = thinking_text.split('\n')[0].strip()
        if len(first_para) > 20:
            return first_para[:200] + ('...' if len(first_para) > 200 else '')
    
    # 如果没有找到标准格式，提取有意义的描述
    paragraphs = [p.strip() for p in reasoning_text.split('\n') if p.strip() and not p.startswith('【')]
    meaningful_paragraphs = []
    for p in paragraphs:
        if any(kw in p for kw in ['细胞', '腺体', '核', '组织', '可见', '呈', '浸润', '观察', '区域', '证据', '性质', '置信度', '病理类型', '发现']):
            meaningful_paragraphs.append(p)
            if len('\n'.join(meaningful_paragraphs)) > 500:
                break
    
    if meaningful_paragraphs:
        result = '\n'.join(meaningful_paragraphs)
        return result[:600] + ('...' if len(result) > 600 else '')
    
    # 最后回退：返回前400字符
    clean_text = reasoning_text.replace('【判断】', '').replace('【思考过程】', '').replace('【选择结果】', '').replace('【Reason】', '').replace('【Plan】', '')
    return clean_text[:400].strip() + ('...' if len(clean_text) > 400 else '')


def _detect_pathology_type(reasoning_text):
    """
    从推理文本中检测病理类型
    
    Args:
        reasoning_text: 完整的推理文本
    
    Returns:
        str: 'malignant' / 'benign' / 'uncertain'
    """
    if not reasoning_text:
        return 'uncertain'
    
    malignant_keywords = ['癌', '恶性', '浸润', '侵犯', '异型明显', '核分裂活跃', '癌栓', '肿瘤细胞']
    benign_keywords = ['正常', '良性', '未见异常', '未见恶性', '无明显异型', '炎症']
    
    has_malignant = any(kw in reasoning_text for kw in malignant_keywords)
    has_benign = any(kw in reasoning_text for kw in benign_keywords)
    
    if has_malignant and not has_benign:
        return 'malignant'
    elif has_benign and not has_malignant:
        return 'benign'
    else:
        return 'uncertain'


def parse_structured_response(response, zoom_level):
    """
    解析GPT的结构化响应
    
    支持的格式：
    1. 新格式【Reason】+【Evidence】（MCP Loop 统一架构）
    2. 旧格式【思考过程】+【分析结论】（向后兼容）
    3. JSON格式（向后兼容）
    
    Args:
        response: GPT的原始响应文本
        zoom_level: 当前倍率（用于默认值）
    
    Returns:
        dict: {findings, confidence, confidence_level, pathology_type, key_features, thinking}
        
    置信度等级说明（5级李克特量表）：
        0 = 证据不足，无法判断（图像质量差/无组织）
        1 = 不确定，需要更多信息
        2 = 可能，存在鉴别诊断
        3 = 较确定，特征较明确
        4 = 确定，特征典型
    """
    import json
    import re
    
    # 置信度等级到百分比的映射
    CONFIDENCE_LEVEL_TO_PERCENT = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}
    
    # 默认值
    default = {
        'findings': '',
        'confidence': 50,           # 百分比（向后兼容）
        'confidence_level': 2,      # 5级量表
        'pathology_type': 'uncertain',
        'key_features': [],
        'thinking': response,  # 保存完整的思考过程
        # ⭐ V2 结构化记忆字段
        'evidence': {},             # {Q1_evidence: ..., ...} 每题独立、不截断
        'q_confidence': {},         # {Q1: 0-4, ...} 每题置信度
        'diagnosis_suggestion': '',
    }
    
    # ===== Method 1: parse 【Reason】 + 【Evidence / path summary】 =====
    reason_match = re.search(
        r'(?:【Reason】|\[Reason\])\s*([\s\S]*?)(?=【Plan】|\[Plan\]|【Evidence】|\[Evidence\]|【路径证据汇总】|\[Path Evidence Summary\]|【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
        response)
    evidence_match = re.search(r'【Evidence】\s*([\s\S]*?)(?=【Plan】|$)', response)
    path_evidence_match = re.search(
        r'(?:【路径证据汇总】|\[Path Evidence Summary\])\s*([\s\S]*?)(?=【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
        response)
    
    if reason_match:
        default['thinking'] = reason_match.group(1).strip()
    
    if evidence_match:
        evidence_text = evidence_match.group(1)
        
        # 从【Evidence】中提取各字段
        # evidence (新字段，映射到 findings)
        evidence_content_match = re.search(r'[-•]\s*evidence[：:]\s*(.+?)(?=\n[-•]|$)', evidence_text, re.IGNORECASE | re.DOTALL)
        if evidence_content_match:
            default['findings'] = evidence_content_match.group(1).strip()
        
        # confidence (新的5级量表，0-4)
        conf_match = re.search(r'[-•]\s*confidence[：:]\s*(\d)', evidence_text, re.IGNORECASE)
        if conf_match:
            level = min(4, max(0, int(conf_match.group(1))))
            default['confidence_level'] = level
            default['confidence'] = CONFIDENCE_LEVEL_TO_PERCENT[level]
        
        # pathology_type
        type_match = re.search(r'[-•]\s*pathology_type[：:]\s*(malignant|benign|uncertain)', evidence_text, re.IGNORECASE)
        if type_match:
            default['pathology_type'] = type_match.group(1).lower()
        
        # key_findings (新字段，映射到 key_features)
        findings_match = re.search(r'[-•]\s*key_findings[：:]\s*(.+?)(?=\n[-•]|$)', evidence_text, re.IGNORECASE | re.DOTALL)
        if findings_match:
            findings_str = findings_match.group(1).strip()
            if findings_str.startswith('[') and ']' in findings_str:
                features = re.findall(r'["\']([^"\']+)["\']', findings_str)
                if features:
                    default['key_features'] = features
                else:
                    default['key_features'] = [findings_str.strip('[]')]
            else:
                default['key_features'] = [findings_str] if findings_str else []
        
        if default['findings']:
            return default
    
    # ===== 方法1b: 解析新格式【路径证据汇总】/[Path Evidence Summary]（绿框新版输出）=====
    # 按每个问题切块，保留每题证据和每题置信度。
    if path_evidence_match:
        pe_text = path_evidence_match.group(1).strip()
        _FINDINGS_CAP = 2000  # 放宽上限, 避免丢失靠后的 Q5/Q6
        q_evidence_struct = {}   # {Q1_evidence: ..., ...}
        q_confidence_struct = {} # {Q1: 3, Q2: 2, ...}
        _markers = list(re.finditer(r'Q([1-9])_evidence[：:]\s*[（(]?', pe_text, re.IGNORECASE))
        for _i, _m in enumerate(_markers):
            _qn = _m.group(1)
            _seg_start = _m.end()
            _seg_end = _markers[_i + 1].start() if _i + 1 < len(_markers) else len(pe_text)
            _seg = pe_text[_seg_start:_seg_end]
            # 该题块内紧邻的 confidence (每题独立保留, 不再取全局 max)
            _cm = re.search(r'confidence[：:]\s*["\']?(\d)', _seg, re.IGNORECASE)
            if _cm:
                q_confidence_struct[f'Q{_qn}'] = min(4, max(0, int(_cm.group(1))))
                _ev_text = _seg[:_cm.start()]
            else:
                _ev_text = _seg
            _ev_text = re.sub(r'^\s*[-•]\s*', '', _ev_text.strip()).rstrip(')-） ').strip()
            if _ev_text:
                q_evidence_struct[f'Q{_qn}_evidence'] = _ev_text
        
        if q_evidence_struct:
            default['evidence'] = q_evidence_struct
            default['q_confidence'] = q_confidence_struct
            default['findings'] = ' | '.join(q_evidence_struct.values())[:_FINDINGS_CAP]
        elif pe_text:
            default['findings'] = pe_text[:_FINDINGS_CAP]
        
        # 总体 confidence 仍取每题最大值 (向后兼容聚合加权), 但每题值已单独留在 q_confidence
        if q_confidence_struct:
            max_conf = min(4, max(q_confidence_struct.values()))
            default['confidence_level'] = max_conf
            default['confidence'] = CONFIDENCE_LEVEL_TO_PERCENT[max_conf]
        else:
            conf_values = re.findall(r'confidence[：:]\s*["\']?(\d)', pe_text, re.IGNORECASE)
            if conf_values:
                max_conf = min(4, max(0, max(int(c) for c in conf_values)))
                default['confidence_level'] = max_conf
                default['confidence'] = CONFIDENCE_LEVEL_TO_PERCENT[max_conf]
        
        diag_match = re.search(r'(?:【诊断建议】|\[Diagnostic [Ii]mpression\])\s*([\s\S]*?)$', response)
        if diag_match:
            diag_text = diag_match.group(1).strip()
            if diag_text:
                default['diagnosis_suggestion'] = diag_text
                default['findings'] = ((default.get('findings', '') + ' | ' + diag_text)[:_FINDINGS_CAP]
                                       if default.get('findings') else diag_text[:_FINDINGS_CAP])
        
        if default['findings']:
            return default
    
    # ===== 方法2: 解析旧格式【思考过程】+【分析结论】（向后兼容）=====
    thinking_match = re.search(r'【思考过程】\s*([\s\S]*?)(?=【分析结论】|$)', response)
    conclusion_match = re.search(r'【分析结论】\s*([\s\S]*?)$', response)
    
    if thinking_match:
        default['thinking'] = thinking_match.group(1).strip()
    
    if conclusion_match:
        conclusion_text = conclusion_match.group(1)
        
        # 从【分析结论】中提取各字段
        # findings
        findings_match = re.search(r'[-•]\s*findings[：:]\s*(.+?)(?=\n[-•]|$)', conclusion_text, re.IGNORECASE)
        if findings_match:
            default['findings'] = findings_match.group(1).strip()
        
        # ⭐ 优先解析 confidence_level（旧的5级量表）
        level_match = re.search(r'[-•]\s*confidence_level[：:]\s*(\d)', conclusion_text, re.IGNORECASE)
        if level_match:
            level = min(4, max(0, int(level_match.group(1))))
            default['confidence_level'] = level
            default['confidence'] = CONFIDENCE_LEVEL_TO_PERCENT[level]
        else:
            # 兼容更旧格式：解析 confidence (0-100)
            confidence_match = re.search(r'[-•]\s*confidence[：:]\s*(\d+)', conclusion_text, re.IGNORECASE)
            if confidence_match:
                conf_value = min(100, max(0, int(confidence_match.group(1))))
                default['confidence'] = conf_value
                # 将百分比反向映射到等级
                if conf_value <= 10:
                    default['confidence_level'] = 0
                elif conf_value <= 35:
                    default['confidence_level'] = 1
                elif conf_value <= 60:
                    default['confidence_level'] = 2
                elif conf_value <= 85:
                    default['confidence_level'] = 3
                else:
                    default['confidence_level'] = 4
        
        # pathology_type
        type_match = re.search(r'[-•]\s*pathology_type[：:]\s*(malignant|benign|uncertain)', conclusion_text, re.IGNORECASE)
        if type_match:
            default['pathology_type'] = type_match.group(1).lower()
        
        # key_features
        features_match = re.search(r'[-•]\s*key_features[：:]\s*\[([^\]]+)\]', conclusion_text, re.IGNORECASE)
        if features_match:
            features_str = features_match.group(1)
            # 解析特征列表
            features = re.findall(r'["\']([^"\']+)["\']', features_str)
            if features:
                default['key_features'] = features
            else:
                # 尝试用逗号分隔
                default['key_features'] = [f.strip().strip('"\'') for f in features_str.split(',') if f.strip()]
        
        # 如果成功解析了结论，返回结果
        if default['findings']:
            return default
    
    # ===== 方法2: 兼容旧的JSON格式 =====
    try:
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            json_str = json_match.group()
            parsed = json.loads(json_str)
            
            conf_value = min(100, max(0, int(parsed.get('confidence', 50))))
            # 将百分比反向映射到等级
            if conf_value <= 10:
                conf_level = 0
            elif conf_value <= 35:
                conf_level = 1
            elif conf_value <= 60:
                conf_level = 2
            elif conf_value <= 85:
                conf_level = 3
            else:
                conf_level = 4
            
            return {
                'findings': parsed.get('findings', response),
                'confidence': conf_value,
                'confidence_level': parsed.get('confidence_level', conf_level),
                'pathology_type': parsed.get('pathology_type', 'uncertain'),
                'key_features': parsed.get('key_features', []),
                'thinking': response
            }
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.debug(f"JSON解析失败({zoom_level}x): {e}")
    
    # ===== 方法3: 从自由文本中提取关键信息 =====
    # 如果没有结构化输出，使用整个响应作为 findings
    if not default['findings']:
        # 尝试提取第一段有意义的描述
        paragraphs = [p.strip() for p in response.split('\n') if p.strip() and not p.startswith('【')]
        if paragraphs:
            # 找到第一个包含形态学描述的段落
            for p in paragraphs:
                if any(kw in p for kw in ['细胞', '腺体', '核', '组织', '可见', '呈', '浸润']):
                    default['findings'] = p[:200]  # 限制长度
                    break
            if not default['findings']:
                default['findings'] = paragraphs[0][:200]
    
    # 检测恶性/良性关键词来推断 pathology_type 和 confidence_level
    malignant_keywords = ['癌', '恶性', '浸润', '侵犯', '异型明显', '核分裂活跃', '癌栓']
    benign_keywords = ['正常', '良性', '增生', '炎症', '未见异常', '未见恶性']
    
    has_malignant = any(kw in response for kw in malignant_keywords)
    has_benign = any(kw in response for kw in benign_keywords)
    
    if has_malignant and not has_benign:
        default['pathology_type'] = 'malignant'
        default['confidence'] = 75
        default['confidence_level'] = 3  # 较确定
    elif has_benign and not has_malignant:
        default['pathology_type'] = 'benign'
        default['confidence'] = 75
        default['confidence_level'] = 3  # 较确定
    
    return default


def analyze_roi_parallel(slide, roi, zoom_level, save_dir=None, parent_context=None, 
                         lang='zh',
                         agent_state=None):
    """
    并行分析单个ROI（支持上下文传递）
    
    Args:
        slide: OpenSlide对象
        roi: ROI信息字典
        zoom_level: 放大倍率
        save_dir: 保存目录路径
        parent_context: 父级分析结果上下文（用于Context Passing）
    
    Returns:
        dict: {'roi_id', 'zoom_level', 'findings', 'confidence', 'pathology_type', 'key_features', 'image_path', 'parent_id', 'rect'}
    """
    try:
        # 提取图像
        roi_image, actual_rect = extract_roi_at_zoom_level(slide, roi, zoom_level)
        
        # 保存图像到磁盘
        image_path = None
        if save_dir:
            from pathlib import Path
            save_dir = Path(save_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            image_filename = f"{roi['id']}_{zoom_level}x.jpg"
            image_path = save_dir / image_filename
            roi_image.save(image_path, 'JPEG', quality=90)
            logger.debug(f"已保存ROI图像: {image_path}")
        
        # 构建Prompt
        buffer = io.BytesIO()
        if roi_image.size[0] > 2000 or roi_image.size[1] > 2000:
            roi_image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        roi_image.save(buffer, format='JPEG', quality=95)
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        # 构建上下文（保留Context Passing功能）
        context_text = ""
        if parent_context and zoom_level > 10:
            context_text = build_context_prompt(zoom_level, parent_context, agent_state=agent_state, lang=lang)
        
        # ⭐ 调试：打印上下文长度和内容摘要（特别关注绿框160x）
        if zoom_level == 160 and context_text:
            context_chars = len(context_text)
            context_tokens_estimate = context_chars // 2  # 中文大约2字符=1token
            print(f"🟢 [Context Debug] {roi['id']}: 上下文长度 = {context_chars} 字符 ≈ {context_tokens_estimate} tokens")
            logger.info(f"🟢 [Context Debug] {roi['id']}: 上下文长度 = {context_chars} 字符 ≈ {context_tokens_estimate} tokens")
            # ⭐ 打印上下文内容的前500字符，便于检查是否正确传递
            print(f"🟢 [Context Content] {roi['id']}:\n{context_text[:500]}...")
            logger.info(f"🟢 [Context Content] {roi['id']}:\n{context_text[:500]}...")
        
        if lang == 'en':
            # ========== Single-WSI English: context-aware evidence collection ==========
            prompt = f"""You are **PathCopilot SubAgent** executing the **final step** of the MCP Loop.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 **Current location**: 160× high-power (path terminal) | ROI: {roi['id']} | Parent: {roi.get('parent_id', 'unknown')}
🔚 **This is the terminal node of the current analysis path.** Complete your observation and summarize evidence for the Orchestrator.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_text}
## 🎯 Your Task (two steps)

**You are the final executor of this analysis path (Thumbnail → Red 10× → Blue 40× → current Green 160×).**

1. **Observe**: Collect cytological evidence from the current 160× field
2. **Summarize**: Integrate findings from the full path context (10×, 40×) above to produce a consolidated evidence summary for the Orchestrator

**Key principles**:
- Only report evidence you **actually observe** — do NOT speculate on what this view cannot address
- This run targets ONE diagnostic objective (see above); if this path has no evidence relevant to it, state that briefly rather than forcing a conclusion

## 🔬 160× Observation Points

Using your professional judgment, observe and describe the cytological and architectural features that are actually visible in this field AND relevant to the diagnostic objective above, collecting evidence to answer it.
Be objective and conservative — report only what you can directly observe in this field, do not over-call, and use a "suspicious / indeterminate" level when the evidence is insufficient.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 Output format (follow strictly)

[Reason]
(Describe morphological features actually observed at 160× in this field)

[Path Evidence Summary]
Integrate the upstream path context (10×→40× observations) with your current 160× findings, and summarize the evidence this path contributes to the diagnostic objective below.

Diagnostic objective:
{CLINICAL_QUESTIONS_EN}

Report:
- evidence: (consolidated evidence across all magnifications on this path, not just 160×; if this path provides no relevant evidence for the objective, state so)
- confidence: output as "N (label)" where N is one of:
  · 0 (insufficient evidence, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features are fairly characteristic)
  · 4 (definitive, features are typical and unambiguous)

[Diagnostic impression]
(One sentence: what does this path as a whole support? What key evidence does it contribute to the Orchestrator's final integration?)
"""
        else:
            # ========== 单WSI中文：上下文感知的证据收集提示词 ==========
            prompt = f"""你是 **PathCopilot SubAgent**，正在执行 **MCP Loop** 的最后一步。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 **当前位置**：160× 高倍镜（路径终点）| ROI: {roi['id']} | 父区域: {roi.get('parent_id', '未知')}
🔚 **这是当前分析路径的终点**。你需要完成观察并为 Orchestrator 汇总本路径的证据。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_text}
## 🎯 你的任务（两步）

**你是这条分析路径（缩略图 → 红框10× → 蓝框40× → 当前绿框160×）的最终执行者。**

1. **观察**：在 160× 高倍镜下观察当前视野，收集细胞学证据
2. **汇总**：结合上方路径上下文（10×、40× 的分析），为 Orchestrator 整合本路径的证据结论

**重要原则**：
- 只报告你**确实观察到**的发现，不要推测当前视野无法判断的内容
- 本轮只聚焦一个诊断目标（见上）；若本路径对该目标没有相关证据，简要注明即可，不要强行下结论


## 🔬 160× 高倍镜观察要点

请基于你的专业判断，针对上述诊断目标，观察并描述当前视野中实际可见、且与该目标相关的细胞学与组织结构特征，为作答收集证据。
请保持客观保守——只报告当前视野能直接观察到的内容，不要过度判读；证据不足时使用"可疑/不确定"级别。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 输出格式（严格按此格式输出）

【Reason】
（描述你在 160× 高倍镜下**实际观察到**的形态学特征）

【路径证据汇总】
结合上方路径上下文（10×→40×观察）和你当前 160× 的发现，汇总本路径为下述诊断目标提供的证据：

诊断目标：
{CLINICAL_QUESTIONS}

请输出：
- evidence:（整合本路径各倍率的证据，而非仅 160× 当前视野；若本路径对该目标无相关证据，则注明"本路径未提供直接证据"）
- confidence: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)

【诊断建议】
（一句话总结：本路径整体支持什么判断？为 Orchestrator 的最终整合提供了哪些关键证据？）
"""
        
        response = call_gpt_vision(prompt, image_base64)
        
        # ⭐ 解析完整的 Reason 响应（【思考过程】+【分析结论】）
        parsed = parse_structured_response(response, zoom_level)
        
        # 160× green box: structured evidence + diagnosis_suggestion
        evidence = {}
        diagnosis_suggestion = ""
        if zoom_level == 160:
            _pe_match = re.search(
                r'(?:【路径证据汇总】|\[Path Evidence Summary\])\s*([\s\S]*?)(?=【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
                response)
            if _pe_match:
                _pe_text = _pe_match.group(1)
                for _qn in range(1, 10):
                    _qm = re.search(rf'Q{_qn}_evidence[：:]\s*([\s\S]*?)(?=Q\d_evidence|[-•]\s*confidence|\n\n|$)', _pe_text, re.IGNORECASE)
                    if _qm:
                        evidence[f'Q{_qn}_evidence'] = _qm.group(1).strip()
                _cm = re.search(r'confidence[：:]\s*(\d)', _pe_text, re.IGNORECASE)
                if _cm:
                    parsed['confidence'] = int(_cm.group(1)) / 4.0 * 100
            _diag_match = re.search(r'(?:【诊断建议】|\[Diagnostic [Ii]mpression\])\s*([\s\S]*?)$', response)
            if _diag_match:
                diagnosis_suggestion = _diag_match.group(1).strip()
        
        # ⭐ V2: 优先采用 parse_structured_response 的结构化结果(每题证据+每题置信度),
        #   本地正则仅作回退, 保证 Memory 树节点带上 q_confidence。
        evidence = parsed.get('evidence') or evidence
        q_confidence = parsed.get('q_confidence', {})
        diagnosis_suggestion = parsed.get('diagnosis_suggestion') or diagnosis_suggestion
        
        return {
            'roi_id': roi['id'],
            'zoom_level': zoom_level,
            'success': True,
            'findings': parsed['findings'],
            'confidence': parsed['confidence'],
            'pathology_type': parsed['pathology_type'],
            'key_features': parsed['key_features'],
            'image_path': str(image_path) if image_path else None,
            'parent_id': roi.get('parent_id'),
            'rect': roi['rect'],
            'reasoning': response,
            'thinking': parsed.get('thinking', response),
            'evidence': evidence,
            'q_confidence': q_confidence,
            'diagnosis_suggestion': diagnosis_suggestion,
            'context_used': context_text
        }
    except Exception as e:
        logger.error(f"并行分析失败: {e}")
        return {
            'roi_id': roi.get('id', 'unknown'),
            'zoom_level': zoom_level,
            'success': False,
            'findings': f"分析失败: {str(e)}",
            'confidence': 0,
            'pathology_type': 'uncertain',
            'key_features': [],
            'image_path': None,
            'parent_id': roi.get('parent_id'),
            'rect': roi.get('rect'),
            'reasoning': f"分析失败: {str(e)}",
            'evidence': {},
            'diagnosis_suggestion': '',
            'context_used': ''
        }

def execute_multiscale_roi_analysis(socketio, room, slide, roi_info, step, file_id, steps_dir, agent_state):
    """
    执行多尺度循证分析（并行版本）：红框 → 蓝框 → 绿框一次完成。
    """
    try:
        import concurrent.futures
        
        logger.info(f"\n{'='*80}")
        logger.info(f"🔍 开始分析红框 {roi_info['id']}，解读如下：")
        logger.info(f"{'='*80}")
        
        # 发送到前端（包含rect用于点击跳转）- 进度消息，不需要确认
        socketio.emit('agent.message', {
            'type': 'assistant',
            'content': _msg('start_analyze_red', agent_state.get('lang', 'zh'), roi_id=roi_info['id']),
            'level': 'red',
            'roi_id': roi_info['id'],
            'rect': roi_info['rect'],  # ⭐ 添加rect用于双向跳转
            'is_progress': True  # ⭐ 标记为进度消息，前端不创建确认卡片
        }, to=room)
        
        # === 第1步：细分为4×4蓝框(16个)并由AI筛选 ===
        # ⭐ MCP关键：蓝框选择的AI响应同时包含【红框分析】和【蓝框选择】
        logger.info(f"父红框 {roi_info['id']} 坐标: x={roi_info['rect']['x']}, y={roi_info['rect']['y']}, w={roi_info['rect']['w']}, h={roi_info['rect']['h']}")
        # 4x4网格 = 16个蓝框候选
        blue_rois = subdivide_roi_into_grid(roi_info, grid_rows=4, grid_cols=4, child_zoom_level=40)
        logger.info(f"红框 {roi_info['id']} → 生成16个蓝框（4×4网格），AI将根据复杂度选择")
        
        # 创建保存目录
        roi_save_dir = steps_dir / roi_info['id']
        
        # ⭐ MCP模式：AI看到红框的蓝框网格图，一个回复中同时给出分析和选择
        # 【思考过程】就是红框分析结果，【选择结果】就是蓝框选择
        logger.info(f"📍 发送蓝框选择请求（红框{roi_info['id']}）...")
        wsi_name = agent_state.get('filename')  # 获取WSI文件名用于调试图片目录
        
        # 设置parent_roi的frame_type为blue，以便使用正确的提示词
        roi_info_for_blue = roi_info.copy()
        roi_info_for_blue['frame_type'] = 'blue'
        
        blue_result = filter_rois_by_gpt_vision(
            slide, blue_rois, (2000, 2000), None, roi_info_for_blue, wsi_name, 
            return_analysis=True, agent_state=agent_state
        )

        def _replan_red(replanning_context):
            logger.info(
                f"🔄 [MCP] 红框 {roi_info['id']}: 回注"
                f"{replanning_context.get('kind', '')}上下文后重新规划一次"
            )
            return filter_rois_by_gpt_vision(
                slide, blue_rois, (2000, 2000), None, roi_info_for_blue, wsi_name, 
                return_analysis=True,
                agent_state=agent_state,
                replanning_context=replanning_context,
            )

        blue_result, mcp_action_result, red_tool_trace = resolve_mcp_action_with_one_replan(
            initial_result=blue_result,
            roi_info=roi_info,
            agent_state=agent_state,
            replan_callback=_replan_red,
            socketio=socketio,
            room=room,
        )
        
        # 根据 MCP 动作决定下一步
        if mcp_action_result['action'] == 'backtrack':
            logger.warning(f"⏪ [MCP] 红框 {roi_info['id']} 触发回溯: {mcp_action_result['reason']}")
            return {
                'finding': f"回溯: {mcp_action_result['reason']}",
                'confidence': 0,
                'next_action': 'backtrack',
                'tools_results': [],
                'tree_nodes': [],
                'backtrack_reason': mcp_action_result['reason']
            }
        
        if mcp_action_result['action'] == 'stop':
            logger.info(f"⏹️ [MCP] 红框 {roi_info['id']} 停止下钻: {mcp_action_result['reason']}")
            selected_blue_rois = []
        else:
            selected_blue_rois = mcp_action_result['selected_rois']
        
        red_analysis_text = blue_result['analysis']  # ⭐ 这就是红框分析结果（AI的【思考过程】）
        
        logger.info(f"✅ 蓝框选择完成: {len(blue_rois)} → {len(selected_blue_rois)}")
        logger.info(f"\n💭 [AI观察 - 红框{roi_info['id']}]")
        logger.info(red_analysis_text[:500] + "..." if len(red_analysis_text) > 500 else red_analysis_text)
        
        # ⭐ MCP关键：从蓝框选择响应构建红框分析结果
        # 响应中的【思考过程】就是红框分析，【选择结果】就是蓝框选择
        # ⭐ 解析AI响应为结构化数据（保留原始文本，同时提取思考过程和选择结果）
        def parse_ai_response(text):
            """解析AI响应，提取思考过程和选择结果"""
            thinking = ""
            selection = ""
            
            # 提取【思考过程】内容
            thinking_match = re.search(r'【思考过程】\s*(.*?)(?=【|$)', text, re.DOTALL)
            if thinking_match:
                thinking = thinking_match.group(1).strip()
            
            # 提取【选择结果】内容
            selection_match = re.search(r'【选择结果】\s*(.*?)$', text, re.DOTALL)
            if selection_match:
                selection = selection_match.group(1).strip()
            
            return {
                'thinking': thinking,
                'selection': selection,
                'raw': text
            }
        
        parsed = parse_ai_response(red_analysis_text)
        
        # ⭐⭐⭐ 核心修复：区分"选择理由"和"形态学分析"
        # - selection_reasoning: AI为什么选择这些蓝框的理由（用于显示）
        # - findings: 应该是形态学分析（MCP模式下红框没有单独分析，设为简短摘要）
        # ⭐ 提取关键信息作为红框findings（而不是完整的选择理由）
        red_finding_summary = ''
        if parsed['thinking']:
            # 从思考过程中提取第一句话作为摘要
            first_sentence = parsed['thinking'].split('。')[0] if '。' in parsed['thinking'] else parsed['thinking'][:100]
            # 过滤掉"区域X、Y、Z"这种选择格式的内容
            if not any(kw in first_sentence for kw in ['区域', '编号', '选择', '我选择']):
                red_finding_summary = first_sentence + '。' if not first_sentence.endswith('。') else first_sentence
            else:
                red_finding_summary = f'AI正在分析红框 {roi_info["id"]}，已选择 {len(selected_blue_rois)} 个蓝框进行详细分析'
        else:
            # English benchmark outputs use [Reason]/[Plan]/[Evidence].  Keep
            # the actual morphology instead of replacing a stopped leaf with
            # a synthetic "selected 0 boxes" status sentence.
            red_finding_summary = _extract_findings_from_reasoning(red_analysis_text)

        red_runtime_action = _effective_action_letter(mcp_action_result)
        
        red_analysis = {
            'findings': red_finding_summary,  # ⭐ 简短摘要，不是完整的选择理由
            'selection_reasoning': parsed['thinking'],  # ⭐ AI选择蓝框的完整理由（用于调试/报告）
            'reasoning': red_analysis_text,  # 完整AI推理过程（用于调试）
            'thinking': parsed['thinking'],  # 思考过程
            'selection': parsed['selection'],  # 选择结果
            'confidence': 75,  # 默认置信度
            'pathology_type': 'analysis',
            'key_features': [],
            'image_path': None,  # 可后续补充
            'plan_action': blue_result.get('mcp_action', ''),
            'selected_indices': list(blue_result.get('selection_indices') or []),
            'runtime_action': red_runtime_action,
            'mcp_tool_trace': red_tool_trace,
        }

        # Make completed 10× evidence available to other red paths that may
        # subsequently request E(neighbor).  The spatial helper is lock-safe.
        update_spatial_index_findings(
            roi_info['id'],
            red_analysis.get('reasoning') or red_analysis.get('findings', ''),
            agent_state,
        )
        
        # ⭐ 先发送蓝框overlays到前端（让图上立即显示蓝框）
        temp_blue_overlays = [create_roi_overlay(roi) for roi in selected_blue_rois]
        
        # ⭐ 关键调试日志：检查蓝框发送
        logger.info(f"📦 [蓝框发送] 红框{roi_info['id']}: selected_blue_rois={len(selected_blue_rois)}个")
        for i, roi in enumerate(selected_blue_rois):
            logger.info(f"    蓝框{i+1}: id={roi.get('id')}, grid_id={roi.get('grid_id')}, rect={roi.get('rect')}")
        logger.info(f"📦 [蓝框发送] 创建了{len(temp_blue_overlays)}个overlays")
        
        overlays_lock = agent_state.get('overlays_lock')
        if overlays_lock:
            with overlays_lock:
                current_overlays = agent_state.get('all_overlays', []).copy()
                current_overlays.extend(temp_blue_overlays)
        else:
            current_overlays = agent_state.get('all_overlays', []).copy()
            current_overlays.extend(temp_blue_overlays)
        
        logger.info(f"📦 [蓝框发送] 发送到前端的total overlays: {len(current_overlays)}个")
        
        socketio.emit('agent.viewport', {
            'type': 'viewport',
            'step': f"{step}.1-preview",
            'overlays': current_overlays,
            'why': f'预览：红框#{step}选中{len(selected_blue_rois)}个蓝框'
        }, to=room)
        
        # 发送蓝框选取完成消息
        # ⭐ Bug修复：包含蓝框的regions数据，用于前端显示和点击聚焦
        blue_regions_info = []
        for i, blue_roi in enumerate(selected_blue_rois, 1):
            blue_regions_info.append({
                'index': i,
                'roi_id': blue_roi['id'],
                'rect': blue_roi['rect'],
                'frame_type': 'blue'
            })
        
        socketio.emit('agent.message', {
            'type': 'system',
            'content': f"✅ {roi_info['id']} 蓝框选取完成: 已选择 {len(selected_blue_rois)} 个蓝框 (40×)",
            'level': 'blue',
            'roi_id': roi_info['id'],
            'rect': roi_info['rect'],
            'regions': blue_regions_info,
            'metadata': {
                'action': 'selected',
                'count': len(selected_blue_rois),
                'parent_id': roi_info['id'],
            }
        }, to=room)
        
        # 发送红框分析结果到前端（用于更新左侧分析树）
        socketio.emit('agent.red_analysis', {
            'roi_id': roi_info['id'],
            'rect': roi_info['rect'],  # ⭐ 红框坐标（用于点击跳转）
            'observation': red_analysis_text,  # AI 的观察结果
            'confidence': red_analysis.get('confidence', 0.5),
            'blue_count': len(selected_blue_rois),
            'blue_ids': [b['id'] for b in selected_blue_rois],
            'blue_rects': [{'id': b['id'], 'rect': b['rect']} for b in selected_blue_rois]  # ⭐ 蓝框坐标
        }, to=room)
        socketio.sleep(0)  # ⭐⭐⭐ 关键：强制立即发送（解决并行场景下消息缓冲问题）
        
        logger.info(f"✅ 红框 {roi_info['id']} 分析完成 + agent.red_analysis 已发送")
        
        
        # ========== 构建红框上下文 (Context Passing - 完整版) ==========
        # ⭐ 传递完整的 Reason + Plan + Action，而不是截断的 findings
        red_context = {
            'red_id': roi_info['id'],
            # Reason: 完整的 AI 思考过程
            'red_reasoning': red_analysis.get('reasoning', red_analysis.get('thinking', '')),
            # Plan: AI 的选择理由
            'red_selection_reasoning': red_analysis.get('selection_reasoning', ''),
            # Action: 选择的蓝框列表
            'red_selected_blues': [roi['id'] for roi in selected_blue_rois],
            # 保留 findings 用于兼容和简短显示
            'red_finding': red_analysis.get('findings', '')
        }
        logger.info(f"📝 构建红框上下文（完整版：Reason+Plan+Action）")
        
        # 发送红框分析结果到左侧面板（层级显示）
        socketio.emit('agent.message', {
            'type': 'assistant',
            'content': f"🔴 红框分析结果：\n{red_analysis['findings']}",
            'reasoning': red_analysis.get('reasoning', red_analysis.get('findings', '')),  # ⭐ AI思考过程
            'level': 'red',
            'roi_id': roi_info['id'],
            'rect': roi_info['rect'],
            'is_result': True
        }, to=room)
            
        logger.info(f"🎯 蓝框选择完成，继续进行下一步")
        
        # ⭐ 为选中的蓝框分配简单ID (R2-B1, R2-B2...)，使用红框ID作为前缀
        assign_simple_roi_ids(selected_blue_rois, 'B', agent_state, parent_id=roi_info['id'])
        logger.info(f"🔵 蓝框ID分配: {[roi['id'] for roi in selected_blue_rois]}")
        
        # ⭐ 发送蓝框overlays（追加到全局overlays，累积显示，线程安全）
        blue_overlays = [create_roi_overlay(roi) for roi in selected_blue_rois]
        
        # 使用锁保护 all_overlays 的并发访问
        overlays_lock = agent_state.get('overlays_lock')
        if overlays_lock:
            with overlays_lock:
                agent_state.setdefault('all_overlays', [])
                before_count = len(agent_state['all_overlays'])
                agent_state['all_overlays'].extend(blue_overlays)
                after_count = len(agent_state['all_overlays'])
                # 复制当前的 overlays 用于发送
                current_overlays = agent_state['all_overlays'].copy()
        else:
            # 降级处理（没有锁时）
            agent_state.setdefault('all_overlays', [])
            before_count = len(agent_state['all_overlays'])
            agent_state['all_overlays'].extend(blue_overlays)
            after_count = len(agent_state['all_overlays'])
            current_overlays = agent_state['all_overlays'].copy()
        
        logger.info(f"📊 蓝框累积：之前{before_count}个框 + 新增{len(blue_overlays)}个蓝框 = 现在{after_count}个框")
        
        # 发送所有已存在的框：10红 + 已分析蓝 + 已分析绿
        socketio.emit('agent.viewport', {
            'type': 'viewport',
            'step': f"{step}.2",
            'overlays': current_overlays,  # 发送全部累积的框
            'why': f'红框{roi_info["id"]}细分: {len(blue_rois)}蓝框→AI选{len(selected_blue_rois)}个（累积显示：{after_count}个框）'
        }, to=room)
        
        # === 第2步：细分蓝框为绿框并筛选 + 并行分析所有蓝框 ===
        logger.info(f"\n{'─'*80}")
        logger.info(f"⚡ 启动并行任务：【分析{len(selected_blue_rois)}个蓝框】 | 【AI筛选绿框】")
        
        # 当前 10× 观察已经完成。空子区域只终止继续下钻，
        # 不应删除已收集的当前层 Reason / Plan / Evidence。
        if not selected_blue_rois:
            logger.info(f"⏹️ 红框 {roi_info['id']} 已完成 10× 观察，无需生成蓝框子节点")
            red_leaf = {
                'roi_id': roi_info['id'],
                'level': 'red',
                'zoom_level': 10,
                'analysis': red_analysis,
                'children': []
            }
            return {
                'finding': red_analysis.get('findings', ''),
                'confidence': red_analysis.get('confidence', 0.5),
                'next_action': 'stop' if mcp_action_result['action'] == 'stop' else 'move_to_next_roi',
                'tools_results': [],
                'tree_nodes': [red_leaf]
            }
        
        # 蓝框的 Reason + Plan 直接通过 get_green_selection_prompt 完成
        try:
            blue_analyses = []
            green_filter_results = []
            skipped_count = 0
            
            logger.info(f"\n{'─'*40}")
            logger.info(f"🔵 执行蓝框 Reason + Plan（共{len(selected_blue_rois)}个蓝框）")
            logger.info(f"{'─'*40}")
            
            # 注意：红框是并行分析的，不在这里发送蓝框进度（会被其他线程覆盖）
            
            for blue_idx, blue_roi in enumerate(selected_blue_rois, 1):
                logger.info(f"  🔵 蓝框 #{blue_idx} ({blue_roi['id']}): 执行 Reason + Plan")
                
                # 生成 16 个绿框候选
                green_rois = subdivide_roi_into_grid(blue_roi, grid_rows=4, grid_cols=4, child_zoom_level=160)
                logger.info(f"  ├─ 蓝框 {blue_roi['id']} → 生成16个绿框候选")
                
                # ⭐ 设置 frame_type='green'，使用 get_green_selection_prompt
                blue_roi_for_green = blue_roi.copy()
                blue_roi_for_green['frame_type'] = 'green'
                
                # ⭐ 一次 API 调用完成：Reason (分析40x蓝框) + Plan (选择绿框)
                # 注意：这个函数会自动保存带网格线的 debug 图片到 debug_images/{wsi_name}/
                green_result = filter_rois_by_gpt_vision(
                    slide, green_rois, (2000, 2000), None, blue_roi_for_green, wsi_name, 
                    return_analysis=True, agent_state=agent_state
                )

                # 40× uses the same A–E executor as 10×.  Neighbor evidence
                # is computed from completed sibling blue boxes inside this red
                # path and is never stored in a shared pending-context slot.
                blue_neighbor_context = get_completed_sibling_context(
                    blue_roi,
                    selected_blue_rois,
                    blue_analyses,
                    max_neighbors=3,
                )

                def _replan_blue(replanning_context):
                    logger.info(
                        f"🔄 [MCP] 蓝框 {blue_roi['id']}: 回注"
                        f"{replanning_context.get('kind', '')}上下文后重新规划一次"
                    )
                    return filter_rois_by_gpt_vision(
                        slide,
                        green_rois,
                        (2000, 2000),
                        None,
                        blue_roi_for_green,
                        wsi_name,
                        return_analysis=True,
                        agent_state=agent_state,
                        replanning_context=replanning_context,
                    )

                green_result, green_action_result, blue_tool_trace = resolve_mcp_action_with_one_replan(
                    initial_result=green_result,
                    roi_info=blue_roi,
                    agent_state=agent_state,
                    replan_callback=_replan_blue,
                    socketio=socketio,
                    room=room,
                    neighbor_context_override=blue_neighbor_context,
                )

                if green_action_result['action'] == 'backtrack':
                    logger.warning(
                        f"⏪ [MCP] 蓝框 {blue_roi['id']} 触发回溯，"
                        "不写入诊断树，继续其他同级蓝框"
                    )
                    skipped_count += 1
                    continue

                if green_action_result['action'] == 'stop':
                    selected_greens = []
                else:
                    selected_greens = list(green_action_result.get('selected_rois') or [])
                
                # ⭐ 从 debug_images 目录查找带网格线的蓝框图片（filter_rois_by_gpt_vision 已生成）
                blue_image_path = None
                try:
                    from pathlib import Path
                    if wsi_name:
                        sample_name = Path(wsi_name).stem
                        debug_dir = Path("debug_images") / sample_name
                        if debug_dir.exists():
                            pattern = f"debug_gpt_{blue_roi['id']}_*candidates_*.jpg"
                            matches = list(debug_dir.glob(pattern))
                            if matches:
                                matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                                blue_image_path = str(matches[0])
                                logger.debug(f"  ├─ 找到蓝框网格图: {blue_image_path}")
                except Exception as e:
                    logger.warning(f"  ├─ 查找蓝框图片失败: {e}")
                
                # 解析返回结果
                green_reasoning = green_result.get('analysis', '') if isinstance(green_result, dict) else ''
                
                # ⭐ 构建蓝框分析结果
                blue_analysis = {
                    'roi_id': blue_roi['id'],
                    'zoom_level': 40,
                    'rect': blue_roi['rect'],
                    'parent_id': blue_roi.get('parent_id'),
                    'image_path': str(blue_image_path) if blue_image_path else None,  # ⭐ 蓝框网格图路径
                    'reasoning': green_reasoning,
                    'findings': _extract_findings_from_reasoning(green_reasoning),
                    'green_selection_reasoning': green_reasoning,
                    'confidence': 75,
                    'pathology_type': _detect_pathology_type(green_reasoning),
                    'key_features': [],
                    'no_green_needed': len(selected_greens) == 0,
                    'plan_action': green_result.get('mcp_action', ''),
                    'selected_indices': list(green_result.get('selection_indices') or []),
                    'runtime_action': _effective_action_letter(green_action_result),
                    'mcp_tool_trace': blue_tool_trace,
                }
                
                blue_analyses.append(blue_analysis)
                green_filter_results.append(selected_greens)
                
                # ⭐ 发送蓝框分析结果到前端（包含完整的 Reason/Plan 格式）
                socketio.emit('agent.blue_analysis', {
                    'parent_roi_id': roi_info['id'],
                    'blue_id': blue_roi['id'],
                    'blue_index': blue_idx,
                    'rect': blue_roi['rect'],
                    'observation': blue_analysis['reasoning'],  # ⭐ 发送完整的 reasoning（包含 Reason/Plan）
                    'findings': blue_analysis['findings'],      # ⭐ 同时保留简短摘要
                    'confidence': blue_analysis['confidence'],
                    'status': 'completed',
                    'no_green_needed': blue_analysis['no_green_needed'],
                    'green_count': len(selected_greens),
                }, to=room)
                
                if len(selected_greens) == 0:
                    logger.info(
                        f"  ⊙ {blue_roi['id']}: 不需要160x放大"
                        f"（运行时动作={blue_analysis['runtime_action'] or '未解析'}）"
                    )
                    logger.info(f"  📤 已发送蓝框完成事件: {blue_roi['id']} (无需160x)")
                    skipped_count += 1
                else:
                    logger.info(f"  ✓ {blue_roi['id']}: 需要160x放大，选中 {len(selected_greens)} 个绿框")
            
            logger.info(f"{'─'*40}")
            logger.info(f"✅ 蓝框 Reason+Plan 完成: {len(selected_blue_rois) - skipped_count}/{len(selected_blue_rois)} 蓝框需要放大")
            logger.info(f"{'─'*40}")
            
            # 合并所有选中的绿框
            selected_green_rois = []
            for green_list in green_filter_results:
                selected_green_rois.extend(green_list if green_list else [])
            
            logger.info(f"✅ 并行任务完成！筛选结果: {len(selected_blue_rois)}个蓝框 → {len(selected_green_rois)}个绿框")
            
            # ⭐ 为选中的绿框分配简单ID (G1, G2, G3...)
            assign_simple_roi_ids(selected_green_rois, 'G', agent_state)
            logger.info(f"🟢 绿框ID分配: {[roi['id'] for roi in selected_green_rois[:5]]}..." if len(selected_green_rois) > 5 else f"🟢 绿框ID分配: {[roi['id'] for roi in selected_green_rois]}")
            
            # ⭐ 先发送绿框overlays到前端（让图上立即显示绿框）
            temp_green_overlays = [create_roi_overlay(roi) for roi in selected_green_rois]
            overlays_lock = agent_state.get('overlays_lock')
            if overlays_lock:
                with overlays_lock:
                    preview_overlays = agent_state.get('all_overlays', []).copy()
                    preview_overlays.extend(temp_green_overlays)
            else:
                preview_overlays = agent_state.get('all_overlays', []).copy()
                preview_overlays.extend(temp_green_overlays)
            
            socketio.emit('agent.viewport', {
                'type': 'viewport',
                'step': f"{step}.2-preview",
                'overlays': preview_overlays,
                'why': f'预览：选中{len(selected_green_rois)}个绿框'
            }, to=room)
            
            # ⭐ 发送绿框选取完成消息
            socketio.emit('agent.message', {
                'type': 'system',
                'content': f"✅ 绿框选取完成: 已选择 {len(selected_green_rois)} 个绿框 (160×)",
                'level': 'green',
                'metadata': {'action': 'selected', 'count': len(selected_green_rois)}
            }, to=room)
        except RuntimeError as e:
            if "cannot schedule new futures after" in str(e):
                logger.warning(f"⚠️ 线程池被中断（可能是程序重启），跳过当前红框: {roi_info['id']}")
                return  # 优雅退出
            raise  # 其他RuntimeError继续抛出
        
        # 打印蓝框分析结果
        logger.info(f"\n📊 蓝框分析结果汇总:")
        
        # Collect blue-box evidence into the pool.
        for ba in blue_analyses:
            collect_evidence(agent_state, ba, {'id': ba['roi_id'], 'rect': ba.get('rect', {})}, level='blue')
        
        # ⭐ 将分析结果中的image_path合并回蓝框ROI对象
        for ba in blue_analyses:
            for roi in selected_blue_rois:
                if roi['id'] == ba['roi_id'] and ba.get('image_path'):
                    roi['image_path'] = ba['image_path']
                    logger.debug(f"  ✅ 蓝框 {roi['id']} image_path: {ba['image_path']}")
        
        # 发送蓝框分析结果到左侧面板（层级显示）
        # 直接显示 AI 原始输出，不做截取拼接（避免内容重复）
        for idx, ba in enumerate(blue_analyses, 1):
            green_reasoning = ba.get('green_selection_reasoning', '')
            blue_findings = ba.get('findings', '')
            display_content = green_reasoning or blue_findings or f'{ba["roi_id"]} 40x分析进行中...'
            logger.info(f"  🔵 {ba['roi_id']}: {display_content[:100]}...")
            socketio.emit('agent.message', {
                'type': 'assistant',
                'content': f"🔵 {ba['roi_id']} 分析结果：\n{display_content[:200]}",
                'reasoning': display_content,
                'level': 'blue',
                'roi_id': ba['roi_id'],
                'parent_id': roi_info['id'],
                'index': idx,
                'rect': ba.get('rect'),
                'is_result': True
            }, to=room)
        
        # ⭐ 发送绿框overlays（追加到全局overlays，累积显示，线程安全）
        green_overlays = [create_roi_overlay(roi) for roi in selected_green_rois]
        
        # ⭐ 调试日志：检查绿框 overlays 的 magnification
        for go in green_overlays[:3]:  # 只打印前3个
            logger.info(f"🟢 [绿框调试] id={go.get('id')}, magnification={go.get('magnification')}, color={go.get('color')}")
        
        # 使用锁保护 all_overlays 的并发访问
        overlays_lock = agent_state.get('overlays_lock')
        if overlays_lock:
            with overlays_lock:
                agent_state.setdefault('all_overlays', [])
                before_count = len(agent_state['all_overlays'])
                agent_state['all_overlays'].extend(green_overlays)
                after_count = len(agent_state['all_overlays'])
                # 复制当前的 overlays 用于发送
                current_overlays = agent_state['all_overlays'].copy()
        else:
            # 降级处理（没有锁时）
            agent_state.setdefault('all_overlays', [])
            before_count = len(agent_state['all_overlays'])
            agent_state['all_overlays'].extend(green_overlays)
            after_count = len(agent_state['all_overlays'])
            current_overlays = agent_state['all_overlays'].copy()
        
        logger.info(f"📊 绿框累积：之前{before_count}个框 + 新增{len(green_overlays)}个绿框 = 现在{after_count}个框")
        
        # 发送所有已存在的框：10红 + 已分析蓝 + 已分析绿
        socketio.emit('agent.viewport', {
            'type': 'viewport',
            'step': f"{step}.3",
            'overlays': current_overlays,  # 发送全部累积的框
            'why': f'最终绿框: {len(selected_blue_rois)}蓝框→AI选中共{len(selected_green_rois)}个绿框(160X)（累积显示：{after_count}个框）'
        }, to=room)
        
        # 更新空间索引的findings
        for ba in blue_analyses:
            if ba.get('findings'):
                update_spatial_index_findings(ba['roi_id'], ba.get('findings', ''), agent_state)
        
        # ========== 构建蓝框上下文字典（完整版：包含 Reason+Plan+Action）==========
        blue_contexts = {}
        for ba in blue_analyses:
            blue_id = ba['roi_id']
            # 找到该蓝框选择的绿框列表
            selected_greens_for_blue = [
                roi['id'] for roi in selected_green_rois 
                if roi.get('parent_id') == blue_id
            ]
            blue_reasoning_val = ba.get('reasoning', ba.get('thinking', ''))
            blue_selection_val = ba.get('green_selection_reasoning', '')
            
            # ⭐ 调试：打印各字段的实际长度
            print(f"📊 [Context构建] {blue_id}: reasoning={len(blue_reasoning_val)}字, selection={len(blue_selection_val)}字, findings={len(ba.get('findings', ''))}字")
            
            blue_contexts[blue_id] = {
                **red_context,  # 继承完整的红框上下文（Reason+Plan+Action）
                # 蓝框 Reason: 完整的 40× 形态学分析思考过程
                'blue_id': blue_id,
                'blue_reasoning': blue_reasoning_val,
                # 蓝框 Plan: 绿框选择的理由
                'blue_selection_reasoning': blue_selection_val,
                # 蓝框 Action: 选择的绿框列表
                'blue_selected_greens': selected_greens_for_blue,
                # 保留 findings 用于兼容
                'blue_finding': ba.get('findings', '')
            }
        logger.info(f"📝 构建{len(blue_contexts)}个蓝框完整上下文（Reason+Plan+Action）用于绿框分析")
        
        # === 第3步：并行分析所有绿框（传入完整上下文：红框+蓝框，不限并发数） ===
        logger.info(f"\n{'─'*80}")
        logger.info(f"⚡ 启动并行任务：【分析{len(selected_green_rois)}个绿框，160×高倍镜，带完整上下文】")
        
        # 注意：红框是并行分析的，不在这里发送绿框进度（会被其他线程覆盖）
        
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(selected_green_rois))) as executor:
                green_analysis_futures = []
                for green_idx, green_roi in enumerate(selected_green_rois, 1):
                    # 解析出父蓝框信息，获取对应上下文
                    parent_blue_id = green_roi.get('parent_id', 'unknown')
                    parent_context = blue_contexts.get(parent_blue_id, red_context)  # 回退到红框上下文
                    
                    logger.info(f"    🟢 开始分析 {roi_info['id']}-{parent_blue_id}-绿框{green_idx}（带完整上下文）")
                    # ⭐ 进度消息：type='progress' 前端不显示在左侧面板
                    socketio.emit('agent.message', {
                        'type': 'progress',
                        'content': f"🟢 开始分析绿框 #{green_idx} (160×)",
                        'level': 'green',
                        'roi_id': green_roi['id'],
                        'parent_id': parent_blue_id,
                        'index': green_idx,
                        'rect': green_roi['rect'],
                        'is_progress': True
                    }, to=room)
                    
                    green_analysis_futures.append(
                        executor.submit(analyze_roi_parallel, slide, green_roi, 160, roi_save_dir, 
                                      parent_context=parent_context,
                                      lang=agent_state.get('lang', 'zh'),
                                      agent_state=agent_state)
                    )
                logger.info(f"⏳ 等待 {len(green_analysis_futures)} 个绿框分析任务完成...")
                green_analyses = [f.result() for f in green_analysis_futures]
            
            logger.info(f"✅ 绿框分析完成！共 {len(green_analyses)} 个绿框")
            
            for ga in green_analyses:
                # 从绿框 ID 中提取父蓝框 ID (例如 R1-B1-G1 → R1-B1)
                green_roi_id = ga.get('roi_id', '')
                parent_blue_id = '-'.join(green_roi_id.split('-')[:-1]) if '-' in green_roi_id else ''
                
                socketio.emit('agent.green_analysis', {
                    'parent_blue_id': parent_blue_id,
                    'green_id': green_roi_id,
                    'rect': ga.get('rect', {}),
                    'observation': ga.get('reasoning', '') or ga.get('findings', ''),  # ⭐ 优先发送完整 reasoning
                    'findings': ga.get('findings', ''),  # ⭐ 同时保留简短摘要
                    'confidence': ga.get('confidence', 0.5)
                }, to=room)
        except RuntimeError as e:
            if "cannot schedule new futures after" in str(e):
                logger.warning(f"⚠️ 线程池被中断（可能是程序重启），跳过当前红框: {roi_info['id']}")
                return  # 优雅退出
            raise  # 其他RuntimeError继续抛出
        logger.info(f"\n📊 绿框分析结果汇总:")
        
        # Collect green-box evidence into the pool.
        for ga in green_analyses:
            collect_evidence(agent_state, ga, {'id': ga['roi_id'], 'rect': ga.get('rect', {})}, level='green')
        
        # ⭐ 将分析结果中的image_path合并回绿框ROI对象
        for ga in green_analyses:
            for roi in selected_green_rois:
                if roi['id'] == ga['roi_id'] and ga.get('image_path'):
                    roi['image_path'] = ga['image_path']
                    logger.debug(f"  ✅ 绿框 {roi['id']} image_path: {ga['image_path']}")
        
        # 发送绿框分析结果到左侧面板（层级显示）
        for idx, ga in enumerate(green_analyses, 1):
            logger.info(f"  🟢 {ga['roi_id']}: {ga['findings'][:100]}...")
            socketio.emit('agent.message', {
                'type': 'assistant',
                'content': f"🟢 {ga['roi_id']} 分析结果：\n{ga['findings']}",
                'reasoning': ga.get('reasoning', ga.get('findings', '')),  # ⭐ AI思考过程
                'level': 'green',
                'roi_id': ga['roi_id'],
                'parent_id': ga.get('parent_id'),
                'index': idx,
                'rect': ga.get('rect'),
                'is_result': True
            }, to=room)
        
        # 构建树状结构
        tree_structure = {
            'roi_id': roi_info['id'],
            'level': 'red',
            'zoom_level': 10,
            'analysis': red_analysis,
            'children': []
        }
        
        # 为每个蓝框构建子树
        for blue_analysis in blue_analyses:
            blue_node = {
                'roi_id': blue_analysis['roi_id'],
                'level': 'blue',
                'zoom_level': 40,
                'analysis': blue_analysis,
                'children': []
            }
            
            # 找到该蓝框的绿框子节点
            for green_analysis in green_analyses:
                if green_analysis.get('parent_id') == blue_analysis['roi_id']:
                    green_node = {
                        'roi_id': green_analysis['roi_id'],
                        'level': 'green',
                        'zoom_level': 160,
                        'analysis': green_analysis,
                        'children': []
                    }
                    blue_node['children'].append(green_node)
            
            tree_structure['children'].append(blue_node)
        
        # 汇总所有分析结果（用于兼容性）
        all_findings = {
            'red_frame': red_analysis,
            'blue_frames': blue_analyses,
            'green_frames': green_analyses,
            'tree': tree_structure
        }
        
        logger.info(f"✅ 完成ROI {roi_info['id']} 多尺度分析: 1红→{len(blue_analyses)}蓝→{len(green_analyses)}绿")
        
        # 返回结果
        return {
            'finding': f"红框{roi_info['id']}: 1红→{len(selected_blue_rois)}蓝→{len(selected_green_rois)}绿",
            'tools_results': all_findings,
            'confidence': None,
            'next_action': {'action': 'move_to_next_roi'},
            'tree_nodes': [tree_structure]
        }
        
    except Exception as e:
        logger.error(f"多尺度ROI分析失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'finding': f"分析失败: {str(e)}",
            'tools_results': {},
            'confidence': 0,
            'next_action': {'action': 'move_to_next_roi'},
            'tree_nodes': []
        }

def extract_roi_at_zoom_level(slide, roi_info, zoom_level):
    """
    在指定缩放级别提取ROI图像
    
    ⭐ 重要修复：
    - 如果 roi_info 是从 subdivide_roi_into_grid 生成的子ROI（有 grid_position），
      则直接使用其 rect 坐标（这是正确的网格位置）
    - 只有在旧的多尺度分析流程中（没有 grid_position），才使用中心计算
    
    Returns:
        (roi_image, actual_rect): ROI图像和实际提取的矩形坐标
    """
    rect = roi_info['rect']
    
    # ⭐ 检查是否是 subdivide_roi_into_grid 生成的子ROI
    # 这些子ROI已经有正确的坐标，直接使用即可
    is_subdivided_roi = 'grid_position' in roi_info or 'grid_number' in roi_info
    
    if is_subdivided_roi:
        # ⭐ 直接使用子ROI的坐标（来自网格划分，已经是正确位置）
        x, y = rect['x'], rect['y']
        w, h = rect['w'], rect['h']
        logger.debug(f"提取子ROI图像: {roi_info.get('id', '?')} at ({x}, {y}), size=({w}, {h})")
        roi_image = safe_read_region(slide, (x, y), 0, (w, h))
        actual_rect = {'x': x, 'y': y, 'w': w, 'h': h}
    else:
        # 旧的多尺度分析流程：根据缩放级别使用固定比例
        center_x = rect['x'] + rect['w'] // 2
        center_y = rect['y'] + rect['h'] // 2
        
        if zoom_level == 10:
            # 10x: 提取完整ROI（红框）
            x, y = rect['x'], rect['y']
            w, h = rect['w'], rect['h']
            roi_image = safe_read_region(slide, (x, y), 0, (w, h))
            actual_rect = {'x': x, 'y': y, 'w': w, 'h': h}
            
        elif zoom_level == 40:
            # 40x: 蓝框（红框边长的50%）
            size = min(rect['w'], rect['h']) // 2
            x = center_x - size // 2
            y = center_y - size // 2
            roi_image = safe_read_region(slide, (x, y), 0, (size, size))
            actual_rect = {'x': x, 'y': y, 'w': size, 'h': size}
            
        elif zoom_level == 160:
            # 160x: 绿框（红框边长的25%）
            size = min(rect['w'], rect['h']) // 4
            x = center_x - size // 2
            y = center_y - size // 2
            roi_image = safe_read_region(slide, (x, y), 0, (size, size))
            actual_rect = {'x': x, 'y': y, 'w': size, 'h': size}
            
        else:
            # 其他倍率，默认行为
            x, y = rect['x'], rect['y']
            w, h = rect['w'], rect['h']
            roi_image = safe_read_region(slide, (x, y), 0, (w, h))
            actual_rect = {'x': x, 'y': y, 'w': w, 'h': h}
    
    return roi_image.convert('RGB'), actual_rect

def build_context_prompt(zoom_level: int, parent_context: dict = None, agent_state: dict = None, roi_id: str = None, lang: str = 'zh') -> str:
    """
    构建带上下文的 Prompt (Context Passing + Spatial Search)
    
    ⭐ 改进版：传递完整的树结构上下文（Reason + Plan + Action），而不是截断的 findings
    ⭐ 双语支持：根据 lang 参数使用对应语言的标签
    
    Args:
        zoom_level: 当前放大倍率
        parent_context: 父级分析结果上下文（包含完整的 Reason+Plan+Action）
        agent_state: Agent状态（可选，用于空间搜索）
        roi_id: 当前ROI的ID（可选，用于空间搜索）
        lang: 语言设置 ('zh' 或 'en')
        
    Returns:
        str: 上下文描述文本
    """
    if zoom_level == 10 or not parent_context:
        return ""
    
    _is_en = lang == 'en'
    _unknown = 'unknown' if _is_en else '未知'
    _none = 'N/A' if _is_en else '暂无'
    
    if _is_en:
        context_text = "[Path Context for Current Analysis]\n"
        context_text += "(You are exploring along a low-to-high magnification analysis path. Below is the full analysis from parent nodes.)\n\n"
    else:
        context_text = "【当前分析路径的上下文信息】\n"
        context_text += "（你正在沿着一条从低倍到高倍的分析路径进行探索，以下是父级节点的完整分析）\n\n"
    
    # ⭐ 注入缩略图全局分析（对 Q3 浸润深度等宏观判断至关重要）
    if agent_state:
        _ga = agent_state.get('global_analysis', '')
        if isinstance(_ga, dict):
            _ga = _ga.get('reasoning', '') or _ga.get('analysis', '')
        elif isinstance(_ga, list):
            _ga = '\n'.join([str(item) for item in _ga])
        if _ga:
            _ga_label = "Thumbnail (Orchestrator — full slide overview)" if _is_en else "缩略图（Orchestrator — 全切片架构观察）"
            context_text += f"=== {_ga_label} ===\n{_ga}\n\n"
    
    if zoom_level == 40:
        red_id = parent_context.get('red_id', _unknown)
        red_reasoning = parent_context.get('red_reasoning', '')
        red_selection = parent_context.get('red_selection_reasoning', '')
        red_selected_blues = parent_context.get('red_selected_blues', [])
        
        if _is_en:
            context_text += f"=== Parent Red Box {red_id} (10×) Analysis ===\n"
            if red_reasoning:
                context_text += f"[Reason - AI Observation]\n{red_reasoning}\n\n"
            if red_selection:
                context_text += f"[Plan - Selection Rationale]\n{red_selection}\n\n"
            if red_selected_blues:
                context_text += f"[Action - Selected Blue Boxes] {', '.join(red_selected_blues)}\n\n"
            if not red_reasoning and not red_selection:
                red_finding = parent_context.get('red_finding', _none)
                if red_finding:
                    context_text += f"[Summary] {red_finding}\n\n"
        else:
            context_text += f"=== 父级红框 {red_id} (10×) 的分析 ===\n"
            if red_reasoning:
                context_text += f"【Reason - AI思考过程】\n{red_reasoning}\n\n"
            if red_selection:
                context_text += f"【Plan - 选择理由】\n{red_selection}\n\n"
            if red_selected_blues:
                context_text += f"【Action - 选择的蓝框】{', '.join(red_selected_blues)}\n\n"
            if not red_reasoning and not red_selection:
                red_finding = parent_context.get('red_finding', _none)
                if red_finding:
                    context_text += f"【摘要】{red_finding}\n\n"
    
    elif zoom_level == 160:
        # ===== 红框上下文 =====
        red_id = parent_context.get('red_id', _unknown)
        red_reasoning = parent_context.get('red_reasoning', '')
        red_selection = parent_context.get('red_selection_reasoning', '')
        red_selected_blues = parent_context.get('red_selected_blues', [])
        
        if _is_en:
            context_text += f"=== Grandparent Red Box {red_id} (10×) Analysis ===\n"
            if red_reasoning:
                context_text += f"[Reason - 10× Global Observation]\n{red_reasoning}\n\n"
            if red_selection:
                context_text += f"[Plan - Blue Box Selection Rationale]\n{red_selection}\n\n"
            if red_selected_blues:
                context_text += f"[Action - Selected Blue Boxes] {', '.join(red_selected_blues)}\n\n"
            if not red_reasoning and not red_selection:
                red_finding = parent_context.get('red_finding', _none)
                if red_finding:
                    context_text += f"[Summary] {red_finding}\n\n"
        else:
            context_text += f"=== 祖父红框 {red_id} (10×) 的分析 ===\n"
            if red_reasoning:
                context_text += f"【Reason - 10× 全局观察】\n{red_reasoning}\n\n"
            if red_selection:
                context_text += f"【Plan - 蓝框选择理由】\n{red_selection}\n\n"
            if red_selected_blues:
                context_text += f"【Action - 选择的蓝框】{', '.join(red_selected_blues)}\n\n"
            if not red_reasoning and not red_selection:
                red_finding = parent_context.get('red_finding', _none)
                if red_finding:
                    context_text += f"【摘要】{red_finding}\n\n"
        
        # ===== 蓝框上下文 =====
        blue_id = parent_context.get('blue_id', _unknown)
        blue_reasoning = parent_context.get('blue_reasoning', '')
        blue_selection = parent_context.get('blue_selection_reasoning', '')
        blue_selected_greens = parent_context.get('blue_selected_greens', [])
        
        if _is_en:
            context_text += f"=== Parent Blue Box {blue_id} (40×) Analysis ===\n"
            if blue_reasoning:
                context_text += f"[Reason - 40× Morphological Analysis]\n{blue_reasoning}\n\n"
            if blue_selection:
                context_text += f"[Plan - Green Box Selection Rationale]\n{blue_selection}\n\n"
            if blue_selected_greens:
                context_text += f"[Action - Selected Green Boxes] {', '.join(blue_selected_greens)}\n\n"
            if not blue_reasoning and not blue_selection:
                blue_finding = parent_context.get('blue_finding', _none)
                if blue_finding:
                    context_text += f"[Summary] {blue_finding}\n\n"
        else:
            context_text += f"=== 父级蓝框 {blue_id} (40×) 的分析 ===\n"
            if blue_reasoning:
                context_text += f"【Reason - 40× 形态学分析】\n{blue_reasoning}\n\n"
            if blue_selection:
                context_text += f"【Plan - 绿框选择理由】\n{blue_selection}\n\n"
            if blue_selected_greens:
                context_text += f"【Action - 选择的绿框】{', '.join(blue_selected_greens)}\n\n"
            if not blue_reasoning and not blue_selection:
                blue_finding = parent_context.get('blue_finding', _none)
                if blue_finding:
                    context_text += f"【摘要】{blue_finding}\n\n"
    
    return context_text


def emit_error(socketio, room, error_msg):
    """发送错误事件"""
    socketio.emit('agent.error', {
        'type': 'error',
        'message': error_msg,
        'timestamp': datetime.now().isoformat()
    }, to=room)

def emit_done(socketio, room, file_id, agent_state, status='completed'):
    """发送完成事件并生成标准化病理报告"""
    try:
        # 生成标准化病理报告
        report_path = build_standardized_pathology_report(file_id, agent_state)
        
        # 汇总分析结果（⭐ Bug修复：处理空列表的情况，避免除零错误）
        confidence_scores = agent_state.get('confidence_scores', [])
        total_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else None
        
        # 综合诊断逻辑
        diagnosis_result = generate_comprehensive_diagnosis(agent_state, total_confidence)
        
        # 统计信息
        tree_nodes = agent_state.get('tree_nodes', [])
        total_red = len(tree_nodes)
        total_blue = sum(len(node.get('children', [])) for node in tree_nodes)
        total_green = sum(
            len(blue_node.get('children', [])) 
            for red_node in tree_nodes 
            for blue_node in red_node.get('children', [])
        )
        
        # ⭐ 获取正确的报告路径（使用 output_paths）
        output_paths = agent_state.get('output_paths', {})
        html_report_url = output_paths.get('report_html', f'/outputs/{file_id}/auto/pathology_report.html')
        
        # ⭐ 发送到聊天框：分析完成消息（双语）
        _lang = agent_state.get('lang', 'zh') if agent_state else 'zh'
        if _lang == 'en':
            _done_content = f"""
## 📊 Pathology Analysis Complete

**Analysis Statistics**:
- 🔴 Low-power (10×): {total_red} red boxes
- 🔵 Medium-power (40×): {total_blue} blue boxes
- 🟢 High-power (160×): {total_green} green boxes

**Options**:
- 📄 [View Full Report]({html_report_url})
- 🌳 [View Analysis Tree](#show-tree)
            """
        else:
            _done_content = f"""
## 📊 病理分析完成

**分析统计**:
- 🔴 低倍镜 (10×): {total_red} 个红框
- 🔵 中倍镜 (40×): {total_blue} 个蓝框
- 🟢 高倍镜 (160×): {total_green} 个绿框

**操作选项**:
- 📄 [查看完整报告]({html_report_url})
- 🌳 [查看分析树结构](#show-tree)
            """
        socketio.emit('agent.message', {
            'type': 'assistant',
            'content': _done_content,
            'metadata': {
                'report_path': str(report_path),
                'file_id': file_id,
                'html_report_path': html_report_url,
                'statistics': {
                    'red': total_red,
                    'blue': total_blue,
                    'green': total_green
                },
                'diagnosis': diagnosis_result,
                'tree_structure': tree_nodes[:3]  # 只发送前3个节点的结构预览
            }
        }, to=room)
        
        socketio.emit('agent.done', {
            'type': 'done',
            'file_id': file_id,
            'report_path': str(report_path),
            'steps': agent_state.get('current_step', 0),
            'roi_count': len(agent_state.get('analyzed_rois', [])),
            'status': status,
            'summary': diagnosis_result
        }, to=room)
        
        logger.info(f"病理分析完成: {file_id}, 诊断: {diagnosis_result['diagnosis']}")
        
    except Exception as e:
        logger.error(f"生成完成事件失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        emit_error(socketio, room, f"生成报告失败: {str(e)}")

def build_standardized_pathology_report(file_id, agent_state):
    """
    生成标准化病理报告
    """
    try:
        # 从 agent_state 获取正确的输出目录
        output_paths = agent_state.get('output_paths', {}) if agent_state else {}
        output_dir = output_paths.get('output_dir', Path(f"outputs/{file_id}"))
        if not isinstance(output_dir, Path):
            output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        
        report_path = output_dir / "pathology_report.md"
        
        # 分析数据汇总
        analyzed_rois = agent_state.get('analyzed_rois', [])
        pathology_findings = agent_state.get('pathology_findings', [])
        tool_results = agent_state.get('tool_results', [])
        confidence_scores = agent_state.get('confidence_scores', [])
        
        # 计算综合指标
        avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else None
        
        # 提取关键病理指标
        all_metrics = {}
        for result in tool_results:
            if isinstance(result, dict) and 'metrics' in result:
                for key, value in result['metrics'].items():
                    if key not in all_metrics:
                        all_metrics[key] = []
                    if isinstance(value, (int, float)):
                        all_metrics[key].append(value)
        
        # 计算平均值
        avg_metrics = {}
        for key, values in all_metrics.items():
            if values:
                avg_metrics[key] = sum(values) / len(values)
        
        # 生成报告内容
        report_content = f"""# 病理切片智能分析报告

## 标本信息
- **文件名**: {agent_state.get('filename', 'Unknown')}
- **分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **图像尺寸**: {agent_state.get('dimensions', 'Unknown')}
- **分析ROI数量**: {len(analyzed_rois)}

## 镜下所见

### 组织结构特征
"""
        
        # 添加ROI分析详情
        for i, (roi, finding) in enumerate(zip(analyzed_rois[:5], pathology_findings[:5])):
            report_content += f"""
#### ROI {i+1} ({roi.get('id', 'Unknown')})
- **位置**: {roi.get('reason', '未知位置')}
- **可疑病变**: {roi.get('suspected_pathology', '待分析')}
- **镜下表现**: {finding if isinstance(finding, str) else '自动分析完成'}
"""
        
        # 生成诊断意见（直接使用GPT报告原文）
        diagnosis_result = generate_comprehensive_diagnosis(agent_state, avg_confidence)
        
        report_content += f"""

## 诊断意见

### 病理诊断
{diagnosis_result['diagnosis']}

## 建议

本报告基于AI分析，最终诊断需病理医生结合临床资料确认。

## ROI标注说明

### 分析区域详情
"""
        
        for i, roi in enumerate(analyzed_rois[:5]):
            report_content += f"""
**ROI {i+1}**:
- 坐标: ({roi['rect']['x']}, {roi['rect']['y']}) 
- 尺寸: {roi['rect']['w']} × {roi['rect']['h']} 像素
- 优先级: {roi.get('priority', 'N/A')}

"""
        
        report_content += f"""
---
**报告生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**AI分析系统**: PathCopilot
**注意**: 本报告为AI辅助分析结果，最终诊断需结合临床资料并由病理医生确认。
"""
        
        # 保存报告
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content)
        
        logger.info(f"标准化病理报告已生成: {report_path}")
        return report_path
        
    except Exception as e:
        logger.error(f"生成标准化报告失败: {str(e)}")
        raise

def generate_comprehensive_diagnosis(agent_state, confidence):
    """
    生成综合诊断结果
    ⭐ 直接使用 GPT 生成的 final_report 原文，保持一致性
    """
    # 直接使用 GPT 生成的最终报告原文
    final_report = agent_state.get('final_report', {})
    gpt_report_text = final_report.get('report', '').strip()
    
    if gpt_report_text:
        return {
            'diagnosis': gpt_report_text,  # ⭐ 直接使用原文
            'confidence': confidence,
            'source': 'gpt_report'
        }
    
    # 备用：如果没有 GPT 报告
    return {
        'diagnosis': "AI分析完成，请查看详细报告",
        'confidence': confidence,
        'source': 'fallback'
    }

def stop_agent(room):
    """停止Agent"""
    agent_stop_flags[room] = True
    logger.info(f"设置Agent停止标志: {room}")
