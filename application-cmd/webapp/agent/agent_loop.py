# -*- coding: utf-8 -*-
"""PathCopilot Agent Loop runtime.

Implements the Reason → Plan → Action → Memory cycle for multiscale WSI
analysis: thumbnail triage, ROI review, optional tools, and evidence memory.
MCP is used only where an actual MCP registry or transport is involved.
"""

import os
import time
import json
import shutil
import uuid
import copy
import random
import re
import html
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
import hashlib

logger = logging.getLogger(__name__)

# ⭐ 全局 OpenSlide 读取锁：序列化所有 read_region 调用，
# 防止 NDPI 文件在 WSL2 9P 文件系统上并发随机读取导致 "Corrupt JPEG data"。
# 使用 RLock（可重入锁）防止同一线程内嵌套调用死锁。
_openslide_lock = threading.RLock()

# 候选框过滤策略同时写入缓存元数据。算法或阈值变化时，旧缓存会自动失效，
# 但不会被删除，便于历史结果追溯。
VIEW_PROFILE_NAME = "thumbnail/10x/40x/160x"
_DEFAULT_TISSUE_MASK_POLICY = (
    "relaxed_gray_std_no_morphology_v3_tissue15_fail_closed"
)
TISSUE_MASK_POLICY = os.environ.get(
    "PATHCOPILOT_MASK_POLICY",
    _DEFAULT_TISSUE_MASK_POLICY,
).strip() or _DEFAULT_TISSUE_MASK_POLICY


def _read_tissue_content_threshold():
    """读取组织占比阈值；非法配置回退到经验证的 15%。"""
    try:
        value = float(os.environ.get("PATHCOPILOT_TISSUE_THRESHOLD", "0.15"))
    except (TypeError, ValueError):
        return 0.15
    return value if 0.0 < value <= 1.0 else 0.15


TISSUE_CONTENT_THRESHOLD = _read_tissue_content_threshold()

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
    from .gpt_client import call_gpt_vision, call_agent_model
    from .report_generator import generate_detailed_word_report, generate_report_with_images
    from .evidence_model import (
        EvidenceUnit, EvidencePool, parse_evidence_from_analysis, 
        aggregate_evidence, generate_evidence_chain, BACKTRACK_TRIGGERS
    )
    from .knowledge_retrieval import (
        get_knowledge_for_report, extract_diagnostic_keywords, search_pubmed,
        format_knowledge_context, search_grading_criteria, get_diagnostic_criteria
    )
    from .mcp_tools import (
        get_mcp_registry, bind_tool_handlers, parse_tool_call_from_response,
        format_tool_result_for_llm, MCPToolRegistry
    )
except ImportError:
    try:
        from gpt_client import call_gpt_vision, call_agent_model
        from report_generator import generate_detailed_word_report, generate_report_with_images
        from evidence_model import (
            EvidenceUnit, EvidencePool, parse_evidence_from_analysis, 
            aggregate_evidence, generate_evidence_chain, BACKTRACK_TRIGGERS
        )
        from knowledge_retrieval import (
            get_knowledge_for_report, extract_diagnostic_keywords, search_pubmed,
            format_knowledge_context, search_grading_criteria, get_diagnostic_criteria
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
        def call_agent_model(
            messages,
            model=None,
            max_tokens=65536,
            max_retries=3,
            **kwargs,
        ):
            logger.warning("Agent model API 不可用，返回空结果")
            return None, "API不可用"
        # 证据模型占位
        EvidencePool = None
        parse_evidence_from_analysis = None
        BACKTRACK_TRIGGERS = {}
        # 知识检索占位
        get_knowledge_for_report = None
        search_grading_criteria = None
        get_diagnostic_criteria = None
        # MCP工具占位
        get_mcp_registry = None
        bind_tool_handlers = None
        parse_tool_call_from_response = None
        format_tool_result_for_llm = None
        MCPToolRegistry = None

try:
    from pathcopilot_outbound_mcp import (
        MCPCallResult,
        call_mcp_tool,
        is_outbound_mcp_enabled,
    )
except ImportError:
    MCPCallResult = None
    call_mcp_tool = None
    is_outbound_mcp_enabled = None

try:
    from .action_contract import (
        MCP_ALL_ACTIONS,
        MCP_NON_DRILL_ACTIONS,
        empty_mcp_decision,
        mcp_decision_is_complete,
        parse_mcp_response,
        parse_mcp_selection,
    )
except ImportError:
    from action_contract import (
        MCP_ALL_ACTIONS,
        MCP_NON_DRILL_ACTIONS,
        empty_mcp_decision,
        mcp_decision_is_complete,
        parse_mcp_response,
        parse_mcp_selection,
    )

_MCP_ALL_ACTIONS = MCP_ALL_ACTIONS
_MCP_NON_DRILL_ACTIONS = MCP_NON_DRILL_ACTIONS
_empty_mcp_decision = empty_mcp_decision
_mcp_decision_is_complete = mcp_decision_is_complete

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
    'blue_confirm_all':     {'zh': 'AI已完成所有 {n} 个红框的蓝框选取，请确认或调整后点击"确认全部"', 'en': 'AI completed blue-box selection for all {n} red boxes — confirm or adjust, then click "Confirm All"'},
    'green_confirm':        {'zh': 'AI已完成绿框选取（共{n}个），请确认后继续', 'en': 'AI completed green-box selection ({n} total) — please confirm to continue'},
    'green_progress':       {'zh': '绿框分析中 ({done}/{total})',         'en': 'Green-box analysis ({done}/{total})'},
    'green_done_all':       {'zh': '✅ 绿框分析完成 ({n}个)',              'en': '✅ Green-box analysis complete ({n} boxes)'},
    'generating_report':    {'zh': '正在生成最终报告...',                  'en': 'Generating final report...'},
    'waiting_upload':       {'zh': '等待 {part} 上传完成...',              'en': 'Waiting for {part} to finish uploading...'},
    'slide_done':           {'zh': '已完成 {done}/{total} 张切片分析',    'en': 'Completed {done}/{total} slide analyses'},
    'generating_patient_report': {'zh': '正在生成患者级别综合报告...', 'en': 'Generating patient-level comprehensive report...'},
    'analysis_done':        {'zh': '分析完成！',                          'en': 'Analysis complete!'},
    'phase1_start':         {'zh': 'Phase 1: 正在分析缩略图，识别候选区域...', 'en': 'Phase 1: Analyzing thumbnail, identifying candidates...'},
    'phase2_start':         {'zh': 'Phase 2: 正在分析 {n} 个红框区域...', 'en': 'Phase 2: Analyzing {n} red-box regions...'},
    'phase3_start':         {'zh': 'Phase 3: 正在分析 {n} 个蓝框区域...', 'en': 'Phase 3: Analyzing {n} blue-box regions...'},
    'phase4_start':         {'zh': 'Phase 4: 正在分析 {n} 个绿框区域...', 'en': 'Phase 4: Analyzing {n} green-box regions...'},
    'phase5_start':         {'zh': 'Phase 5: 正在生成诊断报告...',        'en': 'Phase 5: Generating diagnostic report...'},
    'collab_slide_done':    {'zh': '{part} 协作分析完成！',               'en': '{part} collaborative analysis complete!'},
    'blue_progress':        {'zh': '蓝框分析中 ({done}/{total})',         'en': 'Blue-box analysis ({done}/{total})'},
    'green_select_reason':  {'zh': '🟢 绿框选择原因',                      'en': '🟢 Green-box selection rationale'},
    'patient_start_total':  {'zh': '开始分析 {folder_name} 的 {total_wsi} 张切片...', 'en': 'Starting analysis of {folder_name}: {total_wsi} slides...'},
    'patient_start_skipped':{'zh': '开始分析 {folder_name}：{skipped} 张已加载缓存，{remaining} 张待分析...', 'en': 'Starting analysis of {folder_name}: {skipped} cached, {remaining} remaining...'},
}

def _msg(key: str, lang: str = 'zh', **kwargs) -> str:
    """从双语字典取进度消息，支持 format 占位符"""
    d = _BACKEND_MSGS.get(key, {})
    tmpl = d.get(lang) or d.get('zh') or key
    try:
        return tmpl.format(**kwargs)
    except (KeyError, IndexError):
        return tmpl

# Do not skip diagnostically relevant or heterogeneous regions; missed selections can
# leave the user's question unsupported at the required scale.
# 不要漏选与当前问题相关或具有异质性的区域；否则可能缺失回答所需尺度的证据。
# ============== PathCopilot Agent Loop 规范化提示词系统 ==============
# 
# 设计原则：
# 1. 每个提示词明确标注 Agent Loop 阶段（Reason → Plan → Action → Memory）
# 2. AI 具有元认知能力，可以自主选择动作（下钻/停止/回溯/检索）
# 3. 统一的输出格式，便于代码解析
# 4. 完整的上下文传递（路径上下文 + 邻近区域参考）
#

# Organ-agnostic answer contract used by the open-source application.
# Task-specific evaluation prompts are released separately under evaluation/ for reproducibility.
GENERAL_PATHOLOGY_RESPONSE_CONTRACT = """
通用病理分析任务：
请围绕用户提出的问题，仅依据当前可见的切片、ROI 和已经记录的证据进行器官无关的病理学分析。不得预设器官、肿瘤类型、分级体系、浸润方式或诊断结论。若用户没有提出明确的诊断性问题，请先客观描述可观察到的组织学特征，说明尚缺少的临床、取材或辅助检测信息；不得虚构诊断。

诊断性图像任务统一回答格式：
1. 结论：直接回答当前问题；证据不足时明确写“无法确定”或“无法评价”。
2. 形态学依据与证据来源：列出当前视野中可直接观察的结构和细胞特征，并关联相应 WSI、ROI 或 FOV。
3. 主要鉴别诊断：分别说明各鉴别的支持证据与反对证据。
4. 局限性与建议：说明取材、倍率、图像质量和上下文限制；必要时建议更高倍复核、深切、特殊染色、IHC、分子检测或临床相关性分析。
5. 证据支持度（0–4）：0=无法评价；1=弱支持；2=中等支持但仍有重要鉴别；3=强支持且不确定性有限；4=高度支持，多尺度证据一致且无有力反证。

证据支持度不是概率，也不等同于最终病理诊断。当前视野的直接形态学证据优先于上级推断。普通病理知识问答无需强制套用以上格式。"""

GENERAL_PATHOLOGY_RESPONSE_CONTRACT_EN = """
General pathology task:
Answer the user's question using only the visible slide, ROI, and recorded evidence in an organ-agnostic manner. Do not assume an organ, tumor type, grading system, invasion pattern, or diagnosis. If the user has not provided a specific diagnostic question, first describe the observable histologic features objectively and state what clinical, specimen, or ancillary information is missing; do not invent a diagnosis.

Standard response contract for diagnostic image tasks:
1. Conclusion: answer the current question directly; state “Indeterminate” or “Not assessable” when evidence is insufficient.
2. Morphologic evidence and provenance: list directly observable architectural and cytologic findings and link them to the relevant WSI, ROI, or FOV.
3. Key differential diagnoses: state the supporting and opposing evidence for each important differential.
4. Limitations and next steps: describe sampling, magnification, image-quality, and context limitations; when appropriate, recommend higher-power review, deeper sections, special stains, IHC, molecular testing, or clinical correlation.
5. Evidence-support score (0–4): 0=not assessable; 1=weak support; 2=moderate support with important differentials; 3=strong support with limited uncertainty; 4=very strong support with concordant multiscale evidence and no substantial counter-evidence.

The evidence-support score is not a probability and is not equivalent to a final pathology diagnosis. Direct morphology in the current field takes precedence over inherited hypotheses. Ordinary pathology knowledge questions need not use this structure."""


def get_active_diagnostic_objective(agent_state=None, lang='zh') -> str:
    """Return a user question (dict or string input) plus the generic contract."""
    lang = _normalize_output_language(lang)
    if isinstance(agent_state, dict):
        question = str(agent_state.get('diagnostic_question') or '').strip()
    else:
        question = str(agent_state or '').strip()
    contract = (
        GENERAL_PATHOLOGY_RESPONSE_CONTRACT_EN
        if lang == 'en'
        else GENERAL_PATHOLOGY_RESPONSE_CONTRACT
    )
    return f"{question}\n\n{contract}" if question else contract


def _specimen_note_already_phrased(text) -> bool:
    blob = str(text or '')
    markers = (
        'Trusted specimen context',
        '可信标本背景',
        'User-declared sampling site',
        '用户声明的取材部位',
        'sampling site has not been provided',
        '尚未提供取材部位',
        'does not know the sampling site',
        '不清楚取材部位',
    )
    return any(marker in blob for marker in markers)


def _merge_trusted_specimen_context(question='', specimen_context='', lang='zh') -> str:
    """Attach specimen provenance without asserting diagnosis."""
    question = str(question or '').strip()
    specimen_context = str(specimen_context or '').strip()
    if not specimen_context:
        return question
    if _specimen_note_already_phrased(specimen_context):
        note = specimen_context
    elif _normalize_output_language(lang) == 'en':
        note = (
            "Trusted specimen context (server supplied): " + specimen_context + "\n"
            "This establishes specimen source only. Histologic type, grade, invasion, "
            "and differential conclusions must still be supported by slide morphology."
        )
    else:
        note = (
            "受信任的取材背景（由服务器提供）：" + specimen_context + "\n"
            "该背景仅用于确定取材来源；组织学分型、分级、浸润及鉴别诊断仍必须由切片形态证据支持。"
        )
    return f"{question}\n\n{note}" if question else note


def _specimen_context_for_run(file_id, specimen_context='', lang='zh') -> str:
    note = str(specimen_context or '').strip()
    if note:
        return note
    try:
        from app import uploaded_files, _specimen_prompt_for_file
        return str(_specimen_prompt_for_file(uploaded_files.get(file_id) or {}, lang) or '').strip()
    except Exception:
        return ''


def _organ_label_for_file(file_id, lang='zh') -> str:
    default = '病理切片标本'
    try:
        from app import uploaded_files, _resolve_specimen_context
        resolved = _resolve_specimen_context(uploaded_files.get(file_id) or {}, lang)
        site = str(resolved.get('site') or '').strip()
        if resolved.get('source') == 'user' and site:
            return site
        if resolved.get('source') in {'user_unknown', 'undeclared'}:
            return (
                'unspecified specimen'
                if _normalize_output_language(lang) == 'en'
                else '未指定标本'
            )
    except Exception:
        pass
    return default


def _build_generic_terminal_evidence_prompt(roi, context_text, lang='zh', objective='') -> str:
    """Build the only single-WSI terminal prompt used by the open-source app."""
    lang = _normalize_output_language(lang)
    objective_text = get_active_diagnostic_objective(objective, lang)
    roi_id = roi.get('id', 'unknown')
    parent_id = roi.get('parent_id', 'unknown')
    if lang == 'en':
        return f"""You are PathCopilot SubAgent at the terminal 160× field of a multiscale WSI path.

Current ROI: {roi_id} | Parent ROI: {parent_id}

## Current objective and answer contract
{objective_text}

## Upstream path context
{context_text or 'No upstream text was recorded.'}

## Task
1. First assess technical evaluability (focus, folding, crush, fixation, staining, contamination, or other relevant artifact).
2. Independently describe only morphology directly visible in the current field. Identify positive lineage or differentiation clues before assigning cell identity; never infer lineage merely by exclusion.
3. Assess nuclear, chromatin, cytoplasmic, polarity, mitotic, and locally destructive features, then compare only with mimics actually relevant to this field.
4. Integrate relevant low-, medium-, and high-power evidence without inheriting an unsupported upstream diagnosis. Do not assume an organ, tumor type, grading scheme, invasion target, biomarker, or special finding unless the user asked about it or the image evidence makes it relevant.
5. Treat actively selected high-power fields as non-random samples: do not extrapolate a focal field into a whole-slide proportion, grade, extent, or patient-level conclusion. For any suspected diagnosis, state supporting evidence, opposing evidence or mimics, and what remains unassessable.

## Output format
[Reason]
(Direct high-power observations and multiscale reconciliation.)

[Path Evidence Summary]
- evidence: (concise evidence relevant to the current objective, with ROI provenance)
- limitations_or_counterevidence: (specific uncertainty, mimic, sampling, or scale limitation)
- confidence: N (0=not assessable; 1=weak; 2=moderate with important differentials; 3=strong; 4=very strong concordant multiscale evidence)

[Diagnostic impression]
(One cautious sentence stating what this path supports. Do not make a patient-level diagnosis.)
""" + _natural_language_contract(lang)

    return f"""你是 PathCopilot SubAgent，当前位于一条多尺度 WSI 路径的终末 160× 视野。

当前 ROI：{roi_id} | 父级 ROI：{parent_id}

## 当前目标与回答契约
{objective_text}

## 上游路径上下文
{context_text or '未记录上游文本。'}

## 任务
1. 首先评估当前视野的技术可评估性，包括聚焦、折叠、挤压、固定、染色、污染及其他相关伪影。
2. 独立描述当前视野可直接观察到的形态；在判断细胞性质前先指出正向谱系或分化线索，不得仅靠排除法反推谱系。
3. 核验细胞核、染色质、胞质、极性、核分裂及局部破坏性特征，再与当前视野实际相关的模拟进行比较。
4. 只整合与当前目标相关的低、中、高倍证据，不继承上级未经证实的诊断；除非用户明确询问或图像证据使其相关，不得预设器官、肿瘤类型、分级体系、浸润目标、生物标志物或特殊病理发现。
5. 主动选择的高倍视野不是随机样本，不得把局灶视野外推为全片比例、分级、范围或患者级结论。对任何诊断倾向同时说明支持证据、反对证据或模拟，以及无法评价的内容。

## 输出格式
【Reason】
（当前高倍直接观察与多尺度证据校准。）

【路径证据汇总】
- evidence：（与当前目标相关的简明证据，并标注 ROI 来源）
- limitations_or_counterevidence：（具体不确定性、模拟病变、取材或尺度局限）
- confidence：N（0=无法评价；1=弱支持；2=中等支持但有重要鉴别；3=强支持；4=多尺度一致且无有力反证）

【诊断建议】
（用一句审慎的话说明本路径支持什么；不得在此作患者级最终诊断。）
""" + _natural_language_contract(lang)


def _build_generic_final_report_prompt(agent_state, all_summaries, totals, lang='zh') -> str:
    """Build the organ-agnostic single-WSI synthesis prompt."""
    lang = _normalize_output_language(lang)
    objective = get_active_diagnostic_objective(agent_state, lang)
    red, blue, green = totals
    if lang == 'en':
        return f"""You are PathCopilot Orchestrator. Synthesize all observation paths into an organ-agnostic whole-slide answer.

## Current objective and answer contract
{objective}

## Whole-slide and multiscale evidence
{all_summaries}

## Sampling summary
- 10× red ROIs: {red}
- 40× blue ROIs: {blue}
- 160× green ROIs: {green}

Follow the five-part answer contract above. Cite ROI paths for every material claim. Resolve conflicts using evidence quality, representativeness, scale, and specific counter-evidence rather than voting. High-power fields may establish local cell identity but must not by themselves determine whole-slide proportions, grade, or extent. When a path contains a secondary-review record, its adjudicated verdict overrides the first-pass claim; never resurrect a rejected, indeterminate, or uncompleted claim as confirmed. If the user supplied answer options, choose exactly one and reproduce it verbatim. Do not introduce an organ-specific checklist that the user did not request.
"""
    return f"""你是 PathCopilot Orchestrator。请把全部观察路径整合为器官无关的全切片回答。

## 当前目标与回答契约
{objective}

## 全切片与多尺度证据
{all_summaries}

## 取样统计
- 10× 红框：{red}
- 40× 蓝框：{blue}
- 160× 绿框：{green}

严格采用上方五段式回答契约，并为每个实质性结论标注 ROI 路径。证据冲突时依据证据质量、代表性、倍率与具体反证裁决，不得简单投票。高倍视野可确认局部细胞性质，但不能单独决定全片比例、分级或范围。路径中若存在二次复核记录，复核裁决优先于首次主张；不得把已拒绝、无法确定或未完成复核的主张重新写成确认结论。若用户提供了答案选项，必须恰好选择一个并原文给出。不得自行加入用户未询问的器官专项检查表。
"""

# ============== 输出目录路径管理 ==============
# 
# 目录结构设计（分离自动模式和协作模式的输出）：
# 
# outputs/
# ├── {file_id}/                      # 单张 Slide
# │   ├── auto/                       # 自动模式
# │   │   ├── pathology_report.html
# │   │   ├── analysis_tree.json
# │   │   └── steps/
# │   └── collab/                     # 协作模式
# │       ├── pathology_report.html
# │       ├── analysis_tree.json
# │       └── steps/
# │
# └── patient_{patient_id}/           # Patient Level（多张 Slide）
#     ├── auto/                       # 自动模式
#     │   ├── patient_report.html
#     │   └── wsi_{file_id}/
#     └── collab/                     # 协作模式
#         ├── patient_report.html
#         └── wsi_{file_id}/
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


def get_output_paths(file_id: str, mode: str = 'auto', patient_id: str = None, filename: str = None) -> dict:
    """
    获取统一的输出路径（根据模式和类型自动生成）
    
    命名格式：{原始文件名}_{分析时间戳}，例如：sample_001_20260211_143052
    
    Args:
        file_id: WSI 文件 ID
        mode: 'auto' 或 'collaborative'
        patient_id: Patient Level 模式下的患者 ID（可选）
        filename: 原始文件名（可选，用于生成可读的目录名）
    
    Returns:
        dict: {
            'output_dir': Path,      # 主输出目录
            'steps_dir': Path,       # 步骤图片目录
            'report_html': str,      # HTML 报告路径（前端用）
            'report_txt': str,       # TXT 报告路径（前端用）
            'mode_subdir': str,      # 模式子目录名 ('auto' 或 'collab')
            'dir_name': str,         # 目录名（文件名_时间戳）
        }
    """
    # 规范化模式名称。单 Agent 游走使用独立目录，禁止与现有
    # Swarm auto/collab 的树、缓存和报告互相覆盖。
    if mode == 'single_agent_roam':
        mode_subdir = 'single_agent_roam'
    else:
        mode_subdir = 'collab' if mode in ('collaborative', 'patient_collaborative_draft') else 'auto'
    
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
    
    # ⭐ 批量运行输出目录（可选环境变量 PATHCOPILOT_OUTPUT_TAG）：
    #   outputs/{TAG}/{样本名}/auto/...；样本文件夹用样本名，无时间戳，便于批量核对。
    #   未设该环境变量时, 完全保持网站原有命名行为(文件名_时间戳_numN)。
    _batch_tag = os.environ.get("PATHCOPILOT_OUTPUT_TAG", "").strip()
    if _batch_tag and not patient_id:
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

    if patient_id:
        # Patient Level 模式
        patient_dir_name = patient_id if patient_id.startswith('patient_') else f"patient_{patient_id}"
        base_dir = Path(f"outputs/{patient_dir_name}")
        mode_dir = base_dir / mode_subdir

        # 清理同一切片的旧输出目录（同 safe_name 但不同时间戳）
        if mode_dir.exists():
            import shutil
            old_prefix = f"wsi_{safe_name}_"
            for old_dir in mode_dir.iterdir():
                if old_dir.is_dir() and old_dir.name.startswith(old_prefix):
                    try:
                        shutil.rmtree(str(old_dir))
                        logger.info(f"[Patient Level] 🗑️ 已删除同切片旧目录: {old_dir.name}")
                    except Exception as _e:
                        logger.warning(f"[Patient Level] ⚠️ 删除旧目录失败: {old_dir.name}: {_e}")

        output_dir = mode_dir / f"wsi_{dir_name}"
        report_html = f'/outputs/{patient_dir_name}/{mode_subdir}/wsi_{dir_name}/pathology_report.html'
        report_txt = f'/outputs/{patient_dir_name}/{mode_subdir}/wsi_{dir_name}/pathology_report.txt'
    else:
        # 单张 Slide 模式
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
        'dir_name': dir_name,  # 返回目录名，便于调试
    }


def _write_single_output_owner_manifest(base_dir: Path, file_id: str) -> None:
    """Bind one standalone output directory to the uploaded WSI owner.

    Direct ``/outputs`` URLs are part of the product UI, so their authorization
    cannot depend on a guess derived from a human-readable directory name.
    Patient outputs use their own Patient manifest and are intentionally not
    handled here.
    """
    try:
        from app import uploaded_files
        file_info = uploaded_files.get(file_id) or {}
        owner_id = str(file_info.get('user_id') or '').strip()
        if not owner_id:
            logger.warning('未写入单片输出所有权记录：file_id=%s 缺少 owner', file_id)
            return
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        manifest = base_dir / '.owner.json'
        payload = {
            'object_type': 'single_wsi',
            'file_id': str(file_id),
            'owner_id': owner_id,
            'created_at': datetime.now().isoformat(),
        }
        try:
            with open(manifest, 'x', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except FileExistsError:
            existing = json.loads(manifest.read_text(encoding='utf-8'))
            if (
                existing.get('object_type') != 'single_wsi'
                or existing.get('file_id') != str(file_id)
                or existing.get('owner_id') != owner_id
            ):
                raise PermissionError('单片输出目录已绑定到不同对象')
    except Exception:
        logger.exception('写入单片输出所有权记录失败: file_id=%s', file_id)
        raise


def get_patient_output_paths(patient_id: str, mode: str = 'auto') -> dict:
    """
    获取 Patient Level 模式的患者级别输出路径
    
    Args:
        patient_id: 患者 ID
        mode: 'auto' 或 'collaborative'
    
    Returns:
        dict: {
            'patient_dir': Path,         # 患者主目录
            'patient_report_html': str,  # 患者综合报告路径
        }
    """
    mode_subdir = 'collab' if mode in ('collaborative', 'patient_collaborative_draft') else 'auto'
    patient_dir_name = patient_id if patient_id.startswith('patient_') else f"patient_{patient_id}"
    patient_dir = Path(f"outputs/{patient_dir_name}") / mode_subdir
    
    return {
        'patient_dir': patient_dir,
        'patient_report_html': f'/outputs/{patient_dir_name}/{mode_subdir}/patient_report.html',
        'mode_subdir': mode_subdir,
    }


# Agent Loop 的 Plan 动作定义（A/B/C/D/E 编号 + 中文标注，兼顾稳定性和可读性）
AGENT_LOOP_ACTIONS = {
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

# Backward compatibility for integrations that imported the historical name.
MCP_PLAN_ACTIONS = AGENT_LOOP_ACTIONS


def _normalize_output_language(lang: str) -> str:
    """Collapse locale variants to the two output languages supported by the UI."""
    return "en" if str(lang or "").strip().lower().startswith("en") else "zh"


def _natural_language_contract(lang: str) -> str:
    """Language boundary for prompts without Agent Loop section markers."""
    if _normalize_output_language(lang) == "en":
        return """

## Output language (mandatory)
- Write every natural-language observation, reason, plan, evidence item and conclusion in English.
- If inherited context is Chinese, use its meaning but restate it in English; never copy Chinese passages.
"""
    return """

## 输出语言（强制）
- 除结构化标签、ROI ID、代码及通用医学缩写外，所有自然语言内容必须使用简体中文。
- 即使父级上下文、参考资料或上游输出是英文，也只能理解其含义后用简体中文重新表述，不得复制英文句段。
"""


def _language_output_contract(lang: str) -> str:
    """Hard language boundary shared by orchestrator and every ROI sub-agent."""
    base_contract = _natural_language_contract(lang)
    if _normalize_output_language(lang) == "en":
        return base_contract + """
- Start the structured response directly with [Reason]. Do not output analysis drafts, process narration, or headings before [Reason].
"""
    return base_contract + """
- 必须直接从【Reason】或 [Reason] 开始输出；禁止在其前面输出分析草稿、思考过程、过程叙述或英文小标题。
- 不要输出 “Refining the Selection”“Defining the Regions” 等英文过程标题。
"""


def _strip_structured_preamble(response: str) -> str:
    """Remove model narration before the first structured Agent Loop section."""
    if not isinstance(response, str):
        return response
    positions = [
        position for marker in ("【Reason】", "[Reason]")
        if (position := response.find(marker)) >= 0
    ]
    if positions:
        return response[min(positions):].lstrip()
    return response


def build_agent_loop_prompt(
    zoom_level: int,
    roi_id: str,
    num_candidates: int,
    parent_context: str = "",
    neighbor_context: str = "",
    knowledge_context: str = "",
    organ: str = "",
    is_terminal: bool = False,
    lang: str = 'zh',
    diagnostic_objective: str = ""
) -> str:
    """
    构建规范化的 PathCopilot Agent Loop 提示词（支持中英文双语）。
    
    Args:
        zoom_level: 当前放大倍率 (10/40/160)
        roi_id: 当前 ROI 的 ID
        num_candidates: 子区域候选数量（用于下钻选择）
        parent_context: 路径上下文（父级分析结果）
        neighbor_context: 邻近区域参考（空间搜索结果）
        knowledge_context: 当前 Subagent 主动调用外部知识工具后的结果
        organ: 器官类型
        is_terminal: 是否为终点（160x 绿框）
        lang: 语言 ('zh' 或 'en')
    
    Returns:
        str: 规范化的 Agent Loop Prompt
    """
    lang = _normalize_output_language(lang)
    reason_guidance = _get_reason_guidance(zoom_level, lang)
    plan_options = _get_plan_options(zoom_level, num_candidates, is_terminal, lang)
    active_objective = get_active_diagnostic_objective(diagnostic_objective, lang)
    specimen_label = (organ or '').strip() or ("Not specified" if lang == 'en' else "未指定")
    if lang == 'en':
        zoom_desc = {
            10: "10× Low-power (Red box) — Global architecture survey",
            40: "40× Medium-power (Blue box) — Morphological analysis",
            160: "160× High-power (Green box) — Cytological assessment"
        }.get(zoom_level, f"{zoom_level}× magnification")

        prompt = f"""You are **PathCopilot Sub-agent**, dispatched and supervised by the **Orchestrator**.

## System Architecture
- **Orchestrator**: handles task scheduling, evidence aggregation, and final diagnosis
- **Sub-agent (you)**: responsible for observing and analyzing the current field of view, and planning the next action via the PathCopilot Agent Loop

Your analysis will be collected by the Orchestrator and integrated into the final pathology diagnostic report.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current position: {zoom_desc} | ROI: {roi_id}
🏥 Specimen type: {specimen_label}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**[Diagnostic Objectives]** The Orchestrator requires you to collect evidence to answer:
{active_objective}
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
        if knowledge_context:
            prompt += f"""
**[External Evidence]** (result of your D(Knowledge) MCP tool call)
{knowledge_context}

Use this evidence to refine the differential diagnosis. Do not treat an
abstract as slide-level proof and do not repeat D(Knowledge) for the same view.
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

[Evidence] (for the Memory phase — evidence consolidation)
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
- **Sub-agent（你）**：负责当前视野的观察分析和路径规划，执行 PathCopilot Agent Loop

你的分析结果将被 Orchestrator 收集并整合，用于生成最终病理诊断报告。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前位置：{zoom_desc} | ROI: {roi_id}
🏥 标本类型：{specimen_label}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【诊断目标】Orchestrator 需要你收集证据，帮助回答以下问题：
{active_objective}
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
        if knowledge_context:
            prompt += f"""
【外部循证参考】（你发起 D(查库) 后通过 MCP 工具获得）
{knowledge_context}

请用该证据校准鉴别诊断，但不得把文献摘要当作当前切片的形态学证据；
同一视野不要再次选择 D(查库)。
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

【Evidence】（为 Memory 阶段准备，用于证据固化）
- evidence: （当前路径下钻至当前视野下的证据发现）
- confidence: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例："- confidence: 3 (较确定，特征较明确)"
"""

    return prompt + _language_output_contract(lang)


# Backward-compatible import name; new code should use build_agent_loop_prompt.
build_mcp_prompt = build_agent_loop_prompt


def _get_reason_guidance(zoom_level: int, lang: str = 'zh') -> str:
    """根据倍率获取 Reason 阶段的简要指导（目标导向，不指定具体问题）"""

    if lang == 'en':
        if zoom_level == 10:
            return """You are observing under **10× low-power magnification**.

Focus on tissue adequacy, overall architecture, spatial distribution, lesion extent and borders, necrosis, inflammation, hemorrhage, and technical artifact. Distinguish representative regions from suspicious, discordant, or non-diagnostic regions.

This level can support whole-slide architectural localization and the choice of fields for further review. It must not be used for final cellular lineage, cytological grade, or a diagnosis that requires higher-power evidence. Any malignant interpretation at 10× is only an architectural suspicion pending 40×/160× verification; do not label definite carcinoma, malignant cells, or destructive invasion from low-power appearance alone."""
        elif zoom_level == 40:
            return """You are observing under **40× medium-power magnification**.

Focus on growth pattern, structural organization, lesion borders, relationships with surrounding tissue, stromal response, and heterogeneity. Identify which observations genuinely require 160× verification.

Describe morphology visible at this scale before naming a cell population. A dense, dark, or solid-appearing area is not by itself proof of carcinoma, invasion, or malignant cells. When cell identity or malignancy remains unresolved, use evidence-graded language such as “suspicious,” “favors,” or “requires high-power confirmation,” and list only the differentials that fit the visible tissue context.

The downstream 160× field must independently verify cellular identity and fine morphology. Upstream location and architectural context do not substitute for direct high-power evidence or predetermine its conclusion."""
        else:  # 160x
            return """You are observing under **160× high-power magnification**.

Assess in order: (1) whether the field is technically evaluable; (2) directly visible positive lineage or differentiation clues; (3) nuclear, chromatin, cytoplasmic, polarity, mitotic, and locally destructive features; (4) relevant mimics actually present in this field; and (5) a calibrated synthesis.

Do not infer lineage by exclusion. If positive morphology is insufficient or conflicting, preserve an indeterminate or suspicious interpretation. This selected high-power field may confirm a local finding, but it cannot by itself establish whole-slide proportions, grade, extent, or a patient-level conclusion."""
    else:
        if zoom_level == 10:
            return """你正在 **10× 低倍镜** 下观察。

重点观察取材是否足够、整体组织架构、空间分布、病变范围与边界，以及坏死、炎症、出血和技术伪影；区分代表性区域、可疑区域、与主体不一致区域及无诊断价值区域。

本倍率可用于全片架构定位和选择进一步观察的视野，但不能据此完成细胞谱系定性、细胞学分级，或作出依赖更高倍证据的最终诊断。10× 下若怀疑恶性，只能作为等待 40×/160× 核验的结构性怀疑；不得仅凭低倍外观写成明确癌、明确恶性细胞或明确破坏性浸润。"""
        elif zoom_level == 40:
            return """你正在 **40× 中倍镜** 下观察。

重点观察生长方式、结构组织、病变边界、与周围组织的关系、间质反应及异质性，并指出哪些观察确实需要 160× 核验。

应先描述本倍率可见形态，再对细胞群命名。深染、密集或实性外观本身不能证明癌巢、浸润或恶性细胞。若细胞性质或恶性判断尚未解决，应使用“可疑”“倾向”或“需高倍确认”等证据分级语言，并只列出符合当前组织背景的相关鉴别。

160× 下级视野必须独立核验细胞性质和精细形态；上级的定位与架构背景不能替代高倍直接证据，也不能预先决定其结论。"""
        else:  # 160x
            return """你正在 **160× 高倍镜** 下观察。

请依次核验：（1）当前视野是否具备可评估性；（2）可直接指认的正向谱系或分化线索；（3）细胞核、染色质、胞质、极性、核分裂及局部破坏性特征；（4）当前视野实际存在的相关模拟；（5）校准后的综合判断。

不得仅靠排除法反推谱系。若正向形态证据不足或相互冲突，应保留“可疑”或“无法确定”。定向选择的单个高倍视野可以确认局部发现，但不能单独决定全片比例、分级、范围或患者级结论。"""


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
- Parameter: list of selected region indices, e.g. [1, 5, 9]""")
        options.append(f"""**B(Stop)** → Current evidence is sufficient
- The current {zoom_level}× view clearly answers the diagnostic question
- Use when: definitive features already visible; or region shows no abnormality
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
- 参数：选择的区域编号列表，如 [1, 5, 9]""")
        options.append(f"""**B(停止)** → 当前证据已足够
- 当前 {zoom_level}× 视野已能明确回答诊断问题
- 适用：已见明确特征，无需更高倍镜；或当前区域无异常
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


def parse_agent_loop_response(response: str, valid_indices=None) -> dict:
    """Thin wrapper around the shared action-first parser."""
    return parse_mcp_response(response, valid_indices=valid_indices)


def parse_agent_loop_selection(response: str, valid_indices=None):
    """Thin wrapper around the shared selection parser. Returns indices only."""
    return parse_mcp_selection(response, valid_indices=valid_indices)




def should_execute_backtrack(parsed_response: dict) -> tuple:
    """
    判断是否应该执行回溯
    
    回溯是 Sub-agent 的自主决策，通过选择「回溯」动作触发。
    这符合 Agent Loop 的设计：Sub-agent 拥有完整的元认知能力。
    
    Args:
        parsed_response: parse_agent_loop_response 的返回结果
    
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


def handle_agent_loop_action(
    agent_loop_result: dict = None,
    roi_info: dict = None,
    agent_state: dict = None,
    socketio=None,
    room=None,
    neighbor_context_override=None,
    **legacy_kwargs,
) -> dict:
    """Execute one A–E decision.

    Missing or unknown actions fail closed as stop. B/C/D/E never keep a
    harvested ROI list. D without a query does not invent one or call a tool.
    Website D may still use outbound PubMed MCP, then local retrieval.
    """
    if agent_loop_result is None:
        agent_loop_result = legacy_kwargs.pop('mcp_result', None)
    if agent_loop_result is None:
        agent_loop_result = {}
    if roi_info is None:
        roi_info = {}
    if agent_state is None:
        agent_state = {}

    raw_action = agent_loop_result.get(
        'agent_action', agent_loop_result.get('mcp_action')
    )
    agent_reason = agent_loop_result.get(
        'agent_reason', agent_loop_result.get('mcp_reason', '')
    )
    selected_rois = list(agent_loop_result.get('selected_rois') or [])
    roi_id = roi_info.get('id', '?')

    cn_to_letter = {'下钻': 'A', '停止': 'B', '回溯': 'C', '查库': 'D', '查邻': 'E'}
    non_drill = {'B', 'C', 'D', 'E'}
    all_actions = {'A', 'B', 'C', 'D', 'E'}
    if isinstance(raw_action, str):
        agent_action = raw_action.strip()
        if agent_action in cn_to_letter:
            agent_action = cn_to_letter[agent_action]
        elif agent_action.upper() in all_actions:
            agent_action = agent_action.upper()
        else:
            agent_action = ''
    else:
        agent_action = ''

    if agent_action in non_drill:
        selected_rois = []

    if agent_action == 'A':
        logger.info(f"🔽 [A(下钻)] {roi_id}: 选中 {len(selected_rois)} 个子区域")
        return {
            'action': 'continue',
            'selected_rois': selected_rois,
            'reason': f'下钻到 {len(selected_rois)} 个子区域'
        }

    if agent_action == 'B':
        logger.info(f"⏹️ [B(停止)] {roi_id}: 当前证据已足够 - {agent_reason[:50] if agent_reason else ''}")
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"{roi_id}: 当前证据已足够，停止继续放大",
                'level': 'info',
                'roi_id': roi_id,
                'loop_action': 'B',
                'chat_style': 'agent_status',
            }, to=room)
        return {
            'action': 'stop',
            'selected_rois': [],
            'reason': agent_reason or '当前证据已足够'
        }

    if agent_action == 'C':
        logger.warning(f"⏪ [C(回溯)] {roi_id}: {agent_reason[:50] if agent_reason else '视野质量问题'}")
        backtrack_record = {
            'roi_id': roi_id,
            'reason': agent_reason,
            'timestamp': datetime.now().isoformat(),
            'trigger': 'AI_decision'
        }
        agent_state.setdefault('backtrack_history', []).append(backtrack_record)
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'warning',
                'content': f"{roi_id}: 当前视野无诊断价值，已回溯",
                'level': 'warning',
                'roi_id': roi_id,
                'loop_action': 'C',
                'chat_style': 'agent_status',
            }, to=room)
        return {
            'action': 'backtrack',
            'selected_rois': [],
            'reason': agent_reason or '视野质量问题'
        }

    if agent_action == 'D':
        logger.info(f"🔍 [D(查库)] {roi_id}: {agent_reason[:50] if agent_reason else '知识检索'}")
        search_query = str(agent_reason or '').strip()
        if not search_query:
            logger.warning(f"🔍 [D(查库)] {roi_id}: Plan 缺少检索问题，不调用知识工具")
            return {
                'action': 'stop',
                'selected_rois': [],
                'reason': 'D 动作缺少检索问题，当前层停止',
                'tool_status': 'not_run_missing_query',
            }

        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"🔍 {roi_id}: AI 请求检索外部知识\n查询: {search_query}",
                'level': 'info'
            }, to=room)

        search_result = ''
        retrieval_transport = 'legacy-local'
        tool_status = 'ok'

        def _local_knowledge(query):
            local_fn = globals().get('search_grading_criteria')
            if callable(local_fn):
                return local_fn(query), 'legacy-local'
            report_fn = globals().get('get_knowledge_for_report')
            if callable(report_fn):
                return report_fn(query), 'legacy-local'
            return None, 'unavailable'

        try:
            use_outbound_mcp = bool(
                call_mcp_tool
                and is_outbound_mcp_enabled
                and is_outbound_mcp_enabled()
            )
            if use_outbound_mcp:
                if socketio and room:
                    socketio.emit('agent.mcp_tool', {
                        'status': 'running',
                        'phase': 'knowledge_retrieval',
                        'kind': 'outbound_mcp',
                        'action': 'tools/call',
                        'server': 'pubmed',
                        'tool': 'search_pubmed',
                        'count': 1,
                    }, to=room)
                outcome = call_mcp_tool(
                    'pubmed',
                    'search_pubmed',
                    {'query': search_query, 'max_results': 3},
                )
                retrieval_transport = 'mcp'
                structured = (
                    outcome.structured_content
                    if MCPCallResult and isinstance(outcome, MCPCallResult)
                    else None
                )
                if isinstance(structured, dict):
                    search_result = (
                        structured.get('context')
                        or json.dumps(structured, ensure_ascii=False)
                    )
                else:
                    search_result = getattr(outcome, 'content', '') or str(outcome)
            else:
                search_result, retrieval_transport = _local_knowledge(search_query)
                if search_result is None:
                    tool_status = 'unavailable'
                    retrieval_transport = 'unavailable'
                    search_result = '本地知识检索函数不可用。'
            if not search_result:
                tool_status = 'no_match'
                search_result = '知识库未找到与该查询匹配的参考条目。'
            logger.info(
                f"🔍 知识检索完成 (transport={retrieval_transport}): "
                f"{str(search_result)[:100]}..."
            )
            if socketio and room:
                socketio.emit('agent.mcp_tool', {
                    'status': 'succeeded',
                    'phase': 'knowledge_retrieval',
                    'kind': 'outbound_mcp' if retrieval_transport == 'mcp' else 'local_fallback',
                    'action': 'tools/call' if retrieval_transport == 'mcp' else 'python_call',
                    'server': 'pubmed' if retrieval_transport == 'mcp' else 'local',
                    'tool': 'search_pubmed' if retrieval_transport == 'mcp' else 'knowledge_retrieval',
                    'count': 1,
                }, to=room)
        except Exception as e:
            logger.warning(f"⚠️ MCP 知识检索失败，降级到历史本地检索: {e}")
            retrieval_transport = 'local-fallback-after-mcp-error'
            try:
                search_result, retrieval_transport = _local_knowledge(search_query)
                if search_result is None:
                    raise RuntimeError("本地知识检索函数不可用")
                if retrieval_transport == 'legacy-local':
                    retrieval_transport = 'local-fallback-after-mcp-error'
            except Exception as fallback_error:
                logger.error(f"知识检索失败: {fallback_error}")
                search_result = f"知识检索失败: {fallback_error}"
                retrieval_transport = 'failed'
                tool_status = 'failed'
            if socketio and room:
                socketio.emit('agent.mcp_tool', {
                    'status': 'failed' if retrieval_transport == 'failed' else 'fallback',
                    'phase': 'knowledge_retrieval',
                    'kind': 'outbound_mcp',
                    'action': 'tools/call',
                    'server': 'pubmed',
                    'tool': 'search_pubmed',
                    'failed': 1,
                }, to=room)

        return {
            'action': 'knowledge',
            'selected_rois': [],
            'reason': '知识检索完成',
            'search_query': search_query,
            'search_result': search_result,
            'retrieval_transport': retrieval_transport,
            'tool_status': tool_status,
        }

    if agent_action == 'E':
        logger.info(f"🔎 [E(查邻)] {roi_id}: {agent_reason[:50] if agent_reason else '空间检索'}")
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"🔎 {roi_id}: AI 请求搜索相邻区域\n关系: {agent_reason}",
                'level': 'info'
            }, to=room)
        if neighbor_context_override is None:
            neighbor_ctx = get_neighbor_context(roi_id, agent_state, max_neighbors=3)
        else:
            neighbor_ctx = str(neighbor_context_override or '')
        if not neighbor_ctx:
            neighbor_ctx = f"【邻近区域参考】\n当前区域 {roi_id} 的邻近区域尚未分析完成，暂无可参考信息。\n"
            logger.info(f"🔎 [E(查邻)] {roi_id}: 邻近区域尚无已分析结果")
        else:
            logger.info(f"🔎 [E(查邻)] {roi_id}: 获取到邻近区域上下文 (长度={len(neighbor_ctx)})")
        return {
            'action': 'neighbor',
            'selected_rois': [],
            'reason': agent_reason or '空间检索',
            'neighbor_query': agent_reason,
            'neighbor_context': neighbor_ctx
        }

    logger.warning(
        f"⚠️ [Agent Loop] 未知或缺失动作 '{agent_action}'，fail-closed 停止，不推断下钻"
    )
    return {
        'action': 'stop',
        'selected_rois': [],
        'reason': agent_reason or '未知动作，fail-closed 停止'
    }


# Backward-compatible import name; new code should use handle_agent_loop_action.
handle_mcp_action = handle_agent_loop_action


def _effective_action_letter(action_result: dict) -> str:
    """Map the runtime outcome back to the public A/B/C action vocabulary."""
    return {
        'continue': 'A',
        'stop': 'B',
        'backtrack': 'C',
    }.get((action_result or {}).get('action'), '')


def _bounded_tool_replan(
    initial_result,
    action_result,
    roi_info,
    agent_state,
    replan_callback,
    socketio=None,
    room=None,
    neighbor_context_override=None,
):
    """Inject one D/E result locally and replan the same view once."""
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
    if room and _agent_stop_requested(room):
        return initial_result, {
            'action': 'stop',
            'selected_rois': [],
            'reason': '用户已停止分析',
            'stopped': True,
        }, [tool_trace]
    try:
        replanned_result = replan_callback(replanning_context)
    except TypeError:
        replanned_result = replan_callback()

    replanned_action = str(
        replanned_result.get('agent_action')
        or replanned_result.get('mcp_action')
        or ''
    ).strip().upper()
    if replanned_action in {'D', 'E'}:
        logger.warning(
            f"🛑 [Agent Loop] {roi_info.get('id', '?')}: 同一视野再次请求 "
            f"{replanned_action}，已达回注上限；保留当前层为叶节点"
        )
        bounded_stop = {
            'action': 'stop',
            'selected_rois': [],
            'reason': f'{replanned_action} 回注已达一次上限',
            'bounded_replan': True,
        }
        return replanned_result, bounded_stop, [tool_trace]

    final_action = handle_agent_loop_action(
        agent_loop_result=replanned_result,
        roi_info=roi_info,
        agent_state=agent_state,
        socketio=socketio,
        room=room,
        neighbor_context_override=neighbor_context_override,
    )
    return replanned_result, final_action, [tool_trace]


def resolve_mcp_action_with_one_replan(
    initial_result: dict,
    roi_info: dict,
    agent_state: dict,
    replan_callback,
    socketio=None,
    room=None,
    neighbor_context_override=None,
):
    """Execute one A–E decision and, for D/E, replan the same view once."""
    action_result = handle_agent_loop_action(
        agent_loop_result=initial_result,
        roi_info=roi_info,
        agent_state=agent_state,
        socketio=socketio,
        room=room,
        neighbor_context_override=neighbor_context_override,
    )
    return _bounded_tool_replan(
        initial_result,
        action_result,
        roi_info,
        agent_state,
        replan_callback,
        socketio=socketio,
        room=room,
        neighbor_context_override=neighbor_context_override,
    )


def resolve_agent_tool_actions(
    initial_result: dict,
    initial_action: dict = None,
    *,
    roi_info: dict,
    agent_state: dict,
    rerun=None,
    replan_callback=None,
    socketio=None,
    room=None,
    neighbor_context_override=None,
    max_tool_rounds: int = 2,
):
    """Website wrapper: keep the historical kwargs while bounding D/E to one replan."""
    callback = replan_callback or rerun
    if callback is None:
        raise TypeError("resolve_agent_tool_actions requires rerun or replan_callback")
    if initial_action is None:
        result, action, _trace = resolve_mcp_action_with_one_replan(
            initial_result,
            roi_info,
            agent_state,
            callback,
            socketio=socketio,
            room=room,
            neighbor_context_override=neighbor_context_override,
        )
        return result, action
    result, action, _trace = _bounded_tool_replan(
        initial_result,
        initial_action,
        roi_info,
        agent_state,
        callback,
        socketio=socketio,
        room=room,
        neighbor_context_override=neighbor_context_override,
    )
    return result, action


# Backward-compatible import name; new code should use resolve_agent_tool_actions.
resolve_mcp_tool_actions = resolve_agent_tool_actions


# ============== 红框选择提示词（Orchestrator 全局路径规划）==============
def get_red_selection_prompt_agent_loop(num_red_boxes, organ="", lang='zh', diagnostic_objective=""):
    """
    红框选择提示词 - Orchestrator 全局路径规划 (双语)
    
    Args:
        num_red_boxes: 红框候选数量
        organ: 器官类型
        lang: 语言 ('zh' 或 'en')
    """
    lang = _normalize_output_language(lang)
    active_objective = get_active_diagnostic_objective(diagnostic_objective, lang)
    specimen_label = (organ or '').strip() or ("Not specified" if lang == 'en' else "未指定")
    en_scan_instruction = (
        "Observe the thumbnail systematically, describe the whole-slide architecture and all findings relevant to the current objective, "
        "then identify representative, suspicious, discordant, or technically limited regions that require higher-magnification review."
    )
    zh_scan_instruction = (
        "系统观察缩略图，描述全切片架构以及与当前任务相关的发现；随后识别需要更高倍率复核的代表性、可疑、不一致或受技术限制的区域。"
    )
    en_evidence_note = "Include objective whole-slide observations and the basis for dispatching Sub-agents."
    zh_evidence_note = "请记录客观的全切片观察，以及派发 Subagent 的依据。"

    if lang == 'en':
        return f"""You are **PathCopilot Orchestrator**, performing global path planning.

## System Architecture
- **Orchestrator (you)**: responsible for global path planning, task scheduling, evidence aggregation, and final diagnosis
- **Sub-agent**: responsible for local analysis in each field of view, executing the PathCopilot Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current phase: Global path planning (thumbnail → select red boxes)
🏥 Specimen type: {specimen_label}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Diagnostic Objectives

All your observations and decisions serve to collect evidence for the following questions:
{active_objective}

## Current Task

You are viewing the **thumbnail** of the entire slide (global view).
The thumbnail has been divided into **{num_red_boxes} red box candidates** (numbered 1–{num_red_boxes}).

Like an experienced pathologist performing the first low-power scan after receiving a slide, conduct a systematic and thorough global survey:
1. {en_scan_instruction}
2. Select red boxes for drill-down analysis (selected regions will be examined in detail by Sub-agent assistants at higher magnification)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 Output Format

[Reason]
(Your detailed observations, analysis, and reasoning of the thumbnail)

[Plan]
Selected red box regions = [list of indices]
Rationale: (explain why you selected these regions)

[Evidence] (for the Memory phase — evidence consolidation)
- evidence: (findings observed in the current field. {en_evidence_note})
- confidence: output as "N (label)" where N is one of:
  · 0 (insufficient evidence, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features are fairly characteristic)
  · 4 (definitive, features are typical and unambiguous)
  Example: "- confidence: 3 (probable, features are fairly characteristic)"

⚠️ Note: the selected regions must be a pure list of numbers, e.g. [1, 3, 5, 8]
{_language_output_contract(lang)}
"""
    else:
        return f"""你是 **PathCopilot Orchestrator（编排器）**，正在执行全局路径规划。

## 系统架构
- **Orchestrator（你）**：负责全局路径规划、任务调度、证据整合、最终诊断
- **Sub-agent**：负责各视野的局部分析，执行 PathCopilot Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前阶段：全局路径规划（缩略图 → 选择红框）
🏥 标本类型：{specimen_label}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 诊断目标

你的一切观察和决策都是为了收集证据，回答以下问题：
{active_objective}

## 当前任务

你正在查看整张切片的**缩略图**（全局视野）。
当前缩略图已被划分为 **{num_red_boxes} 个红框候选**（编号 1-{num_red_boxes}）。

请像一位经验丰富的病理医生拿到切片后的第一步那样，进行系统、充分的低倍镜全局扫视：
1. {zh_scan_instruction}
2. 选择需要下钻分析的红框（选择的区域将由 Sub-agent 助手放大进行详细分析）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 输出格式（严格按此格式输出）

【Reason】
（你对缩略图的详细的观察和分析推理过程）

【Plan】
选择的红框区域 = [编号列表]
选择理由：（说明为什么选择这些区域）

【Evidence】（为 Memory 阶段准备，用于证据固化）
- evidence: （当前视野下观察到的证据发现。{zh_evidence_note}）
- confidence: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例："- confidence: 3 (较确定，特征较明确)"

⚠️ 注意：选择的区域必须是纯数字列表，如 [1, 3, 5, 8]
{_language_output_contract(lang)}
"""


# Backward-compatible import name for integrations using the historical suffix.
get_red_selection_prompt_mcp = get_red_selection_prompt_agent_loop


# 保留更早的函数名作为别名，确保兼容性
def get_red_selection_prompt(total_boxes, organ="", lang='zh', diagnostic_objective=""):
    """红框提示词兼容入口，调用 Agent Loop 版本。"""
    return get_red_selection_prompt_agent_loop(total_boxes, organ, lang, diagnostic_objective)


def get_blue_selection_prompt_agent_loop(
    red_idx,
    num_blue_boxes,
    parent_context="",
    neighbor_context="",
    knowledge_context="",
    organ="",
    lang='zh',
    diagnostic_objective="",
):
    """蓝框选择提示词 - Agent Loop 规范化版本（双语）。"""
    base_prompt = build_agent_loop_prompt(
        zoom_level=10,
        roi_id=f"R{red_idx}",
        num_candidates=num_blue_boxes,
        parent_context=parent_context,
        neighbor_context=neighbor_context,
        knowledge_context=knowledge_context,
        organ=organ,
        is_terminal=False,
        lang=lang,
        diagnostic_objective=diagnostic_objective
    )
    if lang == 'en':
        selection_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Note: Blue Box Selection

Red box R{red_idx} has been divided into **{num_blue_boxes} blue box candidates** (numbered 1–{num_blue_boxes}).

If you choose **A(Drill-down)**, specify the selected blue box indices in [Plan], e.g.: [1, 5, 9]

💡 Selection principle: fewest boxes, highest diagnostic value. Select only regions essential to the current question; fewer is better. Do not add extra boxes just in case.

⚠️ Consistency: regions flagged as "suspicious" or "needs further examination" in 【Reason】 should appear in the 【Plan】 action list. Avoid raising a concern in Reason but then dropping it from Plan.

⚠️ Format: the region list must contain only numbers, e.g. [1, 2, 3]
"""
    else:
        selection_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 特别说明：蓝框选择

当前红框 R{red_idx} 已被划分为 **{num_blue_boxes} 个蓝框候选**（编号 1-{num_blue_boxes}）。

如果选择 **A(下钻)**，请在【Plan】中指定选择的蓝框编号列表，如：[1, 5, 9]

💡 选择原则：最少框、最高性价比。只选对回答当前问题必不可少的区域，能少则少，不要为了保险再加框。

⚠️ 一致性约束：【Reason】中被思考标记为"需要进一步检查"或"可疑"的区域，需要出现在【Plan】的动作参数中。不要出现在 Reason 中提出怀疑需要放大却在 Plan 中遗漏。

⚠️ 格式要求：区域列表必须是纯数字，如 [1, 2, 3]
"""
    return base_prompt + selection_note


# Backward-compatible import name for integrations using the historical suffix.
get_blue_selection_prompt_mcp = get_blue_selection_prompt_agent_loop


def get_green_selection_prompt_agent_loop(
    blue_idx,
    num_green_boxes,
    parent_context="",
    neighbor_context="",
    knowledge_context="",
    organ="",
    lang='zh',
    diagnostic_objective="",
):
    """绿框选择提示词 - Agent Loop 规范化版本（双语）。"""
    base_prompt = build_agent_loop_prompt(
        zoom_level=40,
        roi_id=f"{blue_idx}",
        num_candidates=num_green_boxes,
        parent_context=parent_context,
        neighbor_context=neighbor_context,
        knowledge_context=knowledge_context,
        organ=organ,
        is_terminal=False,
        lang=lang,
        diagnostic_objective=diagnostic_objective
    )
    if lang == 'en':
        selection_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Note: Green Box Selection (40× → 160×)

Blue box {blue_idx} has been divided into **{num_green_boxes} green box candidates** (numbered 1–{num_green_boxes}).

**Key judgment**: Can the current 40× view already answer the diagnostic question?

Choose **A(Drill-down)** to 160× if:
- The current question depends on cellular detail, microorganisms, fine luminal or stromal structures, mitoses, or artifact discrimination
- A relevant observation remains unresolved at 40× and 160× is likely to change the answer

Choose **B(Stop)** if:
- The current 40× evidence is sufficient to answer the diagnostic question

💡 Selection principle: fewest boxes, highest diagnostic value. Select only regions essential to the current question; fewer is better. Do not add extra boxes just in case.

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
- 当前问题依赖细胞细节、微生物、细小腔隙或间质结构、核分裂象或伪影鉴别
- 某项与当前问题相关的观察在 40x 仍未解决，且 160x 可能改变答案

如果选择 **B(停止)**：
- 当前 40x 证据已足够回答诊断问题，无需下钻到 160x

💡 选择原则：最少框、最高性价比。只选对回答当前问题必不可少的区域，能少则少，不要为了保险再加框。

⚠️ 一致性约束：【Reason】中被思考标记为"需要高倍确认"或"可疑"的区域，需要出现在【Plan】的动作参数中。不要出现在 Reason 中提出怀疑需要放大却在 Plan 中遗漏。

⚠️ 格式要求：区域列表必须是纯数字，如 [1, 2, 3]；如选 B(停止)，填 []
"""
    return base_prompt + selection_note


# Backward-compatible import name for integrations using the historical suffix.
get_green_selection_prompt_mcp = get_green_selection_prompt_agent_loop



# =============
def _get_patient_level_diagnostic_objective(wsi_index=1, slide_index=1, lang='zh'):
    """Organ-agnostic task contract shared by patient-level slide agents."""
    path_prefix = f"S{wsi_index}"
    if lang == 'en':
        return f"""## Active task
This is {path_prefix}, section {slide_index}, in a multi-slide case.
Answer only the active user-defined diagnostic question. If none was supplied, objectively summarize observable morphology and unresolved information without inventing a diagnosis. Do not assume an organ, tumor type, stage, grade, invasion item, or ancillary question that the user did not request.

## Structured answer contract
1. **Conclusion** — answer the user's question directly; use an uncertainty label when the evidence is insufficient.
2. **Morphologic evidence and provenance** — separate low/intermediate-power architecture from high-power cellular evidence and cite the supporting slide/ROI path.
3. **Differential considerations** — state the main alternatives and the evidence for and against each.
4. **Limitations and next steps** — describe sampling, quality, field-of-view, or ancillary-test limitations and recommend only task-relevant follow-up.
5. **Evidence support (0–4)** — 0=no usable evidence; 1=very limited/indeterminate; 2=suggestive with meaningful alternatives; 3=strong but not definitive; 4=direct, characteristic, internally consistent evidence.

Use evidence identifiers such as {path_prefix}-R2-B4-G1. Parent observations are hypotheses and context, not substitutes for direct evidence in the current field of view. Preserve supporting, conflicting, and uncertain observations rather than forcing a majority vote."""
    return f"""## 当前任务
这是多切片病例中的 {path_prefix}（第 {slide_index} 个切面）。
只回答用户当前实际提出的诊断问题。若未提供具体问题，则仅客观汇总可观察形态及尚缺信息，不得虚构诊断。不得预设用户未要求的器官、肿瘤类型、分期、分级、浸润项目或辅助问题。

## 结构化回答约定
1. **结论**：直接回答用户问题；证据不足时明确使用不确定性表述。
2. **形态学依据与来源**：区分低/中倍结构证据与高倍细胞证据，并标注支持结论的切片/ROI 路径。
3. **鉴别要点**：列出主要鉴别及各自支持、反对证据。
4. **局限性与下一步**：说明取材、质量、视野或辅助检查限制，只建议与当前任务相关的补充步骤。
5. **证据支持度（0–4）**：0=无可用证据；1=证据极少、无法确定；2=提示性证据但存在重要鉴别；3=证据较强但未达到完全确定；4=直接、典型且内部一致的证据。

使用 {path_prefix}-R2-B4-G1 等证据标识。上级观察仅作为待验证背景，不能替代当前视野的直接证据；应同时保留支持、矛盾和不确定观察，不得用简单多数投票替代综合判断。"""


def get_patient_level_red_prompt(total_boxes, part_name="Slide 1", folder_name="Patient", wsi_index=1, total_wsi=1, organ="未指定标本", lang='zh'):
    """Patient Level 红框提示词 - Agent Loop + Evidence-Based Exploration（双语）。"""
    _diag_obj = _get_patient_level_diagnostic_objective(wsi_index=wsi_index, slide_index=wsi_index, lang=lang)
    if lang == 'en':
        return f"""You are **PathCopilot Orchestrator**, executing **Patient Level multi-slide joint analysis**.

## System Architecture
- **Orchestrator (you)**: global path planning, task scheduling, evidence aggregation, final diagnosis
- **Sub-agent**: local analysis per field of view, executing the PathCopilot Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current phase: Global path planning (thumbnail → select red boxes)
🏥 Patient: {folder_name} | Slide: **{part_name}** ({wsi_index}/{total_wsi}) | Specimen: {organ}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{_diag_obj}

## Current Task

The thumbnail has been divided into **{total_boxes} red box candidates** (numbered 1–{total_boxes}).

Like an experienced pathologist receiving a slide, perform a systematic, thorough low-power global scan:
1. Observe the thumbnail carefully and thoughtfully, describe all findings, and collect evidence for the final Patient Level pathology report.
2. Select red box regions for drill-down analysis (selected regions will be magnified and analyzed in detail by the Sub-agent)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 Output Format

[Reason]
(Your detailed observation and analytical reasoning of the thumbnail)

[Plan]
Selected red box regions = [list of indices]
Rationale: (explain why these regions were selected)
💡 Selection principle: fewest boxes, highest diagnostic value. Select only regions essential to the current question; fewer is better. Do not add extra boxes just in case.

[Evidence] (for the Memory phase — evidence consolidation)
- evidence: (findings observed in the current field of view)
- confidence: output format "N (label)", N values:
  · 0 (insufficient evidence, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features fairly characteristic)
  · 4 (definitive, features typical and unambiguous)
  Example: "- confidence: 3 (probable, features fairly characteristic)"

⚠️ Note: selected regions must be a pure list of numbers, e.g. [1, 3, 5, 8]"""

    return f"""你是 **PathCopilot Orchestrator（编排器）**，正在执行 **Patient Level 多切片联合分析**。

## 系统架构
- **Orchestrator（你）**：负责全局路径规划、任务调度、证据整合、最终诊断
- **Sub-agent**：负责各视野的局部分析，执行 PathCopilot Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前阶段：全局路径规划（缩略图 → 选择红框）
🏥 患者: {folder_name} | 切片: **{part_name}** (第 {wsi_index}/{total_wsi} 张) | 标本: {organ}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{_diag_obj}

## 当前任务

当前缩略图已被划分为 **{total_boxes} 个红框候选**（编号 1-{total_boxes}）。

请像一位经验丰富的病理医生拿到切片后的第一步那样，进行系统、充分的低倍镜全局扫视：
1. 观察缩略图，充分而深入地思考，描述你看到的所有发现，为最终的 Patient Level 病理报告收集证据。
2. 选择需要下钻分析的红框（选择的区域将由 Sub-agent 助手放大进行详细分析）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 输出格式（严格按此格式输出）

【Reason】
（你对缩略图的详细的观察和分析推理过程）

【Plan】
选择的红框区域 = [编号列表]
选择理由：（说明为什么选择这些区域）
💡 选择原则：最少框、最高性价比。只选对回答当前问题必不可少的区域，能少则少，不要为了保险再加框。 

【Evidence】（为 Memory 阶段准备，用于证据固化）
- evidence: （当前视野下观察到的证据发现）
- confidence: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例："- confidence: 3 (较确定，特征较明确)"

⚠️ 注意：选择的区域必须是纯数字列表，如 [1, 5, 9]"""


def get_patient_level_blue_prompt(red_idx, num_blue_boxes, part_name="Slide 1", folder_name="Patient", organ="未指定标本", parent_context="", wsi_index=1, lang='zh'):
    """Patient Level 蓝框提示词 - Agent Loop + Evidence-Based Exploration（双语）。"""
    _diag_obj = _get_patient_level_diagnostic_objective(wsi_index=wsi_index, slide_index=wsi_index, lang=lang)
    if lang == 'en':
        context_section = ""
        if parent_context:
            context_section = f"""
## 📋 Path Context (inherited from parent analysis)

Your task is to further collect evidence based on the results of the parent path.

**Parent path output:**
{parent_context}

Combine this with the parent analysis to seek more detailed evidence in the current field of view.
"""
        return f"""You are **PathCopilot Sub-agent**, dispatched by the **Orchestrator** to execute Patient Level analysis.

## System Architecture
- **Orchestrator**: task scheduling, evidence aggregation, final diagnosis
- **Sub-agent (you)**: observe and analyze the current field of view, plan the next action via the PathCopilot Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current position: 10× low-power (Red box R{red_idx}) → select blue boxes
🏥 Patient: {folder_name} | Slide: **{part_name}** | Specimen: {organ}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_section}
{_diag_obj}

## Current Task

Red box R{red_idx} has been divided into **{num_blue_boxes} blue box candidates** (numbered 1–{num_blue_boxes}).
⚠️ This red box's ROI ID is **R{red_idx}** (already contains the "R" prefix). Use it as-is — do NOT write "RR{red_idx}".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 Output Format

[Reason]
1. Region nature: (based on slide site {part_name}, identify the tissue type in the current view)
2. Observations: (describe histological features)
3. Evidence collection: (what evidence does this view provide for the slide's diagnostic task?)

[Plan]
Selected action: A(Drill-down) / B(Stop) / C(Backtrack) / D(Knowledge) / E(Neighbor)
Action parameter:
- A(Drill-down): selected blue box regions = [list of indices]
- B(Stop): reason why current evidence is sufficient
- C(Backtrack): reason (necrosis/artifact/blank/off-target) + suggested new position
- D(Knowledge): specific query (WHO grading criteria, differential diagnosis, etc.)
- E(Neighbor): description of spatial relationship needed
Rationale: (explain why you chose this action)

[Evidence] (for the Memory phase — evidence consolidation)
- Main findings: (findings collected along the current analysis path)
- Confidence: choose one:
  · 0 (insufficient, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features fairly characteristic)
  · 4 (definitive, features typical and unambiguous)

⚠️ Note: selected regions must be a pure list of numbers, e.g. [1, 5, 9]"""

    # ⭐ 构建路径上下文部分
    context_section = ""
    if parent_context:
        context_section = f"""
## 📋 路径上下文（继承自上级分析）

你的任务是基于上级路径的分析结果，进一步收集证据。

**上级路径输出：**
{parent_context}

请结合上级分析，在当前视野中寻找更详细的证据。
"""
    
    return f"""你是 **PathCopilot Sub-agent（子智能体）**，由 **Orchestrator（编排器）** 调度执行 Patient Level 分析。

## 系统架构
- **Orchestrator**：负责任务调度、证据整合、最终诊断
- **Sub-agent（你）**：负责当前视野的观察分析和路径规划，执行 PathCopilot Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前位置：10× 低倍镜（红框 R{red_idx}）→ 选择蓝框
🏥 患者: {folder_name} | 切片: **{part_name}** | 标本: {organ}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_section}
{_diag_obj}

## 当前任务

红框 R{red_idx} 区域已被划分为 **{num_blue_boxes} 个蓝框候选**（编号 1-{num_blue_boxes}）。
⚠️ 本红框的 ROI ID 是 **R{red_idx}**（已包含 R 前缀），请直接使用，**不要写成 RR{red_idx}**。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 输出格式（严格按此格式输出）

【Reason】
1. 区域性质：（基于切片部位 {part_name}，判断当前视野的组织类型）
2. 观察发现：（描述组织学特征）
3. 证据收集：（针对本切片诊断任务，当前视野能提供哪些证据？）

【Plan】
选择动作：A(下钻) / B(停止) / C(回溯) / D(查库) / E(查邻)
动作参数：
- A(下钻)：选择的蓝框区域 = [编号列表]
- B(停止)：说明当前证据已足够的原因
- C(回溯)：回溯原因（坏死/伪影/空白/偏离目标）+ 建议切换位置
- D(查库)：需要检索的具体问题（WHO分级标准、鉴别诊断等）
- E(查邻)：需要参考的空间关系描述
选择理由：（说明为什么做出该动作决策，若下钻，说明为什么选择这些区域）

【Evidence】（为 Memory 阶段准备，用于证据固化）
- 主要发现: （当前路径下钻至当前视野下的证据发现）
- 置信度: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例："- 置信度: 3 (较确定，特征较明确)"

💡 选择原则：最少框、最高性价比。只选对回答当前问题必不可少的区域，能少则少，不要为了保险再加框。 
⚠️ 注意：选择的区域必须是纯数字列表，如 [1, 5, 9]"""


def get_patient_level_green_prompt(blue_idx, num_green_boxes, part_name="Slide 1", folder_name="Patient", organ="未指定标本", parent_context="", wsi_index=1, lang='zh'):
    """Build the generic 40× to 160× selection prompt for a multi-slide case."""
    slide_context = (
        f"Patient/case: {folder_name} | Slide: {part_name} | Specimen: {organ}\n{parent_context}"
        if lang == 'en'
        else f"患者/病例：{folder_name} | 切片：{part_name} | 标本：{organ}\n{parent_context}"
    )
    return get_green_selection_prompt_agent_loop(
        blue_idx=blue_idx,
        num_green_boxes=num_green_boxes,
        parent_context=slide_context,
        organ=organ,
        lang=lang,
        diagnostic_objective=_get_patient_level_diagnostic_objective(
            wsi_index=wsi_index,
            slide_index=wsi_index,
            lang=lang,
        ),
    )


def get_patient_level_green_analysis_prompt(
    roi_id,
    parent_id,
    part_name="Slide 1",
    folder_name="Patient",
    organ="未指定标本",
    parent_context="",
    wsi_index=1,
    lang='zh',
):
    """Build the generic terminal evidence prompt for a multi-slide case."""
    slide_context = (
        f"Patient/case: {folder_name} | Slide: {part_name} | Specimen: {organ}\n{parent_context}"
        if lang == 'en'
        else f"患者/病例：{folder_name} | 切片：{part_name} | 标本：{organ}\n{parent_context}"
    )
    return _build_generic_terminal_evidence_prompt(
        {'id': roi_id, 'parent_id': parent_id},
        slide_context,
        lang=lang,
        objective=_get_patient_level_diagnostic_objective(
            wsi_index=wsi_index,
            slide_index=wsi_index,
            lang=lang,
        ),
    )


def build_path_summary_from_tree_nodes(tree_nodes, global_analysis='', lang='zh'):
    """从 tree_nodes 构建带 R-B-G ID 的完整路径证据文本（用于 Patient Level 单张WSI汇总 prompt）。
    参照 generate_final_pathology_report 的逻辑，保留完整 reasoning + global_analysis。
    Patient Level 与单切片模式共享同一套器官无关证据结构。
    """
    _is_en = (lang == 'en')
    path_summaries = []
    for red_node in (tree_nodes or []):
        red_id = red_node.get('id', red_node.get('roi_id', 'R?'))
        red_analysis = red_node.get('analysis', {})
        red_text = red_analysis.get('reasoning', '') or red_analysis.get('findings', 'No findings' if _is_en else '无发现')
        _r_label = f"Red Box {red_id} (10×)" if _is_en else f"红框 {red_id} (10×)"
        summary_parts = [f"🔴 {_r_label}:\n{red_text}"]

        for blue_node in red_node.get('children', []):
            blue_id = blue_node.get('id', blue_node.get('roi_id', 'B?'))
            blue_analysis = blue_node.get('analysis', {})
            blue_text = blue_analysis.get('reasoning', '') or blue_analysis.get('findings', '')
            _b_label = f"Blue Box {blue_id} (40×)" if _is_en else f"蓝框 {blue_id} (40×)"
            summary_parts.append(f"  └ 🔵 {_b_label}:\n  {blue_text}")

            for green_node in blue_node.get('children', []):
                green_id = green_node.get('id', green_node.get('roi_id', 'G?'))
                green_analysis = green_node.get('analysis', {})
                green_text = green_analysis.get('reasoning', '') or green_analysis.get('findings', '')
                conf = green_analysis.get('confidence', '?')

                _g_label = f"Green Box {green_id} (160×)" if _is_en else f"绿框 {green_id} (160×)"
                _conf_label = "confidence" if _is_en else "置信度"
                green_block = f"    └ 🟢 {_g_label} [{_conf_label}: {conf}]:\n    {green_text}"

                summary_parts.append(green_block)

        path_summaries.append("\n".join(summary_parts))

    all_text = "\n\n".join(path_summaries)

    # 注入全局缩略图分析（与单张模式一致）
    ga_text = global_analysis
    if isinstance(ga_text, dict):
        ga_text = ga_text.get('reasoning', '') or ga_text.get('analysis', '')
    elif isinstance(ga_text, list):
        ga_text = '\n'.join([str(item) for item in ga_text])
    if ga_text:
        _ga_label = "Thumbnail Global Analysis (Orchestrator — full slide overview)" if _is_en else "缩略图全局分析（Orchestrator — 全切片架构观察）"
        all_text = f"🔭 **{_ga_label}**:\n{ga_text}\n\n{'─' * 60}\n\n{all_text}"

    return all_text if all_text else ('No findings' if _is_en else '无明显病理发现')


def _extract_roi_detail(tree_nodes, roi_path, lang='zh'):
    """从 tree_nodes 中按 R-B-G 路径提取特定 ROI 的分析文本和图片路径。

    Args:
        tree_nodes: 该切片的 tree_nodes 列表
        roi_path: 如 'R3', 'R3-B1', 'R3-B1-G2'

    Returns:
        (detail_text: str, image_path: str or None)
    """
    _is_en = (lang == 'en')
    parts = [p.strip() for p in roi_path.split('-') if p.strip()]
    if not parts:
        return ('ROI not found' if _is_en else '未找到对应 ROI'), None

    def _match_id(node, target):
        nid = node.get('id', node.get('roi_id', ''))
        return nid == target or nid.endswith(target) or target in nid

    def _node_text(node, indent=0):
        nid = node.get('id', node.get('roi_id', '?'))
        analysis = node.get('analysis', {})
        reasoning = analysis.get('reasoning', '') or analysis.get('findings', '')
        conf = analysis.get('confidence', '')
        prefix = '  ' * indent
        level_icon = {'red': '🔴', 'blue': '🔵', 'green': '🟢'}.get(node.get('level', ''), '📍')
        header = f"{prefix}{level_icon} {nid}"
        if conf:
            header += f" [confidence: {conf}]"
        lines = [f"{header}:\n{prefix}{reasoning}"]
        for child in node.get('children', []):
            lines.append(_node_text(child, indent + 1))
        return '\n\n'.join(lines)

    def _get_img(node):
        return node.get('image_path') or (node.get('analysis', {}).get('image_path'))

    matched_red = next((n for n in (tree_nodes or []) if _match_id(n, parts[0])), None)
    if not matched_red:
        return (f'ROI {roi_path} not found in tree' if _is_en else f'未在路径树中找到 {roi_path}'), None

    if len(parts) == 1:
        return _node_text(matched_red), _get_img(matched_red)

    matched_blue = next((c for c in matched_red.get('children', []) if _match_id(c, parts[1])), None)
    if not matched_blue:
        return (f'ROI {roi_path} not found' if _is_en else f'未找到 {roi_path}'), None

    if len(parts) == 2:
        return _node_text(matched_blue), _get_img(matched_blue)

    matched_green = next((c for c in matched_blue.get('children', []) if _match_id(c, parts[2])), None)
    if not matched_green:
        return (f'ROI {roi_path} not found' if _is_en else f'未找到 {roi_path}'), None

    return _node_text(matched_green), _get_img(matched_green)


def _compose_detail_images(image_paths_with_labels):
    """将多张 ROI 图片拼接成一张带标签的组合图，返回 base64 字符串。

    Args:
        image_paths_with_labels: [(image_path_str, label_text), ...]

    Returns:
        base64 string or None
    """
    try:
        from PIL import Image, ImageDraw
        import io as _io
        import base64 as _b64
        from pathlib import Path as _Path

        valid = []
        for img_path, label in image_paths_with_labels:
            try:
                p = _Path(str(img_path))
                if p.exists():
                    img = Image.open(p)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    valid.append((img, label))
            except Exception:
                continue

        if not valid:
            return None

        if len(valid) == 1:
            img, _ = valid[0]
            if img.size[0] > 2000 or img.size[1] > 2000:
                img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format='JPEG', quality=95)
            return _b64.b64encode(buf.getvalue()).decode('utf-8')

        MAX_W, LABEL_H = 800, 30
        resized = []
        for img, label in valid:
            ratio = min(MAX_W / img.size[0], 1.0)
            new_h = int(img.size[1] * ratio)
            resized.append((img.resize((MAX_W, new_h), Image.Resampling.LANCZOS), label))

        total_h = sum(im.size[1] + LABEL_H for im, _ in resized)
        canvas = Image.new('RGB', (MAX_W, total_h), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        y = 0
        for im, label in resized:
            draw.rectangle([0, y, MAX_W, y + LABEL_H], fill=(40, 40, 40))
            draw.text((10, y + 5), label, fill=(255, 255, 255))
            y += LABEL_H
            canvas.paste(im, (0, y))
            y += im.size[1]

        if canvas.size[1] > 4096:
            canvas.thumbnail((MAX_W, 4096), Image.Resampling.LANCZOS)

        buf = _io.BytesIO()
        canvas.save(buf, format='JPEG', quality=95)
        return _b64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception as _e:
        logger.warning(f"⚠️ ROI 图片拼接失败: {_e}")
        return None


# ============================================================================
# Generic multiscale secondary review
#
# After all terminal paths finish, the Orchestrator may call an independent
# second-look tool for a focal, image-checkable claim that could materially alter
# the final answer.  The reviewer receives the target 160× crop, its parent-scale
# context and the whole-slide location.  No SubAgent, keyword, organ, disease,
# grading scheme or named finding can trigger the tool by itself.  Review failure
# never aborts the main run, but an uncompleted review cannot preserve the
# selected first-pass claim as confirmed.
# ============================================================================
_SECONDARY_REVIEW_ENABLED = os.getenv(
    "PATHCOPILOT_SECONDARY_REVIEW",
    "1",
).strip().lower() not in ("0", "false", "off", "no", "")


def _diagnostic_question_text(objective) -> str:
    """Return only the user-supplied question, without the generic contract."""
    if isinstance(objective, dict):
        return str(objective.get('diagnostic_question') or '').strip()
    return str(objective or '').strip()


def _secondary_review_task_text(agent_state) -> str:
    """Use the user's question, or the generic morphology task when absent."""
    question = _diagnostic_question_text(agent_state)
    if question:
        return question
    lang = _normalize_output_language(
        agent_state.get('lang', 'zh') if isinstance(agent_state, dict) else 'zh'
    )
    if lang == 'en':
        return (
            'Describe the visible slide morphology and resolve any isolated '
            'focal claim that could materially change the main morphologic conclusion.'
        )
    return '描述本张切片的可见形态，并复核可能实质性改变主要形态学结论的孤立局灶主张。'


def _secondary_review_limit() -> int:
    """Return the bounded per-slide review budget."""
    try:
        value = int(os.getenv('PATHCOPILOT_SECONDARY_REVIEW_MAX', '3'))
    except (TypeError, ValueError):
        value = 3
    return min(8, max(0, value))


def _terminal_review_refs(agent_state: dict) -> dict:
    """Index terminal analyses and their ROI metadata without copying them."""
    refs = {}
    selected = {
        str(roi.get('id') or roi.get('roi_id')): roi
        for roi in agent_state.get('selected_green_rois', [])
        if isinstance(roi, dict) and (roi.get('id') or roi.get('roi_id'))
    }

    def add(analysis, roi=None):
        if not isinstance(analysis, dict) or analysis.get('success') is False:
            return
        roi = roi if isinstance(roi, dict) else {}
        roi_id = str(
            analysis.get('roi_id')
            or roi.get('id')
            or roi.get('roi_id')
            or ''
        )
        if not roi_id:
            return
        selected_roi = selected.get(roi_id, {})
        merged_roi = {
            'id': roi_id,
            'parent_id': (
                analysis.get('parent_id')
                or roi.get('parent_id')
                or selected_roi.get('parent_id')
                or '-'.join(roi_id.split('-')[:2])
            ),
            'rect': (
                analysis.get('rect')
                or roi.get('rect')
                or selected_roi.get('rect')
                or {}
            ),
        }
        refs.setdefault(roi_id, {'analysis': analysis, 'roi': merged_roi})

    for analysis in agent_state.get('green_analyses', []):
        add(analysis)
    for red in agent_state.get('tree_nodes', []):
        for blue in red.get('children', []):
            for green in blue.get('children', []):
                add(green.get('analysis', {}), green)
    return refs


def _secondary_review_system_prompt(lang='zh') -> str:
    """Return the native review-tool policy in the task output language."""
    if _normalize_output_language(lang) == 'en':
        return (
            'You are the main PathCopilot Orchestrator and sole controller '
            'of the optional secondary-review tool. Make conservative, '
            'evidence-linked native tool decisions. Never imitate a tool '
            'call in message text.'
        )
    return (
        '你是 PathCopilot 的主 Orchestrator，也是可选二次复核工具的唯一调用决策者。'
        '请保守地作出与证据明确关联的原生工具调用决策。'
        '绝不能在普通文本中模仿或伪造工具调用。'
    )


def _secondary_review_planning_prompt(agent_state: dict, refs: dict, limit: int) -> str:
    """Build the main-Agent native tool-planning prompt after first pass."""
    question = _secondary_review_task_text(agent_state)
    terminal_evidence = []
    for roi_id, ref in refs.items():
        analysis = ref['analysis']
        evidence = analysis.get('evidence') or {}
        if isinstance(evidence, (dict, list)):
            evidence = json.dumps(evidence, ensure_ascii=False)[:4000]
        else:
            evidence = str(evidence)[:4000]
        terminal_evidence.append({
            'roi_id': roi_id,
            'parent_id': ref['roi'].get('parent_id'),
            'rect': ref['roi'].get('rect'),
            'findings': str(analysis.get('findings') or '')[:1600],
            'evidence': evidence,
            'diagnostic_impression': str(
                analysis.get('diagnosis_suggestion') or ''
            )[:1200],
            'evidence_support': analysis.get(
                'evidence_confidence',
                analysis.get('confidence_level'),
            ),
        })

    upstream = []
    seen_upstream = set()

    def add_upstream(analysis, level, fallback_roi_id=None):
        if not isinstance(analysis, dict) or analysis.get('success') is False:
            return
        roi_id = str(analysis.get('roi_id') or fallback_roi_id or '').strip()
        key = (level, roi_id)
        if not roi_id or key in seen_upstream:
            return
        seen_upstream.add(key)
        upstream.append({
            'roi_id': roi_id,
            'level': level,
            'findings': str(
                analysis.get('findings')
                or analysis.get('reasoning')
                or ''
            )[:1200],
        })

    for level_key in ('red_analyses', 'blue_analyses'):
        for analysis in agent_state.get(level_key, []):
            add_upstream(analysis, level_key.replace('_analyses', ''))
    for red in agent_state.get('tree_nodes', []):
        add_upstream(
            red.get('analysis', {}),
            'red',
            red.get('roi_id') or red.get('id'),
        )
        for blue in red.get('children', []):
            add_upstream(
                blue.get('analysis', {}),
                'blue',
                blue.get('roi_id') or blue.get('id'),
            )

    evidence_bundle = {
        'whole_slide_overview': {
            'evidence_id': 'GLOBAL',
            'findings': str(agent_state.get('global_analysis') or '')[:3500],
        },
        'human_analysis_edits': str(
            agent_state.get('user_analysis_edits', {}) or {}
        )[:4000],
        'upstream_evidence': upstream[:80],
        'terminal_evidence': terminal_evidence,
    }
    lang = _normalize_output_language(agent_state.get('lang', 'zh'))
    if lang == 'zh':
        return f"""你是 PathCopilot 的主 Orchestrator。当前所有 WSI 路径均已完成首次分析。

## 当前诊断任务
{question}

## 当前可用的证据树
{json.dumps(evidence_bundle, ensure_ascii=False, indent=2)}

## 当前可用的原生工具
你可以针对终末 ROI，通过 API 原生工具调用方式调用 `secondary_review`。该工具将独立复核指定的目标 ROI，并结合当前已保存的低倍率父级路径和/或标出目标位置的全切片定位图。不得在普通回复文本中用 JSON 或其他形式模仿工具调用。

## 自主判断是否调用该工具
只有在以下条件全部满足时，才能选择一个主张进行二次复核：
1. 该主张的成立或不成立，可能实质性改变当前诊断任务的主要结论。
2. 该主张具有孤立性或存在有意义的证据不一致。例如，该主张仅出现在一条分析路径中，而同级、邻近区域、父级倍率或其他独立证据未能支持该主张，或者这些证据提示了一个合理的模拟病变或替代解释。周围区域未发现同类证据会提高二次复核的优先级，但仅凭周围阴性不能否定真正的局灶性病变。
3. 该主张必须明确、具体，并且能够通过已经保存的多尺度图像进行复核。
4. 再次进行视觉复核能够解决这一问题，或者能够恰当地将其标记为仍不确定。

不得针对以下情况调用该工具：偶然发现或与当前诊断任务无关的发现；已经获得多条一致证据支持的主张；隐含的固定问题清单或固定癌种检查项目；需要当前不可获得的全器官范围或比例作为分母才能判断的问题；需要临床资料、特殊染色、生物标志物或分子检测结果才能判断的问题；需要进行患者级推断的问题。不得引入当前诊断任务和现有证据均未提出的器官、疾病、分级、分期、浸润目标或特定病理发现。原则上优先不调用。最多允许调用：{limit} 次。

每一次工具调用都必须沿用目标终末 ROI 首次分析中的诊断性表述，列出支持该主张和与该主张冲突的准确证据 ID，并在 `supporting_roi_ids` 中包含目标 ROI。必须分别说明：如果该主张得到确认，用户主问题的答案是什么；如果该主张被否定，用户主问题的答案是什么。这两个反事实答案应当能够体现该主张是否真的会改变主要结论。工具参数中的自然语言内容使用中文。

如果没有任何主张符合调用条件，则不要调用工具，仅简短回复 NO_SECONDARY_REVIEW。"""

    return f"""You are the PathCopilot Orchestrator after all first-pass WSI paths have completed.

## Current diagnostic task
{question}

## Available evidence tree
{json.dumps(evidence_bundle, ensure_ascii=False, indent=2)}

## Available native tool
You may make API-native calls to `secondary_review` on terminal ROIs. The tool independently reviews the exact target together with any saved lower-power parent path and/or marked whole-slide location. Do not write a JSON imitation of a tool call in message text.

## Decide autonomously whether to call the tool
Select a claim only when ALL are true:
1. It could materially change the main conclusion for the current diagnostic task.
2. It is isolated or meaningfully discordant: for example, it appears in one path while sibling, nearby, parent-scale, or other independent evidence does not support it or supports a plausible mimic. Absence nearby raises review priority but does not itself refute a genuinely focal lesion.
3. The claim is precise and image-checkable from the saved multiscale path.
4. A second visual review can resolve or appropriately mark the uncertainty.

Do not call the tool for incidental findings unrelated to the current diagnostic task, already concordant evidence, hidden checklists, or claims requiring unavailable whole-organ denominators, clinical data, stains, biomarkers, or patient-level inference. Do not introduce an organ, disease, grade, stage, invasion target, or named special finding that neither the current task nor the evidence raised. Prefer zero calls. Maximum calls: {limit}.

For every call, reuse the diagnostic wording from the target terminal evidence, enumerate the exact evidence IDs that support or conflict with the claim, and include the target ROI in supporting_roi_ids. The counterfactual answers must state how the main answer differs if the claim is confirmed versus rejected. If no call is justified, do not call a tool and reply briefly with NO_SECONDARY_REVIEW."""


def _parse_secondary_review_plan(model_message, valid_roi_ids, limit: int) -> list:
    """Parse API-native ``secondary_review`` calls; ignore text imitations."""
    if not isinstance(model_message, dict) or limit <= 0:
        return []
    tool_calls = model_message.get('tool_calls') or []
    allowed = {str(roi_id) for roi_id in valid_roi_ids}
    selected = []
    seen = set()
    for call in tool_calls if isinstance(tool_calls, list) else []:
        function = call.get('function') if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get('name') != 'secondary_review':
            continue
        raw_arguments = function.get('arguments') or {}
        try:
            item = (
                raw_arguments
                if isinstance(raw_arguments, dict)
                else json.loads(str(raw_arguments))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        roi_id = str(item.get('roi_id') or '').strip()
        claim = str(item.get('claim') or '').strip()
        reason = str(item.get('reason') or '').strip()
        confirmed = str(item.get('answer_if_confirmed') or '').strip()
        rejected = str(item.get('answer_if_rejected') or '').strip()
        supporting = item.get('supporting_roi_ids')
        conflicting = item.get('conflicting_roi_ids')
        if not isinstance(supporting, list) or not isinstance(conflicting, list):
            continue
        if (
            not roi_id
            or roi_id not in allowed
            or roi_id in seen
            or not claim
            or not reason
            or not confirmed
            or not rejected
            or not isinstance(item.get('image_checkable'), bool)
            or not isinstance(item.get('requires_nonvisual_data'), bool)
        ):
            continue
        selected.append({
            'roi_id': roi_id,
            'claim': claim[:1200],
            'reason': reason[:1200],
            'answer_if_confirmed': confirmed[:1200],
            'answer_if_rejected': rejected[:1200],
            'supporting_roi_ids': [
                str(value).strip() for value in supporting if str(value).strip()
            ][:80],
            'conflicting_roi_ids': [
                str(value).strip() for value in conflicting if str(value).strip()
            ][:80],
            'image_checkable': item['image_checkable'],
            'requires_nonvisual_data': item['requires_nonvisual_data'],
            'tool_call_id': str(call.get('id') or ''),
        })
        seen.add(roi_id)
        if len(selected) >= limit:
            break
    return selected


def _plan_secondary_reviews(agent_state: dict, refs: dict, limit=None) -> list:
    """Let the main Orchestrator choose native review-tool calls."""
    limit = _secondary_review_limit() if limit is None else max(0, int(limit))
    if not refs or limit <= 0:
        return []
    registry = get_mcp_registry() if callable(get_mcp_registry) else None
    if not registry or not registry.get_tool('secondary_review'):
        logger.warning("[Orchestrator二次复核] MCP注册表缺少 secondary_review")
        return []
    prompt = _secondary_review_planning_prompt(agent_state, refs, limit)
    tools = registry.to_openai_tool_list(['secondary_review'])
    messages = [
        {
            'role': 'system',
            'content': _secondary_review_system_prompt(
                agent_state.get('lang', 'zh')
            ),
        },
        {'role': 'user', 'content': prompt},
    ]
    try:
        model_message, error = call_agent_model(
            messages,
            max_tokens=4096,
            call_type='secondary_review_plan',
            tools=tools,
            tool_choice='auto',
            return_message=True,
        )
        if error:
            logger.warning(f"[Orchestrator二次复核] 原生工具规划失败: {error}")
            return []
    except Exception as exc:
        logger.warning(f"[Orchestrator二次复核] 规划调用失败: {exc}")
        return []
    return _parse_secondary_review_plan(model_message, refs.keys(), limit)


def _first_existing(*paths):
    """Return the first existing path, or ``None`` when all are unavailable."""
    for candidate in paths:
        try:
            if candidate and Path(candidate).exists():
                return str(candidate)
        except (OSError, TypeError, ValueError):
            continue
    return None


def _secondary_review_image_paths(agent_state, ref, roi_id, root):
    """Resolve the available thumbnail→red→blue→green review path."""
    analysis = ref.get('analysis', {}) if isinstance(ref, dict) else {}
    roi = ref.get('roi', {}) if isinstance(ref, dict) else {}
    parent_id = str(roi.get('parent_id') or '-'.join(roi_id.split('-')[:2]))
    red_id = roi_id.split('-')[0]

    def analysis_image(collection, wanted_id):
        for item in agent_state.get(collection, []):
            if not isinstance(item, dict):
                continue
            if str(item.get('roi_id') or item.get('id') or '') == wanted_id:
                return item.get('image_path')
        return None

    global_analysis = agent_state.get('global_analysis')
    global_image = (
        global_analysis.get('image_path')
        if isinstance(global_analysis, dict)
        else None
    )
    return {
        'thumbnail': _first_existing(
            root / 'global_thumbnail.jpg' if root else None,
            global_image,
        ),
        'red': _first_existing(
            root / f'red_{red_id}.jpg' if root else None,
            analysis_image('red_analyses', red_id),
        ),
        'blue': _first_existing(
            root / f'blue_{parent_id}.jpg' if root else None,
            analysis_image('blue_analyses', parent_id),
        ),
        'green': _first_existing(
            analysis.get('image_path'),
            root / f'green_{roi_id}.jpg' if root else None,
            root / red_id / f'{roi_id}_160x.jpg' if root else None,
            root / f'{roi_id}_160x.jpg' if root else None,
        ),
    }


def _review_analysis_text(analysis) -> str:
    """Flatten only diagnostic text used by the deterministic review gate."""
    if not isinstance(analysis, dict):
        return str(analysis or '')
    parts = [
        analysis.get('findings'),
        analysis.get('diagnosis_suggestion'),
        analysis.get('reasoning'),
    ]
    evidence = analysis.get('evidence')
    if evidence:
        parts.append(
            json.dumps(evidence, ensure_ascii=False)
            if isinstance(evidence, (dict, list))
            else str(evidence)
        )
    return '\n'.join(str(part) for part in parts if part)[:12000]


def _review_evidence_index(agent_state, refs) -> dict:
    """Build an exact-ID text index for structured support/conflict checks."""
    index = {
        str(roi_id): _review_analysis_text(ref.get('analysis', {}))
        for roi_id, ref in refs.items()
    }
    global_analysis = agent_state.get('global_analysis')
    index['GLOBAL'] = _review_analysis_text(global_analysis)

    def add(analysis, fallback_id=None):
        if not isinstance(analysis, dict) or analysis.get('success') is False:
            return
        evidence_id = str(
            analysis.get('roi_id') or analysis.get('id') or fallback_id or ''
        ).strip()
        if evidence_id:
            index.setdefault(evidence_id, _review_analysis_text(analysis))

    for key in ('red_analyses', 'blue_analyses'):
        for analysis in agent_state.get(key, []):
            add(analysis)
    for red in agent_state.get('tree_nodes', []):
        add(red.get('analysis', {}), red.get('roi_id') or red.get('id'))
        for blue in red.get('children', []):
            add(blue.get('analysis', {}), blue.get('roi_id') or blue.get('id'))
    return index


def _claim_term_set(text) -> set:
    """Extract small generic lexical units for auditable support matching."""
    text = str(text or '').lower()
    stop = {
        'the', 'and', 'for', 'with', 'from', 'this', 'that', 'true', 'focal',
        'finding', 'feature', 'lesion', 'present', 'shows', 'represent',
    }
    terms = {
        token for token in re.findall(r'[a-z][a-z0-9_-]{2,}', text)
        if token not in stop
    }
    for chunk in re.findall(r'[\u4e00-\u9fff]+', text):
        if len(chunk) <= 4:
            terms.add(chunk)
        for width in (2, 3):
            terms.update(
                chunk[pos:pos + width]
                for pos in range(max(0, len(chunk) - width + 1))
            )
    return terms


def _claim_match_score(claim, evidence_text) -> float:
    """Return deterministic lexical coverage of a claim by one evidence text."""
    claim_terms = _claim_term_set(claim)
    if not claim_terms:
        return 0.0
    compact_claim = re.sub(r'\W+', '', str(claim or '').lower(), flags=re.UNICODE)
    compact_text = re.sub(
        r'\W+', '', str(evidence_text or '').lower(), flags=re.UNICODE
    )
    if compact_claim and compact_claim in compact_text:
        return 1.0
    return len(claim_terms & _claim_term_set(evidence_text)) / len(claim_terms)


def _text_asserts_claim(claim, evidence_text) -> bool:
    """Conservatively identify an unnegated textual support for the claim."""
    best_sentence = ''
    best_score = 0.0
    for sentence in re.split(r'[\n。！？!?;；]+', str(evidence_text or '')):
        score = _claim_match_score(claim, sentence)
        if score > best_score:
            best_sentence, best_score = sentence, score
    if best_score < 0.55:
        return False
    negated = re.search(
        r'(^|\b)(no|not|without|absent|negative|lack(?:ing|s|ed)?)(\b|\s)'
        r'|未见|未发现|不支持|无明确|缺乏|阴性',
        best_sentence.lower(),
    )
    return not bool(negated)


def _text_conflicts_claim(claim, evidence_text) -> bool:
    """Identify an explicit negation, mimic or artifact linked to the claim."""
    best_sentence = ''
    best_score = 0.0
    for sentence in re.split(r'[\n。！？!?;；]+', str(evidence_text or '')):
        score = _claim_match_score(claim, sentence)
        if score > best_score:
            best_sentence, best_score = sentence, score
    if best_score < 0.25:
        return False
    return bool(re.search(
        r'(^|\b)(no|not|without|absent|negative|artifact|mimic|alternative|'
        r'inconsistent|discordant|rather than|lack(?:ing|s|ed)?)(\b|\s)'
        r'|未见|未发现|不支持|无明确|缺乏|阴性|伪影|模拟|替代解释|不一致|矛盾',
        best_sentence.lower(),
    ))


def _normalize_counterfactual_answer(value) -> str:
    return re.sub(r'[\W_]+', '', str(value or '').lower(), flags=re.UNICODE)


def _evaluate_secondary_review_gate(
    tool_call,
    agent_state,
    refs,
    image_paths,
):
    """Compute code-level materiality, isolation, conflict and image gates."""
    roi_id = str(tool_call.get('roi_id') or '')
    ref = refs.get(roi_id) or {}
    analysis = ref.get('analysis', {}) if isinstance(ref, dict) else {}
    evidence_index = _review_evidence_index(agent_state, refs)
    valid_ids = set(evidence_index)
    requested_support = {
        str(value) for value in tool_call.get('supporting_roi_ids', []) if value
    }
    requested_conflict = {
        str(value) for value in tool_call.get('conflicting_roi_ids', []) if value
    }
    invalid_ids = sorted((requested_support | requested_conflict) - valid_ids)

    claim = str(tool_call.get('claim') or '').strip()
    # Support/isolation is computed from exact evidence IDs supplied by the
    # Orchestrator. Lexical repetition is retained as an audit metric only: it
    # must not turn generic wording shared by several paths into false consensus.
    verified_support = requested_support & valid_ids
    other_support = sorted(verified_support - {roi_id})
    isolated = not other_support

    parent_id = str((ref.get('roi') or {}).get('parent_id') or '')
    red_id = roi_id.split('-')[0] if roi_id else ''
    valid_conflicts = sorted(requested_conflict & valid_ids)
    cross_scale_conflicts = sorted(
        set(valid_conflicts) & {'GLOBAL', parent_id, red_id}
    )
    cross_scale_conflict = bool(cross_scale_conflicts)

    confirmed_answer = _normalize_counterfactual_answer(
        tool_call.get('answer_if_confirmed')
    )
    rejected_answer = _normalize_counterfactual_answer(
        tool_call.get('answer_if_rejected')
    )
    material = bool(
        confirmed_answer
        and rejected_answer
        and confirmed_answer != rejected_answer
    )
    try:
        evidence_support = float(
            analysis.get('evidence_confidence', analysis.get('confidence_level', 0))
            or 0
        )
    except (TypeError, ValueError):
        evidence_support = 0.0
    target_claim_match = _claim_match_score(
        claim,
        evidence_index.get(roi_id, ''),
    )
    target_linked = bool(
        roi_id in verified_support
        and evidence_index.get(roi_id, '').strip()
    )
    image_checkable = bool(tool_call.get('image_checkable')) and not bool(
        tool_call.get('requires_nonvisual_data')
    )
    precise_claim = 3 <= len(claim) <= 1200
    context_images = sum(
        bool(image_paths.get(key)) for key in ('thumbnail', 'red', 'blue')
    )
    image_ready = bool(image_paths.get('green')) and context_images >= 1

    checks = {
        'material_to_main_answer': material,
        'target_has_linked_first_pass_support': target_linked,
        'isolated_or_evidence_conflict': isolated or bool(valid_conflicts),
        'precise_image_checkable_claim': precise_claim and image_checkable,
        'target_and_context_images_available': image_ready,
        'all_evidence_ids_valid': not invalid_ids,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        'passed': not failed,
        'failed_checks': failed,
        'checks': checks,
        'metrics': {
            'counterfactual_answers_differ': material,
            'target_evidence_support': evidence_support,
            'target_claim_match_score': round(target_claim_match, 3),
            'lexically_matching_evidence_ids': sorted(
                evidence_id
                for evidence_id, evidence_text in evidence_index.items()
                if evidence_id != 'GLOBAL'
                and _text_asserts_claim(claim, evidence_text)
            ),
            'supporting_evidence_ids': sorted(verified_support),
            'other_supporting_evidence_ids': other_support,
            'is_isolated': isolated,
            'conflicting_evidence_ids': valid_conflicts,
            'cross_scale_conflicting_ids': cross_scale_conflicts,
            'has_cross_scale_conflict': cross_scale_conflict,
            'context_image_count': context_images,
            'invalid_evidence_ids': invalid_ids,
            'available_images': {
                key: bool(image_paths.get(key))
                for key in ('thumbnail', 'red', 'blue', 'green')
            },
        },
    }


def _make_marked_review_thumbnail(thumb_path, rect, slide_dims, out_path):
    """Mark the candidate location on the whole-slide thumbnail."""
    try:
        from PIL import ImageDraw

        if not (
            thumb_path
            and Path(thumb_path).exists()
            and isinstance(rect, dict)
            and slide_dims
        ):
            return None
        slide_w, slide_h = float(slide_dims[0]), float(slide_dims[1])
        if slide_w <= 0 or slide_h <= 0:
            return None
        image = Image.open(thumb_path).convert('RGB')
        thumb_w, thumb_h = image.size
        scale_x, scale_y = thumb_w / slide_w, thumb_h / slide_h
        x = int(float(rect.get('x', 0)) * scale_x)
        y = int(float(rect.get('y', 0)) * scale_y)
        width = max(6, int(float(rect.get('w', 0)) * scale_x))
        height = max(6, int(float(rect.get('h', 0)) * scale_y))
        padding = max(8, int(min(thumb_w, thumb_h) * 0.02))
        box = [
            max(0, x - padding),
            max(0, y - padding),
            min(thumb_w - 1, x + width + padding),
            min(thumb_h - 1, y + height + padding),
        ]
        draw = ImageDraw.Draw(image)
        for offset in range(3):
            draw.rectangle(
                [
                    box[0] - offset,
                    box[1] - offset,
                    box[2] + offset,
                    box[3] + offset,
                ],
                outline=(255, 0, 0),
            )
        draw.text((box[0], max(0, box[1] - 14)), 'REVIEW', fill=(255, 0, 0))
        image.save(out_path, 'JPEG', quality=90)
        return str(out_path)
    except Exception as exc:
        logger.debug(f"[二次复核] 缩略图标记失败: {exc}")
        return None


def _build_secondary_review_prompt(
    roi_id,
    rect,
    diagnostic_question,
    candidate_claim,
    first_pass_evidence,
    lang='zh',
):
    """Build an organ-agnostic prompt for independent visual adjudication."""
    position = ''
    if isinstance(rect, dict) and rect:
        position = f"x={rect.get('x')}, y={rect.get('y')}"
    if lang == 'en':
        return f"""You are an independent senior pathologist performing a SECOND LOOK of one focal claim from a WSI agent.

Current diagnostic task: {diagnostic_question}
Candidate ROI: {roi_id} {position}
Claim to adjudicate: {candidate_claim}
First-pass evidence: {first_pass_evidence or 'Not recorded'}

Available images are ordered as: marked whole-slide location → red lower-power path → blue intermediate-power path → green target 160× field; unavailable context levels are omitted. Review the images independently across scales.

Rules:
1. Judge only the precise claim above and only from visible morphology.
2. Check cross-scale consistency, spatial context, image quality, representativeness, and plausible mimics or artifacts.
3. Do not infer an organ, disease, grading system, invasion target, biomarker, or special finding that is absent from the user question and visible evidence.
4. A selected high-power field can establish a local feature, but cannot alone establish a whole-slide proportion, extent, grade, or patient-level diagnosis.
5. CONFIRMED requires direct, concordant evidence sufficient for this exact claim. REJECTED means the visible evidence contradicts the claim or supports a mimic/artifact. INDETERMINATE means the images cannot resolve it.

Give a concise evidence-based reason, then end with exactly one separate line:
VERDICT: CONFIRMED
VERDICT: REJECTED
VERDICT: INDETERMINATE"""

    return f"""你是独立的资深病理医生，正在对 WSI Agent 的一个局灶主张进行二次复核。

当前诊断任务：{diagnostic_question}
候选 ROI：{roi_id} {position}
待裁决主张：{candidate_claim}
首次分析证据：{first_pass_evidence or '未记录'}

可用图像依次排列为：标出目标位置的全切片定位图 → 红框低倍路径 → 蓝框中倍路径 → 绿框目标 160× 视野；不可用的上下文层级会被省略。请跨倍率独立复核，不得照抄首次结论。

规则：
1. 只裁决上面的精确主张，并且只依据可见形态。
2. 核对跨倍率一致性、空间位置、图像质量、代表性以及可能的模拟病变或伪影。
3. 不得引入用户问题和可见证据中不存在的器官、病种、分级体系、浸润目标、生物标志物或特殊病理发现。
4. 主动选择的高倍视野可确认局部特征，但不能单独证明全片比例、范围、分级或患者级诊断。
5. CONFIRMED 表示该精确主张获得直接且跨尺度一致的充分证据；REJECTED 表示可见证据反对该主张或更支持模拟/伪影；INDETERMINATE 表示现有图像不能解决。

先给出简洁的证据理由，最后必须单独一行写三者之一：
二次复核结论: CONFIRMED
二次复核结论: REJECTED
二次复核结论: INDETERMINATE"""


def _run_secondary_review(
    roi_id,
    rect,
    red_path,
    blue_path,
    green_path,
    thumb_path,
    diagnostic_question,
    candidate_claim,
    first_pass_evidence='',
    lang='zh',
    slide_dims=None,
    steps_dir=None,
):
    """Run the independent multiscale review without interrupting the main path."""
    try:
        roi_id = str(roi_id)
        ordered_images = []
        thumbnail_for_use = None
        if thumb_path and Path(thumb_path).exists():
            thumbnail_for_use = str(thumb_path)
            if slide_dims and isinstance(rect, dict):
                output_dir = Path(steps_dir) if steps_dir else Path(thumb_path).parent
                safe_roi_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', roi_id)
                marked = _make_marked_review_thumbnail(
                    thumb_path,
                    rect,
                    slide_dims,
                    output_dir / f'_review_mark_{safe_roi_id}.jpg',
                )
                if marked:
                    thumbnail_for_use = marked
        if thumbnail_for_use:
            ordered_images.append((thumbnail_for_use, 'Whole-slide location (red mark)'))
        if red_path and Path(red_path).exists():
            ordered_images.append((str(red_path), 'Lower-power parent context'))
        if blue_path and Path(blue_path).exists():
            ordered_images.append((str(blue_path), 'Intermediate-power parent context'))
        if green_path and Path(green_path).exists():
            ordered_images.append((str(green_path), f'Target 160x field: {roi_id}'))

        has_target = bool(green_path and Path(green_path).exists())
        if not has_target or len(ordered_images) < 2:
            return {
                'verdict': 'SKIP',
                'reason': 'target image or contextual image is unavailable',
            }
        labels = [
            (path, f'[{index}] {description}')
            for index, (path, description) in enumerate(ordered_images, 1)
        ]
        composed = _compose_detail_images(labels)
        if not composed:
            return {'verdict': 'SKIP', 'reason': 'image composition failed'}

        prompt = _build_secondary_review_prompt(
            roi_id,
            rect,
            diagnostic_question,
            candidate_claim,
            first_pass_evidence,
            lang=lang,
        )
        response = call_gpt_vision(prompt, composed)
        if not response:
            return {'verdict': 'SKIP', 'reason': 'empty review response'}
        match = re.search(
            r'(?:二次复核结论|VERDICT)\s*[:：]\s*'
            r'(CONFIRMED|REJECTED|INDETERMINATE|SUSPICIOUS)',
            response,
            re.IGNORECASE,
        )
        if not match:
            return {
                'verdict': 'SKIP',
                'reason': 'unparseable verdict: ' + response.strip()[:400],
            }
        verdict = match.group(1).upper()
        if verdict == 'SUSPICIOUS':
            verdict = 'INDETERMINATE'
        return {'verdict': verdict, 'reason': response.strip()[:1200]}
    except Exception as exc:
        logger.warning(f"[二次复核] 执行异常: {exc}")
        return {'verdict': 'SKIP', 'reason': f'exception: {exc}'}


def _apply_secondary_review(parsed, response, review, claim, roi_id, lang='zh'):
    """Write the adjudication back to structured evidence and visible reasoning."""
    parsed = parsed if isinstance(parsed, dict) else {}
    evidence = parsed.get('evidence')
    if not isinstance(evidence, dict):
        evidence = {}
        parsed['evidence'] = evidence
    verdict = str(review.get('verdict') or 'SKIP').upper()
    reason = str(review.get('reason') or '')
    record = {
        'verdict': verdict,
        'claim': str(claim or ''),
        'reason': reason,
        'roi_id': str(roi_id),
    }
    evidence['secondary_review'] = record
    parsed['secondary_review'] = record
    parsed['secondary_review_verdict'] = verdict

    if lang == 'en':
        confirmed_note = (
            f"[Secondary review: {verdict}] The independent multiscale review "
            f"confirmed the nominated claim for ROI {roi_id}."
        )
        unresolved_note = (
            f"[Secondary review: {verdict}] The first-pass claim for ROI {roi_id} "
            "is not accepted as confirmed."
        )
    else:
        confirmed_note = (
            f"【二次复核：{verdict}】ROI {roi_id} 的候选主张经独立多尺度复核确认。"
        )
        unresolved_note = (
            f"【二次复核：{verdict}】ROI {roi_id} 的首次主张不作为已确认结论采纳。"
        )

    if verdict == 'CONFIRMED':
        note = confirmed_note
    else:
        note = unresolved_note
        original_summary = str(evidence.get('summary') or '').strip()
        original_findings = str(parsed.get('findings') or '').strip()
        original_diagnosis = str(parsed.get('diagnosis_suggestion') or '').strip()
        record['first_pass_snapshot'] = {
            'summary': original_summary,
            'findings': original_findings,
            'diagnosis_suggestion': original_diagnosis,
            'pathology_type': parsed.get('pathology_type'),
            'key_features': parsed.get('key_features', []),
            'evidence_confidence': parsed.get('evidence_confidence'),
        }
        if original_summary:
            evidence['first_pass_summary'] = original_summary[:4000]
        appended_note = f"{unresolved_note} {reason[:500]}".strip()
        evidence['summary'] = '\n\n'.join(
            part for part in (original_summary, appended_note) if part
        )[:8000]
        previous_limitations = str(
            evidence.get('limitations_or_counterevidence') or ''
        ).strip()
        evidence['limitations_or_counterevidence'] = ' '.join(
            part for part in (previous_limitations, unresolved_note, reason[:500]) if part
        )[:4000]
        parsed['findings'] = '\n\n'.join(
            part for part in (original_findings, appended_note) if part
        )[:8000]
        parsed['diagnosis_suggestion'] = '\n\n'.join(
            part for part in (original_diagnosis, unresolved_note) if part
        )[:4000]
        parsed['secondary_review_claim_status'] = 'not_confirmed'
        parsed['pathology_type'] = 'uncertain'
        downgraded_level = 0 if verdict == 'REJECTED' else 1
        parsed['confidence_level'] = min(
            int(parsed.get('confidence_level', downgraded_level) or 0),
            downgraded_level,
        )
        parsed['confidence'] = parsed['confidence_level'] * 25
        parsed['evidence_confidence'] = parsed['confidence_level']

    updated_response = (response or '').rstrip() + f"\n\n{note}\n{reason[:800]}"
    return parsed, updated_response


def _orchestrate_secondary_reviews(
    agent_state,
    slide,
    steps_dir,
    limit=None,
):
    """Plan native calls, enforce code gates, then dispatch via MCP registry."""
    if (
        not _SECONDARY_REVIEW_ENABLED
        or agent_state.get('is_patient_aggregation')
    ):
        agent_state['secondary_review_plan'] = []
        agent_state['secondary_review_results'] = []
        return []

    refs = _terminal_review_refs(agent_state)
    plan = _plan_secondary_reviews(agent_state, refs, limit=limit)
    if not plan:
        agent_state['secondary_review_plan'] = []
        agent_state['secondary_review_results'] = []
        logger.info("🔁 [Orchestrator二次复核] 主Agent决定不调用复核工具")
        return []

    root = Path(steps_dir) if steps_dir else None
    lang = _normalize_output_language(agent_state.get('lang', 'zh'))
    question = _secondary_review_task_text(agent_state)
    registry = get_mcp_registry() if callable(get_mcp_registry) else None
    if not registry or not callable(bind_tool_handlers):
        agent_state['secondary_review_plan'] = plan
        agent_state['secondary_review_results'] = []
        logger.warning("[Orchestrator二次复核] MCP注册表或处理器不可用")
        return []
    bind_tool_handlers(registry, agent_state)

    def execute_review(tool_call):
        roi_id = tool_call['roi_id']
        ref = refs.get(roi_id)
        if not ref:
            return {
                'tool_status': 'rejected_by_gate',
                'verdict': 'NOT_RUN',
                'review_reason': 'target ROI is absent from terminal evidence',
                'gate': {
                    'passed': False,
                    'failed_checks': ['target_roi_exists'],
                },
            }
        analysis = ref['analysis']
        roi = ref['roi']
        image_paths = _secondary_review_image_paths(
            agent_state,
            ref,
            roi_id,
            root,
        )
        gate = _evaluate_secondary_review_gate(
            tool_call,
            agent_state,
            refs,
            image_paths,
        )
        if not gate['passed']:
            logger.info(
                f"🔒 [Orchestrator二次复核] {roi_id} 未通过代码门控: "
                f"{', '.join(gate['failed_checks'])}"
            )
            review = {
                'verdict': 'NOT_RUN',
                'reason': 'code gate: ' + ', '.join(gate['failed_checks']),
            }
            analysis, updated_reasoning = _apply_secondary_review(
                analysis,
                analysis.get('reasoning') or analysis.get('findings') or '',
                review,
                tool_call['claim'],
                roi_id,
                lang=lang,
            )
            analysis['reasoning'] = updated_reasoning
            return {
                'tool_status': 'rejected_by_gate',
                'verdict': 'NOT_RUN',
                'review_reason': review['reason'],
                'gate': gate,
            }

        evidence = analysis.get('evidence') or {}
        first_pass = (
            json.dumps(evidence, ensure_ascii=False)
            if isinstance(evidence, dict)
            else str(evidence)
        )
        review = _run_secondary_review(
            roi_id,
            roi.get('rect'),
            image_paths['red'],
            image_paths['blue'],
            image_paths['green'],
            image_paths['thumbnail'],
            question,
            tool_call['claim'],
            first_pass_evidence=first_pass,
            lang=lang,
            slide_dims=getattr(slide, 'dimensions', None),
            steps_dir=root,
        )
        analysis, updated_reasoning = _apply_secondary_review(
            analysis,
            analysis.get('reasoning') or analysis.get('findings') or '',
            review,
            tool_call['claim'],
            roi_id,
            lang=lang,
        )
        analysis['reasoning'] = updated_reasoning
        return {
            'tool_status': 'executed',
            'verdict': review.get('verdict', 'SKIP'),
            'review_reason': review.get('reason', ''),
            'gate': gate,
        }

    results = []
    audited_plan = []
    for tool_call in plan:
        execution = registry.execute(
            'secondary_review',
            {
                key: value for key, value in tool_call.items()
                if key != 'tool_call_id'
            },
            context={'secondary_review_executor': execute_review},
        )
        if execution.get('success') and isinstance(execution.get('result'), dict):
            outcome = execution['result']
        else:
            outcome = {
                'tool_status': 'execution_error',
                'verdict': 'NOT_RUN',
                'review_reason': execution.get('error') or 'registry execution failed',
                'gate': {'passed': False, 'failed_checks': ['registry_execution']},
            }
            ref = refs.get(tool_call.get('roi_id'))
            if ref:
                analysis = ref['analysis']
                review = {
                    'verdict': 'NOT_RUN',
                    'reason': outcome['review_reason'],
                }
                analysis, updated_reasoning = _apply_secondary_review(
                    analysis,
                    analysis.get('reasoning') or analysis.get('findings') or '',
                    review,
                    tool_call.get('claim', ''),
                    tool_call.get('roi_id', ''),
                    lang=lang,
                )
                analysis['reasoning'] = updated_reasoning
        result = {**tool_call, **outcome}
        results.append(result)
        audited_plan.append({**tool_call, 'gate': outcome.get('gate', {})})
        logger.info(
            f"🔁 [Orchestrator二次复核] {tool_call['roi_id']} → "
            f"{result['verdict']} ({tool_call.get('reason', '')[:120]})"
        )

    # Collaborative Phase 5 also reads this flat list; rebuild it from the
    # adjudicated terminal records so a rejected first pass cannot reappear.
    if agent_state.get('green_analyses'):
        agent_state['pathology_findings'] = [
            item.get('reasoning') or item.get('findings', '')
            for item in agent_state['green_analyses']
            if isinstance(item, dict) and item.get('success') is not False
        ]
    agent_state['secondary_review_plan'] = audited_plan
    agent_state['secondary_review_results'] = results
    return results




_NEED_DETAIL_BLOCK_RE = re.compile(
    r'\[NEED_DETAIL\](.*?)\[/NEED_DETAIL\]',
    re.DOTALL | re.IGNORECASE,
)
_NEED_DETAIL_NONE_RE = re.compile(
    r'^(?:NONE|N/?A|NULL|NIL|NO|'
    r'无|无需(?:查阅)?|不需要(?:查阅)?|不必(?:查阅)?|没有)\s*[。.\s]*$',
    re.IGNORECASE,
)


def _is_empty_need_detail(block):
    text = str(block or '').strip()
    if not text:
        return True
    compacted = re.sub(r'[\s\-_=*]+', '', text)
    if not compacted:
        return True
    if _NEED_DETAIL_NONE_RE.match(text):
        return True
    return compacted.upper() in {'NONE', 'NA', 'NULL', 'NIL', 'NO'}


def finalize_patient_report_text(report):
    """Strip NEED_DETAIL bookkeeping from a patient-level report.

    Returns ``(cleaned_report, needs_lookup, detail_block)``.
    A copied ``NONE`` placeholder is treated as “no extra lookup”, not failure.
    """
    text = str(report or '')
    if not text.strip():
        return '', False, ''
    match = _NEED_DETAIL_BLOCK_RE.search(text)
    if not match:
        return text.strip(), False, ''
    detail_block = match.group(1).strip()
    cleaned = (text[:match.start()] + text[match.end():]).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    if _is_empty_need_detail(detail_block):
        return cleaned, False, detail_block
    return cleaned, True, detail_block


def get_patient_level_final_summary_prompt(folder_name, all_wsi_summaries, total_wsi, lang='zh'):
    """Build an organ-agnostic, evidence-linked patient-level synthesis prompt."""
    if lang == 'en':
        return f"""You are the patient-level Orchestrator for case {folder_name}. You received independently generated evidence summaries from {total_wsi} slides.

## Slide-level evidence
{all_wsi_summaries}

## Task
Answer only the active user-defined diagnostic question. If none was supplied, provide an objective cross-slide morphology summary and unresolved information without inventing a diagnosis. Do not infer or introduce an organ, tumor type, stage, grade, invasion item, biomarker, or checklist that the user did not request.

Integrate evidence across slides by specimen site and diagnostic dimension:
- distinguish concordant, complementary, conflicting, negative, and missing evidence;
- interpret a negative finding only within tissue actually represented on that slide;
- preserve slide and ROI provenance for every material claim, citing ids such as S1-R2-B1-G3;
- do not use simple counting or majority vote;
- do not let a parent hypothesis override direct high-resolution morphology.

If a critical conflict or information gap cannot be resolved from the summaries, output exactly one lookup block BEFORE the report, using one line per request:
[NEED_DETAIL]
Target: S2-R3-B1 | Need: full_analysis | Reason: why this record is required
[/NEED_DETAIL]
If the summaries are sufficient, omit the [NEED_DETAIL] block entirely. Never write NONE, and never leave the block in the visible report.

Then produce:
1. **Conclusion**
2. **Integrated morphologic evidence and provenance**
3. **Differential considerations and conflict resolution**
4. **Limitations and next steps**
5. **Overall evidence support (0–4)**

Scale: 0=no usable evidence; 1=very limited/indeterminate; 2=suggestive with meaningful alternatives; 3=strong but not definitive; 4=direct, characteristic, internally consistent evidence.
Do not claim definitive diagnosis when the available tissue or evidence is insufficient."""
    return f"""你是病例 {folder_name} 的患者级 Orchestrator。你已收到 {total_wsi} 张切片独立生成的证据摘要。

## 各切片证据
{all_wsi_summaries}

## 任务
只回答用户当前实际提出的诊断问题。若未提供具体问题，则仅客观汇总跨切片形态及尚缺信息，不得虚构诊断。不得推定或新增用户未要求的器官、肿瘤类型、分期、分级、浸润项目、生物标志物或固定问题清单。

请按标本部位与诊断维度整合跨切片证据：
- 区分一致、互补、矛盾、阴性及缺失证据；
- 阴性发现仅能解释该切片实际包含的组织；
- 每项关键结论保留切片与 ROI 证据来源，引用如 S1-R2-B1-G3；
- 不进行简单计数或多数投票；
- 上级假设不得覆盖高分辨率直接形态学证据。

若摘要无法解决关键冲突或信息缺口，只在报告正文之前输出一个查阅块，每行一条：
[NEED_DETAIL]
目标: S2-R3-B1 | 需要: 完整解读 | 原因: 必须回查该路径的理由
[/NEED_DETAIL]
若现有摘要已足够，不要输出 [NEED_DETAIL] 块。不要写 NONE，也不要把该块留在给医生看的报告里。

随后输出：
1. **结论**
2. **整合形态学依据与来源**
3. **鉴别要点与冲突处理**
4. **局限性与下一步**
5. **总体证据支持度（0–4）**

评分：0=无可用证据；1=证据极少、无法确定；2=提示性证据但存在重要鉴别；3=证据较强但未达到完全确定；4=直接、典型且内部一致的证据。
当组织取材或证据不足时，不得给出确定性诊断。"""



# Agent停止标志
agent_stop_flags = {}

# ⭐ 活跃的Agent状态存储（按room/session_id索引，用于人机协作通信）
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


def resolve_collaborative_agent_state(session_id, phase_gate_id='', file_id=''):
    """Locate the collab worker that owns this phase gate.

    Patient Level Swarm runs each slide in a private worker room
    (``{session}:patient:{patient_id}:wsi:{index}``). The browser still
    emits ``agent.phase_confirm`` against the parent session, so the
    handler must follow the gate id (and optional file id) to that worker.
    """
    gate = str(phase_gate_id or '')
    wanted_file = str(file_id or '')
    prefix = f'{session_id}:patient:'
    ordered = []
    direct = get_agent_state(session_id)
    if direct is not None:
        ordered.append((session_id, direct))
    for room, state in list(active_agent_states.items()):
        if room == session_id or not str(room).startswith(prefix):
            continue
        ordered.append((room, state))

    def _matches(state):
        if not isinstance(state, dict):
            return False
        if gate and str(state.get('phase_gate_id') or '') != gate:
            return False
        if wanted_file:
            # Parent Patient-report state has no file_id. Do not let it
            # steal a worker-bound slide gate or analysis edit.
            state_file = str(state.get('file_id') or '')
            if not state_file or state_file != wanted_file:
                return False
        return True

    for room, state in ordered:
        if _matches(state):
            return room, state
    return session_id, direct


# ==================== Agent Loop 高级功能：证据原子化 ====================

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

    每个节点记录：坐标、观察、导航计划、通用结构化证据、证据支持度、
    诊断建议和图像路径。
    同时把全树(含红框, findings 占位时回退到 reason)展开成扁平 EvidencePool, 供聚合与证据链。

    Returns: (memory_tree: dict, evidence_pool: EvidencePool|None)
    """
    pool = EvidencePool() if EvidencePool else None

    def _node_to_memory(node, level):
        a = node.get('analysis', {}) or {}
        raw = a.get('reasoning', '') or ''
        reason, plan = _split_reason_plan(raw)
        findings = a.get('findings', '') or ''
        # 通用结构化证据 / 证据支持度 / 诊断建议
        evidence = a.get('evidence', {}) or {}
        evidence_confidence = a.get('evidence_confidence')
        secondary_review = a.get('secondary_review')
        if not secondary_review and isinstance(evidence, dict):
            secondary_review = evidence.get('secondary_review')
        diag = a.get('diagnosis_suggestion', '') or ''
        # 文本结果尚未结构化时，按当前通用契约现场解析。
        if not evidence and raw and ('路径证据汇总' in raw or 'Path Evidence Summary' in raw or 'Evidence' in raw):
            try:
                _p = parse_structured_response(raw, 160)
                evidence = _p.get('evidence', {}) or {}
                evidence_confidence = _p.get('evidence_confidence')
                if not diag:
                    diag = _p.get('diagnosis_suggestion', '') or ''
            except Exception as _e:
                logger.debug(f"[Memory树] reasoning 重解析失败: {_e}")
        node_id = node.get('roi_id', node.get('id', '?'))
        rect = node.get('rect') or a.get('rect', {}) or {}
        # 证据池描述源：优先用结构化证据拼接，否则回退到 findings/reason。
        if evidence:
            pool_desc = ' | '.join(
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else str(value)
                for value in evidence.values()
                if value
            )
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
            mem['evidence'] = evidence
            if evidence_confidence is not None:
                mem['evidence_confidence'] = evidence_confidence
        if secondary_review:
            mem['secondary_review'] = secondary_review
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
    if 'secondary_review_plan' in agent_state:
        root['secondary_review_plan'] = agent_state.get('secondary_review_plan', [])
    if 'secondary_review_results' in agent_state:
        root['secondary_review_results'] = agent_state.get(
            'secondary_review_results',
            [],
        )
    return root, pool


def strip_for_synthesis(node, drop_keys=('rect', 'image_path')):
    """V2: 由全量 path_tree 派生"喂汇总"的精简 memory —— 仅去掉对文本汇总无意义的
    机器字段(默认 rect 像素坐标 / image_path 图路径), 其余 reason、plan、
    evidence、evidence_confidence 和 diagnosis_suggestion 全部保留，ROI id
    作为可追溯锚点保留。
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


# ==================== Agent Loop 高级功能：空间搜索（Spatial Search） ====================

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
    spatial_index = agent_state.get('spatial_index', {})
    
    if roi_id not in spatial_index:
        return ""
    
    neighbors = spatial_index[roi_id].get('neighbors', [])
    if not neighbors:
        return ""
    
    context_parts = []
    neighbor_count = 0
    
    for neighbor_id in neighbors[:max_neighbors * 2]:  # 多取一些，筛选有内容的
        neighbor_data = spatial_index.get(neighbor_id, {})
        findings = neighbor_data.get('findings', '')
        
        if findings:
            context_parts.append(f"- {neighbor_id}: {findings}")
            neighbor_count += 1
            
            if neighbor_count >= max_neighbors:
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

    This is the 40× E(neighbor) source. It is local to one red path so parallel
    red paths cannot overwrite one another's replanning context.
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


# ==================== Agent Loop 高级功能：回溯/重新路由（Backtrack/Reroute） ====================
#
# 设计说明：
# 回溯是 Sub-agent 的自主决策，通过选择「回溯」动作触发，而非代码强制触发。
# 这符合 Agent Loop 的设计：Sub-agent 拥有完整的元认知能力。
#
# 回溯触发流程：
# 1. Sub-agent 在 Reason 阶段发现视野质量问题（坏死/伪影/空白等）
# 2. Sub-agent 在 Plan 阶段选择「回溯」动作
# 3. handle_agent_loop_action() 函数处理回溯动作，执行回溯
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
_roi_cache_local = threading.local()


def _get_parent_roi_cache():
    """Return the ROI cache for the current worker thread.

    Patient-level automatic and collaborative analysis may run several WSI
    pipelines in parallel. A process-global ROI cache lets identical ROI ids
    (R1/B1/...) from different slides overwrite each other. Thread-local
    storage preserves single-slide behaviour while isolating parallel workers.
    """
    cache = getattr(_roi_cache_local, 'parent_roi_cache', None)
    if cache is None:
        cache = {}
        _roi_cache_local.parent_roi_cache = cache
    return cache

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
    _parent_roi_cache = _get_parent_roi_cache()
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
    _parent_roi_cache = _get_parent_roi_cache()
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

# ==================== 人机协作模式确认函数 ====================
def request_user_confirmation(socketio, room, agent_state, frame_type, parent_roi, selected_rois, slide, progress_info=None):
    """
    请求用户确认AI选择的区域（人机协作模式 - 分层级硬闸门）
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID
        agent_state: Agent状态
        frame_type: 'red' | 'blue' | 'green' | 'report'
        parent_roi: 父ROI信息（report类型时可为None）
        selected_rois: AI选择的ROI列表（report类型时为报告内容）
        slide: OpenSlide对象
        progress_info: 进度信息字典，包含 {
            'red_index': 当前红框索引(1-based),
            'red_total': 红框总数,
            'blue_index': 当前蓝框索引(1-based, 仅绿框确认时),
            'blue_total': 蓝框总数(仅绿框确认时),
            'brief_finding': 简要发现(可选)
        }
    
    Returns:
        list: 最终确认的ROI列表（可能是AI选择或用户手动选择或空列表）
    
    分层级逻辑:
        - red (10x): 全自动，不需要确认
        - blue (40x): 需要用户确认
        - green (160x): 需要用户确认
        - report: 最终报告确认
    
    状态机:
        pending_confirmation = {
            "id": confirmation_id,
            "frame_type": frame_type,
            "parent_roi_id": parent_roi["id"],
            "status": "pending" | "accepted" | "overridden" | "skipped" | "switch_to_auto",
            "result_rois": [...],   # 用户最终确定的ROI列表
            "created_at": <time.time()>
        }
    """
    import uuid as uuid_module
    import time
    
    # ⛔️ 不要动这段：auto 模式直接返回
    if agent_state.get('mode') != 'collaborative':
        return selected_rois
    
    # ⭐ 红框（10x）全自动，不需要用户确认
    if frame_type == 'red':
        logger.info(f"🔴 人机协作模式：红框(10x)自动处理，无需确认，共{len(selected_rois)}个区域")
        return selected_rois
    
    # ⭐ 只有 blue / green / report 才需要用户确认
    confirmation_id = str(uuid_module.uuid4())[:8]
    
    _is_en = agent_state.get('lang', 'zh') == 'en'
    if _is_en:
        frame_names = {'blue': 'Blue Box (40×)', 'green': 'Green Box (160×)', 'report': 'Final Report'}
    else:
        frame_names = {'blue': '蓝框(40×)', 'green': '绿框(160×)', 'report': '最终报告'}
    frame_name = frame_names.get(frame_type, frame_type)
    
    progress_text = ""
    if progress_info:
        red_idx = progress_info.get('red_index', '?')
        red_total = progress_info.get('red_total', '?')
        if frame_type == 'blue':
            progress_text = f"Red {red_idx}/{red_total}" if _is_en else f"红框 {red_idx}/{red_total}"
        elif frame_type == 'green':
            blue_idx = progress_info.get('blue_index', '?')
            blue_total = progress_info.get('blue_total', '?')
            progress_text = f"Red{red_idx} → Blue{blue_idx}/{blue_total}" if _is_en else f"红框{red_idx} → 蓝框{blue_idx}/{blue_total}"
        elif frame_type == 'report':
            progress_text = "All analysis complete" if _is_en else "全部分析完成"
    
    logger.info(f"🤝 人机协作模式：请求确认{frame_name} [{progress_text}]，confirmation_id={confirmation_id}")
    
    # 构建选区信息（report类型特殊处理）
    regions_info = []
    parent_roi_id = None
    parent_rect = None
    message = ""
    
    if frame_type == 'report':
        # report类型：selected_rois 是报告内容字典
        message = "Please confirm the final pathology report" if _is_en else "请确认最终病理报告"
        parent_roi_id = 'report'
    else:
        for i, roi in enumerate(selected_rois, 1):
            regions_info.append({
                'index': i,
                'roi_id': roi['id'],
                'rect': roi['rect'],
                'position': f"({roi['rect']['x']}, {roi['rect']['y']})",
                'size': f"{roi['rect']['w']}×{roi['rect']['h']}"
            })
        parent_roi_id = parent_roi['id'] if parent_roi else None
        parent_rect = parent_roi['rect'] if parent_roi else None
        message = f"AI selected {len(selected_rois)} {frame_name} — confirm or adjust" if _is_en else f"AI已选择{len(selected_rois)}个{frame_name}，请确认或调整"
    
    # ⭐ 初始化 pending_confirmation 状态（硬闸门状态机）
    agent_state['pending_confirmation'] = {
        'id': confirmation_id,
        'frame_type': frame_type,
        'parent_roi_id': parent_roi_id,
        'status': 'pending',
        'result_rois': None,
        'created_at': time.time()
    }
    
    # 发送确认请求到前端
    socketio.emit('agent.confirm_selection', {
        'type': 'confirm_request',
        'confirmation_id': confirmation_id,
        'frame_type': frame_type,
        'parent_roi_id': parent_roi_id,
        'selected_count': len(selected_rois) if frame_type != 'report' else 0,
        'regions': regions_info,
        'message': message,
        'parent_rect': parent_rect,
        # ⭐ 进度信息
        'progress': progress_info or {},
        'progress_text': progress_text,
        'brief_finding': progress_info.get('brief_finding', '') if progress_info else '',
        # ⭐ report类型：包含报告内容
        'report_content': selected_rois if frame_type == 'report' else None
    }, to=room)
    
    logger.info(f"🚦 硬闸门启动：等待用户确认 {frame_name}，confirmation_id={confirmation_id}")
    
    # —— 硬闸门：等待用户响应 —— #
    timeout_sec = 600  # 10分钟超时
    start_time = time.time()
    
    while True:
        pending = agent_state.get('pending_confirmation')
        
        # 被外部清空了，视为取消
        if not pending or pending.get('id') != confirmation_id:
            logger.warning("⚠️ pending_confirmation 丢失或ID不匹配，视为取消本次确认")
            return []
        
        status = pending.get('status')
        
        # ✅ 用户接受 AI 选择
        if status == 'accepted':
            logger.info(f"✅ 用户接受 AI 选择的{frame_name}")
            # ⭐ 修复：用户接受时，直接返回原始AI选择的selected_rois
            # 不使用前端发送的result_rois（可能格式不完整）
            # 清理状态
            agent_state.pop('pending_confirmation', None)
            return selected_rois
        
        # ✏️ 用户修改了 ROI
        if status == 'overridden':
            result_rois = pending.get('result_rois') or []
            logger.info(f"✏️ 用户修改并确认了 {len(result_rois)} 个{frame_name}")
            # 将用户选择的区域转换为ROI格式
            converted_rois = []
            for i, manual_roi in enumerate(result_rois):
                # 如果已经是完整的ROI格式，直接使用
                _manual_reason = 'User manual selection' if _is_en else '用户手动选择'
                _manual_pathology = 'pending' if _is_en else '待分析'
                if 'rect' in manual_roi and isinstance(manual_roi['rect'], dict):
                    roi = {
                        'id': manual_roi.get('roi_id') or f"{parent_roi['id']}_manual_{i}",
                        'rect': manual_roi['rect'],
                        'zoom_level': 40 if frame_type == 'blue' else 160,
                        'priority': 5,
                        'reason': _manual_reason,
                        'suspected_pathology': _manual_pathology,
                        'parent_id': parent_roi['id']
                    }
                else:
                    roi = {
                        'id': f"{parent_roi['id']}_manual_{i}",
                        'rect': {
                            'x': int(manual_roi.get('x', 0)),
                            'y': int(manual_roi.get('y', 0)),
                            'w': int(manual_roi.get('w', 0)),
                            'h': int(manual_roi.get('h', 0))
                        },
                        'zoom_level': 40 if frame_type == 'blue' else 160,
                        'priority': 5,
                        'reason': _manual_reason,
                        'suspected_pathology': _manual_pathology,
                        'parent_id': parent_roi['id']
                    }
                converted_rois.append(roi)
            # 清理状态
            agent_state.pop('pending_confirmation', None)
            return converted_rois if converted_rois else selected_rois
        
        # ⏩ 用户跳过当前父框
        if status == 'skipped':
            logger.info(f"⏩ 用户跳过当前父框 {parent_roi['id']} 的{frame_name}分析")
            # 清理状态
            agent_state.pop('pending_confirmation', None)
            return []
        
        # 🔄 用户切换为全自动模式
        if status == 'switch_to_auto':
            logger.info(f"🔄 用户切换为全自动模式，后续不再请求确认")
            agent_state['mode'] = 'auto'
            # 清理状态
            agent_state.pop('pending_confirmation', None)
            return selected_rois
        
        # ⏰ 超时检查
        if time.time() - start_time > timeout_sec:
            logger.warning(f"⏰ 用户长时间未确认（{timeout_sec}秒），超时取消当前父框分析")
            # 清理状态
            agent_state.pop('pending_confirmation', None)
            return []
        
        # 使用 socketio.sleep 而不是 time.sleep，避免阻塞事件循环
        socketio.sleep(0.5)


# ==================== 解读确认函数（协作模式专用） ====================
def request_analysis_confirmation(socketio, room, agent_state, analysis_type, roi_info, analysis_content, progress_info=None):
    """
    请求用户确认AI的解读结果（协作模式 - 右侧面板确认）
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID
        agent_state: Agent状态
        analysis_type: 'red_analysis' | 'blue_analysis' | 'green_analysis' | 'final_report'
        roi_info: ROI信息（包含id, rect等）
        analysis_content: AI解读内容（字符串）
        progress_info: 进度信息
    
    Returns:
        dict: {'status': 'accepted'|'edited'|'regenerate', 'content': 最终内容}
    """
    import uuid as uuid_module
    import time
    
    # ⛔️ 自动模式直接返回，不阻塞
    if agent_state.get('mode') != 'collaborative':
        return {'status': 'accepted', 'content': analysis_content}
    
    confirmation_id = str(uuid_module.uuid4())[:8]
    
    _is_en = agent_state.get('lang', 'zh') == 'en'
    if _is_en:
        type_names = {'red_analysis': 'Red Box Analysis (10×)', 'blue_analysis': 'Blue Box Analysis (40×)',
                      'green_analysis': 'Green Box Analysis (160×)', 'final_report': 'Final Pathology Report'}
    else:
        type_names = {'red_analysis': '红框解读(10×)', 'blue_analysis': '蓝框解读(40×)',
                      'green_analysis': '绿框解读(160×)', 'final_report': '最终病理报告'}
    type_name = type_names.get(analysis_type, analysis_type)
    
    progress_text = ""
    if progress_info:
        red_idx = progress_info.get('red_index', '?')
        red_total = progress_info.get('red_total', '?')
        if analysis_type == 'red_analysis':
            progress_text = f"Red {red_idx}/{red_total}" if _is_en else f"红框 {red_idx}/{red_total}"
        elif analysis_type == 'blue_analysis':
            blue_idx = progress_info.get('blue_index', '?')
            blue_total = progress_info.get('blue_total', '?')
            progress_text = f"Red{red_idx} → Blue{blue_idx}/{blue_total}" if _is_en else f"红框{red_idx} → 蓝框{blue_idx}/{blue_total}"
        elif analysis_type == 'green_analysis':
            blue_idx = progress_info.get('blue_index', '?')
            green_idx = progress_info.get('green_index', '?')
            green_total = progress_info.get('green_total', '?')
            progress_text = f"Blue{blue_idx} → Green{green_idx}/{green_total}" if _is_en else f"蓝框{blue_idx} → 绿框{green_idx}/{green_total}"
        elif analysis_type == 'final_report':
            progress_text = "All analysis complete" if _is_en else "全部分析完成"
    
    logger.info(f"📝 请求确认{type_name} [{progress_text}]，confirmation_id={confirmation_id}")
    
    # 初始化解读确认状态
    agent_state['pending_analysis_confirmation'] = {
        'id': confirmation_id,
        'analysis_type': analysis_type,
        'roi_id': roi_info.get('id') if roi_info else 'final_report',
        'status': 'pending',
        'edited_content': None,
        'created_at': time.time()
    }
    
    # 发送解读确认请求到前端
    socketio.emit('agent.confirm_analysis', {
        'type': 'analysis_confirm_request',
        'confirmation_id': confirmation_id,
        'analysis_type': analysis_type,
        'roi_id': roi_info.get('id') if roi_info else 'final_report',
        'roi_rect': roi_info.get('rect') if roi_info else None,
        'content': analysis_content,
        'progress_text': progress_text,
        'message': f"Please confirm {type_name}" if _is_en else f"请确认{type_name}内容"
    }, to=room)
    
    logger.info(f"🚦 解读硬闸门启动：等待用户确认 {type_name}，confirmation_id={confirmation_id}")
    
    # —— 硬闸门：等待用户响应 —— #
    timeout_sec = 600  # 10分钟超时
    start_time = time.time()
    
    while True:
        pending = agent_state.get('pending_analysis_confirmation')
        
        if not pending or pending.get('id') != confirmation_id:
            logger.warning("⚠️ pending_analysis_confirmation 丢失，未获用户确认，保持 AI 原文")
            return {'status': 'unconfirmed', 'content': analysis_content}
        
        status = pending.get('status')
        
        # ✅ 用户确认解读
        if status == 'accepted':
            logger.info(f"✅ 用户确认{type_name}")
            agent_state.pop('pending_analysis_confirmation', None)
            return {'status': 'accepted', 'content': analysis_content}
        
        # ✏️ 用户编辑了解读
        if status == 'edited':
            edited_content = pending.get('edited_content') or analysis_content
            logger.info(f"✏️ 用户编辑了{type_name}")
            agent_state.pop('pending_analysis_confirmation', None)
            return {'status': 'edited', 'content': edited_content}
        
        # 🔄 用户要求重新生成
        if status == 'regenerate':
            logger.info(f"🔄 用户要求重新生成{type_name}")
            agent_state.pop('pending_analysis_confirmation', None)
            return {'status': 'regenerate', 'content': analysis_content}
        
        # ⏰ 超时检查
        if time.time() - start_time > timeout_sec:
            logger.warning(f"⏰ 解读确认超时（{timeout_sec}秒），未获用户确认，保持 AI 原文")
            agent_state.pop('pending_analysis_confirmation', None)
            return {'status': 'unconfirmed', 'content': analysis_content}
        
        socketio.sleep(0.5)


def build_modification_record_html(phase_key, color_name, user_modifications, interaction_history, lang='zh'):
    """
    构建人机协作修改记录的 HTML 块（用于在报告对应位置内嵌显示）
    """
    is_en = (lang == 'en')
    phase_mod = user_modifications.get(phase_key, {})
    if not phase_mod.get('modified'):
        return ''
    
    added = phase_mod.get('added', [])
    removed = phase_mod.get('removed', [])
    
    if not added and not removed:
        return ''
    
    changes_html = ''
    if removed:
        label = 'User Removed' if is_en else '用户删除'
        changes_html += f'''
                <div class="mod-change">
                    <div class="change-label">❌ {label}: {', '.join(removed)}</div>
                </div>'''
    if added:
        label = 'User Added' if is_en else '用户新增'
        changes_html += f'''
                <div class="mod-change">
                    <div class="change-label">➕ {label}: {', '.join(added)}</div>
                </div>'''
    
    interaction = interaction_history.get(phase_key, {})
    user_reason = interaction.get('user_reason', '')
    user_html = ''
    if user_reason:
        reason_label = 'User Reason' if is_en else '用户理由'
        user_html = f'''
                <div class="mod-user">
                    <span class="user-icon">👤</span> <strong>{reason_label}</strong>: {user_reason}
                </div>'''
    
    ai_html = ''
    if interaction.get('has_ai_review'):
        ai_approved = interaction.get('ai_approved', True)
        ai_reason = interaction.get('ai_reason', '')
        ai_suggestion = interaction.get('ai_suggestion', '')
        
        status_icon = ('✅ Approved' if ai_approved else '⚠️ Disapproved') if is_en else ('✅ 认可' if ai_approved else '⚠️ 不认可')
        ai_class = '' if ai_approved else ' rejected'
        
        assess_label = 'AI Assessment' if is_en else 'AI 评估'
        ai_html = f'''
                <div class="mod-ai{ai_class}">
                    <span class="ai-icon">🤖</span> <strong>{assess_label}</strong>: {status_icon}
                </div>'''
        if ai_reason:
            reason_lbl = 'AI Reason' if is_en else 'AI 理由'
            ai_html += f'''
                <div class="mod-ai{ai_class}">
                    <span class="ai-icon">💬</span> <strong>{reason_lbl}</strong>: {ai_reason}
                </div>'''
        if ai_suggestion:
            suggest_lbl = 'AI Suggestion' if is_en else 'AI 建议'
            ai_html += f'''
                <div class="mod-ai{ai_class}">
                    <span class="ai-icon">💡</span> <strong>{suggest_lbl}</strong>: {ai_suggestion}
                </div>'''
    
    original_count = interaction.get('original_count', '?')
    modified_count = interaction.get('modified_count', '?')
    
    if is_en:
        title = f'🤝 Human-AI Collaboration Log — {color_name} (AI selected {original_count} → User adjusted to {modified_count})'
    else:
        title = f'🤝 人机协作修改记录 — {color_name} (AI选{original_count}个 → 用户调整为{modified_count}个)'
    
    html = f'''
            <div class="modification-record">
                <div class="mod-title">{title}</div>
                {changes_html}
                {user_html}
                {ai_html}
            </div>'''
    
    return html


def _find_debug_image(pattern, file_id=None, sample_name=None):
    """按保存键优先级查找 debug 网格图。

    保存端（save 调试图）现在优先用 debug_images/{file_id}/；
    历史 run 用的是 debug_images/{文件名 stem}/。查找端必须按同样的
    优先级找，否则写进 analysis_tree.json 的 image_path 会指向旧目录
    的陈年文件甚至死链接（前端表现为「图片加载失败」）。
    sample_name 传入时应当已经是 stem（不要再次 Path(...).stem，
    文件名本身含点号时会被误截断）。
    """
    for key in (str(file_id or '').strip(), str(sample_name or '').strip()):
        if not key:
            continue
        debug_dir = Path("debug_images") / key
        if not debug_dir.exists():
            continue
        matches = list(debug_dir.glob(pattern))
        if matches:
            matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            return str(matches[0])
    return None


def generate_html_visual_report(agent_state, file_id, output_dir, final_report_text, total_red, total_blue, total_green, report_title=None, lang=None):
    """
    生成HTML可视化病理报告 - 完全按照 3.Report_Generator.py 格式
    
    Args:
        agent_state: Agent状态字典
        file_id: 文件ID
        output_dir: 输出目录
        final_report_text: 围绕用户当前问题生成的最终报告文本
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
        organ = agent_state.get('organ', '未指定标本')
        sample_name = Path(filename).stem if filename else file_id

        # ⭐ 英文模式下翻译器官名称
        if is_en:
            _organ_map = {
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
                # 2. 当前保存键：debug_images/{file_id}/
                Path("debug_images") / str(file_id or ''),
                # 3. 历史遗留键：debug_images/{sample_name}/
                Path(f"debug_images/{sample_name}"),
                # 4. 输出目录本身
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
        
        /* 人机协作修改记录 */
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
            # 如果是列表，将其转换为字符串（例如协作模式下可能返回列表）
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
        
        # ⭐ 在全局筛查部分末尾插入 Phase 1 (红框) 修改记录
        user_modifications = agent_state.get('user_modifications', {})
        interaction_history = agent_state.get('interaction_history', {})
        
        phase1_mod_html = build_modification_record_html('phase1', _t['label_red_box'] + '(10×)', user_modifications, interaction_history, lang=lang)
        if phase1_mod_html:
            html_content += phase1_mod_html
        
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
            # （精确匹配，避免 R13 匹配到 R13-B4 等蓝框图片；file_id 键优先）
            if not red_image_path:
                red_image_path = _find_debug_image(
                    f"debug_gpt_{red_id}_*candidates_*.jpg",
                    file_id=file_id,
                    sample_name=sample_name,
                )
                if red_image_path:
                    logger.info(f"找到红框图片: {red_image_path}")
            
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
        
        # ⭐ 在多倍镜分析部分末尾插入 Phase 2 (蓝框) 和 Phase 3 (绿框) 修改记录
        phase2_mod_html = build_modification_record_html('phase2', _t['label_blue_box'] + '(40×)', user_modifications, interaction_history, lang=lang)
        if phase2_mod_html:
            html_content += phase2_mod_html
        
        phase3_mod_html = build_modification_record_html('phase3', _t['label_green_box'] + '(160×)', user_modifications, interaction_history, lang=lang)
        if phase3_mod_html:
            html_content += phase3_mod_html
        
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


def _public_outputs_url(path) -> str:
    parts = list(Path(str(path or '')).parts)
    try:
        idx = parts.index('outputs')
    except ValueError:
        return ''
    return '/' + '/'.join(parts[idx:])


def _patient_run_is_roam(patient_state: dict, wsi_results: dict) -> bool:
    if str(patient_state.get('analysis_strategy') or '') == 'single_agent_roam':
        return True
    for result in (wsi_results or {}).values():
        if not isinstance(result, dict):
            continue
        if result.get('strategy') == 'single_agent_roam':
            return True
        if result.get('trajectory'):
            return True
    return False


def _roam_trajectory_items(result: dict) -> list:
    items = result.get('trajectory') if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _roam_stats_from_result(result: dict) -> dict:
    stats = dict((result or {}).get('statistics') or {})
    items = _roam_trajectory_items(result or {})
    fovs = [item for item in items if int(item.get('step') or 0) > 0]
    has_overview = any(int(item.get('step') or 0) == 0 for item in items)
    fov_count = int(stats.get('roam_steps') or len(fovs))
    independent = int(stats.get('independent_fovs') or 0)
    if not independent and fovs:
        try:
            from .single_agent_roam import _independent_fov_count
            independent = int(_independent_fov_count(items))
        except Exception:
            independent = len(fovs)
    return {
        'fovs': fov_count,
        'overview': 1 if has_overview or items else 0,
        'independent': independent,
        'steps': len(items) or (fov_count + (1 if has_overview else 0)),
    }


def _rewrite_relative_step_images(body: str, html_file: Path) -> str:
    public = _public_outputs_url(Path(html_file).parent / 'steps')
    if not public or not body:
        return body
    return (
        body.replace('src="steps/', f'src="{public}/')
            .replace("src='steps/", f"src='{public}/")
    )


def _render_patient_roam_slide_html(wsi_result: dict, is_en: bool) -> str:
    items = _roam_trajectory_items(wsi_result or {})
    if not items:
        return ''
    output_paths = (wsi_result or {}).get('output_paths') or {}
    steps_dir = Path(str(output_paths.get('steps_dir') or ''))
    if not steps_dir.parts:
        output_dir = output_paths.get('output_dir')
        if output_dir:
            steps_dir = Path(output_dir) / 'steps'
    public_steps = _public_outputs_url(steps_dir)
    cards = []
    for item in items:
        step = int(item.get('step') or 0)
        image_name = str(item.get('image') or '').strip()
        if not image_name:
            image_name = 'overview.jpg' if step == 0 else ''
        img_src = f'{public_steps}/{image_name}' if public_steps and image_name else ''
        label = str(item.get('label') or (
            'Overview' if step == 0 else f"FOV {step}"
        ))
        observation = str(item.get('observation') or '')
        why = str(item.get('why') or '')
        meta_parts = []
        zoom = item.get('zoom')
        if zoom not in (None, ''):
            meta_parts.append(f'{zoom}×')
        x_val, y_val = item.get('x'), item.get('y')
        if x_val not in (None, '') and y_val not in (None, ''):
            try:
                meta_parts.append(f'x:{int(round(float(x_val)))}, y:{int(round(float(y_val)))}')
            except (TypeError, ValueError):
                pass
        rect = item.get('rect') or {}
        if isinstance(rect, dict) and rect.get('w') not in (None, '') and rect.get('h') not in (None, ''):
            try:
                meta_parts.append(f"{int(round(float(rect['w'])))}×{int(round(float(rect['h'])))} px")
            except (TypeError, ValueError, KeyError):
                pass
        image_html = (
            f'<img src="{html.escape(img_src, quote=True)}" alt="{html.escape(label)}">'
            if img_src else ''
        )
        meta_html = (
            f'<div class="meta">{html.escape(" · ".join(meta_parts))}</div>'
            if meta_parts else ''
        )
        why_html = f'<small>{html.escape(why)}</small>' if why else ''
        cards.append(
            '<article>'
            f'<h3>{html.escape(label)}</h3>{image_html}'
            f'<p>{html.escape(observation)}</p>{meta_html}{why_html}'
            '</article>'
        )
    heading = 'Single Agent navigation record' if is_en else '单 Agent 游走过程'
    summary = str(
        (wsi_result or {}).get('wsi_summary')
        or (wsi_result or {}).get('report')
        or ''
    ).strip()
    summary_title = 'Slide diagnostic summary' if is_en else '切片诊断小结'
    summary_html = ''
    if summary:
        summary_html = f"""
                        <div class="thinking">
                            <div class="label collapsible active">💭 {summary_title}</div>
                            <div class="collapsible-content show">
                                <pre>{html.escape(summary)}</pre>
                            </div>
                        </div>
"""
    return f"""
                        <h3 class="section-title" style="font-size:18px;">{heading}</h3>
                        <div class="roam-route">
                            {''.join(cards)}
                        </div>
                        {summary_html}
"""


def generate_patient_level_html_report(patient_state: dict, output_dir: Path, file_infos: list, wsi_results: dict) -> str:
    """
    生成患者级别 HTML 可视化报告 - 合并各切片的完整 HTML 报告 + 底部综合诊断
    
    ⭐ 结构（rbind 上下合并）：
    ┌──────────────────────────────────────┐
    │  Patient Level 综合病理报告 (Header)   │
    ├──────────────────────────────────────┤
    │  📊 总体统计                          │
    ├──────────────────────────────────────┤
    │  切片1: Slide 1: Part A - xxx.svs     │
    │  [该切片完整的 HTML 报告内容]          │
    ├──────────────────────────────────────┤
    │  切片2: Slide 2: Part B - yyy.svs     │
    │  [该切片完整的 HTML 报告内容]          │
    ├──────────────────────────────────────┤
    │  📋 Patient Level 综合诊断报告        │
    └──────────────────────────────────────┘
    
    Args:
        patient_state: 患者状态字典
        output_dir: 输出目录（通常是 outputs/patient_{id}/auto/）
        file_infos: 所有 WSI 文件信息列表
        wsi_results: 各 WSI 的分析结果 {file_id: result}
    
    Returns:
        str: HTML 报告文件路径
    """
    from datetime import datetime
    from pathlib import Path
    
    def extract_wsi_report_content(html_file_path):
        """
        从单张 WSI 的 pathology_report.html 中提取 <div class="content"> 内的所有 section。
        返回所有 section 的 HTML 字符串（不含外层 content div 本身）。
        """
        try:
            with open(html_file_path, 'r', encoding='utf-8') as f:
                html = f.read()
            
            content_marker = '<div class="content">'
            footer_marker = '<div class="footer">'
            
            start = html.find(content_marker)
            end = html.find(footer_marker)
            
            if start == -1 or end == -1:
                main_start = html.find('<main>')
                main_end = html.rfind('</main>')
                if main_start != -1 and main_end > main_start:
                    return html[main_start + len('<main>'):main_end]
                logger.warning(f"[Patient Level] HTML 结构标记未找到: {html_file_path}")
                return None
            
            # 提取从 content 开始到 footer 之前的内容
            body = html[start + len(content_marker):end]
            
            # 去掉末尾关闭 content div 的 </div>
            body = body.rstrip()
            last_close = body.rfind('</div>')
            if last_close != -1:
                body = body[:last_close].rstrip()
            
            return body
        except Exception as e:
            logger.warning(f"[Patient Level] 无法读取 WSI 报告 {html_file_path}: {e}")
            return None
    
    try:
        patient_id = patient_state.get('patient_id', 'unknown')
        folder_name = patient_state.get('folder_name', '未知患者')
        total_wsi = patient_state.get('total_wsi', len(file_infos))
        completed_wsi = patient_state.get('completed_wsi', 0)
        _en = patient_state.get('lang', 'zh') == 'en'
        
        is_roam = _patient_run_is_roam(patient_state, wsi_results)
        
        # 统计所有 WSI 的红/蓝/绿框总数；单 Agent 游走改用视野计数。
        total_red = 0
        total_blue = 0
        total_green = 0
        total_fovs = 0
        total_independent = 0
        total_roam_steps = 0
        
        for file_info in file_infos:
            fid = file_info.get('file_id')
            if fid and fid in wsi_results:
                result = wsi_results[fid]
                if result:
                    stats = result.get('statistics', {})
                    total_red += stats.get('red_frames', stats.get('total_red', 0))
                    total_blue += stats.get('blue_frames', stats.get('total_blue', 0))
                    total_green += stats.get('green_frames', stats.get('total_green', 0))
                    roam_stats = _roam_stats_from_result(result)
                    total_fovs += roam_stats['fovs']
                    total_independent += roam_stats['independent']
                    total_roam_steps += roam_stats['steps']
        
        if is_roam:
            stats_cards_html = f"""
                    <div class="stat-card wsi">
                        <div class="number">{completed_wsi}</div>
                        <div class="label">📋 {'Slides' if _en else '切片数'}</div>
                    </div>
                    <div class="stat-card fov">
                        <div class="number">{total_fovs}</div>
                        <div class="label">🔭 {'Observed FOVs' if _en else '观察视野'}</div>
                    </div>
                    <div class="stat-card indep">
                        <div class="number">{total_independent or total_fovs}</div>
                        <div class="label">📍 {'Independent samples' if _en else '独立取样'}</div>
                    </div>
                    <div class="stat-card steps">
                        <div class="number">{total_roam_steps or (total_fovs + completed_wsi)}</div>
                        <div class="label">🧭 {'Trajectory steps' if _en else '轨迹步数'}</div>
                    </div>
"""
        else:
            stats_cards_html = f"""
                    <div class="stat-card wsi">
                        <div class="number">{completed_wsi}</div>
                        <div class="label">📋 {'Slides' if _en else '切片数'}</div>
                    </div>
                    <div class="stat-card red">
                        <div class="number">{total_red}</div>
                        <div class="label">🔴 {'Red ROI (10x)' if _en else '红框 (10x)'}</div>
                    </div>
                    <div class="stat-card blue">
                        <div class="number">{total_blue}</div>
                        <div class="label">🔵 {'Blue ROI (40x)' if _en else '蓝框 (40x)'}</div>
                    </div>
                    <div class="stat-card green">
                        <div class="number">{total_green}</div>
                        <div class="label">🟢 {'Green ROI (160x)' if _en else '绿框 (160x)'}</div>
                    </div>
"""
        
        # ==================== 构建 HTML ====================
        # CSS 复用单张 WSI 报告的完整样式，并添加 Patient Level 专用样式
        html_content = f"""<!DOCTYPE html>
<html lang="{'en' if _en else 'zh-CN'}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{'Patient Level Comprehensive Pathology Report' if _en else 'Patient Level 综合病理报告'} - {folder_name}</title>
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
        
        /* 全局视图样式（复用单张报告） */
        .global-view {{ text-align: center; margin: 30px 0; }}
        .global-view img {{ max-width: 100%; border: 2px solid #ddd; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
        .global-view .caption {{ margin-top: 10px; font-size: 14px; color: #666; }}
        
        /* 信息框样式 */
        .info-box {{ background: #f8f9fa; border-left: 4px solid #667eea; padding: 15px 20px; margin: 15px 0; border-radius: 4px; }}
        .info-box strong {{ color: #667eea; }}
        
        /* 统计网格 */
        .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: linear-gradient(135deg, #f5f7fa 0%, #e4e8eb 100%); padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-card .number {{ font-size: 36px; font-weight: bold; }}
        .stat-card .label {{ color: #666; margin-top: 5px; }}
        .stat-card.wsi .number {{ color: #9c27b0; }}
        .stat-card.red .number {{ color: #f44336; }}
        .stat-card.blue .number {{ color: #2196F3; }}
        .stat-card.green .number {{ color: #4CAF50; }}
        .stat-card.fov .number {{ color: #c47a2c; }}
        .stat-card.indep .number {{ color: #2e7d6f; }}
        .stat-card.steps .number {{ color: #5c6bc0; }}
        .roam-route {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin: 16px 0 24px; }}
        .roam-route article {{ background: #fff; border: 1px solid #e6ded3; border-radius: 14px; padding: 16px; box-shadow: 0 3px 16px #0000000b; }}
        .roam-route h3 {{ font-size: 16px; margin-bottom: 8px; color: #4a3426; }}
        .roam-route img {{ width: 100%; border-radius: 8px; margin: 8px 0; }}
        .roam-route .meta, .roam-route small {{ display: block; color: #8a7a68; font-size: 12px; margin-top: 6px; }}
        
        /* ⭐ Patient Level 切片分隔样式 */
        .wsi-section {{ border: 2px solid #9c27b0; border-radius: 12px; margin: 30px 0; overflow: hidden; }}
        .wsi-section-header {{ background: linear-gradient(90deg, #9c27b0 0%, #673ab7 100%); color: white; padding: 18px 30px; font-size: 20px; font-weight: bold; cursor: pointer; user-select: none; position: relative; padding-left: 55px; }}
        .wsi-section-header::before {{ content: '▼'; position: absolute; left: 22px; transition: transform 0.3s; font-size: 16px; }}
        .wsi-section-header.collapsed::before {{ transform: rotate(-90deg); }}
        .wsi-section-body {{ padding: 0; }}
        .wsi-section-body.hidden {{ display: none; }}
        
        /* 路径卡片样式（复用单张报告） */
        .path-card {{ border: 1px solid #e0e0e0; border-radius: 8px; padding: 25px; margin: 20px 0; background: #fafafa; }}
        .path-card .path-header {{ background: linear-gradient(90deg, #667eea 0%, #764ba2 100%); color: white; padding: 12px 20px; border-radius: 6px; font-size: 18px; font-weight: bold; margin: -25px -25px 20px -25px; cursor: pointer; user-select: none; position: relative; padding-left: 50px; }}
        .path-card .path-header::before {{ content: '▶'; position: absolute; left: 20px; transition: transform 0.3s; }}
        .path-card .path-header.active::before {{ transform: rotate(90deg); }}
        .path-card .path-body {{ display: none; overflow: hidden; }}
        .path-card .path-body.show {{ display: block; }}
        
        /* ROI 层级样式（复用单张报告） */
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
        
        /* 蓝框内容折叠（复用单张报告） */
        .blue-box-content {{ display: none; overflow: hidden; }}
        .blue-box-content.show {{ display: block; }}
        
        /* ROI 图像网格（复用单张报告） */
        .roi-images {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 15px; margin-top: 15px; }}
        .roi-images img {{ width: 100%; border: 1px solid #ddd; border-radius: 4px; }}
        
        /* 思考过程（复用单张报告） */
        .thinking {{ background: #fff3e0; border-left: 4px solid #ff9800; padding: 15px; margin: 15px 0; border-radius: 4px; }}
        .thinking .label {{ font-weight: bold; color: #e65100; margin-bottom: 8px; cursor: pointer; user-select: none; position: relative; padding-left: 25px; }}
        .thinking .label::before {{ content: '▶'; position: absolute; left: 0; transition: transform 0.3s; }}
        .thinking .label.active::before {{ transform: rotate(90deg); }}
        .thinking pre {{ white-space: pre-wrap; word-wrap: break-word; font-family: inherit; margin: 0; line-height: 1.8; }}
        
        /* 折叠内容通用（复用单张报告） */
        .collapsible {{ cursor: pointer; user-select: none; position: relative; padding-left: 25px; }}
        .collapsible::before {{ content: '▶'; position: absolute; left: 0; transition: transform 0.3s; }}
        .collapsible.active::before {{ transform: rotate(90deg); }}
        .collapsible-content {{ display: none; overflow: hidden; }}
        .collapsible-content.show {{ display: block; }}
        
        /* 最终报告（复用单张报告） */
        .final-report {{ background: #fff; border: 3px solid #667eea; border-radius: 8px; padding: 30px; }}
        .final-report pre {{ white-space: pre-wrap; word-wrap: break-word; font-family: inherit; line-height: 1.8; margin: 0; }}
        
        /* ⭐ Patient Level 综合诊断报告样式 */
        .patient-final-report {{ background: linear-gradient(135deg, #f3e5f5 0%, #ede7f6 100%); border: 3px solid #9c27b0; border-radius: 8px; padding: 30px; }}
        .patient-final-report pre {{ white-space: pre-wrap; word-wrap: break-word; font-family: inherit; line-height: 1.8; margin: 0; }}
        
        /* 页脚 */
        .footer {{ background: #f5f5f5; padding: 20px; text-align: center; color: #666; font-size: 14px; }}
        
        @media print {{
            body {{ background: white; }}
            .container {{ box-shadow: none; }}
            .collapsible-content {{ display: block !important; }}
            .path-body {{ display: block !important; }}
            .blue-box-content {{ display: block !important; }}
            .wsi-section-body {{ display: block !important; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- 报告头部 -->
        <div class="header">
            <h1>🏥 {'Patient Level Comprehensive Pathology Report' if _en else 'Patient Level 综合病理报告'}</h1>
            <div class="meta">
                <p>{'Patient Folder' if _en else '患者文件夹'}: {folder_name}</p>
                <p>{'Analysis ID' if _en else '分析 ID'}: {patient_id}</p>
                <p>{'Slides' if _en else '切片数量'}: {completed_wsi}/{total_wsi}{'' if _en else ' 张'}</p>
                <p>{'Generated' if _en else '生成时间'}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S' if _en else '%Y年%m月%d日 %H:%M:%S')}</p>
            </div>
        </div>
        
        <div class="content">
            <!-- 总体统计 -->
            <div class="section">
                <h2 class="section-title">📊 {'Overall Statistics' if _en else '总体统计'}</h2>
                <div class="stats-grid">
                    {stats_cards_html}
                </div>
            </div>
"""
        
        # ==================== 各切片完整报告（rbind 上下合并） ====================
        output_dir_path = Path(output_dir)
        
        for wsi_idx, file_info in enumerate(file_infos, 1):
            fid = file_info.get('file_id')
            filename = file_info.get('filename', f'WSI_{wsi_idx}')
            part_name = file_info.get('part', f'Slide {wsi_idx}')
            
            # 获取该 WSI 的统计信息
            wsi_result = wsi_results.get(fid, {}) if fid else {}
            wsi_stats = wsi_result.get('statistics', {}) if wsi_result else {}
            wsi_red = wsi_stats.get('red_frames', wsi_stats.get('total_red', 0))
            wsi_blue = wsi_stats.get('blue_frames', wsi_stats.get('total_blue', 0))
            wsi_green = wsi_stats.get('green_frames', wsi_stats.get('total_green', 0))
            roam_stats = _roam_stats_from_result(wsi_result or {})
            if is_roam:
                header_counts = (
                    f"{roam_stats['fovs']} FOVs"
                    if _en else f"{roam_stats['fovs']}个视野"
                )
            else:
                header_counts = f"{wsi_red}{'R' if _en else '红'}/{wsi_blue}{'B' if _en else '蓝'}/{wsi_green}{'G' if _en else '绿'}"
            
            # ⭐ 切片分隔标题
            html_content += f"""
            <!-- ==================== 切片 {wsi_idx}: {part_name} ==================== -->
            <div class="wsi-section">
                <div class="wsi-section-header" onclick="toggleWsiSection(this)">
                    📋 {'Slide' if _en else '切片'} {wsi_idx}/{completed_wsi}: {part_name} - {filename} ({header_counts})
                </div>
                <div class="wsi-section-body">
"""
            
            # ⭐ 尝试读取该 WSI 的完整 HTML 报告（优先使用 output_paths 中的正确路径）
            wsi_html_path = None
            wsi_output_paths = wsi_result.get('output_paths', {}) if wsi_result else {}
            if wsi_output_paths:
                wsi_output_dir = wsi_output_paths.get('output_dir')
                if wsi_output_dir:
                    wsi_html_path = Path(wsi_output_dir) / 'pathology_report.html'
            # 兼容旧格式
            if not wsi_html_path or not wsi_html_path.exists():
                wsi_html_path = output_dir_path / f'wsi_{fid}' / 'pathology_report.html'
            roam_body = _render_patient_roam_slide_html(wsi_result, _en) if is_roam else ''
            wsi_body_content = None
            if roam_body:
                logger.info(f"[Patient Level] ✅ 嵌入 {part_name} 的单 Agent 游走过程")
                html_content += f"""
                    <div style="padding: 20px;">
                        {roam_body}
                    </div>
"""
            else:
                wsi_body_content = extract_wsi_report_content(wsi_html_path) if fid and wsi_html_path.exists() else None
                if wsi_body_content:
                    wsi_body_content = _rewrite_relative_step_images(wsi_body_content, wsi_html_path)
                    logger.info(f"[Patient Level] ✅ 成功嵌入 {part_name} 的完整 HTML 报告 ({len(wsi_body_content)} chars)")
                    html_content += f"""
                    <div style="padding: 20px;">
                        {wsi_body_content}
                    </div>
"""
            if not roam_body and not wsi_body_content:
                # ⭐ 读取失败：使用 tree_nodes 数据作为降级方案
                logger.warning(f"[Patient Level] ⚠️ 无法读取 {part_name} 的 HTML 报告，使用降级展示")
                tree_nodes = wsi_result.get('tree_nodes', []) if wsi_result else []
                wsi_summary = wsi_result.get('wsi_summary', '') if wsi_result else ''
                
                html_content += """
                    <div style="padding: 20px;">
"""
                
                if wsi_summary:
                    html_content += f"""
                        <div class="thinking">
                            <div class="label collapsible active">💭 {'Slide Diagnostic Summary (click to expand/collapse)' if _en else '切片诊断小结 (点击展开/收起)'}</div>
                            <div class="collapsible-content show">
                                <pre>{wsi_summary}</pre>
                            </div>
                        </div>
"""
                
                # 降级展示：遍历 tree_nodes
                for red_idx, red_node in enumerate(tree_nodes, 1):
                    red_id = red_node.get('roi_id', red_node.get('id', f'R{red_idx}'))
                    red_analysis = red_node.get('analysis', {})
                    red_findings = (
                        red_analysis.get('reasoning', '') or 
                        red_analysis.get('findings', '') or 
                        ('No analysis results' if _en else '无分析结果')
                    )
                    
                    html_content += f"""
                        <div class="path-card">
                            <div class="path-header" onclick="togglePath(this)">
                                🔬 {'Path' if _en else '路径'} {red_idx}{'：' if not _en else ': '}{'Red Box' if _en else '红框'} {red_id}
                            </div>
                            <div class="path-body">
                                <div class="roi-level red">
                                    <span class="level-badge">🔴 10x</span>
                                    <div class="thinking">
                                        <div class="label">💭 {'AI Analysis' if _en else 'AI 分析'}</div>
                                        <pre>{red_findings}</pre>
                                    </div>
                                </div>
"""
                    
                    for blue_node in red_node.get('children', []):
                        blue_id = blue_node.get('roi_id', blue_node.get('id', ''))
                        blue_analysis = blue_node.get('analysis', {})
                        blue_findings = blue_analysis.get('reasoning', '') or blue_analysis.get('findings', '') or ''
                        
                        html_content += f"""
                                <div class="roi-level blue">
                                    <span class="level-badge">🔵 40x - {blue_id}</span>
                                    <div class="thinking">
                                        <div class="label">💭 {'AI Analysis' if _en else 'AI 分析'}</div>
                                        <pre>{blue_findings}</pre>
                                    </div>
"""
                        
                        for green_node in blue_node.get('children', []):
                            green_id = green_node.get('roi_id', green_node.get('id', ''))
                            green_analysis = green_node.get('analysis', {})
                            green_findings = green_analysis.get('reasoning', '') or green_analysis.get('findings', '') or ''
                            
                            html_content += f"""
                                    <div class="roi-level green">
                                        <span class="level-badge">🟢 160x - {green_id}</span>
                                        <div class="thinking">
                                            <div class="label">💭 {'AI Analysis' if _en else 'AI 分析'}</div>
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
"""
            
            # 关闭 wsi-section-body 和 wsi-section
            html_content += """
                </div>
            </div>
"""
        
        # ==================== Patient Level 综合诊断报告（底部） ====================
        ai_final_report = patient_state.get('ai_final_report', '')
        if ai_final_report:
            html_content += f"""
            <!-- Patient Level 综合诊断报告 -->
            <div class="section">
                <h2 class="section-title">📋 {'Patient Level Comprehensive Diagnostic Report' if _en else 'Patient Level 综合诊断报告'}</h2>
                <div class="info-box" style="background: #f3e5f5; border-left-color: #9c27b0;">
                    <p><strong>🤖 {'AI Comprehensive Diagnosis — based on all evidence from the above' if _en else 'AI 综合诊断 —— 基于以上'} {completed_wsi} {'slides' if _en else '张切片的全部证据汇总'}</strong></p>
                </div>
                <div class="patient-final-report" style="margin-top: 15px;">
                    <pre>{ai_final_report}</pre>
                </div>
            </div>
"""
        
        # ==================== 页脚 + JavaScript ====================
        html_content += f"""
        </div>
        
        <div class="footer">
            <p>{'This report was generated with PathCopilot AI assistance. For reference only — final diagnosis must be confirmed by a professional pathologist.' if _en else '本报告由 PathCopilot AI 辅助生成，仅供参考，最终诊断需由专业病理医生确认'}</p>
            <p>{'Generated' if _en else '生成时间'}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
    
    <script>
        // ⭐ Patient Level 切片区域折叠/展开
        function toggleWsiSection(header) {{
            header.classList.toggle('collapsed');
            const body = header.nextElementSibling;
            if (body && body.classList.contains('wsi-section-body')) {{
                body.classList.toggle('hidden');
            }}
        }}
        
        // 路径卡片折叠/展开（复用单张报告）
        function togglePath(header) {{
            header.classList.toggle('active');
            const pathBody = header.nextElementSibling;
            if (pathBody && pathBody.classList.contains('path-body')) {{
                pathBody.classList.toggle('show');
            }}
        }}
        
        // 蓝框折叠/展开（复用单张报告）
        function toggleBlueBox(badge) {{
            badge.classList.toggle('active');
            const blueBoxContent = badge.nextElementSibling;
            if (blueBoxContent && blueBoxContent.classList.contains('blue-box-content')) {{
                blueBoxContent.classList.toggle('show');
            }}
        }}
        
        // 内容折叠/展开（复用单张报告）
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
            
            // 默认展开第一个切片
            const firstWsiHeader = document.querySelector('.wsi-section-header');
            if (firstWsiHeader) {{
                // 第一个切片默认展开（不做任何操作，因为默认不带 collapsed/hidden）
            }}
            
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
        
        # 保存 HTML 文件
        output_dir_path.mkdir(parents=True, exist_ok=True)
        html_path = output_dir_path / "patient_report.html"
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info(f"[Patient Level] ✅ 合并 HTML 报告已生成: {html_path}")
        return str(html_path)
        
    except Exception as e:
        logger.error(f"[Patient Level] 生成合并 HTML 报告失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def generate_final_pathology_report(agent_state, file_id, output_dir, socketio=None, room=None, report_title=None, is_patient_level=False):
    """
    聚合所有层级分析结果，生成最终病理报告
    
    Args:
        agent_state: Agent状态字典
        file_id: 文件ID
        output_dir: 输出目录
        socketio: SocketIO实例（可选，用于实时推送）
        room: WebSocket房间ID（可选）
        report_title: 报告标题（可选，默认"最终病理诊断报告"，Patient Level 下使用"单张切片病理诊断报告"）
        is_patient_level: 是否为 Patient Level 多切片模式
    
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
                'pathology_type': analysis.get('pathology_type', 'unclassified'),
                'key_features': analysis.get('key_features', [])
            })
            
            for blue_node in red_node['children']:
                analysis = blue_node['analysis']
                all_blue_data.append({
                    'findings': analysis.get('findings', ''),
                    'confidence': analysis.get('confidence', 50),
                    'pathology_type': analysis.get('pathology_type', 'unclassified'),
                    'key_features': analysis.get('key_features', [])
                })
                
                for green_node in blue_node['children']:
                    analysis = green_node['analysis']
                    all_green_data.append({
                        'findings': analysis.get('findings', ''),
                        'confidence': analysis.get('confidence', 50),
                        'pathology_type': analysis.get('pathology_type', 'unclassified'),
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
                    for key, val in evidence.items():
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
        
        # 注入全局缩略图分析；它提供局部 ROI 无法替代的全片结构与代表性背景。
        global_analysis_text = agent_state.get('global_analysis', '')
        if isinstance(global_analysis_text, dict):
            global_analysis_text = global_analysis_text.get('reasoning', '') or global_analysis_text.get('analysis', '')
        elif isinstance(global_analysis_text, list):
            global_analysis_text = '\n'.join([str(item) for item in global_analysis_text])
        if global_analysis_text:
            _ga_label = "Thumbnail Global Analysis (Orchestrator — full slide overview)" if _is_en else "缩略图全局分析（Orchestrator — 全切片架构观察）"
            all_summaries = f"🔭 **{_ga_label}**:\n{global_analysis_text}\n\n{'─' * 60}\n\n{all_summaries}"
        
        # Prefer the raw, traceable Memory tree over pre-aggregated labels.
        try:
            import json as _json_v2
            _mtree_v2, _ = build_memory_tree(agent_state)
            _mem_v2 = strip_for_synthesis(
                _mtree_v2,
                drop_keys=('rect', 'image_path', 'filename'),
            )
            if _is_en:
                _reason_rules = (
                    "Evidence-integration rules:\n"
                    "1. Answer only the active user-defined objective; do not map observations to a hidden checklist.\n"
                    "2. Treat direct morphology as evidence and upstream conclusions as hypotheses.\n"
                    "3. Resolve conflicts using image quality, specificity, scale, representativeness, and concrete counter-evidence rather than vote counts alone.\n"
                    "4. A selected high-power field may establish a local cellular feature but cannot by itself establish a whole-slide proportion, grade, extent, or absence.\n"
                    "5. Preserve material positive, negative, conflicting, and indeterminate findings, each with ROI provenance.\n"
                    "6. Do not convert missing evidence into a negative finding or invent unobserved anatomy, tests, measurements, or staging information.\n"
                )
                _tree_header = (
                    "[Structured Memory Tree — raw multiscale, traceable ROI evidence]"
                )
            else:
                _reason_rules = (
                    "证据整合规则：\n"
                    "1. 只回答用户当前明确提出的目标，不得把观察结果套入隐藏问题清单。\n"
                    "2. 当前视野直接形态属于证据；上级结论仅是待验证假设。\n"
                    "3. 冲突裁决依据图像质量、特异性、倍率、代表性和具体反证，不得只按数量投票。\n"
                    "4. 主动选择的高倍视野可确认局部细胞特征，但不能单独确定全片比例、分级、范围或阴性结论。\n"
                    "5. 保留重要的阳性、阴性、矛盾和无法确定发现，并逐项标注 ROI 来源。\n"
                    "6. 不得把缺失证据当作阴性，也不得虚构未观察到的解剖部位、检测、测量或分期信息。\n"
                )
                _tree_header = "【结构化记忆树——原始、多尺度、可追溯 ROI 证据】"
            all_summaries = (
                f"{_reason_rules}\n{'─' * 60}\n{_tree_header}\n"
                + _json_v2.dumps(_mem_v2, ensure_ascii=False, indent=1)
            )
            logger.info(
                f"🧠 [Memory] 使用通用原始证据树进行汇总 "
                f"({len(all_summaries) // 1024}KB)"
            )
        except Exception as _e_v2:
            logger.warning(f"[V2] Memory 证据源构建失败, 回退旧叙述式 all_summaries: {_e_v2}")
        
        # All open-source modes share the same user-defined, organ-agnostic synthesis contract.

        # The open-source runtime has one organ-agnostic synthesis contract.
        report_prompt = _build_generic_final_report_prompt(
            agent_state,
            all_summaries,
            (total_red, total_blue, total_green),
            lang=lang,
        )

        # 调用AI生成报告
        logger.info(f"\n{'='*80}")
        logger.info("📝 正在调用AI生成最终病理报告...")
        logger.info(f"{'='*80}")
        from agent.gpt_client import call_gpt_vision
        
        # 由于是纯文本，不需要图像
        final_report_text = call_gpt_vision(report_prompt, None)
        if not (final_report_text or '').strip() or '{"selected_grids"' in final_report_text:
            final_report_text = (
                'Automatic synthesis was not completed; no diagnostic conclusion can be drawn. Please run the analysis again.'
                if lang == 'en' else
                '自动汇总未完成，不能据此形成诊断结论，请重新分析。'
            )
        
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
            # 证据链单独落盘，最终报告只保留面向用户的综合回答。
        
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
            # ⭐ Agent Loop：证据聚合结果
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


def generate_html_visual_report_with_user_report(
    agent_state,
    file_id,
    output_dir,
    report_text,
    socketio=None,
    room=None,
    *,
    user_edited=True,
):
    """
    ⭐ 使用用户编辑的报告直接生成 HTML 报告，不调用 AI
    
    当用户在 Phase 5 手动编辑了报告内容后，直接使用用户的版本生成 HTML，
    避免重复调用 AI 浪费资源并覆盖用户的修改。
    
    Args:
        agent_state: Agent状态字典
        file_id: 文件ID
        output_dir: 输出目录
        report_text: 要发布的报告文本
        socketio: SocketIO实例（可选）
        room: WebSocket房间ID（可选）
    
    Returns:
        dict: 包含报告路径等信息的字典
    """
    try:
        provenance_label = "用户编辑版" if user_edited else "AI生成，经人工审阅未修改"
        logger.info(f"[报告发布] 生成 HTML，来源={provenance_label}")
        
        # 获取统计信息
        tree_nodes = agent_state.get('tree_nodes', [])
        total_red = len(tree_nodes)
        total_blue = sum(len(node.get('children', [])) for node in tree_nodes)
        total_green = sum(
            len(blue.get('children', []))
            for red in tree_nodes
            for blue in red.get('children', [])
        )
        
        # 确保 output_dir 是 Path 对象
        if not isinstance(output_dir, Path):
            output_dir = Path(output_dir)
        
        # 保存用户编辑的报告文本（目录名已包含文件信息，无需在文件名中重复）
        report_path = output_dir / "pathology_report.txt"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(f"# 病理诊断报告 ({provenance_label})\n")
            f.write(f"# 文件: {agent_state.get('filename', '未知')}\n")
            f.write(f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(report_text)
            f.write(f"\n\n---\n")
            f.write(f"## 分析统计\n")
            f.write(f"- 10× 红框: {total_red}个\n")
            f.write(f"- 40× 蓝框: {total_blue}个\n")
            f.write(f"- 160× 绿框: {total_green}个\n")
            if user_edited:
                f.write(f"\n> 📝 此报告由用户手动编辑，未经 AI 重新生成。\n")
            else:
                f.write(f"\n> 📝 此报告由 AI 生成，并经用户审阅批准，正文未作人工改写。\n")
        
        logger.info(f"[报告发布] 报告文本已保存: {report_path}")
        
        # 发送消息到前端
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'assistant',
                'content': f"✅ {('Final Pathology Report (User Edited):' if user_edited else 'Final Pathology Report (AI-generated; human-reviewed):') if agent_state.get('lang', 'zh') == 'en' else ('最终病理报告（用户编辑版）：' if user_edited else '最终病理报告（AI生成，经人工审阅未修改）：')}\n\n{report_text}",
                'metadata': {
                    'message_type': 'final_report',
                    'report': report_text,
                    'user_edited': bool(user_edited),
                    'human_reviewed': True,
                    'human_modified': bool(user_edited)
                }
            }, to=room)
        
        # 生成 HTML 可视化报告
        html_report_path = generate_html_visual_report(
            agent_state, file_id, output_dir, report_text, total_red, total_blue, total_green
        )
        logger.info(f"[报告发布] HTML 报告已生成: {html_report_path}")
        
        # 发送 HTML 报告路径到前端（使用 output_paths 中的正确路径）
        output_paths = agent_state.get('output_paths', {})
        html_path = output_paths.get('report_html', f'/outputs/{file_id}/auto/pathology_report.html')
        txt_path = output_paths.get('report_txt', f'/outputs/{file_id}/auto/pathology_report.txt')
        if socketio and room:
            socketio.emit('agent.report_ready', {
                'html_path': html_path,
                'txt_path': txt_path,
                'type': 'html',
                'user_edited': bool(user_edited),
                'human_reviewed': True,
                'human_modified': bool(user_edited)
            }, to=room)
        
        return {
            'report_text': report_text,
            'report_path': str(report_path),
            'html_report_path': str(html_report_path) if html_report_path else None,
            'statistics': {
                'red_frames': total_red,
                'blue_frames': total_blue,
                'green_frames': total_green
            },
            'tree_structure': tree_nodes,
            'user_edited': bool(user_edited),
            'human_reviewed': True,
            'human_modified': bool(user_edited)
        }
        
    except Exception as e:
        logger.error(f"[报告发布] 生成报告失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'report_text': report_text,
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
        report_lines.append("# 🔬 PathCopilot Agent Loop 路径分析报告")
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
            
            # 红框 Agent Loop 信息
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
            if red_selected:
                report_lines.append(f"- **选择区域**: {red_selected}")
            report_lines.append("")
            
            # 置信度和病理类型
            red_confidence = red_analysis.get('confidence', 0)
            red_confidence_level = red_analysis.get('confidence_level', 2)
            red_pathology_type = red_analysis.get('pathology_type', 'unclassified')
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
                
                # 蓝框 Agent Loop 信息
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
                if blue_selected:
                    report_lines.append(f"- **选择区域**: {blue_selected}")
                report_lines.append("")
                
                # 置信度
                blue_confidence = blue_analysis.get('confidence', 0)
                blue_confidence_level = blue_analysis.get('confidence_level', 2)
                blue_pathology_type = blue_analysis.get('pathology_type', 'unclassified')
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
                    
                    # 绿框 Agent Loop 信息
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
                    green_pathology_type = green_analysis.get('pathology_type', 'unclassified')
                    green_key_features = green_analysis.get('key_features', [])
                    
                    type_emoji = "⚠️" if green_pathology_type == 'malignant' else "✅" if green_pathology_type == 'benign' else "❓"
                    level_desc = {0: '证据不足', 1: '不确定', 2: '可能', 3: '较确定', 4: '确定'}.get(green_confidence_level, '未知')
                    report_lines.append(f"- **最终置信度**: Level {green_confidence_level} ({level_desc})")
                    if green_key_features:
                        report_lines.append(f"- **关键特征**: {', '.join(green_key_features)}")
                    report_lines.append("")
                    
                    # 完整路径回顾
                    report_lines.append(f"###### 【Memory 阶段】路径 #{path_count} 完整链条")
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
        report_lines.append("*本报告由 PathCopilot Agent Loop 自动生成，仅供参考，最终诊断请以专业病理医师意见为准。*")
        
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
                'secondary_review_plan': agent_state.get('secondary_review_plan', []),
                'secondary_review_results': agent_state.get('secondary_review_results', []),
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
                    'confidence': red_node.get('analysis', {}).get('confidence', 0),
                    'pathology_type': red_node.get('analysis', {}).get('pathology_type', 'unclassified')
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
                        'confidence': blue_node.get('analysis', {}).get('confidence', 0),
                        'pathology_type': blue_node.get('analysis', {}).get('pathology_type', 'unclassified')
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
                            'pathology_type': green_node.get('analysis', {}).get('pathology_type', 'unclassified'),
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


# ==================== 五阶段协作模式 ====================
# 
# Phase 1: 缩略图分析 (Orchestrator) - 输出红框选择的 Reason + Plan
# Phase 2: 红框分析 (SubAgents 并行) - 输出蓝框选择的 Reason + Plan
# Phase 3: 蓝框分析 (SubAgents 并行) - 输出绿框选择的 Reason + Plan
# Phase 4: 绿框分析 (SubAgents 并行) - 输出细胞级 Reason
# Phase 5: 汇总报告 (Orchestrator) - 生成诊断报告
#

PHASE_NAMES = {
    'zh': {
        1: "全片缩略图",
        2: "红框分析 (10×)",
        3: "蓝框分析 (40×)",
        4: "绿框分析 (160×)",
        5: "诊断报告汇总"
    },
    'en': {
        1: "Thumbnail Analysis (0×)",
        2: "Red Box Analysis (10×)",
        3: "Blue Box Analysis (40×)",
        4: "Green Box Analysis (160×)",
        5: "Diagnostic Report Summary"
    }
}


def get_phase_name(phase: int, lang: str = 'zh') -> str:
    """获取阶段名称（支持双语）"""
    names = PHASE_NAMES.get(lang, PHASE_NAMES['zh'])
    return names.get(phase, f"Phase {phase}")


def emit_phase_result(socketio, room, phase: int, result: dict, agent_state: dict = None):
    """
    发送阶段结果给前端
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID
        phase: 阶段编号 (1-5)
        result: 阶段结果字典，包含 reasoning, plan, selected_regions 等
        agent_state: Agent状态字典（可选，用于注入 Patient Level WSI 上下文）
    """
    # ⭐⭐⭐ 调试日志：记录 subagent_results 中的 reasoning 内容 ⭐⭐⭐
    subagent_results = result.get('subagent_results', [])
    if subagent_results:
        for idx, sr in enumerate(subagent_results):
            reasoning_len = len(sr.get('reasoning', '') or '')
            findings_len = len(sr.get('findings', '') or '')
            logger.info(f"  📋 [Phase {phase}] SubAgent[{idx}] {sr.get('roi_id', '?')}: reasoning={reasoning_len}字, findings={findings_len}字")
            if reasoning_len > 0:
                logger.info(f"     └─ reasoning 前100字: {(sr.get('reasoning', '') or '')[:100]}...")
    
    # ⭐⭐⭐ 调试：记录发送的 reasoning 长度 ⭐⭐⭐
    reasoning_to_send = result.get('reasoning', '')
    logger.info(f"📤 [Phase {phase}] 准备发送 reasoning，长度: {len(reasoning_to_send)}")
    if reasoning_to_send:
        logger.info(f"   └─ reasoning 前300字: {reasoning_to_send[:300]}...")
        logger.info(f"   └─ reasoning 后100字: ...{reasoning_to_send[-100:]}")
    
    review_policy = (
        str((agent_state or {}).get('collaboration_review_policy') or 'staged')
        .strip().lower()
    )
    # ``staged`` asks for an explicit decision after every phase.
    # ``final`` auto-accepts Phases 1–4. The human gate is the Phase-5
    # draft: Orchestrator writes the report first, then the pathologist
    # reviews the ROI paths that actually ground that draft.
    requires_confirmation = review_policy == 'staged' or int(phase) == 5
    phase_gate_id = uuid.uuid4().hex if requires_confirmation else ''

    # ⭐ 构建事件数据
    phase_data = {
        'phase': phase,
        'phase_name': get_phase_name(phase, agent_state.get('lang', 'zh') if agent_state else 'zh'),
        'reasoning': reasoning_to_send,
        'plan': result.get('plan', []),
        'evidence': result.get('evidence', ''),  # ⭐ Agent Loop Evidence（Phase 1 的证据）
        'selected_regions': result.get('selected_regions', []),
        'all_candidates': result.get('all_candidates', []),  # ⭐ 完整候选框列表（用于点击添加）
        'subagent_results': result.get('subagent_results', []),  # Phase 2-4 的子代理结果
        'report': result.get('report', ''),  # Phase 5 的报告内容
        'reportable_findings': result.get('reportable_findings') or [],
        'metadata': result.get('metadata', {}),
        'review_policy': review_policy,
        'requires_confirmation': requires_confirmation,
        'phase_gate_id': phase_gate_id,
        'file_id': str((agent_state or {}).get('file_id') or ''),
    }
    
    # ⭐ Patient Level 协作模式：附带 WSI 上下文（让前端知道是哪张切片的哪个阶段）
    wsi_context = result.get('wsi_context')
    if not wsi_context and agent_state and agent_state.get('is_patient_level'):
        wsi_context = {
            'wsi_index': agent_state.get('wsi_index', 1),
            'total_wsi': agent_state.get('total_wsi', 1),
            'part_name': agent_state.get('part_name', 'Slide 1'),
            'folder_name': agent_state.get('folder_name', 'Patient'),
            'file_id': agent_state.get('file_id', ''),
        }
    if wsi_context:
        phase_data['wsi_context'] = wsi_context

    # Keep the current collaboration gate recoverable after a browser refresh.
    # This is an in-memory, owner-scoped UI snapshot of the same public payload
    # emitted to the authenticated Socket room; no hidden reasoning or slide
    # object is copied into Runtime persistence.
    if agent_state is not None:
        snapshot_lock = agent_state.get('collaboration_snapshot_lock')
        if snapshot_lock:
            snapshot_lock.acquire()
        try:
            # Keep the last public payload for every completed/visible phase.
            # The browser uses this process-local history after a refresh to
            # rebuild the same editable/quotable ROI rows that were already
            # shown live. It is not a private reasoning replay and does not
            # alter the Runtime event cursor.
            history = agent_state.setdefault('collaboration_phase_history', {})
            history[str(int(phase))] = phase_data
            agent_state['current_phase'] = int(phase)
            agent_state['phase_requires_confirmation'] = requires_confirmation
            agent_state['phase_status'] = 'pending' if requires_confirmation else 'auto_accepted'
            agent_state['phase_confirmation'] = None
            agent_state['phase_gate_id'] = phase_gate_id
            agent_state['current_phase_payload'] = phase_data
        finally:
            if snapshot_lock:
                snapshot_lock.release()
    
    socketio.emit('agent.phase_result', phase_data, to=room)
    
    # ⭐ 关键：强制让出控制权，让 eventlet 有机会发送 WebSocket 消息
    # 否则后续的阻塞操作（如 wait_for_phase_confirmation）会导致消息堆积
    socketio.sleep(0)
    
    logger.info(f"📤 [Phase {phase}] 已发送阶段结果: {get_phase_name(phase)}")


def emit_orchestrator_confirmation(socketio, room, user_action: str, modification_summary: str = "", lang: str = 'zh'):
    """
    发送 Orchestrator 对用户修改的确认消息
    """
    if lang == 'en':
        if user_action == 'execute':
            message = "OK, proceeding to the next analysis phase."
        else:
            message = f"Understood: {modification_summary}\n\nPlan updated, proceeding to the next phase."
    else:
        if user_action == 'execute':
            message = "好的，我将继续执行下一阶段的分析。"
        else:
            message = f"我已理解您的修改：{modification_summary}\n\n已更新分析计划，现在继续执行下一阶段。"
    
    socketio.emit('agent.orchestrator_reply', {
        'type': 'orchestrator_confirmation',
        'action': user_action,
        'message': message
    }, to=room)
    
    # ⭐ 强制发送 WebSocket 消息
    socketio.sleep(0)


def emit_pre_report_review_gate(socketio, room, agent_state: dict, review_queue: list) -> str:
    """Open the evidence-queue gate before the final report is generated.

    Used only by ``review_policy='final'``.  It reuses the Phase-4 approval
    transport after green analysis has finished, and marks the payload as
    ``pre_report`` so the frontend does not pretend a draft already exists.
    Signing off the later Phase-5 draft is a separate human decision.
    """
    lang = agent_state.get('lang', 'zh')
    gate_id = uuid.uuid4().hex
    if lang == 'en':
        phase_name = 'Final evidence review'
        reasoning = (
            f'{len(review_queue)} unresolved ROI(s) are available for inspection. '
            'Review the highlighted evidence before authorizing final-report generation.'
        )
    else:
        phase_name = '终末证据审阅'
        reasoning = (
            f'共有 {len(review_queue)} 个未解决 ROI 可供检查。'
            '请先审阅高亮证据，再明确授权生成最终报告。'
        )
    phase_data = {
        'phase': 4,
        'phase_name': phase_name,
        'reasoning': reasoning,
        'plan': [],
        'evidence': '',
        'selected_regions': [],
        'all_candidates': [],
        'subagent_results': [],
        'report': '',
        'metadata': {'review_queue_count': len(review_queue)},
        'review_policy': 'final',
        'review_gate': 'pre_report',
        'requires_confirmation': True,
        'phase_gate_id': gate_id,
    }
    snapshot_lock = agent_state.get('collaboration_snapshot_lock')
    if snapshot_lock:
        snapshot_lock.acquire()
    try:
        agent_state['current_phase'] = 4
        agent_state['phase_requires_confirmation'] = True
        agent_state['phase_status'] = 'pending'
        agent_state['phase_confirmation'] = None
        agent_state['phase_gate_id'] = gate_id
        agent_state['current_phase_payload'] = phase_data
    finally:
        if snapshot_lock:
            snapshot_lock.release()
    socketio.emit('agent.phase_result', phase_data, to=room)
    socketio.sleep(0)
    return gate_id


def _emit_collaborative_runtime_roi_events(socketio, room, agent_state: dict, level: str) -> None:
    """Publish confirmed collaborative ROI work through the Runtime bridge.

    Only stable ``R*``/``R*-B*``/``R*-B*-G*`` identifiers are emitted.  Child
    identifiers are planning counts for the projector; they are never used to
    pre-create temporary grid tasks.
    """
    stable_patterns = {
        "red": re.compile(r"^R\d+$"),
        "blue": re.compile(r"^R\d+-B\d+$"),
        "green": re.compile(r"^R\d+-B\d+-G\d+$"),
    }
    if level not in stable_patterns:
        return

    def stable(value, expected_level):
        text = str(value or "").strip()
        return text if stable_patterns[expected_level].fullmatch(text) else ""

    if level == "red":
        analyses = {
            stable(item.get("roi_id") or item.get("id"), "red"): item
            for item in (agent_state.get("red_analyses") or [])
            if isinstance(item, dict)
        }
        blue_by_parent = {}
        for roi in agent_state.get("selected_blue_rois") or []:
            if not isinstance(roi, dict):
                continue
            blue_id = stable(roi.get("id") or roi.get("roi_id"), "blue")
            parent_id = stable(roi.get("parent_id") or roi.get("parent_roi_id"), "red")
            if blue_id and parent_id:
                blue_by_parent.setdefault(parent_id, []).append(blue_id)
        for roi in agent_state.get("selected_red_rois") or []:
            if not isinstance(roi, dict):
                continue
            roi_id = stable(roi.get("id") or roi.get("roi_id"), "red")
            if not roi_id:
                continue
            analysis = analyses.get(roi_id) or {}
            child_ids = blue_by_parent.get(roi_id, [])
            socketio.emit("agent.red_analysis", {
                "roi_id": roi_id,
                "observation": analysis.get("reasoning") or analysis.get("findings") or "",
                "blue_ids": child_ids,
                "blue_count": len(child_ids),
            }, to=room)
        socketio.sleep(0)
        return

    if level == "blue":
        analyses = {
            stable(item.get("roi_id") or item.get("blue_id") or item.get("id"), "blue"): item
            for item in (agent_state.get("blue_analyses") or [])
            if isinstance(item, dict)
        }
        green_by_parent = {}
        for roi in agent_state.get("selected_green_rois") or []:
            if not isinstance(roi, dict):
                continue
            green_id = stable(roi.get("id") or roi.get("roi_id"), "green")
            parent_id = stable(roi.get("parent_id") or roi.get("parent_blue_id"), "blue")
            if green_id and parent_id:
                green_by_parent.setdefault(parent_id, []).append(green_id)
        for roi in agent_state.get("selected_blue_rois") or []:
            if not isinstance(roi, dict):
                continue
            blue_id = stable(roi.get("id") or roi.get("roi_id"), "blue")
            parent_id = stable(roi.get("parent_id") or roi.get("parent_roi_id"), "red")
            if not blue_id:
                continue
            analysis = analyses.get(blue_id) or {}
            child_ids = green_by_parent.get(blue_id, [])
            socketio.emit("agent.blue_analysis", {
                "blue_id": blue_id,
                "parent_roi_id": parent_id,
                "findings": analysis.get("reasoning") or analysis.get("findings") or "",
                "green_count": len(child_ids),
                "no_green_needed": not child_ids,
            }, to=room)
        socketio.sleep(0)
        return

    analyses = {
        stable(item.get("roi_id") or item.get("green_id") or item.get("id"), "green"): item
        for item in (agent_state.get("green_analyses") or [])
        if isinstance(item, dict)
    }
    for roi in agent_state.get("selected_green_rois") or []:
        if not isinstance(roi, dict):
            continue
        green_id = stable(roi.get("id") or roi.get("roi_id"), "green")
        parent_id = stable(roi.get("parent_id") or roi.get("parent_blue_id"), "blue")
        if not green_id:
            continue
        analysis = analyses.get(green_id) or {}
        socketio.emit("agent.green_analysis", {
            "green_id": green_id,
            "parent_blue_id": parent_id,
            "findings": analysis.get("reasoning") or analysis.get("findings") or "",
        }, to=room)
    socketio.sleep(0)


def wait_for_phase_confirmation(socketio, room, agent_state: dict, phase: int, timeout=None) -> dict:
    """
    等待用户确认当前阶段（硬闸门）
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID
        agent_state: Agent状态字典
        phase: 当前阶段编号
        timeout: 超时时间（秒），默认30分钟
    
    Returns:
        dict: {'action': 'execute'/'modify', 'modified_data': {...} 或 None}
    """
    import time
    
    snapshot_lock = agent_state.get('collaboration_snapshot_lock')
    if snapshot_lock:
        snapshot_lock.acquire()
    try:
        requires_confirmation = bool(agent_state.get('phase_requires_confirmation'))
        current_phase = int(agent_state.get('current_phase') or 0)
    finally:
        if snapshot_lock:
            snapshot_lock.release()
    if not requires_confirmation:
        return {'action': 'execute', 'modified_data': None, 'accepted_by_policy': True}
    if current_phase != int(phase):
        raise RuntimeError(f"phase gate mismatch: expected {current_phase}, got {phase}")
    
    logger.info(f"⏳ [Phase {phase}] 等待用户确认...")
    
    start_time = time.time()
    while True:
        if snapshot_lock:
            snapshot_lock.acquire()
        try:
            phase_status = agent_state.get('phase_status')
        finally:
            if snapshot_lock:
                snapshot_lock.release()
        if phase_status != 'pending':
            break
        # 检查停止标志
        if agent_stop_flags.get(room, False):
            logger.info(f"⛔ [Phase {phase}] Agent被用户停止")
            return {'action': 'stopped', 'modified_data': None}
        
        # 检查超时
        if timeout is not None and time.time() - start_time > timeout:
            logger.warning(f"⏰ [Phase {phase}] 等待确认超时（{timeout}秒），停止本次协作分析")
            return {'action': 'stopped', 'modified_data': None, 'reason': 'confirmation_timeout'}
        
        socketio.sleep(0.5)
    
    # 获取确认结果
    if snapshot_lock:
        snapshot_lock.acquire()
    try:
        confirmation = dict(agent_state.get('phase_confirmation') or {})
    finally:
        if snapshot_lock:
            snapshot_lock.release()
    action = confirmation.get('action', 'execute')
    modified_data = confirmation.get('modified_data')
    
    logger.info(f"✅ [Phase {phase}] 用户确认: action={action}")
    
    return {'action': action, 'modified_data': modified_data}


def run_collaborative_agent(socketio, room, file_id: str, slide, filename: str, dimensions: tuple,
                           output_dir: Path, steps_dir: Path, max_steps: int = 12, 
                           strategy: str = "default", patient_level_context: dict = None,
                           output_paths: dict = None, lang: str = 'zh',
                           diagnostic_question: str = '',
                           review_policy: str = 'staged', runtime_run_id: str = '') -> dict:
    """
    五阶段协作模式主循环
    
    Phase 1: 缩略图分析 (Orchestrator) - 输出红框选择的 Reason + Plan
    Phase 2: 红框分析 (SubAgents 并行) - 输出蓝框选择的 Reason + Plan  
    Phase 3: 蓝框分析 (SubAgents 并行) - 输出绿框选择的 Reason + Plan
    Phase 4: 绿框分析 (SubAgents 并行) - 输出细胞级 Reason
    Phase 5: 汇总报告 (Orchestrator) - 生成诊断报告
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID
        file_id: WSI文件ID
        slide: OpenSlide对象
        filename: 文件名
        dimensions: WSI尺寸 (width, height)
        output_dir: 输出目录
        steps_dir: 步骤目录
        max_steps: 最大步数
        strategy: 分析策略
        patient_level_context: Patient Level上下文（可选）
        output_paths: 输出路径信息（由 get_output_paths 生成）
    
    Returns:
        dict: 分析结果
    """
    try:
        logger.info(f"\n{'='*80}")
        logger.info(f"🤝 五阶段协作模式启动")
        logger.info(f"   文件: {filename}")
        logger.info(f"   尺寸: {dimensions}")
        logger.info(f"   输出目录: {output_dir}")
        logger.info(f"{'='*80}\n")
        
        # 初始化 Agent 状态
        organ = _organ_label_for_file(file_id, lang)
        review_policy = 'final' if str(review_policy).lower() == 'final' else 'staged'
        agent_state = {
            'mode': 'collaborative',
            'current_phase': 1,
            'phase_status': 'pending',  # pending / confirmed / modified
            'phase_confirmation': None,
            'file_id': file_id,
            'filename': filename,
            'dimensions': dimensions,
            'room': room,
            'slide': slide,
            'output_paths': output_paths,
            'lang': lang,  # ⭐ 语言设置: 'zh' 或 'en'
            'organ': organ,
            'diagnostic_question': str(diagnostic_question or '').strip(),
            'collaboration_review_policy': review_policy,
            'runtime_run_id': str(runtime_run_id or ''),
            'collaboration_snapshot_lock': threading.RLock(),
            # ROI 数据
            'candidate_rois': [],       # Phase 1 输出的红框
            'selected_red_rois': [],    # 用户确认的红框
            'blue_rois': [],            # Phase 2 输出的蓝框
            'selected_blue_rois': [],   # 用户确认的蓝框
            'green_rois': [],           # Phase 3 输出的绿框
            'selected_green_rois': [],  # 用户确认的绿框
            # 分析结果
            'red_analyses': [],         # 红框分析结果
            'blue_analyses': [],        # 蓝框分析结果
            'green_analyses': [],       # 绿框分析结果
            'pathology_findings': [],   # 所有病理发现
            # ROI 计数器
            'r_roi_counter': 0,
            'b_roi_counter': 0,
            'g_roi_counter': 0,
            # 树结构
            'tree_nodes': [],
            'all_overlays': [],
            # 证据池
            'evidence_pool': EvidencePool() if EvidencePool else [],
            # ⭐ 协作模式专用：用户修改历史追踪
            'user_modifications': {
                'phase1': {'modified': False, 'ai_selection': [], 'user_selection': [], 'added': [], 'removed': []},
                'phase2': {'modified': False, 'ai_selection': [], 'user_selection': [], 'added': [], 'removed': []},
                'phase3': {'modified': False, 'ai_selection': [], 'user_selection': [], 'added': [], 'removed': []},
                'phase5': {'modified': False, 'original_report': '', 'edited_report': ''}
            },
        }
        
        # ⭐ 删除旧的 HTML 报告，避免用户在协作模式分析完成前看到旧版本（可能是自动模式的报告）
        old_html_report = output_dir / "pathology_report.html"
        if old_html_report.exists():
            old_html_report.unlink()
            logger.info(f"[协作模式] 已删除旧的 HTML 报告，新报告将在分析完成后生成")
        
        # 存储到全局状态字典（用于 socket 事件处理器访问）
        set_agent_state(room, agent_state)
        
        # Patient Level 上下文
        if patient_level_context:
            agent_state['is_patient_level'] = True
            agent_state['part_name'] = patient_level_context.get('part_name', 'Slide 1')
            agent_state['folder_name'] = patient_level_context.get('folder_name', 'Patient')
            agent_state['wsi_index'] = patient_level_context.get('wsi_index', 1)
            agent_state['total_wsi'] = patient_level_context.get('total_wsi', 1)
            agent_state['patient_id'] = patient_level_context.get('patient_id')
        
        # ==================== Phase 1: 缩略图分析 ====================
        logger.info(f"\n{'─'*60}")
        logger.info(f"📍 Phase 1: 缩略图分析 (Orchestrator)")
        logger.info(f"{'─'*60}")
        
        socketio.emit('agent.progress', {
            'type': 'progress',
            'phase': 1,
            'percentage': 0,
            'status': 'phase_start',
            'message': _msg('phase1_start', agent_state.get('lang', 'zh'))
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 progress 消息
        
        phase1_result = execute_phase1_thumbnail(socketio, room, slide, file_id, steps_dir, filename, agent_state)
        
        if not phase1_result.get('success'):
            emit_error(socketio, room, f"Phase 1 失败: {phase1_result.get('error', '未知错误')}")
            slide.close()
            return {'success': False, 'error': 'Phase 1 failed'}
        
        # 发送 Phase 1 结果
        emit_phase_result(socketio, room, phase=1, result=phase1_result, agent_state=agent_state)
        
        # 等待用户确认
        confirmation = wait_for_phase_confirmation(socketio, room, agent_state, phase=1)
        
        if confirmation['action'] == 'stopped':
            slide.close()
            emit_done(socketio, room, file_id, agent_state, status='stopped')
            return {'success': False, 'status': 'stopped'}
        
        # 处理用户修改
        if confirmation['action'] == 'modify' and confirmation['modified_data']:
            handle_phase1_modification(agent_state, confirmation['modified_data'])
            _mod_msg = f"Updated red box selection to {len(agent_state['selected_red_rois'])} regions" if lang == 'en' else f"已更新红框选择为 {len(agent_state['selected_red_rois'])} 个区域"
            emit_orchestrator_confirmation(socketio, room, 'modify', _mod_msg, lang=lang)
        else:
            agent_state['selected_red_rois'] = agent_state['candidate_rois'].copy()
            if not confirmation.get('accepted_by_policy'):
                emit_orchestrator_confirmation(socketio, room, 'execute', lang=lang)
        
        # ==================== Phase 2: 红框分析 ====================
        logger.info(f"\n{'─'*60}")
        logger.info(f"📍 Phase 2: 红框分析 (SubAgents 并行)")
        logger.info(f"{'─'*60}")
        
        socketio.emit('agent.progress', {
            'type': 'progress',
            'phase': 2,
            'percentage': 20,
            'status': 'phase_start',
            'message': _msg('phase2_start', agent_state.get('lang', 'zh'), n=len(agent_state["selected_red_rois"]))
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 progress 消息
        
        phase2_result = execute_phase2_red_analysis(socketio, room, slide, file_id, steps_dir, agent_state)
        
        if not phase2_result.get('success'):
            emit_error(socketio, room, f"Phase 2 失败: {phase2_result.get('error', '未知错误')}")
            slide.close()
            return {'success': False, 'error': 'Phase 2 failed'}
        
        # 发送 Phase 2 结果
        emit_phase_result(socketio, room, phase=2, result=phase2_result, agent_state=agent_state)
        
        # 等待用户确认
        confirmation = wait_for_phase_confirmation(socketio, room, agent_state, phase=2)
        
        if confirmation['action'] == 'stopped':
            slide.close()
            emit_done(socketio, room, file_id, agent_state, status='stopped')
            return {'success': False, 'status': 'stopped'}
        
        # 处理用户修改
        if confirmation['action'] == 'modify' and confirmation['modified_data']:
            handle_phase2_modification(agent_state, confirmation['modified_data'])
            _mod_msg = f"Updated blue box selection to {len(agent_state['selected_blue_rois'])} regions" if lang == 'en' else f"已更新蓝框选择为 {len(agent_state['selected_blue_rois'])} 个区域"
            emit_orchestrator_confirmation(socketio, room, 'modify', _mod_msg, lang=lang)
        else:
            agent_state['selected_blue_rois'] = agent_state['blue_rois'].copy()
            if not confirmation.get('accepted_by_policy'):
                emit_orchestrator_confirmation(socketio, room, 'execute', lang=lang)
        _emit_collaborative_runtime_roi_events(socketio, room, agent_state, "red")
        
        # ==================== Phase 3: 蓝框分析 ====================
        logger.info(f"\n{'─'*60}")
        logger.info(f"📍 Phase 3: 蓝框分析 (SubAgents 并行)")
        logger.info(f"{'─'*60}")
        
        socketio.emit('agent.progress', {
            'type': 'progress',
            'phase': 3,
            'percentage': 40,
            'status': 'phase_start',
            'message': _msg('phase3_start', agent_state.get('lang', 'zh'), n=len(agent_state["selected_blue_rois"]))
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 progress 消息
        
        phase3_result = execute_phase3_blue_analysis(socketio, room, slide, file_id, steps_dir, agent_state)
        
        if not phase3_result.get('success'):
            emit_error(socketio, room, f"Phase 3 失败: {phase3_result.get('error', '未知错误')}")
            slide.close()
            return {'success': False, 'error': 'Phase 3 failed'}
        
        # 发送 Phase 3 结果
        emit_phase_result(socketio, room, phase=3, result=phase3_result, agent_state=agent_state)
        
        # 等待用户确认
        confirmation = wait_for_phase_confirmation(socketio, room, agent_state, phase=3)
        
        if confirmation['action'] == 'stopped':
            slide.close()
            emit_done(socketio, room, file_id, agent_state, status='stopped')
            return {'success': False, 'status': 'stopped'}
        
        # 处理用户修改
        if confirmation['action'] == 'modify' and confirmation['modified_data']:
            handle_phase3_modification(agent_state, confirmation['modified_data'])
            _mod_msg = f"Updated green box selection to {len(agent_state['selected_green_rois'])} regions" if lang == 'en' else f"已更新绿框选择为 {len(agent_state['selected_green_rois'])} 个区域"
            emit_orchestrator_confirmation(socketio, room, 'modify', _mod_msg, lang=lang)
        else:
            agent_state['selected_green_rois'] = agent_state['green_rois'].copy()
            if not confirmation.get('accepted_by_policy'):
                emit_orchestrator_confirmation(socketio, room, 'execute', lang=lang)
        _emit_collaborative_runtime_roi_events(socketio, room, agent_state, "blue")
        
        # ==================== Phase 4: 绿框分析 ====================
        logger.info(f"\n{'─'*60}")
        logger.info(f"📍 Phase 4: 绿框分析 (SubAgents 并行)")
        logger.info(f"{'─'*60}")
        
        socketio.emit('agent.progress', {
            'type': 'progress',
            'phase': 4,
            'percentage': 60,
            'status': 'phase_start',
            'message': _msg('phase4_start', agent_state.get('lang', 'zh'), n=len(agent_state["selected_green_rois"]))
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 progress 消息
        
        phase4_result = execute_phase4_green_analysis(socketio, room, slide, file_id, steps_dir, agent_state)
        
        if not phase4_result.get('success'):
            emit_error(socketio, room, f"Phase 4 失败: {phase4_result.get('error', '未知错误')}")
            slide.close()
            return {'success': False, 'error': 'Phase 4 failed'}

        # Complete the Orchestrator-owned secondary review before exposing the
        # Phase-4 evidence gate.  In staged collaboration the user must approve
        # the post-review evidence (including REJECTED / INDETERMINATE claims),
        # not the superseded first-pass Green observations.
        _orchestrate_secondary_reviews(agent_state, slide, steps_dir)
        reviewed_green = agent_state.get('green_analyses', []) or []
        _emit_collaborative_runtime_roi_events(socketio, room, agent_state, "green")
        phase4_result['subagent_results'] = reviewed_green
        if reviewed_green:
            if lang == 'en':
                phase4_result['reasoning'] = (
                    f"Completed cellular-level analysis of {len(reviewed_green)} green ROIs "
                    "and applied the Orchestrator secondary-review decisions.\n\n"
                    + "\n".join(
                        f"- {item.get('roi_id', '?')}: "
                        f"{item.get('reasoning') or item.get('findings') or 'No finding recorded'}"
                        for item in reviewed_green
                    )
                )
            else:
                phase4_result['reasoning'] = (
                    f"完成了 {len(reviewed_green)} 个绿框区域的细胞级分析，并已纳入 Orchestrator 二次复核裁决。\n\n"
                    + "\n".join(
                        f"- {item.get('roi_id', '?')}: "
                        f"{item.get('reasoning') or item.get('findings') or '无记录'}"
                        for item in reviewed_green
                    )
                )
        phase4_result.setdefault('metadata', {})['review_queue_count'] = 0

        # 发送 Phase 4 结果
        emit_phase_result(socketio, room, phase=4, result=phase4_result, agent_state=agent_state)
        
        # 等待用户确认
        confirmation = wait_for_phase_confirmation(socketio, room, agent_state, phase=4)
        
        if confirmation['action'] == 'stopped':
            slide.close()
            emit_done(socketio, room, file_id, agent_state, status='stopped')
            return {'success': False, 'status': 'stopped'}
        
        if not confirmation.get('accepted_by_policy'):
            emit_orchestrator_confirmation(socketio, room, confirmation['action'], lang=lang)
        
        # ==================== Phase 5: 汇总报告 ====================
        logger.info(f"\n{'─'*60}")
        logger.info(f"📍 Phase 5: 诊断报告汇总 (Orchestrator)")
        logger.info(f"{'─'*60}")
        
        socketio.emit('agent.progress', {
            'type': 'progress',
            'phase': 5,
            'percentage': 80,
            'status': 'phase_start',
            'message': _msg('phase5_start', agent_state.get('lang', 'zh'))
        }, to=room)
        socketio.sleep(0)  # ⭐ 强制发送 progress 消息
        
        # ⭐⭐⭐ 协作模式专用：在生成报告前构建 tree_nodes ⭐⭐⭐
        build_collaborative_tree_nodes(agent_state)
        
        # ⭐⭐⭐ 使用协作模式专用的报告生成函数（包含用户修改痕迹）⭐⭐⭐
        phase5_result = execute_phase5_report_collaborative(socketio, room, slide, file_id, output_dir, agent_state)

        # The review list is report-grounded: only after the Orchestrator
        # writes the draft do we know which ROI paths actually decided it.
        review_queue = _build_collaborative_review_queue(
            agent_state, report_text=phase5_result.get('report') or ''
        )
        phase5_result.setdefault('metadata', {})['review_queue_count'] = len(review_queue)
        snapshot_lock = agent_state.get('collaboration_snapshot_lock')
        if snapshot_lock:
            snapshot_lock.acquire()
        try:
            agent_state['review_queue'] = review_queue
        finally:
            if snapshot_lock:
                snapshot_lock.release()
        socketio.emit('agent.review_queue', {
            'review_policy': review_policy,
            'items': review_queue,
            'count': len(review_queue),
            'published': True,
            'review_queue_published': True,
            'grounding': 'draft_report',
            'file_id': file_id,
            'runtime': {'run_id': str(runtime_run_id or '')},
        }, to=room)
        socketio.sleep(0)
        
        # 发送 Phase 5 结果
        emit_phase_result(socketio, room, phase=5, result=phase5_result, agent_state=agent_state)
        
        # 等待用户确认报告
        confirmation = wait_for_phase_confirmation(socketio, room, agent_state, phase=5)
        
        if confirmation['action'] == 'stopped':
            slide.close()
            emit_done(socketio, room, file_id, agent_state, status='stopped')
            return {'success': False, 'status': 'stopped'}
        
        # 处理报告编辑 / HAFL 干预。execute 也可以携带 AI 修订后的正文。
        user_edited_report = False
        modified_data = confirmation.get('modified_data') or {}
        original_report = phase5_result.get('report', '')
        edited_report = (
            modified_data.get('edited_report')
            or modified_data.get('revised_report')
            or modified_data.get('report')
            or ''
        )
        intervention_events = modified_data.get('intervention_events') or []
        if isinstance(intervention_events, dict):
            intervention_events = [intervention_events]
        if not isinstance(intervention_events, list):
            intervention_events = []
        if edited_report and str(edited_report).strip() != str(original_report or '').strip():
            save_edited_report(output_dir, edited_report)
            user_edited_report = True
            logger.info(f"[Phase 5] 用户定稿了修订报告，长度: {len(edited_report)} 字符")
            agent_state.setdefault('user_modifications', {})['phase5'] = {
                'modified': True,
                'original_report': original_report,
                'edited_report': edited_report,
                'intervention_events': intervention_events,
            }
            _saved_msg = "Report saved" if lang == 'en' else "已保存您编辑后的报告"
            emit_orchestrator_confirmation(socketio, room, 'modify', _saved_msg, lang=lang)
        elif intervention_events:
            agent_state.setdefault('user_modifications', {})['phase5'] = {
                'modified': False,
                'original_report': original_report,
                'edited_report': original_report,
                'intervention_events': intervention_events,
            }
        
        # ==================== 完成 ====================
        slide.close()
        
        # ⭐⭐⭐ 清理父ROI图像缓存，防止多切片间 ROI ID 重名导致图像串用 ⭐⭐⭐
        clear_roi_cache()
        
        # ⭐⭐⭐ Patient Level 协作模式：不 emit done，不清理状态，生成 WSI 汇总 ⭐⭐⭐
        if patient_level_context:
            logger.info(f"\n{'='*80}")
            logger.info(f"✅ [Patient Level 协作模式] 单切片五阶段完成: {agent_state.get('part_name', 'Slide')}")
            logger.info(f"{'='*80}\n")
            
            # ⭐ 生成 pathology_report.html（Patient Level 子报告）
            try:
                if user_edited_report:
                    final_report = generate_html_visual_report_with_user_report(
                        agent_state, file_id, output_dir, edited_report, socketio, room
                    )
                else:
                    existing_report = phase5_result.get('report', '')
                    if existing_report and len(existing_report) > 50:
                        final_report = generate_html_visual_report_with_user_report(
                            agent_state, file_id, output_dir, existing_report, socketio, room,
                            user_edited=False
                        )
                    else:
                        _pl_title = "Single Slide Pathology Report" if agent_state.get('lang', 'zh') == 'en' else "单张切片病理诊断报告"
                        final_report = generate_final_pathology_report(
                            agent_state, file_id, output_dir, socketio, room,
                            report_title=_pl_title,
                            is_patient_level=True
                        )
                agent_state['final_report'] = final_report
                logger.info(f"[Patient Level 协作模式] WSI HTML 报告已生成")
            except Exception as e:
                logger.error(f"[Patient Level 协作模式] WSI HTML 报告生成失败: {e}")
            
            # ⭐ 从 generate_final_pathology_report 的输出中提取 wsi_summary（版本A格式，无需额外 LLM 调用）
            wsi_summary = ''
            if final_report and isinstance(final_report, dict):
                wsi_summary = final_report.get('report_text', '')
            if not wsi_summary:
                wsi_summary = phase5_result.get('report', '') if phase5_result else ''
            logger.info(f"[Patient Level 协作模式] wsi_summary 已提取: {len(wsi_summary)} 字符")
            
            agent_state['_wsi_summary_phase5'] = wsi_summary

            # ⭐ 生成 analysis_tree.json
            try:
                path_report_result = generate_standardized_path_report(
                    agent_state, file_id, output_dir, socketio, room
                )
                if path_report_result and path_report_result.get('success'):
                    agent_state['tree_json'] = path_report_result.get('tree_json')
                    logger.info(f"[Patient Level 协作模式] WSI analysis_tree.json 已生成")
            except Exception as e:
                logger.warning(f"[Patient Level 协作模式] 路径报告生成失败: {e}")
            
            # ⭐ 统计信息
            tree_nodes = agent_state.get('tree_nodes', [])
            red_count = len(tree_nodes)
            blue_count = sum(len(red.get('children', [])) for red in tree_nodes)
            green_count = sum(
                len(blue.get('children', []))
                for red in tree_nodes
                for blue in red.get('children', [])
            )
            
            socketio.emit('agent.progress', {
                'type': 'progress',
                'phase': 5,
                'percentage': 100,
                'status': 'completed',
                'message': _msg('collab_slide_done', agent_state.get('lang', 'zh'), part=agent_state.get('part_name', 'Slide'))
            }, to=room)
            
            # ⭐ 不 emit done，不 clear state —— 由 run_patient_level_agent 统一管理
            
            return {
                'success': True,
                'pathology_findings': agent_state.get('pathology_findings', []),
                'tree_nodes': tree_nodes,
                'all_overlays': agent_state.get('all_overlays', []),
                'wsi_summary': wsi_summary,
                'report': phase5_result.get('report', ''),
                'output_paths': agent_state.get('output_paths', {}),
                'statistics': {
                    'red_frames': red_count,
                    'blue_frames': blue_count,
                    'green_frames': green_count
                }
            }
        
        # ⭐⭐⭐ 非 Patient Level：原有单切片协作模式逻辑（保持不变）⭐⭐⭐
        # 生成 pathology_report.html
        try:
            if user_edited_report:
                logger.info(f"[协作模式] 用户已编辑报告，跳过 AI 生成，直接使用用户版本...")
                final_report = generate_html_visual_report_with_user_report(
                    agent_state, file_id, output_dir, edited_report, socketio, room
                )
            else:
                existing_report = phase5_result.get('report', '')
                if existing_report and len(existing_report) > 50:
                    logger.info(f"[协作模式] 用户直接确认，复用已有报告 (长度: {len(existing_report)})，跳过重复 AI 调用")
                    final_report = generate_html_visual_report_with_user_report(
                        agent_state, file_id, output_dir, existing_report, socketio, room,
                        user_edited=False
                    )
                else:
                    logger.warning(f"[协作模式] Phase 5 报告为空，需重新生成...")
                    final_report = generate_final_pathology_report(agent_state, file_id, output_dir, socketio, room)
            
            agent_state['final_report'] = final_report
            logger.info(f"[协作模式] pathology_report.html 生成完成")
        except Exception as e:
            logger.error(f"[协作模式] 生成 pathology_report.html 失败: {e}")
        
        socketio.emit('agent.progress', {
            'type': 'progress',
            'phase': 5,
            'percentage': 100,
            'status': 'completed',
            'message': _msg('analysis_done', agent_state.get('lang', 'zh'))
        }, to=room)
        
        # 发送完成事件
        emit_done(socketio, room, file_id, agent_state, status='completed')
        
        logger.info(f"\n{'='*80}")
        logger.info(f"✅ 五阶段协作模式完成")
        logger.info(f"{'='*80}\n")
        
        # 清理全局状态
        clear_agent_state(room)
        
        return {
            'success': True,
            'pathology_findings': agent_state.get('pathology_findings', []),
            'report': phase5_result.get('report', '')
        }
        
    except Exception as e:
        logger.error(f"五阶段协作模式失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
        try:
            slide.close()
        except:
            pass
        
        emit_error(socketio, room, f"协作模式执行失败: {str(e)}")
        clear_agent_state(room)
        
        return {'success': False, 'error': str(e)}


def handle_phase1_modification(agent_state: dict, modified_data: dict):
    """处理 Phase 1 用户修改（红框选择）"""
    modified_regions = modified_data.get('regions', [])
    _is_en = agent_state.get('lang', 'zh') == 'en'
    
    ai_selection = [roi.get('id') for roi in agent_state.get('candidate_rois', [])]
    
    selected_rois = []
    for region in modified_regions:
        roi = {
            'id': region.get('roi_id', region.get('id', f"R{len(selected_rois)+1}")),
            'rect': region.get('rect', {}),
            'reason': region.get('reason', 'User selection' if _is_en else '用户选择'),
            'priority': region.get('priority', 5),
            'source': 'user' if region.get('is_user_added') else 'ai',  # ⭐ 标记来源
        }
        selected_rois.append(roi)
    
    agent_state['selected_red_rois'] = selected_rois
    
    # ⭐ 记录用户修改历史
    user_selection = [roi['id'] for roi in selected_rois]
    added = [id for id in user_selection if id not in ai_selection]
    removed = [id for id in ai_selection if id not in user_selection]
    
    agent_state['user_modifications']['phase1'] = {
        'modified': True,
        'ai_selection': ai_selection,
        'user_selection': user_selection,
        'added': added,
        'removed': removed
    }
    
    # ⭐⭐⭐ 同步更新 all_overlays：保留蓝框和绿框，重建红框列表 ⭐⭐⭐
    # 1. 保留所有非红框的 overlay（蓝框和绿框）
    new_all_overlays = [
        overlay for overlay in agent_state.get('all_overlays', [])
        if overlay.get('level') != 'red'
    ]
    
    # 2. 为用户最终选择的所有红框添加 overlay（包括新增的）
    for roi in selected_rois:
        # ⭐ 确保 zoom_level 设置正确（Phase 2 会使用）
        roi['zoom_level'] = 10
        new_all_overlays.append({
            'id': roi['id'],
            'rect': roi['rect'],
            'color': '#FF0000',
            'magnification': 10,
            'label': '',
            'level': 'red'
        })
        logger.info(f"  ✅ 添加红框 {roi['id']} 到 overlays, rect={roi['rect']}")
    
    agent_state['all_overlays'] = new_all_overlays
    logger.info(f"📝 [Phase 1] 用户修改了红框选择: {len(selected_rois)} 个区域 (新增: {added}, 删除: {removed}), overlays 总数: {len(new_all_overlays)}")


def handle_phase2_modification(agent_state: dict, modified_data: dict):
    """处理 Phase 2 用户修改（蓝框选择）"""
    modified_regions = modified_data.get('regions', [])
    _is_en = agent_state.get('lang', 'zh') == 'en'
    
    logger.info(f"📥 [Phase 2] 收到修改数据: {len(modified_regions)} 个区域")
    for idx, region in enumerate(modified_regions):
        logger.info(f"  [{idx}] roi_id={region.get('roi_id')}, rect={region.get('rect')}, parent_id={region.get('parent_id')}, is_user_added={region.get('is_user_added')}")
    
    ai_selection = [roi.get('id') for roi in agent_state.get('blue_rois', [])]
    
    selected_rois = []
    for region in modified_regions:
        roi = {
            'id': region.get('roi_id', region.get('id', f"B{len(selected_rois)+1}")),
            'rect': region.get('rect', {}),
            'parent_id': region.get('parent_id', ''),
            'reason': region.get('reason', 'User selection' if _is_en else '用户选择'),
            'priority': region.get('priority', 5),
            'source': 'user' if region.get('is_user_added') else 'ai',  # ⭐ 标记来源
        }
        selected_rois.append(roi)
    
    agent_state['selected_blue_rois'] = selected_rois
    
    # ⭐ 记录用户修改历史
    user_selection = [roi['id'] for roi in selected_rois]
    added = [id for id in user_selection if id not in ai_selection]
    removed = [id for id in ai_selection if id not in user_selection]
    
    agent_state['user_modifications']['phase2'] = {
        'modified': True,
        'ai_selection': ai_selection,
        'user_selection': user_selection,
        'added': added,
        'removed': removed
    }
    
    # ⭐⭐⭐ 同步更新 all_overlays：保留红框和绿框，重建蓝框列表 ⭐⭐⭐
    # 1. 保留所有非蓝框的 overlay（红框和绿框）
    new_all_overlays = [
        overlay for overlay in agent_state.get('all_overlays', [])
        if overlay.get('level') != 'blue'
    ]
    
    # 2. 为用户最终选择的所有蓝框添加 overlay（包括新增的）
    for roi in selected_rois:
        # ⭐ 确保 zoom_level 设置正确（Phase 3 会使用）
        roi['zoom_level'] = 40
        new_all_overlays.append({
            'id': roi['id'],
            'rect': roi['rect'],
            'color': '#3B82F6',
            'magnification': 40,
            'label': '',
            'level': 'blue',
            'parent_id': roi.get('parent_id', '')
        })
        logger.info(f"  ✅ 添加蓝框 {roi['id']} 到 overlays, rect={roi['rect']}")
    
    agent_state['all_overlays'] = new_all_overlays
    logger.info(f"📝 [Phase 2] 用户修改了蓝框选择: {len(selected_rois)} 个区域 (新增: {added}, 删除: {removed}), overlays 总数: {len(new_all_overlays)}")


def handle_phase3_modification(agent_state: dict, modified_data: dict):
    """处理 Phase 3 用户修改（绿框选择）"""
    modified_regions = modified_data.get('regions', [])
    _is_en = agent_state.get('lang', 'zh') == 'en'
    
    ai_selection = [roi.get('id') for roi in agent_state.get('green_rois', [])]
    
    selected_rois = []
    for region in modified_regions:
        roi = {
            'id': region.get('roi_id', region.get('id', f"G{len(selected_rois)+1}")),
            'rect': region.get('rect', {}),
            'parent_id': region.get('parent_id', ''),
            'reason': region.get('reason', 'User selection' if _is_en else '用户选择'),
            'priority': region.get('priority', 5),
            'source': 'user' if region.get('is_user_added') else 'ai',  # ⭐ 标记来源
        }
        selected_rois.append(roi)
    
    agent_state['selected_green_rois'] = selected_rois
    
    # ⭐ 记录用户修改历史
    user_selection = [roi['id'] for roi in selected_rois]
    added = [id for id in user_selection if id not in ai_selection]
    removed = [id for id in ai_selection if id not in user_selection]
    
    agent_state['user_modifications']['phase3'] = {
        'modified': True,
        'ai_selection': ai_selection,
        'user_selection': user_selection,
        'added': added,
        'removed': removed
    }
    
    # ⭐⭐⭐ 同步更新 all_overlays：保留红框和蓝框，重建绿框列表 ⭐⭐⭐
    # 1. 保留所有非绿框的 overlay（红框和蓝框）
    new_all_overlays = [
        overlay for overlay in agent_state.get('all_overlays', [])
        if overlay.get('level') != 'green'
    ]
    
    # 2. 为用户最终选择的所有绿框添加 overlay（包括新增的）
    for roi in selected_rois:
        # ⭐ 确保 zoom_level 设置正确（Phase 4 会使用）
        roi['zoom_level'] = 160
        new_all_overlays.append({
            'id': roi['id'],
            'rect': roi['rect'],
            'color': '#22C55E',
            'magnification': 160,
            'label': '',
            'level': 'green',
            'parent_id': roi.get('parent_id', '')
        })
        logger.info(f"  ✅ 添加绿框 {roi['id']} 到 overlays, rect={roi['rect']}")
    
    agent_state['all_overlays'] = new_all_overlays
    logger.info(f"📝 [Phase 3] 用户修改了绿框选择: {len(selected_rois)} 个区域 (新增: {added}, 删除: {removed}), overlays 总数: {len(new_all_overlays)}")


def save_edited_report(output_dir: Path, report_content: str):
    """保存用户编辑后的报告"""
    report_path = output_dir / "report_edited.md"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_content)
    logger.info(f"📝 已保存用户编辑的报告: {report_path}")


_ROI_CITE_RE = re.compile(r'\bR\d+(?:-B\d+)?(?:-G\d+)?\b')


def _cited_roi_ids_from_report(report_text: str) -> list:
    """Return ROI paths cited in an Orchestrator draft, in first-seen order."""
    seen = []
    for match in _ROI_CITE_RE.findall(str(report_text or '')):
        if match not in seen:
            seen.append(match)
    return seen


def _analysis_review_reasons(analysis: dict, level: str) -> list:
    """Uncertainty signals that annotate a ROI, never the sole queue filter."""
    reasons = []
    if analysis.get('success') is False or analysis.get('error'):
        reasons.append('analysis_failed')
    evidence = analysis.get('evidence') if isinstance(analysis.get('evidence'), dict) else {}
    confidence = analysis.get(
        'confidence_level',
        analysis.get('evidence_confidence', evidence.get('confidence'))
    )
    if confidence is None and isinstance(analysis.get('confidence'), (int, float)):
        raw_confidence = float(analysis['confidence'])
        confidence = round(raw_confidence / 25) if raw_confidence > 4 else round(raw_confidence)
    try:
        confidence = int(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    pathology_type = str(
        analysis.get('pathology_type')
        or evidence.get('pathology_type')
        or ''
    ).strip().lower()
    if confidence is not None and confidence <= 2:
        reasons.append('low_confidence')
    if (
        pathology_type in {'uncertain', 'indeterminate', '无法确定', '不确定'}
        and (confidence is None or confidence <= 2)
    ):
        reasons.append('uncertain_classification')
    if level == 'green':
        secondary_review = analysis.get('secondary_review')
        if not isinstance(secondary_review, dict):
            secondary_review = {}
        verdict = str(
            analysis.get('secondary_review_verdict')
            or secondary_review.get('verdict', '')
        ).strip().upper()
        if verdict == 'INDETERMINATE':
            reasons.append(f'secondary_review_{verdict.lower()}')
    return reasons, confidence


def _roi_ids_grounding_report(agent_state: dict, report_text: str) -> list:
    """ROI paths that actually ground the draft; citations first, else greens."""
    cited = _cited_roi_ids_from_report(report_text)
    if cited:
        return cited
    fallback = []
    for key in ('green_analyses', 'blue_analyses', 'red_analyses'):
        for analysis in agent_state.get(key) or []:
            roi_id = str(analysis.get('roi_id') or analysis.get('id') or '').strip()
            if roi_id and roi_id not in fallback:
                fallback.append(roi_id)
        if fallback:
            break
    return fallback


def _build_collaborative_review_queue(agent_state: dict, report_text=None) -> list:
    """Return ROI items a pathologist should inspect against the draft report.

    When ``report_text`` is provided, the queue is the ROI paths that ground
    that draft (cited IDs, or green evidence if the draft omitted citations).
    Low confidence is an annotation on those items, not the admission rule.
    Without a report, keep the legacy uncertainty-only list for unit tests.
    """
    roi_lookup = {}
    analysis_lookup = {}
    for key in ('selected_red_rois', 'selected_blue_rois', 'selected_green_rois'):
        for roi in agent_state.get(key, []) or []:
            roi_id = str(roi.get('id') or roi.get('roi_id') or '')
            if roi_id:
                roi_lookup[roi_id] = roi
    for level, key in (
        ('red', 'red_analyses'), ('blue', 'blue_analyses'), ('green', 'green_analyses')
    ):
        for analysis in agent_state.get(key, []) or []:
            roi_id = str(analysis.get('roi_id') or analysis.get('id') or '')
            if roi_id:
                analysis_lookup[roi_id] = (level, analysis)

    grounded_ids = None
    if report_text is not None:
        grounded_ids = _roi_ids_grounding_report(agent_state, report_text)

    queue = []
    if grounded_ids is not None:
        for roi_id in grounded_ids:
            level, analysis = analysis_lookup.get(roi_id, ('green', {}))
            extra, confidence = _analysis_review_reasons(analysis, level) if analysis else ([], None)
            roi = roi_lookup.get(roi_id, {})
            queue.append({
                'roi_id': roi_id,
                'level': level,
                'rect': roi.get('rect') or analysis.get('rect') or {},
                'reason_codes': sorted(set(['decisive_for_report', *extra])),
                'confidence': confidence,
                'summary': str(
                    analysis.get('findings') or analysis.get('reasoning') or analysis.get('error') or ''
                )[:600],
            })
        return queue

    for roi_id, (level, analysis) in analysis_lookup.items():
        extra, confidence = _analysis_review_reasons(analysis, level)
        if not extra:
            continue
        roi = roi_lookup.get(roi_id, {})
        queue.append({
            'roi_id': roi_id,
            'level': level,
            'rect': roi.get('rect') or analysis.get('rect') or {},
            'reason_codes': sorted(set(extra)),
            'confidence': confidence,
            'summary': str(
                analysis.get('findings') or analysis.get('reasoning') or analysis.get('error') or ''
            )[:600],
        })
    return queue


def build_collaborative_tree_nodes(agent_state: dict):
    """
    协作模式专用：构建 tree_nodes 数据结构（红框 → 蓝框 → 绿框层级关系）
    用于前端路径树可视化和 pathology_report.html 生成
    """
    try:
        tree_nodes = []
        
        # 获取各层级数据
        red_analyses = agent_state.get('red_analyses', [])
        blue_analyses = agent_state.get('blue_analyses', [])
        green_analyses = agent_state.get('green_analyses', [])
        selected_red_rois = agent_state.get('selected_red_rois', [])
        selected_blue_rois = agent_state.get('selected_blue_rois', [])
        selected_green_rois = agent_state.get('selected_green_rois', [])
        user_modifications = agent_state.get('user_modifications', {})
        review_by_id = {
            item.get('roi_id'): item
            for item in agent_state.get('review_queue', []) or []
            if item.get('roi_id')
        }
        wsi_name = agent_state.get('filename', 'unknown')
        sample_name = Path(wsi_name).stem if wsi_name else 'unknown'
        
        # ⭐ 调试日志
        logger.info(f"[build_collaborative_tree_nodes] 开始构建 tree_nodes")
        logger.info(f"  - selected_red_rois: {len(selected_red_rois)} 个")
        logger.info(f"  - selected_blue_rois: {len(selected_blue_rois)} 个")
        logger.info(f"  - selected_green_rois: {len(selected_green_rois)} 个")
        logger.info(f"  - red_analyses: {len(red_analyses)} 个")
        logger.info(f"  - blue_analyses: {len(blue_analyses)} 个")
        logger.info(f"  - green_analyses: {len(green_analyses)} 个")
        
        # ⭐ 辅助函数：从 debug_images 目录查找图片
        _tree_file_id = str(agent_state.get('file_id') or '').strip()

        def find_image_path(roi_id, parent_id=None):
            """查找 ROI 对应的图片路径（file_id 键优先，文件名键兜底）
            
            ⭐ 重要：必须精确匹配，避免 R13 的通配符匹配到 R13-B4 的蓝框图片
            例如 *R13*.jpg 会错误匹配 debug_gpt_R13-B8_16candidates_xxx.jpg
            """
            # Pattern 1: 标准命名 debug_gpt_{roi_id}_{N}candidates_{ts}.jpg
            patterns = [
                f"debug_gpt_{roi_id}_*candidates_*.jpg",
            ]
            
            # Pattern 2: 带父级前缀（如 debug_gpt_{parent}_{roi_id}_*.jpg）
            if parent_id:
                patterns.append(f"debug_gpt_{parent_id}_{roi_id}_*.jpg")
            
            # ⭐ 注意：不再使用 *{roi_id}*.jpg 这种宽泛匹配！
            # 因为 *R13*.jpg 会匹配到 R13-B4, R13-B8 等蓝框图片，导致报告图片错误
            
            for pattern in patterns:
                found = _find_debug_image(pattern, file_id=_tree_file_id, sample_name=sample_name)
                if found:
                    return found
            return None
        
        # 构建红框节点
        for red_roi in selected_red_rois:
            red_id = red_roi.get('id', red_roi.get('roi_id', '?'))
            
            # 查找对应的分析结果（支持多种 ID 格式匹配）
            red_analysis = next((a for a in red_analyses if a.get('roi_id') == red_id), None)
            if not red_analysis:
                # 尝试其他匹配方式
                red_analysis = next((a for a in red_analyses if str(a.get('roi_id', '')).upper() == str(red_id).upper()), {})
            else:
                red_analysis = red_analysis or {}
            
            # ⭐ 查找红框对应的图片（通常是蓝框网格图）
            red_image_path = find_image_path(red_id)
            
            red_node = {
                'id': red_id,
                'roi_id': red_id,
                'level': 'red',
                'rect': red_roi.get('rect', {}),
                'source': red_roi.get('source', 'ai'),
                'image_path': red_image_path,  # ⭐ 添加 image_path
                'analysis': {
                    'reasoning': red_analysis.get('reasoning', ''),
                    'findings': red_analysis.get('findings', '') or red_analysis.get('reasoning', ''),
                    'plan': red_analysis.get('plan', []),
                    'pathology_type': red_analysis.get('evidence', {}).get('pathology_type', 'unclassified') if isinstance(red_analysis.get('evidence'), dict) else 'unclassified',
                    'confidence': red_analysis.get('confidence', 50),
                    'key_features': [],
                    'image_path': red_image_path
                },
                'children': []
            }
            if red_id in review_by_id:
                red_node['review_required'] = True
                red_node['review_reason_codes'] = review_by_id[red_id].get('reason_codes', [])
            
            # 找到属于这个红框的蓝框
            for blue_roi in selected_blue_rois:
                blue_id = blue_roi.get('id', blue_roi.get('roi_id', '?'))
                blue_parent = blue_roi.get('parent_id', '')
                
                # 判断蓝框是否属于当前红框（通过 parent_id 或 ID 前缀）
                if blue_parent == red_id or blue_id.startswith(f"{red_id}-"):
                    blue_analysis = next((a for a in blue_analyses if a.get('roi_id') == blue_id), {})
                    
                    # ⭐ 查找蓝框对应的图片（通常是绿框网格图）
                    blue_image_path = find_image_path(blue_id, red_id)
                    
                    blue_node = {
                        'id': blue_id,
                        'roi_id': blue_id,
                        'level': 'blue',
                        'rect': blue_roi.get('rect', {}),
                        'parent_id': red_id,
                        'source': blue_roi.get('source', 'ai'),
                        'image_path': blue_image_path,  # ⭐ 添加 image_path
                        'analysis': {
                            'reasoning': blue_analysis.get('reasoning', ''),
                            'findings': blue_analysis.get('findings', '') or blue_analysis.get('reasoning', ''),
                            'plan': blue_analysis.get('plan', []),
                            'pathology_type': blue_analysis.get('evidence', {}).get('pathology_type', 'unclassified') if isinstance(blue_analysis.get('evidence'), dict) else 'unclassified',
                            'confidence': blue_analysis.get('confidence', 50),
                            'key_features': [],
                            'image_path': blue_image_path
                        },
                        'children': []
                    }
                    if blue_id in review_by_id:
                        blue_node['review_required'] = True
                        blue_node['review_reason_codes'] = review_by_id[blue_id].get('reason_codes', [])
                    
                    # 找到属于这个蓝框的绿框
                    for green_roi in selected_green_rois:
                        green_id = green_roi.get('id', green_roi.get('roi_id', '?'))
                        green_parent = green_roi.get('parent_id', '')
                        
                        if green_parent == blue_id or green_id.startswith(f"{blue_id}-"):
                            green_analysis = next((a for a in green_analyses if a.get('roi_id') == green_id), {})
                            
                            # ⭐ 查找绿框对应的图片（协作模式格式：steps/green_{roi_id}.jpg）
                            green_image_path = green_analysis.get('image_path') or green_roi.get('image_path')
                            
                            # ⭐⭐⭐ 如果没有 image_path，根据协作模式命名规则构造 ⭐⭐⭐
                            if not green_image_path:
                                output_paths = agent_state.get('output_paths', {})
                                steps_dir = output_paths.get('steps_dir', Path(f"outputs/{agent_state.get('file_id', '')}/steps"))
                                green_image_path = f"{steps_dir}/green_{green_id}.jpg"
                            
                            green_node = {
                                'id': green_id,
                                'roi_id': green_id,
                                'level': 'green',
                                'rect': green_roi.get('rect', {}),
                                'parent_id': blue_id,
                                'source': green_roi.get('source', 'ai'),
                                'image_path': green_image_path,  # ⭐ 添加 image_path
                                'analysis': {
                                    'reasoning': green_analysis.get('reasoning', ''),
                                    'findings': green_analysis.get('findings', '') or green_analysis.get('reasoning', ''),
                                    'evidence': green_analysis.get('evidence', {}),
                                    'evidence_confidence': green_analysis.get('evidence_confidence'),
                                    'secondary_review': green_analysis.get('secondary_review'),
                                    'secondary_review_verdict': green_analysis.get('secondary_review_verdict'),
                                    'diagnosis_suggestion': green_analysis.get('diagnosis_suggestion', ''),
                                    'confidence': green_analysis.get('confidence', 0),
                                    'pathology_type': green_analysis.get('evidence', {}).get('pathology_type', 'unclassified') if isinstance(green_analysis.get('evidence'), dict) else 'unclassified',
                                    'key_features': [green_analysis.get('evidence', {}).get('key_findings', '')] if isinstance(green_analysis.get('evidence'), dict) and green_analysis.get('evidence', {}).get('key_findings') else [],
                                    'image_path': green_image_path
                                },
                                'children': []
                            }
                            if green_id in review_by_id:
                                green_node['review_required'] = True
                                green_node['review_reason_codes'] = review_by_id[green_id].get('reason_codes', [])
                            blue_node['children'].append(green_node)
                    
                    red_node['children'].append(blue_node)
            
            tree_nodes.append(red_node)
            logger.info(f"  - 红框 {red_id}: {len(red_node['children'])} 个蓝框子节点")
        
        agent_state['tree_nodes'] = tree_nodes
        
        # 统计
        total_blue = sum(len(node.get('children', [])) for node in tree_nodes)
        total_green = sum(
            len(blue.get('children', [])) 
            for red in tree_nodes 
            for blue in red.get('children', [])
        )
        
        logger.info(f"📊 [协作模式] tree_nodes 已构建: {len(tree_nodes)}个红框, {total_blue}个蓝框, {total_green}个绿框")
        
        # ⭐⭐⭐ 保存 analysis_tree.json 文件（供前端树可视化使用）⭐⭐⭐
        try:
            file_id = agent_state.get('file_id', '')
            output_paths = agent_state.get('output_paths', {})
            output_dir = output_paths.get('output_dir', Path(f"outputs/{file_id}"))
            if not isinstance(output_dir, Path):
                output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # 构建与自动模式兼容的 analysis_tree.json 结构
            filename = agent_state.get('filename', 'unknown')
            
            analysis_tree_data = {
                'metadata': {
                    'filename': filename,
                    'file_id': file_id,
                    'mode': 'collaborative',
                    'analysis_time': datetime.now().isoformat(),
                    'statistics': {
                        'total_red': len(tree_nodes),
                        'total_blue': total_blue,
                        'total_green': total_green
                    },
                    'global_analysis': agent_state.get('global_analysis', ''),
                    'thumbnail_url': agent_state.get('global_thumbnail_path', ''),
                    'secondary_review_plan': agent_state.get('secondary_review_plan', []),
                    'secondary_review_results': agent_state.get('secondary_review_results', []),
                    'review_queue': agent_state.get('review_queue', [])
                },
                'tree_nodes': tree_nodes
            }
            
            tree_json_path = output_dir / "analysis_tree.json"
            with open(tree_json_path, 'w', encoding='utf-8') as f:
                json.dump(analysis_tree_data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"✅ [协作模式] analysis_tree.json 已保存: {tree_json_path}")
            
        except Exception as save_error:
            logger.error(f"保存 analysis_tree.json 失败: {save_error}")
        
    except Exception as e:
        logger.error(f"构建 tree_nodes 失败: {e}")
        import traceback
        logger.error(traceback.format_exc())


def _reportable_findings_for_review(agent_state: dict, report_text: str | None = None) -> list:
    """Public ROI findings a pathologist can quote or exclude at report sign-off."""
    allowed = None
    if report_text is not None:
        allowed = set(_roi_ids_grounding_report(agent_state, report_text))
    items = []
    for level, key in (
        ('red', 'red_analyses'),
        ('blue', 'blue_analyses'),
        ('green', 'green_analyses'),
    ):
        for analysis in agent_state.get(key) or []:
            roi_id = str(analysis.get('roi_id') or analysis.get('id') or '').strip()
            if not roi_id:
                continue
            if allowed is not None and roi_id not in allowed:
                continue
            summary = str(
                analysis.get('findings') or analysis.get('reasoning') or ''
            ).strip()
            if not summary:
                continue
            items.append({
                'roi_id': roi_id,
                'level': level,
                'summary': summary[:500],
            })
    return items


def execute_phase5_report_collaborative(
    socketio,
    room,
    slide,
    file_id: str,
    output_dir: Path,
    agent_state: dict,
) -> dict:
    """Generate a collaborative report with the shared organ-agnostic contract."""
    try:
        logger.info("[Phase 5] 协作模式：开始生成通用诊断报告")
        lang = _normalize_output_language(agent_state.get('lang', 'zh'))
        is_en = lang == 'en'
        red_analyses = agent_state.get('red_analyses', [])
        blue_analyses = agent_state.get('blue_analyses', [])
        green_analyses = agent_state.get('green_analyses', [])
        user_modifications = agent_state.get('user_modifications', {}) or {}
        user_analysis_edits = agent_state.get('user_analysis_edits', {}) or {}

        sections = []
        global_analysis = agent_state.get('global_analysis', '')
        if isinstance(global_analysis, dict):
            global_analysis = (
                global_analysis.get('reasoning', '')
                or global_analysis.get('analysis', '')
            )
        elif isinstance(global_analysis, list):
            global_analysis = '\n'.join(str(item) for item in global_analysis)
        if global_analysis:
            heading = "Whole-slide overview" if is_en else "全切片缩略图观察"
            sections.append(f"## {heading}\n{global_analysis}")

        level_specs = (
            ("10×", red_analyses),
            ("40×", blue_analyses),
            ("160×", green_analyses),
        )
        for level, analyses in level_specs:
            rows = []
            for item in analyses:
                if not item.get('success'):
                    continue
                roi_id = item.get('roi_id', '?')
                text_value = item.get('reasoning') or item.get('findings', '')
                edited = item.get('user_edited') or roi_id in user_analysis_edits
                edited_tag = " [human edited]" if is_en else " [人工编辑]"
                rows.append(f"- {roi_id}{edited_tag if edited else ''}: {text_value}")
            if rows:
                heading = f"{level} ROI evidence" if is_en else f"{level} ROI 证据"
                sections.append(f"## {heading}\n" + "\n".join(rows))

        pathology_findings = [
            str(item)
            for item in agent_state.get('pathology_findings', [])
            if item
        ]
        if pathology_findings:
            heading = "Recorded findings" if is_en else "已记录发现"
            sections.append(
                f"## {heading}\n"
                + "\n".join(f"- {item}" for item in pathology_findings)
            )

        if 'secondary_review_results' in agent_state:
            heading = (
                "Orchestrator secondary-review tool record"
                if is_en
                else "Orchestrator 二次复核工具记录"
            )
            sections.append(
                f"## {heading}\n"
                + json.dumps(
                    agent_state.get('secondary_review_results', []),
                    ensure_ascii=False,
                    indent=2,
                )
            )

        collaboration_log = {
            'selection_changes': user_modifications,
            'analysis_edits': user_analysis_edits,
        }
        if user_modifications or user_analysis_edits:
            heading = "Human collaboration record" if is_en else "人工协作记录"
            sections.append(
                f"## {heading}\n"
                + json.dumps(collaboration_log, ensure_ascii=False, indent=2)
            )

        evidence_bundle = "\n\n".join(sections)
        if not evidence_bundle:
            evidence_bundle = (
                "No usable evidence was recorded."
                if is_en
                else "未记录到可用证据。"
            )

        prompt = _build_generic_final_report_prompt(
            agent_state,
            evidence_bundle,
            (len(red_analyses), len(blue_analyses), len(green_analyses)),
            lang=lang,
        )
        system_content = (
            "You are PathCopilot Orchestrator. Produce an evidence-linked, "
            "organ-agnostic pathology answer. Treat human edits as explicit "
            "case evidence and disclose unresolved conflicts."
            if is_en
            else
            "你是 PathCopilot Orchestrator。请生成可溯源、器官无关的病理学回答；"
            "人工编辑属于明确的病例证据，尚未解决的冲突必须如实披露。"
        )
        messages = [
            {'role': 'system', 'content': system_content},
            {'role': 'user', 'content': prompt},
        ]
        report_content, _ = call_agent_model(messages)
        if not report_content:
            report_content = generate_fallback_report_collaborative(
                evidence_bundle,
                user_modifications,
            )

        report_path = output_dir / 'report.md'
        with open(report_path, 'w', encoding='utf-8') as report_file:
            report_file.write(report_content)

        try:
            word_result = generate_detailed_word_report(
                agent_state.get('filename', 'unknown'),
                agent_state.get('tree_nodes', []),
                str(output_dir),
                file_id,
            )
            word_path = (
                word_result.get('path', '')
                if isinstance(word_result, dict)
                else ''
            )
        except Exception:
            word_path = ''

        return {
            'success': True,
            'reasoning': (
                'Integrated task-relevant multiscale evidence and human edits.'
                if is_en
                else
                '已整合与当前任务相关的多尺度证据及人工编辑。'
            ),
            'plan': (
                ['Report generated, awaiting user confirmation']
                if is_en
                else ['报告已生成，等待用户确认']
            ),
            'report': report_content,
            'selected_regions': [],
            'subagent_results': [],
            'reportable_findings': _reportable_findings_for_review(
                agent_state, report_content
            ),
            'metadata': {
                'report_path': str(report_path),
                'word_path': word_path,
                'statistics': {
                    'red_count': len(red_analyses),
                    'blue_count': len(blue_analyses),
                    'green_count': len(green_analyses),
                },
                'user_modifications': user_modifications,
            },
        }
    except Exception as exc:
        logger.exception("[Phase 5] 协作模式通用报告生成失败")
        return {
            'success': False,
            'error': str(exc),
            'report': 'Report generation failed' if agent_state.get('lang') == 'en' else '报告生成失败',
        }


def generate_fallback_report_collaborative(findings_summary: str, user_modifications: dict) -> str:
    """协作模式的回退报告（当 GPT 调用失败时使用）"""
    
    # 构建用户修改说明
    mod_note = ""
    has_modifications = False
    for phase_key in ['phase1', 'phase2', 'phase3']:
        phase_mod = user_modifications.get(phase_key, {})
        if phase_mod.get('modified'):
            has_modifications = True
            break
    
    if has_modifications:
        mod_note = "\n\n**注意**：本报告基于用户确认/修改后的分析区域生成。\n"
    
    return f"""# 病理分析报告
{mod_note}
## 镜下所见
{findings_summary}

---

## 结论
自动汇总未完成，不能据此形成诊断结论。

## 形态学依据与证据来源
以上为分析流程已经记录的原始发现，需结合相应 ROI 图像逐项人工复核。

## 主要鉴别诊断
当前回退信息不足，无法可靠列出。

## 局限性与建议
报告自动生成失败；请复核取材、倍率、图像质量、ROI 路径及必要的辅助检查。

## 证据支持度（0–4）
0（无法评价）
"""


def evaluate_user_modification(agent_state: dict, phase: int, original_selection: list, 
                               user_selection: list, user_reason: str, 
                               thumbnail_base64: str = None) -> dict:
    """
    评估用户对 AI 选择结果的修改是否合理
    
    Args:
        agent_state: Agent状态字典
        phase: 当前阶段编号 (1-5)
        original_selection: AI 原始选择的区域列表
        user_selection: 用户修改后的区域列表
        user_reason: 用户提供的修改理由
        thumbnail_base64: 缩略图的 base64 编码（可选，用于视觉分析）
    
    Returns:
        dict: {
            'approved': bool,       # AI 是否认可修改
            'ai_reason': str,       # AI 的评估理由
            'suggestion': str       # AI 的建议（如果不认可）
        }
    """
    try:
        # 分析修改内容
        original_ids = {roi.get('id', roi.get('roi_id', '')) for roi in original_selection}
        user_ids = {roi.get('id', roi.get('roi_id', '')) for roi in user_selection}
        
        added_ids = user_ids - original_ids
        removed_ids = original_ids - user_ids
        
        phase_name = get_phase_name(phase)
        color_name = {1: '红框', 2: '蓝框', 3: '绿框', 4: '绿框'}.get(phase, '区域')
        
        # 构建修改描述
        changes_desc = []
        if added_ids:
            changes_desc.append(f"新增了 {len(added_ids)} 个{color_name}: {', '.join(added_ids)}")
        if removed_ids:
            changes_desc.append(f"删除了 {len(removed_ids)} 个{color_name}: {', '.join(removed_ids)}")
        if not changes_desc:
            changes_desc.append("区域选择没有变化")
        
        changes_text = "；".join(changes_desc)
        
        # 构建 GPT 提示词
        prompt = f"""你是 **PathCopilot Orchestrator（编排器）**，需要评估病理专家对你选择结果的修改。

## 当前阶段
{phase_name}

## AI 原始选择
选中了 {len(original_selection)} 个{color_name}: {', '.join(original_ids) if original_ids else '无'}

## 用户修改
{changes_text}
修改后选中 {len(user_selection)} 个{color_name}: {', '.join(user_ids) if user_ids else '无'}

## 用户提供的理由
{user_reason if user_reason else '用户未提供具体理由'}

## 评估任务
请基于病理诊断的最佳实践，评估用户的修改是否合理。

考虑以下因素：
1. 用户可能是经验丰富的病理专家，对病变区域有专业判断
2. 用户可能看到了你忽略的重要特征
3. 但如果修改明显不合理（如删除了关键病变区域），应该提出建议

## 输出要求
请以 JSON 格式输出：

```json
{{
    "approved": true/false,
    "ai_reason": "你对用户修改的评估理由",
    "suggestion": "如果不认可，你的具体建议是什么（如果认可则为空字符串）"
}}
```

注意：
- 依据当前图像证据独立评估；用户可能是经验丰富的病理专家，其修改应被认真考虑
- 仅在有明确图像证据时提出不认可
- 理由要简洁专业
"""
        
        # 调用 GPT
        logger.info(f"🔄 [Phase {phase}] 正在调用 AI 评估用户修改...")
        if thumbnail_base64:
            response = call_gpt_vision(prompt, thumbnail_base64)
        else:
            response = call_gpt_text(prompt)
        
        # ⭐ 记录 API 原始响应（便于调试）
        if response:
            logger.info(f"📝 [Phase {phase}] AI 原始响应 (前500字):\n{response[:500]}...")
        else:
            logger.warning(f"⚠️ [Phase {phase}] AI 返回空响应")
        
        # 解析响应 - 改进的 JSON 解析逻辑
        if response:
            try:
                # 尝试多种 JSON 提取方式
                json_match = None
                
                # 方式1: 提取 ```json ... ``` 代码块
                code_block_match = re.search(r'```json\s*([\s\S]*?)\s*```', response)
                if code_block_match:
                    json_str = code_block_match.group(1).strip()
                    logger.info(f"📋 [Phase {phase}] 从代码块提取 JSON")
                else:
                    # 方式2: 直接匹配 JSON 对象
                    json_match = re.search(r'\{[\s\S]*?\}', response)
                    if json_match:
                        json_str = json_match.group()
                        logger.info(f"📋 [Phase {phase}] 直接提取 JSON 对象")
                    else:
                        json_str = None
                
                if json_str:
                    result = json.loads(json_str)
                    approved = result.get('approved', True)
                    ai_reason = result.get('ai_reason', '')
                    suggestion = result.get('suggestion', '')
                    
                    logger.info(f"🤖 [Phase {phase}] AI 评估完成: {'✅ 认可' if approved else '❌ 不认可'}")
                    logger.info(f"   └─ 理由: {ai_reason[:150]}...")
                    if suggestion:
                        logger.info(f"   └─ 建议: {suggestion[:100]}...")
                    
                    return {
                        'approved': approved,
                        'ai_reason': ai_reason,
                        'suggestion': suggestion
                    }
                else:
                    logger.warning(f"⚠️ [Phase {phase}] 无法从响应中提取 JSON")
                    
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ [Phase {phase}] JSON 解析失败: {e}")
        
        # 解析失败：AI 评估不可用，按医生修改执行，不生成 AI 理由
        logger.warning(f"[Phase {phase}] AI 评估响应解析失败，AI 评估不可用，按医生修改执行")
        return {
            'approved': True,
            'ai_reason': '',
            'suggestion': '',
            'assessment_status': 'unavailable'
        }
        
    except Exception as e:
        logger.error(f"[Phase {phase}] 评估用户修改失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        # 出错：AI 评估不可用，按医生修改执行，不生成 AI 理由
        return {
            'approved': True,
            'ai_reason': '',
            'suggestion': '',
            'assessment_status': 'unavailable'
        }


def call_gpt_text(prompt: str) -> str:
    """
    调用 GPT 纯文本接口（不带图片）
    ⭐ 使用 OpenRouter API（与 call_gpt_vision 一致），避免 API Key 配置问题
    
    Args:
        prompt: 提示词
    
    Returns:
        str: GPT 响应文本
    """
    try:
        from .gpt_client import get_next_api_key, get_headers_for_key, mark_key_success, mark_key_failed, OPENROUTER_API_URL, DEFAULT_MODEL
        import requests
        
        api_key = get_next_api_key()
        headers = get_headers_for_key(api_key)
        key_index = api_key[-4:]  # 用于日志标识
        
        payload = {
            "model": DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": "你是一个专业的病理 AI 助手 (PathCopilot Orchestrator)。请用中文回答。"},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 65536,
            "temperature": 0.3
        }
        
        logger.info(f"🔄 call_gpt_text: 使用模型 {DEFAULT_MODEL}，Key#{key_index}")
        
        start_time = time.time()
        response = requests.post(
            OPENROUTER_API_URL,
            headers=headers,
            json=payload,
            timeout=60
        )
        elapsed = time.time() - start_time
        
        if response.status_code == 200:
            result = response.json()
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            mark_key_success(api_key)
            logger.info(f"✅ call_gpt_text 成功 ({elapsed:.1f}s, Key#{key_index}), 响应长度: {len(content)}")
            return content
        else:
            mark_key_failed(api_key)
            logger.error(f"❌ call_gpt_text 失败: HTTP {response.status_code}, {response.text[:200]}")
            return ""
        
    except Exception as e:
        logger.error(f"GPT 文本调用失败: {e}")
        return ""


# ==================== Phase 执行函数 ====================

def execute_phase1_thumbnail(socketio, room, slide, file_id: str, steps_dir: Path, 
                            wsi_name: str, agent_state: dict) -> dict:
    """
    Phase 1: 缩略图分析 (Orchestrator)
    
    ⭐⭐⭐ 复用自动模式的网格生成逻辑，保持红框大小一致 ⭐⭐⭐
    
    流程：
    1. 使用 get_candidate_rois_from_thumbnail() 生成规则网格红框（与自动模式一致）
    2. 让 Orchestrator 分析这些红框，输出 Reasoning + Plan
    
    Returns:
        dict: {
            'success': bool,
            'reasoning': str,          # Orchestrator 的推理过程
            'plan': list,              # 分析计划列表
            'selected_regions': list,  # 选中的红框区域
            'metadata': dict
        }
    """
    try:
        logger.info(f"[Phase 1] 开始缩略图分析: {wsi_name}")
        dimensions = safe_get_slide_metadata(slide)['dimensions']
        
        # ⭐⭐⭐ 复用自动模式的网格生成逻辑 ⭐⭐⭐
        # 这会生成规则的、大小一致的红框网格
        candidate_rois = get_candidate_rois_from_thumbnail(
            socketio, room, slide, file_id, steps_dir, 
            wsi_name=wsi_name, agent_state=agent_state
        )
        
        if not candidate_rois:
            return {'success': False, 'error': '缩略图ROI识别失败'}
        
        # 为红框分配简单ID (R1, R2, R3...)
        assign_simple_roi_ids(candidate_rois, 'R', agent_state)
        logger.info(f"🔴 [Phase 1] 红框ID分配: {[roi['id'] for roi in candidate_rois]}")
        
        # 更新 agent_state
        agent_state['candidate_rois'] = candidate_rois
        agent_state['r_roi_counter'] = len(candidate_rois)
        
        # ⭐⭐⭐ 关键优化：先检查是否可以复用 global_analysis，避免无意义的耗时操作 ⭐⭐⭐
        # get_candidate_rois_from_thumbnail() 中的 AI 筛选已经生成了完整的分析结果
        # 包括 【Reason】【Plan】【Evidence】，所以大多数情况下可以直接复用
        
        existing_analysis = agent_state.get('global_analysis', '')
        reasoning = ""
        plan = []
        evidence = {}
        
        if existing_analysis and isinstance(existing_analysis, str) and len(existing_analysis) > 50:
            # ⭐⭐⭐ 直接复用已有分析，跳过所有耗时的缩略图操作 ⭐⭐⭐
            logger.info(f"[Phase 1] ✅ 复用已有的 global_analysis (长度: {len(existing_analysis)})，跳过缩略图生成")
            reasoning = existing_analysis  # 直接使用完整输出
            plan = []
            evidence = {}
            # ⭐ 使用已保存的缩略图路径（在 get_candidate_rois_from_thumbnail 中生成）
            thumb_path = agent_state.get('global_thumbnail_path', steps_dir / f"step_0_thumbnail.jpg")
        else:
            # ⭐ 极少数情况：没有已有分析，需要重新生成缩略图并调用 GPT
            logger.warning(f"[Phase 1] ⚠️ 没有找到已有的 global_analysis，开始生成缩略图并调用 GPT...")
            
            # 生成缩略图（仅在需要时才执行）
            thumb_size = (1024, 1024)
            with _openslide_lock:
                thumbnail = slide.get_thumbnail(thumb_size)
            
            # 保存缩略图
            thumb_path = steps_dir / f"thumbnail_{file_id}.jpg"
            if thumbnail.mode != 'RGB':
                thumbnail = thumbnail.convert('RGB')
            thumbnail.save(thumb_path, 'JPEG', quality=90)
            
            # 转换为 base64
            buffer = io.BytesIO()
            thumbnail.save(buffer, format='JPEG', quality=90)
            thumb_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            # 构建红框信息描述（用于提示词）
            roi_descriptions = []
            for roi in candidate_rois:
                roi_descriptions.append(f"- {roi['id']}: 位置({roi['rect']['x']}, {roi['rect']['y']})")
            roi_info = "\n".join(roi_descriptions[:20])
            
            # 通用开源应用只围绕用户当前问题规划；疾病/器官特异任务位于 evaluation/。
            lang = _normalize_output_language(agent_state.get('lang', 'zh'))
            objective = get_active_diagnostic_objective(agent_state, lang)
            if lang == 'en':
                prompt = f"""You are the PathCopilot Orchestrator performing whole-slide path planning.

Current user task:
{objective}

You are viewing the whole-slide thumbnail.
- Original dimensions: {dimensions[0]} × {dimensions[1]} pixels
- Candidate Level-1 regions: {len(candidate_rois)} (R1-R{len(candidate_rois)})

Candidate regions:
{roi_info}
{"... " + str(len(candidate_rois)) + " regions in total" if len(candidate_rois) > 20 else ""}

Use only morphology visible in the thumbnail. Select representative, heterogeneous,
question-relevant or potentially contradictory regions. Do not assume an organ,
disease, tumor type, grade, stage, invasion status, biomarker or predefined checklist
unless the user explicitly supplied it. Do not claim cell-level findings from this view.

Return exactly:
[Reason]
- global tissue/lesion distribution and heterogeneity
- findings directly relevant to the user's task
- important uncertainty or competing explanations

[Plan]
Selected Level-1 regions = [R...]
Selection rationale = ...
Next observations needed = ...

[Evidence]
- evidence = directly visible thumbnail evidence
- confidence = 0-4 (0 insufficient, 1 uncertain, 2 possible, 3 well supported, 4 compelling)
"""
            else:
                prompt = f"""你是 PathCopilot Orchestrator，正在执行全片缩略图的路径规划。

当前用户任务：
{objective}

你正在查看整张切片的缩略图：
- 原始尺寸：{dimensions[0]} × {dimensions[1]} 像素
- Level 1 候选区域：{len(candidate_rois)} 个（R1-R{len(candidate_rois)}）

候选区域：
{roi_info}
{"……共 " + str(len(candidate_rois)) + " 个区域" if len(candidate_rois) > 20 else ""}

仅依据缩略图中可直接观察的形态进行判断。选择具有代表性、异质性、与用户问题相关，
或可能提供反证的区域。除非用户明确给出，不得预设器官、疾病、肿瘤类型、分级、分期、
浸润状态、生物标志物或固定问题清单；不得在低倍缩略图上虚构细胞级证据。

严格输出：
【Reason】
- 全片组织/病变分布与异质性
- 与用户任务直接相关的可见发现
- 关键不确定性或竞争性解释

【Plan】
选择的 Level 1 区域 = [R...]
选择理由 = ...
下一步需要观察的证据 = ...

【Evidence】
- evidence = 当前缩略图可直接支持的证据
- confidence = 0-4（0证据不足，1不确定，2可能，3较充分，4非常充分）
"""
            
            socketio.emit('agent.progress', {
                'type': 'progress',
                'phase': 1,
                'percentage': 30,
                'status': 'analyzing',
                'message': _msg('ai_analyzing_reds', agent_state.get('lang', 'zh'), n=len(candidate_rois))
            }, to=room)
            socketio.sleep(0)
            
            response = call_gpt_vision(prompt, thumb_base64)
            
            if response:
                reasoning = response
            else:
                reasoning = (
                    f"Identified {len(candidate_rois)} candidate Level-1 regions for task-directed multiscale review."
                    if _normalize_output_language(agent_state.get('lang', 'zh')) == 'en'
                    else f"已识别 {len(candidate_rois)} 个 Level 1 候选区域，将围绕用户当前问题进行多尺度复核。"
                )
        
        # ⭐ 保存全局分析结果：统一存为字符串（与自动模式一致），避免前端解析差异
        # 同时在 agent_state 中保留结构化版本供内部使用
        agent_state['global_analysis'] = reasoning  # 字符串，直接是 AI 原始响应
        agent_state['global_analysis_structured'] = {
            'reasoning': reasoning,
            'plan': plan,
            'evidence': evidence
        }
        
        # 构建 overlays（与现有 drawColoredROI 格式兼容）
        overlays = []
        for roi in candidate_rois:
            overlays.append({
                'id': roi['id'],
                'original_id': roi.get('original_id') or roi.get('id'),
                'rect': roi['rect'],
                'color': '#FF0000',  # 红色
                'magnification': 10,  # ⭐ 关键：用于 CSS 类选择
                'label': '',  # ⭐ 不显示标签，保持界面干净（通过点击映射实现交互）
                'level': 'red'
            })
        agent_state['all_overlays'] = overlays
        
        logger.info(f"[Phase 1] 完成，共 {len(candidate_rois)} 个红框区域")
        
        # ⭐⭐⭐ 获取完整候选框列表（用于点击添加功能）⭐⭐⭐
        all_candidates = agent_state.get('all_candidate_rois', [])
        
        # 为完整候选框生成 overlays（包括未选中的，用灰色显示）
        all_candidate_overlays = []
        selected_ids = {roi['id'] for roi in candidate_rois}
        for roi in all_candidates:
            is_selected = roi.get('id') in selected_ids or roi.get('grid_id') in [r.get('grid_id') for r in candidate_rois]
            all_candidate_overlays.append({
                'id': roi.get('id', f"candidate_{roi.get('candidate_index', 0)}"),
                'rect': roi['rect'],
                'color': '#FF0000' if is_selected else '#808080',  # 选中红色，未选中灰色
                'magnification': 10,
                'label': '',  # ⭐ 不显示标签，保持界面干净
                'level': 'red',
                'is_selected': is_selected,
                'candidate_index': roi.get('candidate_index', 0),
                'grid_id': roi.get('grid_id'),
                'grid_position': roi.get('grid_position', {})
            })
        
        return {
            'success': True,
            'reasoning': reasoning,
            'plan': plan,
            'evidence': evidence,  # ⭐ Agent Loop Evidence（与自动模式一致）
            'selected_regions': candidate_rois,
            'all_candidates': all_candidates,  # ⭐ 完整候选框列表（用于点击添加）
            'metadata': {
                'thumbnail_path': str(thumb_path),
                'dimensions': dimensions,
                'overlays': overlays,
                'all_candidate_overlays': all_candidate_overlays  # ⭐ 包含所有候选框的 overlays
            }
        }
        
    except Exception as e:
        logger.error(f"[Phase 1] 执行失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'success': False, 'error': str(e)}


def execute_phase2_red_analysis(socketio, room, slide, file_id: str, steps_dir: Path, 
                                agent_state: dict) -> dict:
    """
    Phase 2: 红框分析 (SubAgents 并行)
    
    ⭐⭐⭐ 复用自动模式的逻辑：
    1. 使用 subdivide_roi_into_grid 为每个红框生成 16 个规则蓝框候选
    2. 使用 filter_rois_by_gpt_vision 让 AI 筛选蓝框
    ⭐⭐⭐
    
    Returns:
        dict: {
            'success': bool,
            'reasoning': str,           # 汇总推理
            'plan': list,               # 汇总计划
            'selected_regions': list,   # 选中的蓝框区域
            'subagent_results': list,   # 各 SubAgent 的详细结果
            'metadata': dict
        }
    """
    try:
        import concurrent.futures
        
        selected_red_rois = agent_state.get('selected_red_rois', [])
        wsi_name = agent_state.get('filename', 'unknown')
        
        logger.info(f"[Phase 2] 开始红框分析: {len(selected_red_rois)} 个区域")
        
        if not selected_red_rois:
            return {
                'success': True,
                'reasoning': '没有选中的红框区域',
                'plan': [],
                'selected_regions': [],
                'subagent_results': [],
                'metadata': {}
            }
        
        subagent_results = []
        all_blue_rois = []
        all_blue_candidates = []  # ⭐ 保存所有蓝框候选（用于协作模式点击添加）
        
        def analyze_red_roi(roi_info):
            """分析单个红框，复用自动模式逻辑"""
            try:
                roi_id = roi_info['id']
                
                # ⭐ 复用：生成 16 个规则蓝框候选（4×4 网格）
                blue_candidates = subdivide_roi_into_grid(
                    roi_info, grid_rows=4, grid_cols=4, child_zoom_level=40
                )
                logger.info(f"  ├─ 红框 {roi_id} → 生成 {len(blue_candidates)} 个蓝框候选")
                
                # ⭐⭐⭐ 为候选框分配完整 ID（用于协作模式）⭐⭐⭐
                for idx, candidate in enumerate(blue_candidates):
                    grid_id = candidate.get('grid_id', idx + 1)
                    candidate['id'] = f"{roi_id}-B{grid_id}"
                    candidate['parent_id'] = roi_id
                    candidate['zoom_level'] = 40
                    candidate['is_candidate'] = True  # 标记为候选框
                
                # ⭐ 复用：设置 frame_type='blue'，使用正确的提示词
                roi_info_for_blue = roi_info.copy()
                roi_info_for_blue['frame_type'] = 'blue'
                
                # ⭐ 复用：使用 filter_rois_by_gpt_vision 让 AI 筛选蓝框
                blue_result = filter_rois_by_gpt_vision(
                    slide, blue_candidates, (2000, 2000), None, 
                    roi_info_for_blue, wsi_name, 
                    return_analysis=True, agent_state=agent_state
                )
                
                # 解析结果
                if isinstance(blue_result, dict):
                    selected_blues = blue_result.get('selected_rois', [])
                    analysis = blue_result.get('analysis', '')
                    agent_reason = blue_result.get('agent_reason', blue_result.get('mcp_reason', ''))
                    # ⭐ 优先使用 analysis（AI 完整分析）；agent_reason 只是动作原因（通常为空）
                    reasoning = analysis if analysis else agent_reason
                    findings = reasoning[:200] if reasoning else ''
                else:
                    selected_blues = blue_result if isinstance(blue_result, list) else []
                    reasoning = ''
                    findings = ''
                
                # 为蓝框分配 ID - ⭐ 使用原始 grid_id 保持与自动模式一致
                for idx, blue_roi in enumerate(selected_blues):
                    grid_id = blue_roi.get('grid_id', idx + 1)  # 优先使用原始网格编号
                    blue_roi['id'] = f"{roi_id}-B{grid_id}"
                    blue_roi['parent_id'] = roi_id
                    blue_roi['zoom_level'] = 40
                
                logger.info(f"  └─ 红框 {roi_id} → AI选中 {len(selected_blues)} 个蓝框")
                
                # ⭐ 提取持久化图片路径
                _red_image_path = blue_result.get('image_path') if isinstance(blue_result, dict) else None
                
                return {
                    'roi_id': roi_id,
                    'success': True,
                    'reasoning': reasoning,
                    'findings': findings,
                    'plan': [f'对 {len(selected_blues)} 个蓝框进行 40× 分析'],
                    'blue_rois': selected_blues,
                    'blue_candidates': blue_candidates,  # ⭐ 完整候选框列表（16宫格）
                    'rect': roi_info.get('rect'),  # ⭐ 添加输入 ROI 的 rect 信息，用于前端映射
                    'image_path': _red_image_path,  # ⭐ 红框分析图片持久化路径
                }
                
            except Exception as e:
                logger.error(f"[Phase 2] 红框 {roi_info.get('id', '?')} 分析失败: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return {
                    'roi_id': roi_info.get('id', '?'),
                    'success': False,
                    'error': str(e),
                    'blue_candidates': []
                }
        
        # 并行分析所有红框
        completed_count = 0
        total_count = len(selected_red_rois)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(selected_red_rois))) as executor:
            futures = [executor.submit(analyze_red_roi, roi) for roi in selected_red_rois]
            
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                subagent_results.append(result)
                completed_count += 1
                
                if result.get('success'):
                    blue_rois = result.get('blue_rois', [])
                    if blue_rois:
                        all_blue_rois.extend(blue_rois)
                    # ⭐ 收集所有蓝框候选（用于协作模式点击添加）
                    if result.get('blue_candidates'):
                        all_blue_candidates.extend(result['blue_candidates'])
                    
                    # ⭐⭐⭐ 实时推送：每完成一个红框，立即在前端显示其蓝框 ⭐⭐⭐
                    if socketio and room:
                        # ⭐ 调试日志：检查蓝框数据
                        logger.info(f"📦 [Phase 2 实时推送] 红框 {result.get('roi_id')}: {len(blue_rois)} 个蓝框")
                        for i, roi in enumerate(blue_rois):
                            logger.info(f"    蓝框 {i+1}: id={roi.get('id')}, rect={roi.get('rect')}")
                        
                        # 构建该红框的蓝框 overlays
                        blue_overlays = [{
                            'id': roi['id'],
                            'rect': roi['rect'],
                            'color': '#3B82F6',
                            'magnification': 40,
                            'label': '',
                            'level': 'blue',
                            'parent_id': roi.get('parent_id', '')
                        } for roi in blue_rois]
                        
                        socketio.emit('agent.phase2_progress', {
                            'type': 'phase2_progress',
                            'red_roi_id': result.get('roi_id'),
                            'completed': completed_count,
                            'total': total_count,
                            'blue_count': len(blue_rois),
                            'blue_overlays': blue_overlays,
                            'reasoning': result.get('reasoning', '')[:300],  # 前300字
                            'message': _msg('red_done', agent_state.get('lang', 'zh'), roi_id=result.get('roi_id'), n=len(blue_rois), done=completed_count, total=total_count)
                        }, to=room)
                        socketio.sleep(0)  # 立即发送
        
        # 更新 agent_state
        agent_state['blue_rois'] = all_blue_rois
        agent_state['red_analyses'] = subagent_results
        agent_state['b_roi_counter'] = len(all_blue_rois)
        
        # 添加蓝框到 overlays
        for roi in all_blue_rois:
            agent_state['all_overlays'].append({
                'id': roi['id'],
                'rect': roi['rect'],
                'color': '#3B82F6',
                'magnification': 40,
                'label': '',  # ⭐ 不显示标签，保持界面干净
                'level': 'blue',
                'parent_id': roi.get('parent_id', '')
            })
        
        # 汇总推理
        combined_reasoning = f"分析了 {len(selected_red_rois)} 个红框区域，共发现 {len(all_blue_rois)} 个需要进一步分析的蓝框区域。\n\n"
        combined_reasoning += "\n".join([f"- {r['roi_id']}: {r.get('findings', '无')}" for r in subagent_results if r.get('success')])
        
        logger.info(f"[Phase 2] 完成，共 {len(all_blue_rois)} 个蓝框区域，总候选 {len(all_blue_candidates)} 个")
        
        # ⭐ 保存完整候选框列表到 agent_state（用于协作模式）
        agent_state['all_blue_candidates'] = all_blue_candidates
        
        return {
            'success': True,
            'reasoning': combined_reasoning,
            'plan': [f'对 {len(all_blue_rois)} 个蓝框区域进行 40× 分析'],
            'selected_regions': all_blue_rois,
            'all_candidates': all_blue_candidates,  # ⭐ 完整候选框列表（所有红框的 16 宫格）
            'subagent_results': subagent_results,
            'metadata': {
                'red_count': len(selected_red_rois),
                'blue_count': len(all_blue_rois),
                'total_candidates': len(all_blue_candidates),  # ⭐ 总候选数
                'overlays': agent_state['all_overlays']
            }
        }
        
    except Exception as e:
        logger.error(f"[Phase 2] 执行失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'success': False, 'error': str(e)}


def execute_phase3_blue_analysis(socketio, room, slide, file_id: str, steps_dir: Path,
                                 agent_state: dict) -> dict:
    """
    Phase 3: 蓝框分析 (SubAgents 并行)
    
    ⭐⭐⭐ 复用自动模式的逻辑：
    1. 使用 subdivide_roi_into_grid 为每个蓝框生成 16 个规则绿框候选
    2. 使用 filter_rois_by_gpt_vision 让 AI 筛选绿框
    ⭐⭐⭐
    """
    try:
        import concurrent.futures
        
        selected_blue_rois = agent_state.get('selected_blue_rois', [])
        wsi_name = agent_state.get('filename', 'unknown')
        
        # ⭐⭐⭐ 详细日志：记录要分析的蓝框列表 ⭐⭐⭐
        logger.info(f"[Phase 3] 开始蓝框分析: {len(selected_blue_rois)} 个区域")
        for idx, blue_roi in enumerate(selected_blue_rois):
            logger.info(f"  [{idx}] id={blue_roi.get('id')}, rect={blue_roi.get('rect')}, parent_id={blue_roi.get('parent_id')}, source={blue_roi.get('source')}")
        
        if not selected_blue_rois:
            return {
                'success': True,
                'reasoning': '没有选中的蓝框区域',
                'plan': [],
                'selected_regions': [],
                'subagent_results': [],
                'metadata': {}
            }
        
        subagent_results = []
        all_green_rois = []
        all_green_candidates = []  # ⭐ 保存所有绿框候选（用于协作模式点击添加）
        
        def analyze_blue_roi(roi_info):
            """分析单个蓝框，复用自动模式逻辑"""
            try:
                roi_id = roi_info['id']
                
                # ⭐ 复用：生成 16 个规则绿框候选（4×4 网格）
                green_candidates = subdivide_roi_into_grid(
                    roi_info, grid_rows=4, grid_cols=4, child_zoom_level=160
                )
                logger.info(f"  ├─ 蓝框 {roi_id} → 生成 {len(green_candidates)} 个绿框候选")
                
                # ⭐⭐⭐ 为候选框分配完整 ID（用于协作模式）⭐⭐⭐
                for idx, candidate in enumerate(green_candidates):
                    grid_id = candidate.get('grid_id', idx + 1)
                    candidate['id'] = f"{roi_id}-G{grid_id}"
                    candidate['parent_id'] = roi_id
                    candidate['zoom_level'] = 160
                    candidate['is_candidate'] = True  # 标记为候选框
                
                # ⭐ 复用：设置 frame_type='green'，使用正确的提示词
                roi_info_for_green = roi_info.copy()
                roi_info_for_green['frame_type'] = 'green'
                
                # ⭐ 复用：使用 filter_rois_by_gpt_vision 让 AI 筛选绿框
                green_result = filter_rois_by_gpt_vision(
                    slide, green_candidates, (2000, 2000), None,
                    roi_info_for_green, wsi_name,
                    return_analysis=True, agent_state=agent_state
                )
                
                # 解析结果
                if isinstance(green_result, dict):
                    selected_greens = green_result.get('selected_rois', [])
                    analysis = green_result.get('analysis', '')
                    agent_reason = green_result.get('agent_reason', green_result.get('mcp_reason', ''))
                    # ⭐ 优先使用 analysis（AI 完整分析）；agent_reason 只是动作原因（通常为空）
                    reasoning = analysis if analysis else agent_reason
                    findings = reasoning[:200] if reasoning else ''
                else:
                    selected_greens = green_result if isinstance(green_result, list) else []
                    reasoning = ''
                    findings = ''
                
                # 为绿框分配 ID - ⭐ 使用原始 grid_id 保持与自动模式一致
                for idx, green_roi in enumerate(selected_greens):
                    grid_id = green_roi.get('grid_id', idx + 1)  # 优先使用原始网格编号
                    green_roi['id'] = f"{roi_id}-G{grid_id}"
                    green_roi['parent_id'] = roi_id
                    green_roi['zoom_level'] = 160
                
                logger.info(f"  └─ 蓝框 {roi_id} → AI选中 {len(selected_greens)} 个绿框")
                
                return {
                    'roi_id': roi_id,
                    'success': True,
                    'reasoning': reasoning,
                    'findings': findings,
                    'plan': [f'对 {len(selected_greens)} 个绿框进行 160× 细胞级分析'],
                    'green_rois': selected_greens,
                    'green_candidates': green_candidates,  # ⭐ 完整候选框列表（16宫格）
                    'rect': roi_info.get('rect')  # ⭐ 添加输入 ROI 的 rect 信息，用于前端映射
                }
                
            except Exception as e:
                logger.error(f"[Phase 3] 蓝框 {roi_info.get('id', '?')} 分析失败: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return {
                    'roi_id': roi_info.get('id', '?'),
                    'success': False,
                    'error': str(e),
                    'green_candidates': []
                }
        
        # 并行分析所有蓝框
        completed_count = 0
        total_count = len(selected_blue_rois)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(selected_blue_rois))) as executor:
            futures = [executor.submit(analyze_blue_roi, roi) for roi in selected_blue_rois]
            
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                subagent_results.append(result)
                completed_count += 1
                
                if result.get('success'):
                    green_rois = result.get('green_rois', [])
                    if green_rois:
                        all_green_rois.extend(green_rois)
                    # ⭐ 收集所有绿框候选（用于协作模式点击添加）
                    if result.get('green_candidates'):
                        all_green_candidates.extend(result['green_candidates'])
                    
                    # ⭐⭐⭐ 实时推送：每完成一个蓝框，立即在前端显示其绿框 ⭐⭐⭐
                    if socketio and room:
                        # 构建该蓝框的绿框 overlays
                        green_overlays = [{
                            'id': roi['id'],
                            'rect': roi['rect'],
                            'color': '#22C55E',
                            'magnification': 160,
                            'label': '',
                            'level': 'green',
                            'parent_id': roi.get('parent_id', '')
                        } for roi in green_rois]
                        
                        socketio.emit('agent.phase3_progress', {
                            'type': 'phase3_progress',
                            'blue_roi_id': result.get('roi_id'),
                            'completed': completed_count,
                            'total': total_count,
                            'green_count': len(green_rois),
                            'green_overlays': green_overlays,
                            'reasoning': result.get('reasoning', '')[:300],  # 前300字
                            'message': _msg('blue_done', agent_state.get('lang', 'zh'), roi_id=result.get('roi_id'), n=len(green_rois), done=completed_count, total=total_count)
                        }, to=room)
                        socketio.sleep(0)  # 立即发送
        
        # 更新状态
        agent_state['green_rois'] = all_green_rois
        agent_state['blue_analyses'] = subagent_results
        agent_state['g_roi_counter'] = len(all_green_rois)
        
        # 添加绿框到 overlays
        for roi in all_green_rois:
            agent_state['all_overlays'].append({
                'id': roi['id'],
                'rect': roi['rect'],
                'color': '#22C55E',
                'magnification': 160,
                'label': '',  # ⭐ 不显示标签，保持界面干净
                'level': 'green',
                'parent_id': roi.get('parent_id', '')
            })
        
        # 汇总
        combined_reasoning = f"分析了 {len(selected_blue_rois)} 个蓝框区域，发现 {len(all_green_rois)} 个绿框区域。\n\n"
        combined_reasoning += "\n".join([f"- {r['roi_id']}: {r.get('findings', '无')}" for r in subagent_results if r.get('success')])
        
        logger.info(f"[Phase 3] 完成，共 {len(all_green_rois)} 个绿框区域，总候选 {len(all_green_candidates)} 个")
        
        # ⭐ 保存完整候选框列表到 agent_state（用于协作模式）
        agent_state['all_green_candidates'] = all_green_candidates
        
        return {
            'success': True,
            'reasoning': combined_reasoning,
            'plan': [f'对 {len(all_green_rois)} 个绿框区域进行 160× 细胞级分析'],
            'selected_regions': all_green_rois,
            'all_candidates': all_green_candidates,  # ⭐ 完整候选框列表（所有蓝框的 16 宫格）
            'subagent_results': subagent_results,
            'metadata': {
                'blue_count': len(selected_blue_rois),
                'green_count': len(all_green_rois),
                'total_candidates': len(all_green_candidates),  # ⭐ 总候选数
                'overlays': agent_state['all_overlays']
            }
        }
        
    except Exception as e:
        logger.error(f"[Phase 3] 执行失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'success': False, 'error': str(e)}


def _build_green_box_context(parent_id: str, agent_state: dict) -> str:
    """Build full path context (thumbnail→red→blue) for green box in single-WSI auto mode.
    
    Looks up global analysis, red (10×) and blue (40×) analyses stored in agent_state
    so the green box can make context-aware observations along its exact parent path.
    
    Example: for green box R5-B3-G6, this returns:
      - Thumbnail global analysis (Orchestrator)
      - Red box R5 (10× observation)
      - Blue box R5-B3 (40× morphological analysis)
    """
    if not parent_id:
        return ""

    _is_en = agent_state.get('lang', 'zh') == 'en'

    # ⭐ 1. 缩略图全局分析
    global_text = ""
    global_analysis = agent_state.get('global_analysis', '')
    if isinstance(global_analysis, str) and global_analysis:
        global_text = global_analysis
    elif isinstance(global_analysis, dict):
        global_text = global_analysis.get('reasoning', '') or global_analysis.get('analysis', '')

    # ⭐ 2. 蓝框自身的分析（从 blue_analyses 按 roi_id 精确匹配）
    blue_reasoning = ""
    for ba in agent_state.get('blue_analyses', []):
        if ba.get('roi_id') == parent_id:
            blue_reasoning = ba.get('reasoning', ba.get('findings', ''))
            break

    # ⭐ 3. 父红框的分析（从 parent_id 解析出红框 ID，再从 red_analyses 精确匹配）
    red_id = parent_id.split('-B')[0] if '-B' in parent_id else ''
    red_reasoning = ""
    if red_id:
        for ra in agent_state.get('red_analyses', []):
            if ra.get('roi_id') == red_id:
                red_reasoning = ra.get('reasoning', ra.get('findings', ''))
                break

    if not global_text and not red_reasoning and not blue_reasoning:
        return ""

    if _is_en:
        ctx = "\n## 📋 Parent Analysis Path (Context)\n\n"
        if global_text:
            ctx += f"=== Thumbnail (Orchestrator global observation) ===\n{global_text}\n\n"
        if red_reasoning:
            ctx += f"=== Red Box {red_id} (10× global observation) ===\n{red_reasoning}\n\n"
        if blue_reasoning:
            ctx += f"=== Blue Box {parent_id} (40× morphological analysis) ===\n{blue_reasoning}\n\n"
    else:
        ctx = "\n## 📋 上级分析路径（Context）\n\n"
        if global_text:
            ctx += f"=== 缩略图（Orchestrator 全局观察）===\n{global_text}\n\n"
        if red_reasoning:
            ctx += f"=== 红框 {red_id}（10× 全局观察）===\n{red_reasoning}\n\n"
        if blue_reasoning:
            ctx += f"=== 蓝框 {parent_id}（40× 形态学分析）===\n{blue_reasoning}\n\n"

    return ctx


def execute_phase4_green_analysis(socketio, room, slide, file_id: str, steps_dir: Path,
                                  agent_state: dict) -> dict:
    """Analyze all selected 160× ROIs with the shared organ-agnostic contract."""
    try:
        import concurrent.futures

        selected_green_rois = agent_state.get('selected_green_rois', [])
        is_patient_level = agent_state.get('is_patient_level', False)
        lang = _normalize_output_language(agent_state.get('lang', 'zh'))

        logger.info(
            f"[Phase 4] 开始通用绿框分析: {len(selected_green_rois)} 个区域, "
            f"Patient Level: {is_patient_level}"
        )

        if not selected_green_rois:
            return {
                'success': True,
                'reasoning': 'No green ROIs selected.' if lang == 'en' else '没有选中的绿框区域。',
                'plan': [],
                'selected_regions': [],
                'subagent_results': [],
                'metadata': {},
            }

        def analyze_green_roi(roi_info):
            try:
                roi_id = roi_info['id']
                rect = roi_info['rect']
                region = safe_read_region(
                    slide,
                    (rect['x'], rect['y']),
                    0,
                    (rect['w'], rect['h']),
                ).convert('RGB')

                target_size = 2000
                if max(region.size) > target_size:
                    ratio = target_size / max(region.size)
                    region = region.resize(
                        (int(region.width * ratio), int(region.height * ratio)),
                        Image.Resampling.LANCZOS,
                    )

                roi_path = steps_dir / f"green_{roi_id}.jpg"
                region.save(roi_path, 'JPEG', quality=95)
                buffer = io.BytesIO()
                region.save(buffer, format='JPEG', quality=95)
                roi_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

                parent_id = roi_info.get('parent_id', '')
                context_text = _build_green_box_context(parent_id, agent_state)
                if is_patient_level:
                    case_context = (
                        f"Case: {agent_state.get('folder_name', 'Patient')} | "
                        f"Slide: {agent_state.get('part_name', 'Slide 1')}\n"
                        if lang == 'en'
                        else
                        f"病例：{agent_state.get('folder_name', 'Patient')} | "
                        f"切片：{agent_state.get('part_name', 'Slide 1')}\n"
                    )
                    context_text = case_context + context_text

                prompt = _build_generic_terminal_evidence_prompt(
                    roi_info,
                    context_text,
                    lang=lang,
                    objective=agent_state,
                )
                response = _strip_structured_preamble(call_gpt_vision(prompt, roi_base64))
                if not response:
                    return {'roi_id': roi_id, 'success': False, 'error': 'Empty model response'}

                parsed = parse_structured_response(response, 160)
                return {
                    'roi_id': roi_id,
                    'success': True,
                    'reasoning': response,
                    'findings': parsed.get('findings', ''),
                    'evidence': parsed.get('evidence', {}),
                    'evidence_confidence': parsed.get('evidence_confidence'),
                    'diagnosis_suggestion': parsed.get('diagnosis_suggestion', ''),
                    'confidence': parsed.get('confidence', 50),
                    'confidence_level': parsed.get('confidence_level', 2),
                    'pathology_type': parsed.get('pathology_type', 'unclassified'),
                    'key_features': parsed.get('key_features', []),
                    'secondary_review': parsed.get('secondary_review'),
                    'secondary_review_verdict': parsed.get('secondary_review_verdict'),
                    'image_path': str(roi_path),
                    'rect': rect,
                }
            except Exception as exc:
                return {
                    'roi_id': roi_info.get('id', '?'),
                    'success': False,
                    'error': str(exc),
                    'rect': roi_info.get('rect'),
                }

        subagent_results = []
        all_findings = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(selected_green_rois))
        ) as executor:
            futures = [executor.submit(analyze_green_roi, roi) for roi in selected_green_rois]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                subagent_results.append(result)
                if result.get('success'):
                    all_findings.append(result.get('reasoning') or result.get('findings', ''))

        # ``as_completed`` is intentionally concurrent but its completion order
        # is nondeterministic.  Restore the selected ROI order before review,
        # reporting and export so identical evidence has a reproducible trace.
        selected_order = {
            str(roi.get('id') or roi.get('roi_id') or ''): index
            for index, roi in enumerate(selected_green_rois)
        }
        subagent_results.sort(
            key=lambda item: selected_order.get(
                str(item.get('roi_id') or item.get('id') or ''),
                len(selected_order),
            )
        )
        all_findings = [
            item.get('reasoning') or item.get('findings', '')
            for item in subagent_results if item.get('success')
        ]

        agent_state['green_analyses'] = subagent_results
        agent_state['pathology_findings'] = all_findings

        if lang == 'en':
            combined_reasoning = (
                f"Completed cellular-level analysis of {len(selected_green_rois)} green ROIs.\n\n"
            )
            empty_text = 'No finding recorded'
            plan = ['Integrate all task-relevant evidence into the final answer']
        else:
            combined_reasoning = (
                f"完成了 {len(selected_green_rois)} 个绿框区域的细胞级分析。\n\n"
            )
            empty_text = '无记录'
            plan = ['整合与当前任务相关的全部证据，生成最终回答']
        combined_reasoning += "\n".join(
            f"- {item['roi_id']}: {item.get('reasoning') or item.get('findings', empty_text)}"
            for item in subagent_results
            if item.get('success')
        )

        return {
            'success': True,
            'reasoning': combined_reasoning,
            'plan': plan,
            'selected_regions': [],
            'subagent_results': subagent_results,
            'metadata': {
                'green_count': len(selected_green_rois),
                'findings_count': len(all_findings),
                'overlays': agent_state.get('all_overlays', []),
            },
        }
    except Exception as exc:
        logger.error(f"[Phase 4] 执行失败: {exc}")
        logger.exception("[Phase 4] 通用绿框分析异常")
        return {'success': False, 'error': str(exc)}


def execute_phase5_report(socketio, room, slide, file_id: str, output_dir: Path,
                          agent_state: dict) -> dict:
    """Compatibility entry point for the shared organ-agnostic Phase 5 report."""
    return execute_phase5_report_collaborative(
        socketio,
        room,
        slide,
        file_id,
        output_dir,
        agent_state,
    )


def _upsert_tree_nodes(agent_state, nodes):
    """Thread-safely persist each completed Red ROI branch exactly once."""
    if not isinstance(nodes, (list, tuple)) or not nodes:
        return 0
    lock = agent_state.setdefault('tree_nodes_lock', threading.RLock())
    with lock:
        tree_nodes = agent_state.setdefault('tree_nodes', [])
        index_by_id = {}
        for index, node in enumerate(tree_nodes):
            if isinstance(node, dict):
                node_id = node.get('roi_id') or node.get('id')
                if node_id:
                    index_by_id[str(node_id)] = index
        changed = 0
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get('roi_id') or node.get('id')
            if not node_id:
                continue
            node_id = str(node_id)
            existing_index = index_by_id.get(node_id)
            if existing_index is None:
                index_by_id[node_id] = len(tree_nodes)
                tree_nodes.append(node)
            else:
                tree_nodes[existing_index] = node
            changed += 1
        return changed


def _stopped_red_leaf(roi_info, red_analysis):
    """10× observation kept as a childless red node when drill-down stops."""
    roi_id = (roi_info or {}).get('id', '')
    return {
        'id': roi_id,
        'roi_id': roi_id,
        'level': 'red',
        'zoom_level': 10,
        'analysis': red_analysis or {},
        'children': [],
    }


def _agent_stop_requested(room):
    """Return the cooperative cancellation flag for one analysis room."""
    return bool(agent_stop_flags.get(room, False))


def _stopped_multiscale_result(roi_info, red_analysis=None):
    """Stop one branch without inventing unfinished child evidence."""
    red_analysis = red_analysis or {}
    tree_nodes = (
        [_stopped_red_leaf(roi_info, red_analysis)]
        if red_analysis
        else []
    )
    return {
        'finding': red_analysis.get('findings', ''),
        'confidence': red_analysis.get('confidence', 0),
        'next_action': {'action': 'stopped'},
        'tools_results': {},
        'tree_nodes': tree_nodes,
        'stopped': True,
    }


def _collaborative_empty_blue_phase2_result(conf_id, conf_data):
    """After confirmation, an empty blue list is a finished red leaf, not a skip."""
    roi_info = (conf_data or {}).get('roi_info') or {}
    red_analysis = (conf_data or {}).get('red_analysis') or {}
    red_leaf = (conf_data or {}).get('red_leaf') or _stopped_red_leaf(roi_info, red_analysis)
    return {
        'conf_id': conf_id,
        'skipped': False,
        'empty_blue_stop': True,
        'step': (conf_data or {}).get('step'),
        'roi_info': roi_info,
        'red_analysis': red_analysis,
        'blue_analyses': [],
        'green_rois': [],
        'green_confirmations': {},
        'tree_nodes': [red_leaf],
        'roi_save_dir': (conf_data or {}).get('roi_save_dir'),
        'wsi_name': (conf_data or {}).get('wsi_name'),
    }


def _collaborative_phase2a_stop_or_skip(conf_id, conf_data, raw_blue_rois):
    """User skip stays out of the tree; an empty blue list keeps the red leaf."""
    if (conf_data or {}).get('status') == 'skipped':
        return {'conf_id': conf_id, 'skipped': True, 'step': (conf_data or {}).get('step')}
    if not raw_blue_rois:
        return _collaborative_empty_blue_phase2_result(conf_id, conf_data)
    return None


def _tree_branch_ids(agent_state):
    lock = agent_state.setdefault('tree_nodes_lock', threading.RLock())
    with lock:
        return {
            str(node.get('roi_id') or node.get('id'))
            for node in agent_state.get('tree_nodes', [])
            if isinstance(node, dict) and (node.get('roi_id') or node.get('id'))
        }


def run_agent(socketio, room, file_id: str, max_steps: int = 12, strategy: str = "default", mode: str = "ai",
              patient_level_context: dict = None, lang: str = 'zh', diagnostic_question: str = '',
              specimen_context: str = '',
              analysis_strategy: str = 'agent_swarm',
              collaboration_review_policy: str = 'staged',
              runtime_run_id: str = '',
              command_fingerprint: str = '') -> dict:
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
        mode: 分析模式 ('ai' 纯AI模式, 'collaborative' 人机协作模式)
        patient_level_context: Patient Level上下文（可选），包含:
            - part_name: 切片部位名称
            - folder_name: 患者文件夹名
            - wsi_index: 当前WSI索引
            - total_wsi: 总WSI数量
        lang: 语言设置 ('zh' 中文, 'en' 英文)
    
    Returns:
        dict: 分析结果（仅Patient Level模式返回），包含pathology_findings等
    """
    analysis_strategy = str(analysis_strategy or 'agent_swarm').strip().lower()
    diagnostic_question = str(diagnostic_question or '').strip()
    specimen_context = _specimen_context_for_run(file_id, specimen_context, lang)
    organ = _organ_label_for_file(file_id, lang)
    if analysis_strategy not in {'agent_swarm', 'single_agent_roam'}:
        raise ValueError(f"Unsupported analysis_strategy: {analysis_strategy}")
    if analysis_strategy == 'single_agent_roam':
        if mode != 'ai':
            raise ValueError('single_agent_roam is an independent AI route and cannot use collaborative mode')
        from .single_agent_roam import run_single_agent_roam
        return run_single_agent_roam(
            socketio=socketio,
            room=room,
            file_id=file_id,
            max_steps=max_steps,
            lang=lang,
            diagnostic_question=diagnostic_question,
            specimen_context=specimen_context,
            runtime_run_id=runtime_run_id,
            patient_level_context=patient_level_context,
        )

    patient_event_sink = None
    if isinstance(patient_level_context, dict):
        patient_event_sink = patient_level_context.get('_patient_event_sink')
    if callable(patient_event_sink):
        # Standalone single-WSI runs never set this sink. Wrapping here would
        # duplicate session-room events and change the finalized WSI UI.
        socketio = _PatientLiveSocket(socketio, patient_event_sink)

    diagnostic_question = _merge_trusted_specimen_context(
        diagnostic_question, specimen_context, lang,
    )
    try:
        mode_name = "人机协作" if mode == "collaborative" else "纯AI"
        logger.info(f"启动病理Agent ({mode_name}模式): file_id={file_id}, room={room}, max_steps={max_steps}")
        
        # ⭐ 启动 LLM 日志会话
        from .llm_logger import start_log_session, end_log_session
        from .gpt_client import set_current_session, clear_current_session
        
        diagnostic_question = str(diagnostic_question or '').strip()
        log_metadata = {
            "mode": mode,
            "strategy": strategy,
            "max_steps": max_steps,
            "room": room
        }
        if diagnostic_question:
            log_metadata['diagnostic_question'] = diagnostic_question
        if patient_level_context:
            log_metadata.update({
                "is_patient_level": True,
                "part_name": patient_level_context.get('part_name'),
                "folder_name": patient_level_context.get('folder_name'),
                "wsi_index": patient_level_context.get('wsi_index'),
                "total_wsi": patient_level_context.get('total_wsi')
            })
        
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
        
        # Standalone runs start with a clean stop flag. Patient workers are
        # registered by the parent first; never erase a concurrent parent stop.
        if patient_level_context and patient_level_context.get('preserve_stop_flag'):
            if agent_stop_flags.get(room, False):
                logger.info('Patient worker cancelled before Agent startup: %s', room)
                return None
        else:
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
        patient_id = patient_level_context.get('patient_id') if patient_level_context else None
        paths = get_output_paths(file_id, mode, patient_id, filename=filename)
        output_dir = paths['output_dir']
        steps_dir = paths['steps_dir']
        if not patient_id:
            _write_single_output_owner_manifest(paths['base_dir'], file_id)
        steps_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 输出目录: {output_dir} (目录名: {paths['dir_name']}, 模式: {paths['mode_subdir']})")
        
        # 打开WSI文件
        slide = openslide.OpenSlide(file_path)
        dimensions = safe_get_slide_metadata(slide)['dimensions']
        
        # ⭐⭐⭐ 五阶段协作模式分流 ⭐⭐⭐
        # collaborative 模式使用新的五阶段状态机，auto 模式保持原有逻辑
        if mode == 'collaborative':
            logger.info(f"🤝 启用五阶段协作模式，跳转到 run_collaborative_agent()")
            return run_collaborative_agent(
                socketio, room, file_id, slide, filename, dimensions,
                output_dir, steps_dir, max_steps, strategy, patient_level_context,
                output_paths=paths, lang=lang,
                diagnostic_question=diagnostic_question,
                review_policy=collaboration_review_policy,
                runtime_run_id=runtime_run_id,
            )
        
        # ========== 以下为 auto 模式原有逻辑（保持不变）==========
        
        # Agent状态初始化
        agent_state = {
            'current_step': 0,
            'file_id': file_id,  # ⭐ 文件ID，用于输出路径等
            'filename': filename,
            'organ': organ,
            'diagnostic_question': diagnostic_question,
            'analysis_strategy': analysis_strategy,
            'dimensions': dimensions,
            'candidate_rois': [],
            'analyzed_rois': [],
            'pathology_findings': [],
            'tool_results': [],
            'confidence_scores': [],
            'next_action_suggestions': [],
            'tree_nodes': [],  # 存储所有树结构
            'tree_nodes_lock': threading.RLock(),  # 保护并行红框分支
            'all_overlays': [],  # 累积所有已显示的overlays（红蓝绿框）
            'mode': mode,  # 分析模式: 'ai' 或 'collaborative'
            'room': room,   # WebSocket房间ID，用于人机协作通信
            'lang': lang,   # ⭐ 语言设置: 'zh' 或 'en'
            'pending_blue_confirmations': {},  # ⭐ 预先初始化，用于协作模式统一确认
            'pending_blue_lock': threading.Lock(),  # ⭐ 线程锁保护并发写入
            # ⭐ ROI计数器（用于简化命名: R1, R2, B1, B2, G1, G2）
            'r_roi_counter': 0,
            'b_roi_counter': 0,
            'g_roi_counter': 0,
            # ⭐ Agent Loop 高级功能
            'evidence_pool': EvidencePool() if EvidencePool else [],  # 证据原子化存储
            'spatial_index': {},  # 空间搜索索引
            'backtrack_history': [],  # 回溯历史记录
            # ⭐ 输出路径信息（用于分离存储）
            'output_paths': paths,
        }
        
        # ⭐ Patient Level 上下文（如果存在）
        if patient_level_context:
            agent_state['is_patient_level'] = True
            agent_state['part_name'] = patient_level_context.get('part_name', 'Slide 1')
            agent_state['folder_name'] = patient_level_context.get('folder_name', 'Patient')
            agent_state['wsi_index'] = patient_level_context.get('wsi_index', 1)
            agent_state['total_wsi'] = patient_level_context.get('total_wsi', 1)
            logger.info(f"[Patient Level] 启用Patient Level上下文: {agent_state['part_name']}")
        
        # ⭐ 存储到全局状态字典（用于人机协作socket事件访问）
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

        # The thumbnail/model request may finish after the user has pressed
        # Stop. Do not turn that late response into a new Subagent wave.
        if _agent_stop_requested(room):
            logger.info("⛔ 缩略图返回后检测到停止请求；不启动红框 Subagent")
            emit_done(socketio, room, file_id, agent_state, status='stopped')
            slide.close()
            return {'success': False, 'status': 'stopped'}
        
        # ⭐ 为选中的红框分配简单ID (R1, R2, R3...)
        assign_simple_roi_ids(candidate_rois, 'R', agent_state)
        logger.info(f"🔴 红框ID分配: {[roi['id'] for roi in candidate_rois]}")
        
        # ⭐ Patient Level: 发送简化ID后的红框列表 + 全局分析理由
        # 注意：必须在 assign_simple_roi_ids 之后发射，确保 ID 与后续 trace 事件一致
        socketio.emit('agent.trace', {
            'type': 'trace',
            'step': 0,
            'is_patient_level': bool(agent_state.get('is_patient_level', False)),
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
        
        # ⭐ Agent Loop：构建空间索引（用于空间搜索功能）
        build_spatial_index(candidate_rois, agent_state)
            
        agent_state['candidate_rois'] = candidate_rois
        
        # ⭐ 初始化全局 overlays（10 个红框）- 累积显示所有框
        agent_state['all_overlays'] = [create_roi_overlay(roi) for roi in candidate_rois]
        # Thumbnail overlays were emitted with grid IDs (roi_10x_…). Re-send
        # after R{n} assignment so the viewer overlay id matches the tree card.
        socketio.emit('agent.viewport', {
            'type': 'viewport',
            'overlays': agent_state['all_overlays'],
            'why': 'red_ids_assigned',
        }, to=room)
        socketio.sleep(0)
        
        # ⭐ 初始化线程锁（用于保护 all_overlays 的并发访问）
        agent_state['overlays_lock'] = threading.Lock()
        
        logger.info(f"✅ 从缩略图识别出 {len(candidate_rois)} 个10X红框，已初始化全局overlays")
        logger.info(f"📊 分析架构: 红框 → 每框16蓝框候选(AI选) → 每蓝16绿框候选(AI选)")

        # Close the small race between thumbnail presentation and executor
        # creation. Stop means "do not create any new child work".
        if _agent_stop_requested(room):
            logger.info("⛔ 红框线程池创建前检测到停止请求；本轮安全结束")
            emit_done(socketio, room, file_id, agent_state, status='stopped')
            slide.close()
            return {'success': False, 'status': 'stopped'}
        
        # === 步骤1-N：真正的并行分析所有ROI ===
        logger.info(f"🚀 开始并行分析 {len(candidate_rois)} 个红框...")
        
        import concurrent.futures
        
        is_collaborative = agent_state.get('mode') == 'collaborative'
        
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
                if _agent_stop_requested(room):
                    return {
                        'roi': current_roi,
                        'result': _stopped_multiscale_result(current_roi),
                        'index': roi_index,
                        'stopped': True,
                    }
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
                    if _agent_stop_requested(room):
                        return {
                            'roi': current_roi,
                            'result': _stopped_multiscale_result(current_roi),
                            'index': roi_index,
                            'stopped': True,
                        }
                    # 多尺度ROI分析（每个红框内部是并行的：同时分析红/蓝/绿框）
                    roi_analysis_result = execute_multiscale_roi_analysis(
                        socketio, room, _thread_slide, current_roi, step, file_id, steps_dir, agent_state
                    )
                finally:
                    _thread_slide.close()

                # Commit the branch before emitting presentation/Runtime
                # events.  A sidecar failure must never erase valid ROI data.
                if isinstance(roi_analysis_result, dict):
                    _upsert_tree_nodes(
                        agent_state,
                        roi_analysis_result.get('tree_nodes', []),
                    )
                
                logger.info(f"✅ [线程{roi_index+1}] 完成红框 {current_roi['id']} 分析")
                
                # ⭐ 发送到聊天框：完成分析某个红框（仅自动模式，协作模式在确认后发送）
                # 标记 is_result=true 使其只在左侧显示，不在右侧聊天区显示
                if roi_analysis_result and 'finding' in roi_analysis_result and not is_collaborative:
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
        
        # ⭐ 收集所有结果（包括协作模式的Phase 1结果）
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
                    for pending_future in futures:
                        pending_future.cancel()
                    executor.shutdown(wait=False)
                    emit_done(socketio, room, file_id, agent_state, status='stopped')
                    slide.close()
                    return
                
                completed_count += 1
                result_data = future.result()
                all_results.append(result_data)
                
                # 自动模式：立即处理结果
                if not is_collaborative and result_data.get('result'):
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
                        _upsert_tree_nodes(
                            agent_state,
                            roi_analysis_result['tree_nodes'],
                        )
                
                # 发送进度更新（统一格式）
                # 统一显示主路径进度
                progress_msg = _msg('main_path_progress', agent_state.get('lang', 'zh'), done=completed_count, total=len(candidate_rois))
                
                socketio.emit('agent.progress', {
                    'type': 'progress',
                    'step': completed_count,
                    'total_steps': len(candidate_rois),
                    'percentage': int((completed_count / len(candidate_rois)) * 50 if is_collaborative else (completed_count / len(candidate_rois)) * 90),
                    'status': 'roi_analysis',
                    'message': progress_msg
                }, to=room)
        
        logger.info(f"✅ 所有 {len(candidate_rois)} 个红框并行分析完成！")

        # Refuse to publish a visually convincing but structurally empty
        # report.  Every selected Red ROI must own one canonical tree branch.
        if not is_collaborative:
            selected_ids = {
                str(roi.get('id') or roi.get('roi_id'))
                for roi in candidate_rois
                if isinstance(roi, dict) and (roi.get('id') or roi.get('roi_id'))
            }
            branch_ids = _tree_branch_ids(agent_state)
            failed_ids = {
                str(item.get('roi', {}).get('id') or item.get('roi', {}).get('roi_id'))
                for item in all_results
                if not item.get('result')
            }
            missing_ids = selected_ids - branch_ids
            if failed_ids or missing_ids:
                details = []
                if failed_ids:
                    details.append("failed=" + ",".join(sorted(failed_ids)))
                if missing_ids:
                    details.append(
                        "missing_tree=" + ",".join(sorted(missing_ids))
                    )
                error_message = (
                    "ROI branch integrity check failed; no final report was "
                    "generated (" + "; ".join(details) + ")"
                )
                logger.error(error_message)
                emit_error(socketio, room, error_message)
                try:
                    slide.close()
                except Exception:
                    pass
                clear_agent_state(room)
                return {'success': False, 'error': error_message}
        
        # ⭐⭐⭐ 协作模式 Phase 2：统一确认后再执行蓝绿框分析 ⭐⭐⭐
        logger.info(f"🔍 [调试] 主循环：is_collaborative={is_collaborative}, mode={agent_state.get('mode')}")
        if is_collaborative:
            logger.info(f"\n{'='*80}")
            logger.info(f"🤝 协作模式 Phase 2：等待用户统一确认所有蓝框选择")
            logger.info(f"{'='*80}")
            
            # 按红框索引排序
            all_results.sort(key=lambda x: x.get('index', 0))
            
            pending = agent_state.get('pending_blue_confirmations', {})
            total_pending = len(pending)
            
            # ⭐ 调试：打印待确认列表内容
            logger.info(f"🔍 调试：pending_blue_confirmations 数量 = {total_pending}")
            for k, v in pending.items():
                logger.info(f"   - {k}: roi={v.get('roi_info', {}).get('id')}, step={v.get('step')}")
            
            # ⭐ 新方案：发送"所有蓝框选取完成"事件，让前端显示统一确认按钮
            # 构建所有待确认蓝框的摘要信息
            pending_summary = []
            for conf_id, conf_data in pending.items():
                pending_summary.append({
                    'confirmation_id': conf_id,
                    'roi_id': conf_data['roi_info']['id'],
                    'step': conf_data['step'],
                    'blue_count': len(conf_data['selected_blue_rois']),
                    'rect': conf_data['roi_info']['rect']
                })
            
            # 发送统一确认请求
            socketio.emit('agent.all_blue_selected', {
                'type': 'all_blue_selected',
                'total_red_frames': total_pending,
                'pending_confirmations': pending_summary,
                'message': _msg('blue_confirm_all', agent_state.get('lang', 'zh'), n=total_pending)
            }, to=room)
            
            logger.info(f"📤 已发送统一确认请求：{total_pending} 个红框待确认")
            
            # ⭐ 等待用户点击"确认全部"按钮
            import time
            timeout_sec = 1800  # 30分钟超时
            start_time = time.time()
            
            # 等待 all_blue_confirmed 标志
            while not agent_state.get('all_blue_confirmed', False):
                # 检查停止标志
                if agent_stop_flags.get(room, False):
                    logger.info(f"Agent被用户停止: {room}")
                    slide.close()
                    emit_done(socketio, room, file_id, agent_state, status='stopped')
                    return
                
                # 检查超时
                if time.time() - start_time > timeout_sec:
                    logger.warning(f"⏰ 等待统一确认超时（{timeout_sec}秒）")
                    break
                
                # 短暂等待，避免CPU空转
                socketio.sleep(0.5)
            
            logger.info(f"✅ 用户已确认所有蓝框，开始执行蓝框分析+绿框筛选...")
            
            # ⭐⭐⭐ Phase 2A: 并行执行所有红框的蓝框分析+绿框筛选 ⭐⭐⭐
            import concurrent.futures
            
            all_phase2a_results = []
            
            def execute_phase2a(conf_id, conf_data):
                """Phase 2A: 蓝框分析 + 绿框筛选（不包含绿框分析）"""
                roi_info = conf_data['roi_info']
                step = conf_data['step']
                red_analysis = conf_data['red_analysis']
                roi_save_dir = conf_data['roi_save_dir']
                wsi_name = conf_data['wsi_name']
                
                # ⭐ 核心修复：优先使用用户修改的 result_rois，否则使用 AI 原始选择
                # 注意：如果 result_rois 是空列表（用户删除了所有蓝框），应该使用空列表
                # 只有当 result_rois 是 None 时，才使用原始选择
                if conf_data.get('result_rois') is not None:
                    raw_blue_rois = conf_data['result_rois']
                    logger.info(f"📝 使用用户修改后的蓝框数据: {len(raw_blue_rois)} 个 (status={conf_data.get('status')})")
                else:
                    raw_blue_rois = conf_data['selected_blue_rois']
                    logger.info(f"📝 使用AI原始蓝框数据: {len(raw_blue_rois)} 个 (status={conf_data.get('status')})")
                
                early = _collaborative_phase2a_stop_or_skip(conf_id, conf_data, raw_blue_rois)
                if early is not None:
                    if early.get('skipped'):
                        logger.info(f"⏩ 红框 {conf_id} 被用户跳过")
                    else:
                        logger.info(f"⏹️ 红框 {conf_id} 已完成 10× 观察，确认后无蓝框，写入红框叶节点")
                    return early
                
                # ⭐ 核心修复：统一格式转换（前端用 roi_id，后端用 id）
                confirmed_blue_rois = []
                for roi in raw_blue_rois:
                    if isinstance(roi, dict):
                        # 如果已经有 'id' 字段，直接使用
                        if 'id' in roi:
                            confirmed_blue_rois.append(roi)
                        # 前端格式：roi_id -> id
                        elif 'roi_id' in roi:
                            converted_roi = {
                                'id': roi['roi_id'],
                                'rect': roi.get('rect', {}),
                                'zoom_level': roi.get('zoom_level', 40),
                                'priority': roi.get('priority', 5),
                                'reason': roi.get('reason', 'User confirmed'),
                                'suspected_pathology': roi.get('suspected_pathology', 'pending'),
                                'parent_id': roi_info['id']
                            }
                            confirmed_blue_rois.append(converted_roi)
                            logger.info(f"  ✏️ 格式转换: roi_id={roi['roi_id']} -> id={converted_roi['id']}")
                        else:
                            logger.warning(f"  ⚠️ 未知ROI格式，跳过: {roi}")
                    else:
                        logger.warning(f"  ⚠️ ROI不是字典类型，跳过: {type(roi)}")
                
                if not confirmed_blue_rois:
                    logger.warning(f"⚠️ 格式转换后没有有效蓝框: conf_id={conf_id}")
                    return _collaborative_empty_blue_phase2_result(conf_id, conf_data)
                
                logger.info(f"📦 红框 {conf_id} 确认使用 {len(confirmed_blue_rois)} 个蓝框 (status={conf_data.get('status')})")
                
                # 执行蓝框分析 + 绿框筛选
                result = execute_blue_analysis_and_green_selection(
                    socketio, room, slide, roi_info, step,
                    confirmed_blue_rois, red_analysis, roi_save_dir, wsi_name, agent_state
                )
                result['conf_id'] = conf_id
                result['conf_data'] = conf_data
                return result
            
            # 并行执行所有红框的 Phase 2A
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(pending))) as executor:
                futures = [
                    executor.submit(execute_phase2a, conf_id, conf_data)
                    for conf_id, conf_data in pending.items()
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    if agent_stop_flags.get(room, False):
                        logger.info(f"Agent被用户停止")
                        break
                    result = future.result()
                    all_phase2a_results.append(result)
            
            logger.info(f"✅ Phase 2A 完成：所有蓝框分析+绿框筛选完成")
            
            # ⭐⭐⭐ 收集所有绿框，等待统一确认 ⭐⭐⭐
            all_pending_greens = {}
            for result in all_phase2a_results:
                if result.get('skipped') or result.get('empty_blue_stop'):
                    continue
                green_confirmations = result.get('green_confirmations', {})
                all_pending_greens.update(green_confirmations)
            
            if all_pending_greens:
                # 存储到 agent_state
                agent_state['pending_green_confirmations'] = all_pending_greens
                total_green_count = sum(len(g.get('green_rois', [])) for g in all_pending_greens.values())
                
                # 发送"所有绿框选取完成"事件
                green_summary = []
                for conf_id, conf_data in all_pending_greens.items():
                    green_summary.append({
                        'confirmation_id': conf_id,
                        'blue_roi_id': conf_data.get('blue_roi_id'),
                        'green_count': len(conf_data.get('green_rois', [])),
                        'step': conf_data.get('step')
                    })
                
                socketio.emit('agent.all_green_selected', {
                    'type': 'all_green_selected',
                    'total_blue_frames': len(all_pending_greens),
                    'total_green_count': total_green_count,
                    'pending_confirmations': green_summary,
                    'message': _msg('green_confirm', agent_state.get('lang', 'zh'), n=total_green_count)
                }, to=room)
                
                logger.info(f"📤 已发送绿框统一确认请求：{len(all_pending_greens)} 个蓝框下共 {total_green_count} 个绿框待确认")
                
                # ⭐ 等待用户确认绿框
                start_time = time.time()
                while not agent_state.get('all_green_confirmed', False):
                    if agent_stop_flags.get(room, False):
                        logger.info(f"Agent被用户停止")
                        slide.close()
                        emit_done(socketio, room, file_id, agent_state, status='stopped')
                        return
                    
                    if time.time() - start_time > timeout_sec:
                        logger.warning(f"⏰ 等待绿框确认超时")
                        break
                    
                    socketio.sleep(0.5)
                
                logger.info(f"✅ 用户已确认所有绿框，开始执行绿框分析...")
            
            # ⭐⭐⭐ Phase 2B: 并行执行所有绿框分析 ⭐⭐⭐
            # 收集所有绿框（来自所有红框）
            all_green_rois_for_analysis = []
            all_blue_analyses = []
            all_red_analyses = []
            
            for result in all_phase2a_results:
                if result.get('skipped') or result.get('empty_blue_stop'):
                    conf_data = pending.get(result.get('conf_id'), {})
                    red_analysis = result.get('red_analysis') or conf_data.get('red_analysis', {})
                    agent_state['pathology_findings'].append(red_analysis.get('findings', ''))
                    continue
                
                green_rois = result.get('green_rois', [])
                blue_analyses = result.get('blue_analyses', [])
                red_analysis = result.get('red_analysis', {})
                roi_save_dir = result.get('roi_save_dir')
                
                # 构建蓝框上下文（完整版：Reason+Plan+Action）
                blue_contexts = {}
                red_context = {
                    'red_id': result.get('roi_info', {}).get('id', ''),
                    'red_reasoning': red_analysis.get('reasoning', red_analysis.get('thinking', '')),
                    'red_selection_reasoning': red_analysis.get('selection_reasoning', ''),
                    'red_selected_blues': [ba['roi_id'] for ba in blue_analyses],
                    'red_finding': red_analysis.get('findings', '')
                }
                for ba in blue_analyses:
                    blue_id = ba['roi_id']
                    selected_greens_for_blue = [roi['id'] for roi in green_rois if roi.get('parent_id') == blue_id]
                    blue_contexts[blue_id] = {
                        **red_context,
                        'blue_id': blue_id,
                        'blue_reasoning': ba.get('reasoning', ba.get('thinking', '')),
                        'blue_selection_reasoning': ba.get('green_selection_reasoning', ''),
                        'blue_selected_greens': selected_greens_for_blue,
                        'blue_finding': ba.get('findings', '')
                    }
                
                for green_roi in green_rois:
                    parent_blue_id = green_roi.get('parent_id', '')
                    parent_context = blue_contexts.get(parent_blue_id, red_context)
                    all_green_rois_for_analysis.append({
                        'green_roi': green_roi,
                        'parent_context': parent_context,
                        'roi_save_dir': roi_save_dir,
                        'result': result
                    })
                
                all_blue_analyses.extend(blue_analyses)
                all_red_analyses.append(red_analysis)
            
            total_green_count = len(all_green_rois_for_analysis)
            logger.info(f"\n{'─'*80}")
            logger.info(f"🟢 Phase 2B: 并行分析所有 {total_green_count} 个绿框")
            logger.info(f"{'─'*80}")
            
            # ⭐ 发送进度更新：开始绿框分析
            socketio.emit('agent.progress', {
                'type': 'progress',
                'percentage': 80,
                'status': 'green_analysis_start',
                'message': _msg('green_progress', agent_state.get('lang', 'zh'), done=0, total=total_green_count)
            }, to=room)
            
            # 并行分析所有绿框
            green_analyses = []
            completed_green_count = 0  # 用于跟踪完成数量
            if total_green_count > 0:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, total_green_count)) as executor:
                    futures = []
                    for idx, item in enumerate(all_green_rois_for_analysis, 1):
                        green_roi = item['green_roi']
                        parent_context = item['parent_context']
                        roi_save_dir = item['roi_save_dir']
                        
                        # ⭐ 进度消息：type='progress' 前端不显示在左侧面板
                        socketio.emit('agent.message', {
                            'type': 'progress',
                            'content': f"🟢 开始分析绿框 #{idx}/{total_green_count} (160×)",
                            'level': 'green',
                            'roi_id': green_roi['id'],
                            'rect': green_roi['rect'],
                            'is_progress': True
                        }, to=room)
                        
                        # ⭐ Patient Level 模式传递额外信息
                        pl_is_patient_level = agent_state.get('is_patient_level', False)
                        pl_info = {
                            'part_name': agent_state.get('part_name', 'Slide 1'),
                            'folder_name': agent_state.get('folder_name', 'Patient'),
                            'organ': agent_state.get('organ', '未指定标本')
                        } if pl_is_patient_level else None
                        
                        futures.append(
                            executor.submit(analyze_roi_parallel, slide, green_roi, 160, roi_save_dir, 
                                          parent_context=parent_context,
                                          is_patient_level=pl_is_patient_level,
                                          patient_level_info=pl_info,
                                          lang=agent_state.get('lang', 'zh'),
                                          agent_state=agent_state)
                        )
                    
                    # 每个绿框一完成就推到前端，不要等整池结束。
                    for future in concurrent.futures.as_completed(futures):
                        if agent_stop_flags.get(room, False):
                            break
                        ga = future.result()
                        green_analyses.append(ga)
                        completed_green_count += 1
                        _emit_green_analysis_result(
                            socketio, room, ga, completed_green_count
                        )
                        socketio.emit('agent.progress', {
                            'type': 'progress',
                            'percentage': 80 + (completed_green_count / total_green_count) * 15,
                            'status': 'green_analysis',
                            'message': _msg('green_progress', agent_state.get('lang', 'zh'), done=completed_green_count, total=total_green_count)
                        }, to=room)
                
                for ga in green_analyses:
                    logger.info(f"  🟢 {ga.get('roi_id')}: {str(ga.get('findings') or '')[:80]}...")
            
            logger.info(f"✅ Phase 2B 完成: {len(green_analyses)} 绿框并行分析完成")
            
            # 汇总所有分析结果
            for ga in green_analyses:
                agent_state['pathology_findings'].append(ga.get('findings', ''))
            
            # ⭐ Bug修复：构建 tree_nodes 数据，用于最终报告统计
            # 从 all_phase2a_results（Phase 2A结果）获取数据，而不是 all_results（Phase 1结果）
            for result in all_phase2a_results:
                if result.get('skipped'):
                    continue
                if result.get('empty_blue_stop'):
                    _upsert_tree_nodes(agent_state, result.get('tree_nodes', []))
                    continue
                
                roi_info = result.get('roi_info', {})
                red_analysis = result.get('red_analysis', {})
                blue_analyses = result.get('blue_analyses', [])
                green_rois = result.get('green_rois', [])
                
                # 构建红框节点
                red_node = {
                    'id': roi_info.get('id', ''),
                    'roi_id': roi_info.get('id', ''),
                    'level': 'red',
                    'analysis': red_analysis,
                    'findings': red_analysis.get('findings', ''),
                    'rect': roi_info.get('rect'),
                    'children': []
                }
                
                # 构建蓝框节点
                for ba in blue_analyses:
                    blue_node = {
                        'id': ba['roi_id'],
                        'roi_id': ba['roi_id'],
                        'level': 'blue',
                        'analysis': ba,
                        'findings': ba.get('findings', ''),
                        'rect': ba.get('rect'),
                        'parent_id': roi_info.get('id', ''),
                        'children': []
                    }
                    
                    # 添加绿框子节点
                    for ga in green_analyses:
                        parent_id = ga.get('parent_id', '')
                        roi_id = ga.get('roi_id', '')
                        # 检查绿框是否属于当前蓝框
                        if parent_id == ba['roi_id'] or roi_id.startswith(ba['roi_id']):
                            green_node = {
                                'id': ga['roi_id'],
                                'roi_id': ga['roi_id'],
                                'level': 'green',
                                'analysis': ga,
                                'findings': ga.get('findings', ''),
                                'rect': ga.get('rect'),
                                'parent_id': ba['roi_id']
                            }
                            blue_node['children'].append(green_node)
                    
                    red_node['children'].append(blue_node)
                
                _upsert_tree_nodes(agent_state, [red_node])
            
            # 统计并打印
            total_blue = sum(len(node.get('children', [])) for node in agent_state['tree_nodes'])
            total_green = sum(
                len(blue.get('children', [])) 
                for red in agent_state['tree_nodes'] 
                for blue in red.get('children', [])
            )
            logger.info(f"📊 tree_nodes 已构建: {len(agent_state['tree_nodes'])}个红框, {total_blue}个蓝框, {total_green}个绿框")
            
            socketio.emit('agent.progress', {
                'type': 'progress',
                'step': total_green_count,
                'total_steps': total_green_count,
                'percentage': 100,
                'status': 'green_analysis_done',
                'message': _msg('green_done_all', agent_state.get('lang', 'zh'), n=total_green_count)
            }, to=room)
        else:
            # 自动模式：已在 as_completed 循环中完成处理，无需再次遍历
            # ⚠️ 旧代码在这里重复添加 tree_nodes 导致数据翻倍，已删除
            logger.info(f"✅ 自动模式：{len(all_results)} 个红框分析结果已在并行循环中处理完毕")

        # 所有首轮分支完成后、最终报告前，由主 Orchestrator 统一调度可选
        # 二次复核工具。Patient-level 路径由工具内部保持不重复触发。
        _orchestrate_secondary_reviews(agent_state, slide, steps_dir)
        
        slide.close()
        
        # ⭐ 清理父ROI缓存
        clear_roi_cache()
        
        # 完成分析
        if not agent_stop_flags.get(room, False):
            # ⭐ Patient Level模式：生成版本A结构化证据摘要（同时作为 HTML 报告 + wsi_summary）
            if patient_level_context:
                logger.info("="*60)
                logger.info(f"[Patient Level] 生成版本A结构化证据摘要: {patient_level_context.get('part_name', 'Slide')}")
                
                # 统计信息（tree_nodes 是嵌套结构：红框→蓝框→绿框）
                tree_nodes = agent_state.get('tree_nodes', [])
                red_count = len(tree_nodes)
                blue_count = sum(len(red.get('children', [])) for red in tree_nodes)
                green_count = sum(
                    len(blue.get('children', []))
                    for red in tree_nodes
                    for blue in red.get('children', [])
                )
                
                logger.info(f"📈 切片分析统计: {red_count}红框 + {blue_count}蓝框 + {green_count}绿框")
                
                # ⭐ 一次 LLM 调用完成：生成版本A结构化证据摘要（HTML报告 + wsi_summary 共用）
                pl_report = None
                wsi_summary = ''
                try:
                    _pl_title = "Single Slide Pathology Report" if lang == 'en' else "单张切片病理诊断报告"
                    pl_report = generate_final_pathology_report(
                        agent_state, file_id, output_dir, socketio, room,
                        report_title=_pl_title,
                        is_patient_level=True
                    )
                    wsi_summary = pl_report.get('report_text', '') if pl_report else ''
                    logger.info(f"[Patient Level] ✅ WSI 版本A证据摘要已生成: {file_id} ({len(wsi_summary)} 字符)")
                except Exception as e:
                    logger.warning(f"[Patient Level] WSI 报告生成失败: {e}")
                    wsi_summary = f"【切片部位】{patient_level_context.get('part_name', 'Slide')}\n【主要诊断】分析过程中出现错误\n【本切片诊断小结】需重新分析"
                
                agent_state['_wsi_summary_phase5'] = wsi_summary

                try:
                    path_report_result = generate_standardized_path_report(
                        agent_state, file_id, output_dir, socketio, room
                    )
                    if path_report_result and path_report_result.get('success'):
                        agent_state['tree_json'] = path_report_result.get('tree_json')
                        logger.info(f"[Patient Level] ✅ WSI analysis_tree.json 已生成: {file_id}")
                except Exception as e:
                    logger.warning(f"[Patient Level] WSI 路径报告生成失败（不影响主流程）: {e}")
                
                logger.info("="*60)
                
                # 返回切片级别的结果（供 run_patient_level_agent 聚合）
                return {
                    'pathology_findings': agent_state.get('pathology_findings', []),
                    'tree_nodes': tree_nodes,
                    'all_overlays': agent_state.get('all_overlays', []),
                    'wsi_summary': wsi_summary,
                    'statistics': {
                        'red_frames': red_count,
                        'blue_frames': blue_count,
                        'green_frames': green_count
                    },
                    'output_paths': agent_state.get('output_paths', {}),
                }
            
            # ⭐ 单张 WSI 模式：生成最终病理报告
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
            
            # ⭐ 协作模式：请求用户确认最终报告（步骤7）
            if is_collaborative and final_report.get('report'):
                report_content = final_report.get('report', '')
                progress_info = {
                    'red_total': stats.get('red_frames', 0),
                    'blue_total': stats.get('blue_frames', 0),
                    'green_total': stats.get('green_frames', 0)
                }
                
                confirmation_result = request_analysis_confirmation(
                    socketio, room, agent_state, 'final_report', 
                    None, report_content, progress_info
                )
                
                if confirmation_result['status'] == 'edited':
                    # 用户编辑了报告，更新报告内容
                    final_report['report'] = confirmation_result['content']
                    agent_state['final_report'] = final_report
                    logger.info(f"✏️ 用户编辑了最终报告")
                elif confirmation_result['status'] == 'regenerate':
                    # 用户要求重新生成（暂不实现完整的重新生成逻辑）
                    logger.info(f"🔄 用户要求重新生成报告（当前版本继续使用）")
                elif confirmation_result['status'] == 'accepted':
                    logger.info(f"✅ 用户确认最终报告")
                else:
                    logger.warning(f"⏰ 最终报告未获用户确认，保持 AI 原文")
            
            # ⭐ 生成规范化路径报告（Agent Loop 路径文档）
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
        # ⭐ Patient Level模式：返回None表示失败
        if patient_level_context:
            return None
    finally:
        # 清理停止标志
        if room in agent_stop_flags:
            del agent_stop_flags[room]
        # ⭐ 清理全局agent_state（人机协作模式，但Patient Level模式不清理）
        if not patient_level_context:
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


# ================== Patient Level 多WSI并行分析 ==================

def encode_image_file_to_base64(image_path):
    """将图像文件编码为base64"""
    import base64
    try:
        if image_path and Path(image_path).exists():
            with open(image_path, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        logger.warning(f"无法编码图像 {image_path}: {e}")
    return None


def self_wait_for_file_upload(patient_id: str, wsi_index: int, patient_sessions: dict,
                              socketio, room: str, part_name: str, filename: str,
                              max_wait: int = 600, poll_interval: int = 3,
                              lang: str = 'zh') -> str:
    """
    等待指定 WSI 文件上传完成（用于"提前启动分析"场景）。
    前端会通过 socket 事件 patient_level_file_ready 通知后端新文件就绪，
    存入 patient_sessions[patient_id]['pending_files'][index]。
    
    Args:
        patient_id: 患者ID
        wsi_index: WSI 文件索引
        patient_sessions: 共享的患者会话字典
        max_wait: 最大等待秒数（默认 10 分钟）
        poll_interval: 轮询间隔秒数
    
    Returns:
        file_id 字符串，超时返回 None
    """
    import time
    waited = 0
    logger.info(f"[Patient Level] ⏳ 等待文件上传: {part_name} ({filename}), index={wsi_index}")
    
    patient_run_token = (
        (patient_sessions or {}).get(patient_id, {}).get('run_token')
        if patient_sessions else None
    )
    socketio.emit('agent.patient_progress', {
        'percentage': -1,
        'message': _msg('waiting_upload', lang, part=part_name),
        'patient_id': patient_id,
        'run_token': patient_run_token,
    }, to=room)
    
    while waited < max_wait:
        if agent_stop_flags.get(room, False):
            logger.info('[Patient Level] 等待上传时收到停止请求: %s', part_name)
            return None
        # 检查 patient_sessions 中是否有新文件
        if patient_sessions and patient_id in patient_sessions:
            session = patient_sessions[patient_id]
            pending = session.get('pending_files', {})
            if wsi_index in pending:
                file_id = pending[wsi_index].get('file_id')
                if file_id:
                    logger.info(f"[Patient Level] ✅ 文件就绪: {part_name}, file_id={file_id} (等待了 {waited}s)")
                    return file_id
        
        time.sleep(poll_interval)
        waited += poll_interval
        
        if waited % 15 == 0:  # 每15秒输出日志
            logger.info(f"[Patient Level] ⏳ 仍在等待 {part_name} 上传... ({waited}s/{max_wait}s)")
    
    logger.error(f"[Patient Level] ❌ 等待 {part_name} 上传超时 ({max_wait}s)")
    return None


def find_output_dir_for_file_by_id(file_id: str) -> Path:
    """
    根据 file_id 查找输出目录（在 outputs/ 下搜索 analysis_tree.json 中的 file_id 匹配）
    同时搜索单张模式（outputs/{dir_name}/）和 Patient Level 模式（outputs/patient_xxx/auto/wsi_{dir_name}/）
    """
    outputs_dir = Path("outputs")
    
    # 1. 旧格式
    old_dir = outputs_dir / file_id
    if old_dir.exists():
        return old_dir
    
    # 2. 搜索所有子目录（包括 patient_ 目录下的 wsi_ 子目录）
    if outputs_dir.exists():
        # 收集所有需要搜索的目录
        search_dirs = []
        for subdir in outputs_dir.iterdir():
            if subdir.is_dir():
                if subdir.name.startswith('patient_'):
                    # Patient Level：搜索 patient_xxx/auto/wsi_* 和 patient_xxx/collab/wsi_*
                    for mode_dir in ['auto', 'collab']:
                        mode_path = subdir / mode_dir
                        if mode_path.exists():
                            for wsi_dir in mode_path.iterdir():
                                if wsi_dir.is_dir() and wsi_dir.name.startswith('wsi_'):
                                    search_dirs.append(wsi_dir)
                else:
                    search_dirs.append(subdir)
        
        for search_dir in search_dirs:
            for mode_dir in ['auto', 'collab', '']:
                tree_file = search_dir / mode_dir / 'analysis_tree.json' if mode_dir else search_dir / 'analysis_tree.json'
                if tree_file.exists():
                    try:
                        with open(tree_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            if data.get('metadata', {}).get('file_id') == file_id:
                                return search_dir
                    except:
                        pass
    return None


def _load_cached_wsi_result(file_id: str, mode_suffix: str, matched_tree_path: str = '') -> dict:
    """
    从缓存中加载单张 WSI 的分析结果，用于 Patient Level 断点续传
    
    优先从 analysis_tree.json 加载（包含完整的 tree_nodes），
    其次从 analysis_cache state.json 加载。
    wsi_summary 优先从同目录的 pathology_report.txt 读取（最权威的 Phase 5 结果）。
    
    Returns:
        dict: 与 run_single_wsi_for_patient 返回格式兼容的结果，失败返回 None
    """
    def _read_report_txt(tree_json_path: Path) -> str:
        """从 analysis_tree.json 同目录读取 pathology_report.txt"""
        txt_path = tree_json_path.parent / 'pathology_report.txt'
        if txt_path.exists():
            try:
                return txt_path.read_text(encoding='utf-8')
            except Exception:
                pass
        return ''

    result = {
        'pathology_findings': [],
        'tree_nodes': [],
        'all_overlays': [],
        'wsi_summary': '',
        'statistics': {'red_frames': 0, 'blue_frames': 0, 'green_frames': 0},
        'output_paths': {},
        'from_cache': True
    }
    
    # 0. 如果有直接匹配的 tree_path（按文件名匹配的），优先使用
    if matched_tree_path and mode_suffix != 'single_agent_roam':
        matched_p = Path(matched_tree_path)
        if matched_p.exists():
            try:
                with open(matched_p, 'r', encoding='utf-8') as f:
                    tree_data = json.load(f)
                result['tree_nodes'] = tree_data.get('tree_nodes', [])
                metadata = tree_data.get('metadata', {})
                stats = metadata.get('statistics', {})
                result['statistics'] = {
                    'red_frames': stats.get('total_red', 0),
                    'blue_frames': stats.get('total_blue', 0),
                    'green_frames': stats.get('total_green', 0)
                }
                result['wsi_summary'] = _read_report_txt(matched_p) or metadata.get('wsi_summary', '') or metadata.get('global_analysis', '')
                result['output_paths'] = {
                    'output_dir': matched_p.parent,
                    'steps_dir': matched_p.parent / 'steps',
                    'base_dir': matched_p.parent.parent if matched_p.parent.name in ['auto', 'collab'] else matched_p.parent,
                }
                logger.info(f"[断点续传] ✅ 从匹配的 tree_path 加载: {matched_p} (wsi_summary: {len(result['wsi_summary'])} 字符)")
                return result
            except Exception as e:
                logger.warning(f"[断点续传] 匹配 tree_path 加载失败: {e}")
    
    # 1. 尝试从 analysis_tree.json 加载（按 file_id）
    output_dir = find_output_dir_for_file_by_id(file_id)
    if output_dir and mode_suffix != 'single_agent_roam':
        for mode_dir in ['auto', 'collab', '']:
            tree_path = output_dir / mode_dir / 'analysis_tree.json' if mode_dir else output_dir / 'analysis_tree.json'
            if tree_path.exists():
                try:
                    with open(tree_path, 'r', encoding='utf-8') as f:
                        tree_data = json.load(f)
                    
                    result['tree_nodes'] = tree_data.get('tree_nodes', [])
                    metadata = tree_data.get('metadata', {})
                    stats = metadata.get('statistics', {})
                    result['statistics'] = {
                        'red_frames': stats.get('total_red', 0),
                        'blue_frames': stats.get('total_blue', 0),
                        'green_frames': stats.get('total_green', 0)
                    }
                    result['wsi_summary'] = _read_report_txt(tree_path) or metadata.get('wsi_summary', '') or metadata.get('global_analysis', '')
                    
                    # 构造 output_paths
                    mode_subdir = mode_dir if mode_dir else 'auto'
                    result['output_paths'] = {
                        'output_dir': tree_path.parent,
                        'steps_dir': tree_path.parent / 'steps',
                        'base_dir': output_dir,
                    }
                    
                    logger.info(f"[断点续传] ✅ 从 analysis_tree.json 加载: {tree_path}")
                    return result
                except Exception as e:
                    logger.warning(f"[断点续传] 加载 analysis_tree.json 失败: {e}")
    
    # 2. 从 analysis_cache state.json 加载（支持可读文件名 + 旧 UUID 文件名）
    cache_dir = Path("uploads/analysis_cache")
    cache_names_to_try = [f'{file_id}_{mode_suffix}_state.json', f'{file_id}_state.json']
    index_file = cache_dir / '_index.json'
    if index_file.exists():
        try:
            with open(index_file, 'r', encoding='utf-8') as _if:
                _idx = json.load(_if)
            idx_name = _idx.get(f'{file_id}_{mode_suffix}')
            if idx_name:
                cache_names_to_try.insert(0, idx_name)
        except Exception:
            pass
    for cache_name in cache_names_to_try:
        cache_file = cache_dir / cache_name
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                if str(cache_data.get('file_id') or '') != str(file_id):
                    logger.warning(
                        "[断点续传] 忽略 file_id 不匹配的 state.json: %s",
                        cache_file,
                    )
                    continue
                
                # state.json 中主要是前端 UI 状态，提取 ROI 信息
                overlays = cache_data.get('roiOverlays', cache_data.get('overlays', []))
                result['all_overlays'] = overlays
                if mode_suffix == 'single_agent_roam':
                    result['trajectory'] = list(cache_data.get('trajectory') or [])
                    result['report'] = str(cache_data.get('report') or '')
                    result['wsi_summary'] = result['report']
                    result['analysis_strategy'] = 'single_agent_roam'
                result['statistics'] = {
                    'red_frames': sum(1 for o in overlays if o.get('color') == '#FF0000'),
                    'blue_frames': sum(1 for o in overlays if o.get('color') == '#0066FF'),
                    'green_frames': sum(1 for o in overlays if o.get('color') == '#00CC00'),
                }
                
                logger.info(f"[断点续传] ✅ 从 state.json 加载: {cache_file}")
                return result
            except Exception as e:
                logger.warning(f"[断点续传] 加载 state.json 失败: {e}")
    
    return None


_patient_collab_state_lock = threading.RLock()
_patient_worker_state_lock = threading.RLock()


def _patient_execution_already_cancelled(patient_state):
    """True when THIS Patient execution already accepted a cooperative stop."""
    return (
        patient_state.get('cancel_requested') is True
        or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
    )


def _patient_result_can_commit(room, patient_state, expected_run_token):
    """Called while holding _patient_worker_state_lock."""
    return not (
        agent_stop_flags.get(room, False)
        or _patient_execution_already_cancelled(patient_state)
        or str(patient_state.get('run_token') or '') != str(expected_run_token or '')
    )


def _publish_patient_artifacts_atomically(publish_pairs, backup_root):
    """Publish a Patient artifact set with rollback on partial I/O failure.

    Cooperative Stop is linearized by the caller's Patient state lock.  This
    helper adds the complementary filesystem guarantee: either every staged
    artifact replaces its prior public version, or every prior version is
    restored before the exception escapes.
    """
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    records = []
    try:
        for index, (staged_path, final_path) in enumerate(publish_pairs):
            staged_path = Path(staged_path)
            final_path = Path(final_path)
            if not staged_path.exists():
                continue
            final_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path = backup_root / f'{index:04d}-{final_path.name}.bak'
            had_final = final_path.exists()
            if had_final:
                os.replace(final_path, backup_path)
            records.append((final_path, backup_path, had_final))
            os.replace(staged_path, final_path)
    except Exception as publish_error:
        rollback_errors = []
        for final_path, backup_path, had_final in reversed(records):
            try:
                if final_path.exists():
                    final_path.unlink()
                if had_final and backup_path.exists():
                    os.replace(backup_path, final_path)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                'Patient artifact publish failed and rollback was incomplete: '
                + '; '.join(rollback_errors)
            ) from publish_error
        raise


def _begin_patient_level_execution(room: str, patient_state: dict) -> bool:
    """Initialize one Patient run without erasing an already accepted stop.

    The HTTP endpoint can return before the worker thread is scheduled.  A
    user may therefore press Stop before this function runs.  Cancellation is
    monotonic for an execution: once observed, startup must fail closed.
    """
    with _patient_worker_state_lock:
        # Stop is keyed by the Socket.IO session room, which is reused across
        # consecutive Patient runs.  A leftover session flag from patient N
        # must not fail-close patient N+1.  This execution's own state is the
        # source of truth for "already stopped before the worker started".
        if _patient_execution_already_cancelled(patient_state):
            agent_stop_flags[room] = True
            patient_state['cancel_requested'] = True
            return False
        agent_stop_flags[room] = False
        patient_state['active_worker_rooms'] = []
        patient_state['cancel_requested'] = False
        patient_state['stop_terminal_emitted'] = False
        patient_state.setdefault('run_token', uuid.uuid4().hex)
        patient_state['roam_by_file'] = {}
        return True


def _register_patient_worker(room: str, patient_state: dict, worker_room: str) -> bool:
    """Atomically register a worker unless the parent run is being stopped."""
    with _patient_worker_state_lock:
        if (
            agent_stop_flags.get(room, False)
            or patient_state.get('cancel_requested') is True
            or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
        ):
            agent_stop_flags[worker_room] = True
            return False
        active = set(patient_state.get('active_worker_rooms') or ())
        active.add(worker_room)
        patient_state['active_worker_rooms'] = sorted(active)
        agent_stop_flags[worker_room] = False
        return True


def _unregister_patient_worker(patient_state: dict, worker_room: str) -> None:
    with _patient_worker_state_lock:
        active = set(patient_state.get('active_worker_rooms') or ())
        active.discard(worker_room)
        patient_state['active_worker_rooms'] = sorted(active)
        agent_stop_flags.pop(worker_room, None)


def request_patient_level_stop(room: str, patient_state: dict) -> dict:
    """Atomically accept or reject a cooperative Patient cancellation.

    The terminal check shares ``_patient_worker_state_lock`` with the final
    completion commit.  This is the linearization point that prevents an HTTP
    response from claiming "stopping" after the report was already committed.
    """
    with _patient_worker_state_lock:
        current_status = str(patient_state.get('status') or '')
        if current_status in {'completed', 'failed', 'stopped', 'cancelled'}:
            return {
                'accepted': False,
                'already_terminal': True,
                'status': current_status,
                'active_rooms': [],
            }
        agent_stop_flags[room] = True
        patient_state['cancel_requested'] = True
        active_rooms = list(patient_state.get('active_worker_rooms') or ())
        for worker_room in active_rooms:
            agent_stop_flags[worker_room] = True
        patient_state['status'] = 'stopping'
        patient_state['updated_at'] = datetime.now().isoformat()
        return {
            'accepted': True,
            'already_terminal': False,
            'status': 'stopping',
            'active_rooms': active_rooms,
        }


def get_patient_roam_snapshot(patient_state: dict) -> dict:
    """Return only the public projected per-slide roam timeline."""
    with _patient_worker_state_lock:
        return {
            'run_token': patient_state.get('run_token'),
            'roam_by_file': copy.deepcopy(patient_state.get('roam_by_file') or {}),
        }


def _finish_patient_level_stop(socketio, room: str, patient_state: dict) -> None:
    """Publish the terminal Patient cancellation exactly once."""
    with _patient_worker_state_lock:
        if patient_state.get('status') in {'completed', 'failed'}:
            return
        already_stopped = patient_state.get('stop_terminal_emitted') is True
        patient_state['status'] = 'stopped'
        patient_state['cancel_requested'] = True
        patient_state['stop_terminal_emitted'] = True
        patient_state['stopped_at'] = patient_state.get('stopped_at') or datetime.now().isoformat()
        patient_state['updated_at'] = datetime.now().isoformat()
        patient_id = patient_state.get('patient_id')
        run_token = patient_state.get('run_token')
    if not already_stopped:
        socketio.emit(
            'agent.patient_stopped',
            {'patient_id': patient_id, 'status': 'stopped', 'run_token': run_token},
            to=room,
        )
        socketio.emit(
            'agent.patient_progress',
            {
                'percentage': None,
                'message': '患者级分析已停止',
                'status': 'stopped',
                'patient_id': patient_id,
                'run_token': run_token,
            },
            to=room,
        )
        socketio.sleep(0)


class _PatientLiveSocket:
    """Mirror worker-room emits onto the parent Patient Level session.

    Parallel WSI workers use private Socket.IO rooms so ROI state cannot
    collide. The browser only joins the session room, so Swarm live events
    must be copied there with slide identity attached.
    """

    def __init__(self, socketio, sink):
        self._socketio = socketio
        self._sink = sink

    def emit(self, event, data=None, **kwargs):
        self._socketio.emit(event, data, **kwargs)
        if not callable(self._sink):
            return
        try:
            self._sink(event, data if data is not None else {})
        except Exception:
            logger.exception('Patient live event relay failed: %s', event)

    def sleep(self, *args, **kwargs):
        return self._socketio.sleep(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._socketio, name)


_PATIENT_SWARM_LIVE_EVENTS = frozenset({
    'agent.trace',
    'agent.progress',
    'agent.message',
    'agent.viewport',
    'agent.red_analysis',
    'agent.blue_analysis',
    'agent.green_analysis',
    'agent.phase_result',
    'agent.phase2_progress',
    'agent.phase3_progress',
    'agent.orchestrator_reply',
    'agent.report_ready',
    'agent.all_blue_selected',
    'agent.all_green_selected',
    'agent.error',
})


def _patient_run_is_cancelled(room: str, patient_state: dict) -> bool:
    return bool(
        agent_stop_flags.get(room, False)
        or patient_state.get('cancel_requested') is True
        or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
    )


def _project_patient_live_event(
    socketio,
    room: str,
    patient_state: dict,
    info: dict,
    source_event: str,
    source_payload: dict,
) -> None:
    """Copy an allowlisted worker-room event onto the parent session room."""
    event_name = str(source_event or '')
    if event_name.startswith('agent.roam_'):
        _project_patient_roam_event(
            socketio, room, patient_state, info, event_name, source_payload,
        )
        return
    if event_name not in _PATIENT_SWARM_LIVE_EVENTS:
        return
    if not isinstance(source_payload, dict):
        return
    if _patient_run_is_cancelled(room, patient_state):
        return
    try:
        payload = copy.deepcopy(source_payload)
    except Exception:
        payload = dict(source_payload)
    payload.update({
        'patient_id': patient_state.get('patient_id'),
        'file_id': str(info.get('file_id') or payload.get('file_id') or ''),
        'wsi_index': int(info.get('index') or 0),
        'run_token': patient_state.get('run_token'),
        'is_patient_level': True,
        'part': info.get('part') or payload.get('part'),
        'filename': info.get('filename') or payload.get('filename'),
    })
    socketio.emit(event_name, payload, to=room)
    socketio.sleep(0)


def _project_patient_roam_event(
    socketio,
    room: str,
    patient_state: dict,
    info: dict,
    source_event: str,
    source_payload: dict,
) -> None:
    """Project an allowlisted Single-Agent event from a private WSI room."""
    event_map = {
        'agent.roam_started': 'agent.patient_roam_started',
        'agent.roam_action': 'agent.patient_roam_action',
        'agent.roam_step': 'agent.patient_roam_step',
        'agent.roam_done': 'agent.patient_roam_done',
    }
    public_event = event_map.get(str(source_event or ''))
    if not public_event or not isinstance(source_payload, dict):
        return
    allowed = {
        'step', 'zoom', 'rect', 'observation', 'why', 'status', 'stopped_by',
        'steps', 'max_steps', 'strategy', 'sufficient', 'next',
        'x', 'y', 'label',
    }
    payload = {
        key: copy.deepcopy(value)
        for key, value in source_payload.items()
        if key in allowed
    }
    file_id = str(info.get('file_id') or '')
    payload.update({
        'patient_id': patient_state.get('patient_id'),
        'file_id': file_id,
        'wsi_index': int(info.get('index') or 0),
        'run_token': patient_state.get('run_token'),
        'part': info.get('part') or '',
        'filename': info.get('filename') or '',
    })
    kind = public_event.rsplit('_', 1)[-1]
    with _patient_worker_state_lock:
        if (
            agent_stop_flags.get(room, False)
            or patient_state.get('cancel_requested') is True
            or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
        ):
            return
        by_file = patient_state.setdefault('roam_by_file', {})
        entry = by_file.setdefault(file_id, {
            'run_token': patient_state.get('run_token'),
            'events': [],
            'status': 'running',
        })
        if entry.get('run_token') != patient_state.get('run_token'):
            return
        entry['events'].append({'kind': kind, 'payload': copy.deepcopy(payload)})
        entry['events'] = entry['events'][-100:]
        entry['status'] = payload.get('status') or (
            'completed' if kind == 'done' else 'running'
        )
        entry['updated_at'] = datetime.now().isoformat()
    socketio.emit(public_event, payload, to=room)
    socketio.sleep(0)


def _patient_report_text(wsi_result: dict) -> str:
    """Return the authoritative per-slide review text without changing the report."""
    if not wsi_result:
        return ''
    report_text = str(wsi_result.get('wsi_summary') or '').strip()
    output_paths = wsi_result.get('output_paths') or {}
    output_dir = output_paths.get('output_dir')
    if output_dir:
        report_path = Path(str(output_dir)) / 'pathology_report.txt'
        if report_path.exists():
            try:
                disk_text = report_path.read_text(encoding='utf-8').strip()
                if disk_text:
                    report_text = disk_text
            except Exception as exc:
                logger.warning("[Patient Collab] Failed to read slide report: %s", exc)
    return report_text


def _patient_report_url(wsi_result: dict, patient_dir_name: str, file_id: str) -> str:
    output_paths = (wsi_result or {}).get('output_paths') or {}
    report_url = str(output_paths.get('report_html') or '').strip()
    if report_url:
        return report_url
    output_dir = output_paths.get('output_dir')
    if output_dir:
        try:
            output_name = Path(str(output_dir)).name
            return f'/outputs/{patient_dir_name}/collab/{output_name}/pathology_report.html'
        except Exception:
            pass
    return f'/outputs/{patient_dir_name}/collab/wsi_{file_id}/pathology_report.html'


def _public_patient_review(review: dict) -> dict:
    """Strip internal values before a review record is emitted to the browser."""
    allowed = (
        'file_id', 'index', 'filename', 'part', 'status', 'source',
        'current_report', 'html_report_url', 'review_version',
        'feedback_history', 'evidence_additions', 'updated_at', 'error',
    )
    return {key: review.get(key) for key in allowed}


def _register_patient_slide_review(
    socketio,
    room: str,
    patient_state: dict,
    patient_dir_name: str,
    *,
    file_id: str,
    index: int,
    filename: str,
    part_name: str,
    wsi_result: dict,
    from_cache: bool,
    auto_confirm: bool = False,
) -> dict:
    """Create the mandatory pathologist gate for one WSI."""
    report_text = _patient_report_text(wsi_result)
    now = datetime.now().isoformat()
    review = {
        'file_id': file_id,
        'index': index,
        'filename': filename,
        'part': part_name,
        'status': 'confirmed' if auto_confirm else 'pending_review',
        'source': 'cache' if from_cache else 'analysis',
        'current_report': report_text,
        'html_report_url': _patient_report_url(
            wsi_result, patient_dir_name, file_id,
        ),
        'review_version': 1,
        'feedback_history': [],
        'evidence_additions': [],
        'updated_at': now,
        'error': '',
    }
    with _patient_collab_state_lock:
        patient_state.setdefault('reviews', {})[file_id] = review
        patient_state.setdefault('wsi_results', {})[file_id] = wsi_result
        patient_state['review_required'] = True
        patient_state['updated_at'] = now

    if not auto_confirm:
        payload = {
            'patient_id': patient_state.get('patient_id'),
            'run_token': patient_state.get('run_token'),
            'review': _public_patient_review(review),
        }
        socketio.emit('agent.patient_slide_review_ready', payload, to=room)
        socketio.sleep(0)
    return review


def _patient_slide_review_status(patient_state: dict, file_id: str) -> str:
    reviews = (patient_state or {}).get('reviews') or {}
    review = reviews.get(file_id) or reviews.get(str(file_id)) or {}
    return str(review.get('status') or '')


def _wait_for_serial_slide_review(
    socketio,
    room: str,
    patient_state: dict,
    file_id: str,
    expected_run_token: str,
    total_wsi: int,
) -> bool:
    """Serial collab: do not start the next WSI until this slide is confirmed.

    Roam has no intra-slide phase gates, so this is the pathologist stop.
    Swarm staged auto-confirms the slide review after its own gates, so
    this returns immediately.
    """
    file_id = str(file_id or '')
    last_counts = None
    while True:
        if (
            agent_stop_flags.get(room, False)
            or patient_state.get('cancel_requested') is True
            or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
            or str(patient_state.get('run_token') or '') != str(expected_run_token or '')
        ):
            return False
        status = _patient_slide_review_status(patient_state, file_id)
        if status in {'confirmed', 'error'}:
            return True
        if last_counts is None:
            logger.info(
                '[Patient Collab] 串行等待切片确认: file_id=%s status=%s',
                file_id,
                status or 'pending_review',
            )
        with _patient_collab_state_lock:
            reviews = list((patient_state.get('reviews') or {}).values())
            confirmed = sum(1 for item in reviews if item.get('status') == 'confirmed')
            revising = sum(1 for item in reviews if item.get('status') == 'revising')
            pending = len(reviews) - confirmed
        counts = (confirmed, pending, revising)
        if counts != last_counts:
            socketio.emit(
                'agent.patient_review_gate',
                {
                    'patient_id': patient_state.get('patient_id'),
                    'run_token': patient_state.get('run_token'),
                    'status': 'awaiting_review',
                    'hold_next_slide': True,
                    'waiting_file_id': file_id,
                    'total': total_wsi,
                    'confirmed': confirmed,
                    'pending': pending,
                    'revising': revising,
                    'errors': 0,
                },
                to=room,
            )
            socketio.sleep(0)
            last_counts = counts
        time.sleep(0.25)


def _run_patient_collaborative_slide_stage(
    socketio,
    room: str,
    patient_id: str,
    patient_state: dict,
    patient_sessions: dict,
    all_wsi_infos: list,
    total_wsi: int,
    folder_name: str,
    skip_file_ids: set,
    lang: str,
    wsi_execution_mode: str = 'parallel',
    analysis_strategy: str = 'agent_swarm',
    diagnostic_question: str = '',
) -> bool:
    """Analyze WSIs with the selected scheduler, then require slide review.

    Agent Swarm collaborative runs reuse ``run_collaborative_agent`` so each
    slide has the same staged/final gates as single-WSI Swarm. Single Agent
    roam still has no intra-slide gates; Patient Level collaboration for roam
    is the explicit review after each slide. After every slide is confirmed,
    the patient-level summary has its own review.
    """
    import concurrent.futures
    import threading

    wsi_execution_mode = str(wsi_execution_mode or '').strip().lower()
    if wsi_execution_mode not in {'parallel', 'serial'}:
        raise ValueError(f'Unsupported Patient Level execution mode: {wsi_execution_mode}')
    execution_is_parallel = wsi_execution_mode == 'parallel'
    analysis_strategy = str(analysis_strategy or 'agent_swarm').strip().lower()
    if analysis_strategy not in {'agent_swarm', 'single_agent_roam'}:
        raise ValueError(f'Unsupported Patient Level strategy: {analysis_strategy}')
    diagnostic_question = str(diagnostic_question or '').strip()

    # Use the shared lock and commit predicate when available; the local
    # fallbacks preserve the same stop/token rule when the function runs alone.
    commit_lock = globals().get('_patient_worker_state_lock') or threading.RLock()
    shared_commit_check = globals().get('_patient_result_can_commit')

    def result_can_commit(expected_token):
        if callable(shared_commit_check):
            return shared_commit_check(room, patient_state, expected_token)
        return not (
            agent_stop_flags.get(room, False)
            or patient_state.get('cancel_requested') is True
            or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
            or str(patient_state.get('run_token') or '') != str(expected_token or '')
        )

    patient_dir_name = (
        patient_id if patient_id.startswith('patient_')
        else f'patient_{patient_id}'
    )
    resolved_infos = []
    for idx in range(total_wsi):
        info = dict(all_wsi_infos[idx] if idx < len(all_wsi_infos) else {})
        info.setdefault('index', idx)
        info.setdefault('filename', f'WSI_{idx + 1}')
        info.setdefault('part', f'Slide {idx + 1}')
        file_id = info.get('file_id')
        if not file_id:
            file_id = self_wait_for_file_upload(
                patient_id,
                idx,
                patient_sessions,
                socketio,
                room,
                info['part'],
                info['filename'],
                lang=lang,
            )
            info['file_id'] = file_id
        if not file_id:
            error_review = {
                'file_id': f'missing-{idx}',
                'index': idx,
                'filename': info['filename'],
                'part': info['part'],
                'status': 'error',
                'source': 'upload',
                'current_report': '',
                'html_report_url': '',
                'review_version': 0,
                'feedback_history': [],
                'evidence_additions': [],
                'updated_at': datetime.now().isoformat(),
                'error': 'WSI upload timed out',
            }
            with _patient_collab_state_lock:
                patient_state.setdefault('reviews', {})[
                    error_review['file_id']
                ] = error_review
            socketio.emit(
                'agent.patient_slide_review_ready',
                {
                    'patient_id': patient_id,
                    'run_token': patient_state.get('run_token'),
                    'review': _public_patient_review(error_review),
                },
                to=room,
            )
            continue
        resolved_infos.append(info)

    patient_state['file_infos'] = resolved_infos
    patient_state['status'] = 'analyzing_slides'
    patient_state['review_required'] = True

    # Cached slides are not "done" in collaborative mode.  They enter the
    # same review queue as freshly analyzed slides.
    pending_infos = []
    for info in resolved_infos:
        file_id = info['file_id']
        if file_id not in skip_file_ids:
            pending_infos.append(info)
            continue
        cached_result = patient_state.get('wsi_results', {}).get(file_id) or {}
        _register_patient_slide_review(
            socketio,
            room,
            patient_state,
            patient_dir_name,
            file_id=file_id,
            index=info['index'],
            filename=info['filename'],
            part_name=info['part'],
            wsi_result=cached_result,
            from_cache=True,
        )
        socketio.emit(
            'agent.wsi_completed',
            {
                'index': info['index'],
                'file_id': file_id,
                'filename': info['filename'],
                'part': info['part'],
                'patient_id': patient_id,
                'run_token': patient_state.get('run_token'),
                'mode': 'collaborative',
                'from_cache': True,
                'requires_review': True,
                'html_report_url': _patient_report_url(
                    cached_result, patient_dir_name, file_id,
                ),
                'report_txt': _patient_report_text(cached_result),
            },
            to=room,
        )

    def analyze_one(info):
        idx = int(info['index'])
        file_id = info['file_id']
        worker_room = f'{room}:patient:{patient_id}:wsi:{idx}'
        if not _register_patient_worker(room, patient_state, worker_room):
            raise RuntimeError('Patient Level analysis was stopped before this slide started')
        socketio.emit(
            'agent.wsi_started',
            {
                'index': idx,
                'file_id': file_id,
                'filename': info['filename'],
                'part': info['part'],
                'patient_id': patient_id,
                'run_token': patient_state.get('run_token'),
                'parallel': execution_is_parallel,
                'execution_mode': wsi_execution_mode,
            },
            to=room,
        )
        clear_roi_cache()
        try:
            patient_event_sink = lambda event, payload, _info=info: _project_patient_live_event(
                socketio,
                room,
                patient_state,
                _info,
                event,
                payload,
            )
            slide_mode = 'collaborative'
            slide_strategy = 'agent_swarm'
            slide_review_policy = str(
                patient_state.get('collaboration_review_policy') or 'staged'
            )
            if analysis_strategy == 'single_agent_roam':
                # Intra-slide roam has no five-phase gates. Patient Level
                # collaboration is the review registered after this returns.
                slide_mode = 'ai'
                slide_strategy = 'single_agent_roam'
                slide_review_policy = 'final'
            result = run_single_wsi_for_patient(
                socketio=socketio,
                room=worker_room,
                file_id=file_id,
                part_name=info['part'],
                folder_name=folder_name,
                wsi_index=idx + 1,
                total_wsi=total_wsi,
                mode=slide_mode,
                patient_id=patient_id,
                lang=lang,
                analysis_strategy=slide_strategy,
                diagnostic_question=diagnostic_question,
                patient_event_sink=patient_event_sink,
                collaboration_review_policy=slide_review_policy,
            )
            return info, result
        finally:
            clear_agent_state(worker_room)
            _unregister_patient_worker(patient_state, worker_room)

    completed = len(skip_file_ids)
    failures = []
    expected_run_token = str(patient_state.get('run_token') or '')

    def commit_collaborative_result(info, wsi_result, error=None):
        nonlocal completed
        with commit_lock:
            if not result_can_commit(expected_run_token):
                return False
            if error is None:
                with _patient_collab_state_lock:
                    patient_state.setdefault('wsi_results', {})[
                        info['file_id']
                    ] = wsi_result
                    patient_state.setdefault('aggregated_findings', []).append({
                        'part': info['part'],
                        'filename': info['filename'],
                        'findings': wsi_result.get('pathology_findings', []),
                    })
                _register_patient_slide_review(
                    socketio,
                    room,
                    patient_state,
                    patient_dir_name,
                    file_id=info['file_id'],
                    index=info['index'],
                    filename=info['filename'],
                    part_name=info['part'],
                    wsi_result=wsi_result,
                    from_cache=False,
                    auto_confirm=analysis_strategy == 'agent_swarm',
                )
                socketio.emit(
                    'agent.wsi_completed',
                    {
                        'index': info['index'],
                        'file_id': info['file_id'],
                        'filename': info['filename'],
                        'part': info['part'],
                        'patient_id': patient_id,
                        'run_token': expected_run_token,
                        'mode': 'collaborative',
                        'parallel': execution_is_parallel,
                        'execution_mode': wsi_execution_mode,
                        'requires_review': True,
                        'html_report_url': _patient_report_url(
                            wsi_result, patient_dir_name, info['file_id'],
                        ),
                        'report_txt': _patient_report_text(wsi_result),
                        'result': {
                            'findings_count': len(
                                wsi_result.get('pathology_findings', [])
                            ),
                            'tree_nodes': wsi_result.get('tree_nodes', []),
                            'global_reasoning': wsi_result.get('global_reasoning', ''),
                            'trajectory': wsi_result.get('trajectory', []),
                            'report': (
                                wsi_result.get('report', '')
                                or wsi_result.get('wsi_summary', '')
                            ),
                        },
                    },
                    to=room,
                )
            else:
                failures.append(info['file_id'])
                error_review = {
                    'file_id': info['file_id'],
                    'index': info['index'],
                    'filename': info['filename'],
                    'part': info['part'],
                    'status': 'error',
                    'source': 'analysis',
                    'current_report': '',
                    'html_report_url': '',
                    'review_version': 0,
                    'feedback_history': [],
                    'evidence_additions': [],
                    'updated_at': datetime.now().isoformat(),
                    'error': str(error),
                }
                with _patient_collab_state_lock:
                    patient_state.setdefault('reviews', {})[
                        info['file_id']
                    ] = error_review
                socketio.emit(
                    'agent.patient_slide_review_ready',
                    {
                        'patient_id': patient_id,
                        'run_token': expected_run_token,
                        'review': _public_patient_review(error_review),
                    },
                    to=room,
                )
            completed += 1
            patient_state['completed_wsi'] = completed
            socketio.emit(
                'agent.patient_progress',
                {
                    'percentage': 10 + completed / max(total_wsi, 1) * 65,
                    'completed_wsi': completed,
                    'total_wsi': total_wsi,
                    'patient_id': patient_id,
                    'run_token': expected_run_token,
                    'execution_mode': wsi_execution_mode,
                    'message': _msg(
                        'slide_done', lang, done=completed, total=total_wsi,
                    ),
                },
                to=room,
            )
            return True

    def process_collaborative_info(info, future=None):
        try:
            if future is None:
                _, wsi_result = analyze_one(info)
            else:
                _, wsi_result = future.result()
            if not wsi_result:
                raise RuntimeError('WSI analysis returned no result')
            if not commit_collaborative_result(info, wsi_result):
                return
        except Exception as exc:
            if not commit_collaborative_result(info, None, error=exc):
                logger.info(
                    '[Patient Collab] Slide stopped: %s', info['filename'],
                )
                return
            logger.exception(
                '[Patient Collab] Slide analysis failed: %s', info['filename'],
            )
        socketio.sleep(0)

    if pending_infos and execution_is_parallel:
        # No product-level concurrency cap: every uploaded WSI may progress.
        # OpenSlide pixel reads remain protected by _openslide_lock, while
        # network/model work can run concurrently.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(pending_infos)),
            thread_name_prefix='patient-wsi',
        ) as pool:
            futures = {pool.submit(analyze_one, info): info for info in pending_infos}
            for future in concurrent.futures.as_completed(futures):
                info = futures[future]
                process_collaborative_info(info, future)
    elif pending_infos:
        for info in pending_infos:
            if not result_can_commit(expected_run_token):
                break
            process_collaborative_info(info)
            file_id = str(info.get('file_id') or '')
            if file_id not in (patient_state.get('reviews') or {}):
                break
            if not _wait_for_serial_slide_review(
                socketio,
                room,
                patient_state,
                file_id,
                expected_run_token,
                total_wsi,
            ):
                break

    if agent_stop_flags.get(room, False):
        patient_state['status'] = 'stopped'
        return False

    if failures or len(patient_state.get('reviews', {})) != total_wsi:
        patient_state['status'] = 'slide_analysis_failed'
        patient_state['error_file_ids'] = failures
        socketio.emit(
            'agent.patient_review_gate',
            {
                'patient_id': patient_id,
                'run_token': patient_state.get('run_token'),
                'status': 'blocked',
                'total': total_wsi,
                'confirmed': 0,
                'pending': total_wsi,
                'errors': len(failures),
            },
            to=room,
        )
        return False

    # Hard gate: a generated report is a draft, never an implicit confirmation.
    patient_state['status'] = 'awaiting_slide_review'
    last_counts = None
    while True:
        if agent_stop_flags.get(room, False):
            patient_state['status'] = 'stopped'
            return False
        with _patient_collab_state_lock:
            reviews = list((patient_state.get('reviews') or {}).values())
            confirmed = sum(1 for item in reviews if item.get('status') == 'confirmed')
            revising = sum(1 for item in reviews if item.get('status') == 'revising')
            pending = len(reviews) - confirmed
        counts = (confirmed, pending, revising)
        if counts != last_counts:
            patient_state['confirmed_wsi'] = confirmed
            patient_state['pending_review_wsi'] = pending
            socketio.emit(
                'agent.patient_review_gate',
                {
                    'patient_id': patient_id,
                    'status': (
                        'ready' if confirmed == total_wsi else 'awaiting_review'
                    ),
                    'total': total_wsi,
                    'confirmed': confirmed,
                    'pending': pending,
                    'revising': revising,
                    'errors': 0,
                },
                to=room,
            )
            socketio.sleep(0)
            last_counts = counts
        if confirmed == total_wsi:
            break
        time.sleep(0.25)

    patient_state['status'] = 'aggregating'
    patient_state['all_slides_confirmed_at'] = datetime.now().isoformat()
    return True


def _run_patient_automatic_slide_stage(
    socketio,
    room: str,
    patient_id: str,
    patient_state: dict,
    patient_sessions: dict,
    all_wsi_infos: list,
    total_wsi: int,
    folder_name: str,
    skip_file_ids: set,
    lang: str,
    mode: str = 'ai',
    analysis_strategy: str = 'agent_swarm',
    diagnostic_question: str = '',
    wsi_execution_mode: str = 'parallel',
) -> bool:
    """Analyze independent Patient Level WSIs with the selected scheduler.

    Every slide gets a private worker room so concurrent single-WSI state
    cannot collide. Allowlisted live ROI events are copied onto the parent
    session room with ``file_id`` / ``wsi_index`` so the browser can update
    the matching slide card, viewer overlays, and Subagent count.
    """
    import concurrent.futures
    import threading

    wsi_execution_mode = str(wsi_execution_mode or '').strip().lower()
    if wsi_execution_mode not in {'parallel', 'serial'}:
        raise ValueError(f'Unsupported Patient Level execution mode: {wsi_execution_mode}')
    execution_is_parallel = wsi_execution_mode == 'parallel'

    commit_lock = globals().get('_patient_worker_state_lock') or threading.RLock()
    shared_commit_check = globals().get('_patient_result_can_commit')

    def result_can_commit(expected_token):
        if callable(shared_commit_check):
            return shared_commit_check(room, patient_state, expected_token)
        return not (
            agent_stop_flags.get(room, False)
            or patient_state.get('cancel_requested') is True
            or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
            or str(patient_state.get('run_token') or '') != str(expected_token or '')
        )

    patient_dir_name = (
        patient_id if patient_id.startswith('patient_')
        else f'patient_{patient_id}'
    )
    skip_file_ids = set(skip_file_ids or ())
    patient_state['status'] = 'analyzing_slides'
    patient_state['review_required'] = False

    resolved_by_index = {}
    cached_infos = []
    pending_infos = []
    for idx in range(total_wsi):
        info = dict(all_wsi_infos[idx] if idx < len(all_wsi_infos) else {})
        info.setdefault('index', idx)
        info.setdefault('filename', f'WSI_{idx + 1}')
        info.setdefault('part', f'Slide {idx + 1}')
        resolved_by_index[idx] = info
        if info.get('file_id') in skip_file_ids:
            cached_infos.append(info)
        else:
            pending_infos.append(info)

    # A cached slide is already complete, but the browser still needs the same
    # completion event in order to populate its Patient Level slide card.
    for info in cached_infos:
        file_id = info['file_id']
        cached_result = patient_state.get('wsi_results', {}).get(file_id) or {}
        output_paths = cached_result.get('output_paths') or {}
        socketio.emit(
            'agent.wsi_completed',
            {
                'index': info['index'],
                'file_id': file_id,
                'filename': info['filename'],
                'part': info['part'],
                'patient_id': patient_id,
                'run_token': patient_state.get('run_token'),
                'mode': mode,
                'parallel': execution_is_parallel,
                'execution_mode': wsi_execution_mode,
                'from_cache': True,
                'html_report_url': output_paths.get('report_html', ''),
                'report_txt': _patient_report_text(cached_result),
                'result': {
                    'findings_count': len(
                        cached_result.get('pathology_findings', [])
                    ),
                    'statistics': cached_result.get('statistics', {}),
                    'tree_nodes': cached_result.get('tree_nodes', []),
                    'trajectory': cached_result.get('trajectory', []),
                    'report': (
                        cached_result.get('report', '')
                        or cached_result.get('wsi_summary', '')
                    ),
                },
            },
            to=room,
        )

    def analyze_one(raw_info):
        info = dict(raw_info)
        idx = int(info['index'])
        file_id = info.get('file_id')
        if not file_id:
            # Upload waits are independent: an uploaded slide can start while
            # another expected slide is still arriving.
            file_id = self_wait_for_file_upload(
                patient_id,
                idx,
                patient_sessions,
                socketio,
                room,
                info['part'],
                info['filename'],
                lang=lang,
            )
            info['file_id'] = file_id
        if not file_id:
            raise TimeoutError(f"WSI upload timed out: {info['part']}")

        worker_room = f'{room}:patient:{patient_id}:wsi:{idx}'
        if not _register_patient_worker(room, patient_state, worker_room):
            raise RuntimeError('Patient Level analysis was stopped before this slide started')
        socketio.emit(
            'agent.wsi_started',
            {
                'index': idx,
                'file_id': file_id,
                'filename': info['filename'],
                'part': info['part'],
                'patient_id': patient_id,
                'run_token': patient_state.get('run_token'),
                'mode': mode,
                'parallel': execution_is_parallel,
                'execution_mode': wsi_execution_mode,
            },
            to=room,
        )
        socketio.sleep(0)
        clear_roi_cache()
        try:
            patient_event_sink = lambda event, payload, _info=info: _project_patient_live_event(
                socketio,
                room,
                patient_state,
                _info,
                event,
                payload,
            )
            result = run_single_wsi_for_patient(
                socketio=socketio,
                room=worker_room,
                file_id=file_id,
                part_name=info['part'],
                folder_name=folder_name,
                wsi_index=idx + 1,
                total_wsi=total_wsi,
                mode=mode,
                patient_id=patient_id,
                lang=lang,
                analysis_strategy=analysis_strategy,
                diagnostic_question=diagnostic_question,
                patient_event_sink=patient_event_sink,
            )
            return info, result
        finally:
            # Patient-context runs intentionally keep their state for the old
            # sequential flow.  Private parallel worker rooms must not leak it.
            clear_agent_state(worker_room)
            _unregister_patient_worker(patient_state, worker_room)

    completed = len(cached_infos)
    failures = []
    expected_run_token = str(patient_state.get('run_token') or '')

    def commit_automatic_result(info, wsi_result, error=None):
        nonlocal completed
        with commit_lock:
            if not result_can_commit(expected_run_token):
                return False
            file_id = str(info.get('file_id') or '')
            if error is None:
                resolved_by_index[int(info['index'])] = info
                patient_state.setdefault('wsi_results', {})[file_id] = wsi_result
                patient_state.setdefault('aggregated_findings', []).append({
                    'part': info['part'],
                    'filename': info['filename'],
                    'findings': wsi_result.get('pathology_findings', []),
                })
                output_paths = wsi_result.get('output_paths') or {}
                report_url = str(output_paths.get('report_html') or '').strip()
                if not report_url:
                    mode_subdir = str(output_paths.get('mode_subdir') or '').strip()
                    if not mode_subdir:
                        mode_subdir = (
                            'single_agent_roam'
                            if analysis_strategy == 'single_agent_roam'
                            else 'auto'
                        )
                    out_dir = output_paths.get('output_dir')
                    if out_dir:
                        report_url = (
                            f'/outputs/{patient_dir_name}/{mode_subdir}/'
                            f'{Path(str(out_dir)).name}/pathology_report.html'
                        )
                    else:
                        report_url = (
                            f'/outputs/{patient_dir_name}/{mode_subdir}/'
                            f'wsi_{file_id}/pathology_report.html'
                        )
                socketio.emit(
                    'agent.wsi_completed',
                    {
                        'index': info['index'],
                        'file_id': file_id,
                        'filename': info['filename'],
                        'part': info['part'],
                        'patient_id': patient_id,
                        'run_token': expected_run_token,
                        'mode': mode,
                        'parallel': execution_is_parallel,
                        'execution_mode': wsi_execution_mode,
                        'html_report_url': report_url,
                        'report_txt': _patient_report_text(wsi_result),
                        'diagnostic_question': diagnostic_question,
                        'result': {
                            'findings_count': len(
                                wsi_result.get('pathology_findings', [])
                            ),
                            'tree_nodes': wsi_result.get('tree_nodes', []),
                            'global_reasoning': wsi_result.get('global_reasoning', ''),
                            'trajectory': wsi_result.get('trajectory', []),
                            'report': (
                                wsi_result.get('report', '')
                                or wsi_result.get('wsi_summary', '')
                            ),
                        },
                    },
                    to=room,
                )
            else:
                failed_id = file_id or f'missing-{info["index"]}'
                failures.append(failed_id)
                socketio.emit(
                    'agent.wsi_completed',
                    {
                        'index': info['index'],
                        'file_id': file_id,
                        'filename': info['filename'],
                        'part': info['part'],
                        'patient_id': patient_id,
                        'run_token': expected_run_token,
                        'mode': mode,
                        'parallel': execution_is_parallel,
                        'execution_mode': wsi_execution_mode,
                        'error': str(error),
                    },
                    to=room,
                )
            completed += 1
            patient_state['completed_wsi'] = completed
            socketio.emit(
                'agent.patient_progress',
                {
                    'percentage': 10 + completed / max(total_wsi, 1) * 80,
                    'completed_wsi': completed,
                    'total_wsi': total_wsi,
                    'patient_id': patient_id,
                    'run_token': expected_run_token,
                    'execution_mode': wsi_execution_mode,
                    'message': _msg(
                        'slide_done', lang, done=completed, total=total_wsi,
                    ),
                },
                to=room,
            )
            return True

    def process_automatic_info(submitted_info, future=None):
        info = dict(submitted_info)
        try:
            if future is None:
                info, wsi_result = analyze_one(info)
            else:
                info, wsi_result = future.result()
            if not wsi_result:
                raise RuntimeError('WSI analysis returned no result')
            if not commit_automatic_result(info, wsi_result):
                return
        except Exception as exc:
            if not commit_automatic_result(info, None, error=exc):
                logger.info(
                    '[Patient Auto] Slide stopped: %s',
                    info.get('filename') or info.get('file_id') or info.get('index'),
                )
                return
            logger.exception(
                '[Patient Auto] Slide analysis failed: %s',
                info.get('filename') or info.get('file_id') or info.get('index'),
            )
        socketio.sleep(0)

    if pending_infos and execution_is_parallel:
        # All WSI pipelines may progress concurrently. OpenSlide reads remain
        # protected by _openslide_lock; model/network waits overlap safely.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(pending_infos)),
            thread_name_prefix='patient-wsi-auto',
        ) as pool:
            futures = {
                pool.submit(analyze_one, info): info for info in pending_infos
            }
            for future in concurrent.futures.as_completed(futures):
                submitted_info = futures[future]
                process_automatic_info(submitted_info, future)

                if agent_stop_flags.get(room, False):
                    for worker_idx in range(total_wsi):
                        agent_stop_flags[
                            f'{room}:patient:{patient_id}:wsi:{worker_idx}'
                        ] = True
    elif pending_infos:
        for info in pending_infos:
            if not result_can_commit(expected_run_token):
                break
            process_automatic_info(info)

    patient_state['file_infos'] = [
        resolved_by_index[idx] for idx in sorted(resolved_by_index)
    ]
    patient_state['error_file_ids'] = failures
    if agent_stop_flags.get(room, False):
        patient_state['status'] = 'stopped'
        return False
    patient_state['status'] = (
        'aggregating_with_slide_errors' if failures else 'aggregating'
    )
    return True


def run_patient_level_agent(socketio, room: str, patient_id: str, file_ids: list, 
                            file_infos: list, folder_name: str, mode: str = "ai",
                            total_expected_wsi: int = None, patient_sessions: dict = None,
                            lang: str = 'zh', cache_action: str = '',
                            skip_file_ids: list = None,
                            analysis_strategy: str = 'agent_swarm',
                            collaboration_review_policy: str = None,
                            diagnostic_question: str = '',
                            wsi_execution_mode: str = 'parallel') -> None:
    """
    运行Patient Level多WSI分析，并在全部结束后进行患者级汇总
    
    Args:
        socketio: SocketIO实例
        room: WebSocket房间ID (session_id)
        patient_id: 患者ID
        file_ids: 当前已上传的WSI文件ID列表
        file_infos: 所有预期WSI文件信息列表 [{file_id, filename, part, index}, ...]
        folder_name: 患者文件夹名称
        mode: 分析模式 ('ai' 或 'collaborative')
        total_expected_wsi: 预期的WSI总数（支持提前启动：部分上传即可开始分析）
        patient_sessions: 共享的患者会话字典（用于接收后续上传的文件）
        lang: 语言设置 ('zh' 中文, 'en' 英文)
    """
    try:
        analysis_strategy = str(analysis_strategy or '').strip().lower()
        if analysis_strategy not in {'agent_swarm', 'single_agent_roam'}:
            raise ValueError(f'Unsupported Patient Level strategy: {analysis_strategy}')
        wsi_execution_mode = str(wsi_execution_mode or '').strip().lower()
        if wsi_execution_mode not in {'parallel', 'serial'}:
            raise ValueError(
                f'Unsupported Patient Level execution mode: {wsi_execution_mode}'
            )
        if mode == 'collaborative':
            requested_policy = str(collaboration_review_policy or '').strip().lower()
            if analysis_strategy == 'single_agent_roam':
                collaboration_review_policy = 'final'
            elif requested_policy in {'staged', 'final'}:
                collaboration_review_policy = requested_policy
            else:
                collaboration_review_policy = 'staged'
        else:
            collaboration_review_policy = None
        total_wsi = total_expected_wsi or len(file_ids)
        uploaded_count = len(file_ids)
        logger.info(f"\n{'='*60}")
        logger.info(f"🏥 启动Patient Level分析")
        logger.info(f"   患者: {folder_name}")
        logger.info(f"   预期切片数: {total_wsi} (已上传: {uploaded_count})")
        logger.info(f"   模式: {'人机协作' if mode == 'collaborative' else '纯AI'}")
        logger.info(f"   执行路线: {analysis_strategy}")
        logger.info(f"   切片调度: {wsi_execution_mode}")
        logger.info(f"{'='*60}\n")
        
        # 创建患者输出目录（前端生成的 patient_id 已带 'patient_' 前缀）
        patient_dir_name = patient_id if patient_id.startswith('patient_') else f"patient_{patient_id}"
        patient_output_dir = Path(f"outputs/{patient_dir_name}")
        patient_output_dir.mkdir(parents=True, exist_ok=True)
        
        # ⭐ 构建完整的文件列表（按 index 排序），包含已上传和待上传的
        # file_infos 包含所有预期文件，其中 file_id 可能为 null
        all_wsi_infos = sorted(file_infos, key=lambda x: x.get('index', 0))
        # 确保至少有 total_wsi 个条目
        while len(all_wsi_infos) < total_wsi:
            all_wsi_infos.append({
                'index': len(all_wsi_infos),
                'filename': f'WSI_{len(all_wsi_infos)+1}',
                'part': f'Slide {len(all_wsi_infos)+1}',
                'file_id': None
            })
        
        # 初始化患者状态
        patient_state = {
            'patient_id': patient_id,
            'folder_name': folder_name,
            'total_wsi': total_wsi,
            'completed_wsi': 0,
            'file_infos': file_infos,
            'wsi_results': {},
            'aggregated_findings': [],
            'all_evidence': [],
            'lang': lang,  # ⭐ 语言设置
            'analysis_strategy': analysis_strategy,
            'collaboration_review_policy': collaboration_review_policy,
            'diagnostic_question': diagnostic_question,
            'wsi_execution_mode': wsi_execution_mode,
        }
        # Reuse the state object created by the API route.  The browser review
        # endpoints and this worker must observe the same reviews/status map.
        if patient_sessions is not None:
            shared_state = patient_sessions.get(patient_id)
            if isinstance(shared_state, dict):
                was_cancelled = _patient_execution_already_cancelled(shared_state)
                shared_update = {
                    'folder_name': folder_name,
                    'total_wsi': total_wsi,
                    'completed_wsi': 0,
                    'file_infos': file_infos,
                    'wsi_results': {},
                    'aggregated_findings': [],
                    'all_evidence': [],
                    'reviews': {},
                    'review_required': mode == 'collaborative',
                    'analysis_strategy': analysis_strategy,
                    'collaboration_review_policy': collaboration_review_policy,
                    'diagnostic_question': diagnostic_question,
                    'wsi_execution_mode': wsi_execution_mode,
                    'lang': lang,
                    'updated_at': datetime.now().isoformat(),
                }
                if not was_cancelled:
                    shared_update['status'] = 'initializing'
                shared_state.update(shared_update)
                patient_state = shared_state

        if not _begin_patient_level_execution(room, patient_state):
            _finish_patient_level_stop(socketio, room, patient_state)
            return
        
        # ⭐⭐⭐ 断点续传：检测哪些切片已有分析结果 ⭐⭐⭐
        cached_wsi_list = []  # 有缓存的切片列表
        _param_skip_file_ids = list(skip_file_ids or [])  # 保存前端传入的原始参数
        skip_file_ids = set()  # 用户选择跳过的 file_id 集合（后续流程填充）
        
        cache_dir = Path("uploads/analysis_cache")
        index_file = cache_dir / '_index.json'
        mode_suffix = (
            'single_agent_roam' if analysis_strategy == 'single_agent_roam'
            else ('collaborative' if mode == 'collaborative' else 'ai')
        )
        normalized_diagnostic_question = ' '.join(
            str(diagnostic_question or '').casefold().split()
        )
        diagnostic_question_fingerprint = hashlib.sha256(
            normalized_diagnostic_question.encode('utf-8')
        ).hexdigest()
        
        for wsi_info in all_wsi_infos:
            fid = wsi_info.get('file_id')
            wsi_filename = wsi_info.get('filename', '')
            if not fid:
                continue
            
            has_cache = False
            cached_time = ''
            matched_tree_path = None
            
            # 1. 按 file_id 查找 state.json 缓存（支持可读文件名 + 旧 UUID 文件名）
            _cache_candidates = [cache_dir / f'{fid}_{mode_suffix}_state.json', cache_dir / f'{fid}_state.json']
            if index_file.exists():
                try:
                    with open(index_file, 'r', encoding='utf-8') as _if2:
                        _idx2 = json.load(_if2)
                    _idx_name2 = _idx2.get(f'{fid}_{mode_suffix}')
                    if _idx_name2:
                        _cache_candidates.insert(0, cache_dir / _idx_name2)
                except Exception:
                    pass
            cache_file = None
            for _cc in _cache_candidates:
                if _cc.exists():
                    try:
                        with open(_cc, 'r', encoding='utf-8') as _cf:
                            _cache_identity = json.load(_cf)
                        cache_matches_file = (
                            str(_cache_identity.get('file_id') or '') == str(fid)
                        )
                        cache_matches_question = True
                        if analysis_strategy == 'single_agent_roam':
                            cached_fingerprint = str(
                                _cache_identity.get('diagnostic_question_fingerprint')
                                or ''
                            ).strip()
                            cache_matches_question = (
                                cached_fingerprint == diagnostic_question_fingerprint
                            )
                        if cache_matches_file and cache_matches_question:
                            cache_file = _cc
                            break
                        if cache_matches_file and not cache_matches_question:
                            logger.info(
                                '[断点续传] Single Agent 缓存问题不匹配，忽略: %s',
                                _cc,
                            )
                            continue
                        logger.warning(
                            "[断点续传] 忽略 file_id 不匹配的缓存: %s",
                            _cc,
                        )
                    except Exception:
                        continue
            if cache_file:
                has_cache = True
                try:
                    cached_time = datetime.fromtimestamp(cache_file.stat().st_mtime).strftime('%m/%d %H:%M')
                except:
                    pass
            
            # 2. 按 file_id 查找 analysis_tree.json
            if not has_cache and analysis_strategy != 'single_agent_roam':
                output_dir = find_output_dir_for_file_by_id(fid)
                if output_dir:
                    for tree_candidate in [output_dir / 'analysis_tree.json',
                                           output_dir / 'auto' / 'analysis_tree.json',
                                           output_dir / 'collab' / 'analysis_tree.json']:
                        if tree_candidate.exists():
                            has_cache = True
                            matched_tree_path = tree_candidate
                            try:
                                cached_time = datetime.fromtimestamp(tree_candidate.stat().st_mtime).strftime('%m/%d %H:%M')
                            except:
                                pass
                            break
            
            if has_cache:
                cached_wsi_list.append({
                    'file_id': fid,
                    'filename': wsi_filename,
                    'part': wsi_info.get('part', ''),
                    'index': wsi_info.get('index', 0),
                    'cached_at': cached_time,
                    'matched_tree_path': str(matched_tree_path) if matched_tree_path else ''
                })
        
        # 如果有缓存，根据前端决定或走旧弹窗流程
        if cached_wsi_list:
            logger.info(f"[Patient Level] 🔍 检测到 {len(cached_wsi_list)}/{total_wsi} 张切片已有分析结果")
            
            # ⭐ 前端已做缓存预检决定：直接使用，不再弹窗
            if cache_action == 'use_selected':
                _frontend_skip_ids = set(_param_skip_file_ids)
                action = 'skip_cached'
                logger.info(f"[Patient Level] 前端已决定: use_selected, skip_file_ids={_frontend_skip_ids}")
            elif cache_action == 'rerun_all':
                action = 'rerun_all'
                logger.info("[Patient Level] 前端已决定: rerun_all")
            else:
                # 旧流程：发送事件到前端，等待用户决定
                cache_gate_id = uuid.uuid4().hex
                temp_state = {
                    'cache_decision': None,
                    'cache_gate_id': cache_gate_id,
                    'patient_id': patient_id,
                    'run_token': patient_state.get('run_token'),
                    'cache_gate_consumed': False,
                }
                with _patient_worker_state_lock:
                    set_agent_state(room, temp_state)
                socketio.emit('agent.patient_cache_check', {
                    'cached_slides': cached_wsi_list,
                    'total_wsi': total_wsi,
                    'folder_name': folder_name,
                    'mode': mode,
                    'patient_id': patient_id,
                    'run_token': patient_state.get('run_token'),
                    'cache_gate_id': cache_gate_id,
                }, to=room)
                socketio.sleep(0)
                
                start_time = time.time()
                timeout = 120
                while temp_state.get('cache_decision') is None:
                    if agent_stop_flags.get(room, False):
                        with _patient_worker_state_lock:
                            temp_state['cache_gate_consumed'] = True
                        logger.info("[Patient Level] 用户取消，停止分析")
                        _finish_patient_level_stop(socketio, room, patient_state)
                        return
                    if time.time() - start_time > timeout:
                        logger.warning("[Patient Level] 等待用户决定超时，默认跳过已有结果")
                        with _patient_worker_state_lock:
                            if temp_state.get('cache_decision') is None:
                                temp_state['cache_decision'] = {'action': 'skip_cached'}
                                temp_state['cache_gate_consumed'] = True
                        break
                    socketio.sleep(0.5)
                
                decision = temp_state.get('cache_decision', {})
                action = decision.get('action', 'skip_cached')
                with _patient_worker_state_lock:
                    if get_agent_state(room) is temp_state:
                        clear_agent_state(room)
            
            if action == 'skip_cached':
                if cache_action == 'use_selected':
                    skip_file_ids = _frontend_skip_ids
                else:
                    skip_file_ids = set(item['file_id'] for item in cached_wsi_list)
                logger.info(f"[Patient Level] ✅ 跳过 {len(skip_file_ids)} 张已有结果的切片")
                
                # 加载已有结果到 patient_state
                for cached_info in cached_wsi_list:
                    fid = cached_info['file_id']
                    matched_tree = cached_info.get('matched_tree_path', '')
                    loaded_result = _load_cached_wsi_result(fid, mode_suffix, matched_tree_path=matched_tree)
                    if loaded_result:
                        patient_state['wsi_results'][fid] = loaded_result
                        if loaded_result.get('pathology_findings'):
                            patient_state['aggregated_findings'].append({
                                'part': cached_info.get('part', ''),
                                'filename': cached_info.get('filename', ''),
                                'findings': loaded_result['pathology_findings']
                            })
                        logger.info(f"[Patient Level] 📦 已加载缓存: {cached_info.get('filename', fid)}")
                    else:
                        # 缓存加载失败，不跳过这张
                        skip_file_ids.discard(fid)
                        logger.warning(f"[Patient Level] ⚠️ 缓存加载失败，将重新分析: {cached_info.get('filename', fid)}")
                
                patient_state['completed_wsi'] = len(skip_file_ids)
                
                # ⭐ 仅对被跳过复用的切片复制输出目录（不复制即将重新分析的切片）
                mode_subdir = (
                    'single_agent_roam' if analysis_strategy == 'single_agent_roam'
                    else ('collab' if mode == 'collaborative' else 'auto')
                )
                for cached_info in cached_wsi_list:
                    if analysis_strategy == 'single_agent_roam':
                        # Roam cache is a trajectory snapshot, not an R-B-G
                        # output directory.  Never copy an unrelated Swarm
                        # tree merely because the same WSI also has one.
                        continue
                    fid = cached_info.get('file_id', '')
                    if fid not in skip_file_ids:
                        continue
                    matched_tree = cached_info.get('matched_tree_path', '')
                    wsi_filename = cached_info.get('filename', '')
                    
                    # 如果没有 matched_tree_path（state.json 匹配的情况），尝试查找 analysis_tree.json
                    if not matched_tree:
                        # 按 file_id 查找
                        _out_dir = find_output_dir_for_file_by_id(fid) if fid else None
                        if _out_dir:
                            for _tp in [_out_dir / 'analysis_tree.json', _out_dir / 'auto' / 'analysis_tree.json']:
                                if _tp.exists():
                                    matched_tree = str(_tp)
                                    break
                    if matched_tree:
                        old_wsi_dir = Path(matched_tree).parent  # analysis_tree.json 所在目录
                        # 检查是否已在当前 patient 文件夹下
                        if str(patient_output_dir) not in str(old_wsi_dir):
                            new_wsi_dir = patient_output_dir / mode_subdir / old_wsi_dir.name
                            if not new_wsi_dir.exists():
                                try:
                                    shutil.copytree(str(old_wsi_dir), str(new_wsi_dir))
                                    logger.info(f"[Patient Level] 📂 已复制旧结果到当前文件夹: {old_wsi_dir.name}")
                                except Exception as e:
                                    logger.warning(f"[Patient Level] ⚠️ 复制旧结果失败: {e}")
                            else:
                                logger.info(f"[Patient Level] 📂 目标已存在，跳过复制: {new_wsi_dir.name}")
                        else:
                            logger.info(f"[Patient Level] 📂 结果已在当前文件夹: {old_wsi_dir.name}")
                    else:
                        logger.warning(f"[Patient Level] ⚠️ 未找到 {wsi_filename} 的输出目录，无法复制")
                
                # ⭐ 如果所有切片都有缓存，生成聚合树后通知前端再结束
                current_cached_report = (
                    patient_output_dir / mode_subdir / 'ai_final_report.txt'
                )
                if (
                    len(skip_file_ids) == total_wsi
                    and mode != 'collaborative'
                    and current_cached_report.exists()
                ):
                    logger.info(f"[Patient Level] ✅ 全部 {total_wsi} 张切片已从缓存加载")
                    
                    # ⭐ 聚合各 WSI 的 analysis_tree.json → 患者级别树（与完整分析流程逻辑一致）
                    import json as json_module
                    patient_tree_url = None
                    cached_tree_staging_path = None
                    cached_tree_final_path = None
                    ai_report_text = ''
                    try:
                        patient_tree = {
                            'metadata': {
                                'patient_id': patient_id,
                                'folder_name': folder_name,
                                'total_wsi': total_wsi,
                                'analysis_time': datetime.now().isoformat(),
                                'ai_final_report': ''
                            },
                            'tree_nodes': []
                        }
                        
                        for f_info in file_infos:
                            fid = f_info.get('file_id')
                            if not fid:
                                continue
                            
                            wsi_tree_path = None
                            _wsi_res = patient_state.get('wsi_results', {}).get(fid) or {}
                            wsi_output_paths = _wsi_res.get('output_paths', {})
                            if wsi_output_paths:
                                wsi_output_dir = wsi_output_paths.get('output_dir')
                                if wsi_output_dir:
                                    wsi_tree_path = Path(wsi_output_dir) / 'analysis_tree.json'
                            
                            if not wsi_tree_path or not wsi_tree_path.exists():
                                search_dir = patient_output_dir / mode_subdir
                                if search_dir.exists():
                                    for d in search_dir.glob('wsi_*'):
                                        candidate = d / 'analysis_tree.json'
                                        if candidate.exists():
                                            try:
                                                with open(candidate, 'r', encoding='utf-8') as check_f:
                                                    check_data = json_module.load(check_f)
                                                    if check_data.get('metadata', {}).get('file_id') == fid:
                                                        wsi_tree_path = candidate
                                                        break
                                            except:
                                                pass
                            
                            if not wsi_tree_path or not wsi_tree_path.exists():
                                wsi_tree_path = patient_output_dir / mode_subdir / f'wsi_{fid}' / 'analysis_tree.json'
                            if not wsi_tree_path or not wsi_tree_path.exists():
                                wsi_tree_path = Path(f"outputs/{fid}/{mode_subdir}/analysis_tree.json")
                            if not wsi_tree_path or not wsi_tree_path.exists():
                                wsi_tree_path = Path(f"outputs/{fid}/analysis_tree.json")
                            
                            if (
                                analysis_strategy != 'single_agent_roam'
                                and wsi_tree_path
                                and wsi_tree_path.exists()
                            ):
                                with open(wsi_tree_path, 'r', encoding='utf-8') as tf:
                                    wsi_tree = json_module.load(tf)
                                
                                wsi_thumbnail_url = wsi_tree.get('metadata', {}).get('thumbnail_url', '')
                                if not wsi_thumbnail_url:
                                    wsi_steps_dir = wsi_tree_path.parent / 'steps'
                                    if wsi_steps_dir.exists():
                                        thumb_patterns = list(wsi_steps_dir.glob('thumbnail_*.jpg')) + list(wsi_steps_dir.glob('step_0_*.jpg'))
                                        if thumb_patterns:
                                            wsi_dir_name = wsi_tree_path.parent.name
                                            wsi_thumbnail_url = f'/outputs/{patient_dir_name}/{mode_subdir}/{wsi_dir_name}/steps/{thumb_patterns[0].name}'
                                
                                wsi_idx_num = len(patient_tree['tree_nodes']) + 1
                                patient_tree['tree_nodes'].append({
                                    'roi_id': f'Slide {wsi_idx_num}',
                                    'level': 'wsi',
                                    'filename': f_info.get('filename', ''),
                                    'part_name': f_info.get('part', f'Slide {wsi_idx_num}'),
                                    'file_id': fid,
                                    'thumbnail_url': wsi_thumbnail_url,
                                    'children': wsi_tree.get('tree_nodes', []),
                                    'analysis': {
                                        'findings': wsi_tree.get('metadata', {}).get('global_analysis', ''),
                                        'reasoning': ''
                                    },
                                    'metadata': wsi_tree.get('metadata', {})
                                })
                                logger.info(f"[Patient Level All-Cached] ✅ 聚合 WSI 树: {f_info.get('part', fid)}, {len(wsi_tree.get('tree_nodes', []))} 个红框")
                            elif analysis_strategy == 'single_agent_roam' and _wsi_res:
                                trajectory = list(_wsi_res.get('trajectory') or [])
                                roam_children = []
                                for step_index, step in enumerate(trajectory, start=1):
                                    payload = step.get('payload') if isinstance(step, dict) else None
                                    item = payload if isinstance(payload, dict) else step
                                    if not isinstance(item, dict):
                                        continue
                                    rect = item.get('rect')
                                    if not rect:
                                        continue
                                    zoom = int(item.get('zoom') or 10)
                                    roam_children.append({
                                        'roi_id': f'Roam-{step_index}',
                                        'level': 'roam_step',
                                        'rect': rect,
                                        'magnification': zoom,
                                        'analysis': {
                                            'findings': str(item.get('observation') or ''),
                                            'reasoning': str(item.get('why') or ''),
                                        },
                                        'children': [],
                                        'metadata': {
                                            'step': int(item.get('step') or step_index),
                                            'strategy': 'single_agent_roam',
                                        },
                                    })
                                wsi_idx_num = len(patient_tree['tree_nodes']) + 1
                                patient_tree['tree_nodes'].append({
                                    'roi_id': f'Slide {wsi_idx_num}',
                                    'level': 'wsi_single_agent',
                                    'filename': f_info.get('filename', ''),
                                    'part_name': f_info.get('part', f'Slide {wsi_idx_num}'),
                                    'file_id': fid,
                                    'thumbnail_url': '',
                                    'children': roam_children,
                                    'analysis': {
                                        'findings': str(
                                            _wsi_res.get('report')
                                            or _wsi_res.get('wsi_summary')
                                            or ''
                                        ),
                                        'reasoning': 'single_agent_roam',
                                    },
                                    'metadata': {
                                        'strategy': 'single_agent_roam',
                                        'steps': len(roam_children),
                                    },
                                })
                                logger.info(
                                    '[Patient Level All-Cached] ✅ 聚合 Single Agent 轨迹: %s, %s 个视野',
                                    f_info.get('part', fid),
                                    len(roam_children),
                                )
                            else:
                                logger.warning(f"[Patient Level All-Cached] ⚠️ WSI 树文件不存在: {wsi_tree_path}")
                        
                        # Reuse only a report already published for this exact
                        # Patient object.  Never scan other patient_* folders:
                        # matching folder names are not an authorization or an
                        # evidence identity boundary.
                        current_report_path = (
                            patient_output_dir / mode_subdir / 'ai_final_report.txt'
                        )
                        if current_report_path.exists():
                            try:
                                text = current_report_path.read_text(encoding='utf-8')
                                lines = [line for line in text.split('\n') if not line.startswith('#')]
                                ai_report_text = '\n'.join(lines).strip()
                            except Exception:
                                logger.warning(
                                    '[Patient Level All-Cached] 当前患者旧报告读取失败',
                                    exc_info=True,
                                )
                        if ai_report_text:
                            patient_tree['metadata']['ai_final_report'] = ai_report_text
                            logger.info(f"[Patient Level All-Cached] ✅ 找到以前的 AI 报告，长度: {len(ai_report_text)}")
                        
                        patient_tree_dir = patient_output_dir / mode_subdir
                        patient_tree_dir.mkdir(parents=True, exist_ok=True)
                        cached_tree_final_path = patient_tree_dir / 'patient_analysis_tree.json'
                        cached_tree_staging_path = patient_tree_dir / '.patient_analysis_tree.json.tmp'
                        with open(cached_tree_staging_path, 'w', encoding='utf-8') as tf:
                            json_module.dump(patient_tree, tf, ensure_ascii=False, indent=2)
                        logger.info(
                            "[Patient Level All-Cached] ✅ 患者级聚合树已写入待发布文件: %s",
                            cached_tree_staging_path,
                        )
                        patient_tree_url = f'/api/agent/patient-analysis-tree/{patient_id}'
                    except Exception as e:
                        logger.warning(f"[Patient Level All-Cached] 聚合树生成失败: {e}")
                        import traceback
                        logger.warning(traceback.format_exc())
                    
                    with _patient_worker_state_lock:
                        cache_publish_cancelled = (
                            agent_stop_flags.get(room, False)
                            or patient_state.get('cancel_requested') is True
                            or patient_state.get('status') in {'stopping', 'stopped', 'cancelled'}
                        )
                        if not cache_publish_cancelled:
                            if cached_tree_staging_path and cached_tree_final_path:
                                os.replace(cached_tree_staging_path, cached_tree_final_path)
                            patient_state['status'] = 'completed'
                            patient_state['all_cached'] = True
                            patient_state['ai_final_report'] = ai_report_text
                            patient_state['completed_at'] = datetime.now().isoformat()
                            patient_state['updated_at'] = patient_state['completed_at']
                    if cache_publish_cancelled:
                        if cached_tree_staging_path:
                            try:
                                Path(cached_tree_staging_path).unlink(missing_ok=True)
                            except Exception:
                                logger.warning(
                                    '[Patient Level All-Cached] 待发布树清理失败',
                                    exc_info=True,
                                )
                        _finish_patient_level_stop(socketio, room, patient_state)
                        return

                    socketio.emit('agent.patient_level_all_cached', {
                        'cached_slides': cached_wsi_list,
                        'mode': mode,
                        'folder_name': folder_name,
                        'patient_id': patient_id,
                        'run_token': patient_state.get('run_token'),
                        'tree_url': patient_tree_url,
                        'status': 'completed',
                        'summary': ai_report_text,
                        'report_url': (
                            f'/api/patient/{patient_id}/report/word'
                            if any(patient_output_dir.glob('*.docx')) else None
                        ),
                        'html_report_url': (
                            f'/outputs/{patient_id}/{mode_subdir}/patient_report.html'
                            if (patient_output_dir / mode_subdir / 'patient_report.html').exists()
                            else None
                        ),
                    }, to=room)
                    socketio.sleep(0)
                    return
            else:
                # 用户选择全部重新分析
                logger.info("[Patient Level] 🔄 用户选择全部重新分析")
        
        # 发送初始进度
        skipped_count = len(skip_file_ids)
        start_msg = _msg('patient_start_total', lang, folder_name=folder_name, total_wsi=total_wsi)
        if skipped_count > 0:
            start_msg = _msg('patient_start_skipped', lang, folder_name=folder_name, skipped=skipped_count, remaining=total_wsi - skipped_count)
        
        socketio.emit('agent.patient_progress', {
            'percentage': 5 + skipped_count / total_wsi * 80 if skipped_count > 0 else 5,
            'completed_wsi': skipped_count,
            'total_wsi': total_wsi,
            'patient_id': patient_id,
            'run_token': patient_state.get('run_token'),
            'message': start_msg
        }, to=room)
        socketio.sleep(0)

        if mode == 'collaborative':
            slide_stage_complete = _run_patient_collaborative_slide_stage(
                socketio=socketio,
                room=room,
                patient_id=patient_id,
                patient_state=patient_state,
                patient_sessions=patient_sessions,
                all_wsi_infos=all_wsi_infos,
                total_wsi=total_wsi,
                folder_name=folder_name,
                skip_file_ids=skip_file_ids,
                lang=lang,
                wsi_execution_mode=wsi_execution_mode,
                analysis_strategy=analysis_strategy,
                diagnostic_question=diagnostic_question,
            )
        else:
            slide_stage_complete = _run_patient_automatic_slide_stage(
                socketio=socketio,
                room=room,
                patient_id=patient_id,
                patient_state=patient_state,
                patient_sessions=patient_sessions,
                all_wsi_infos=all_wsi_infos,
                total_wsi=total_wsi,
                folder_name=folder_name,
                skip_file_ids=skip_file_ids,
                lang=lang,
                mode=mode,
                analysis_strategy=analysis_strategy,
                diagnostic_question=diagnostic_question,
                wsi_execution_mode=wsi_execution_mode,
            )
        if not slide_stage_complete:
            if agent_stop_flags.get(room, False):
                _finish_patient_level_stop(socketio, room, patient_state)
            return
        
        # 所有WSI分析完成，生成综合报告
        logger.info(f"\n{'='*60}")
        logger.info(f"所有WSI分析完成，开始生成患者级别综合报告")
        logger.info(f"{'='*60}")
        
        # ⭐ 根据模式确定输出子目录
        mode_subdir = (
            'single_agent_roam' if analysis_strategy == 'single_agent_roam'
            else ('collab' if mode == 'collaborative' else 'auto')
        )
        
        socketio.emit('agent.patient_progress', {
            'percentage': 90,
            'completed_wsi': total_wsi,
            'total_wsi': total_wsi,
            'patient_id': patient_id,
            'run_token': patient_state.get('run_token'),
            'message': _msg('generating_patient_report', lang)
        }, to=room)
        
        # ⭐⭐⭐ 调用 AI 生成最终综合报告
        ai_final_report = None
        try:
            from agent.gpt_client import call_gpt_vision
            
            # 1. ⭐ 收集各 WSI 的结构化证据摘要（不再传入完整路径 reasoning，避免 token 溢出）
            # 每张 WSI 的 wsi_summary 已经是增强版结构化证据摘要（含 R-B-G 追溯），
            # 完整路径 reasoning 仍保存在 analysis_tree.json 中可随时查阅。
            # ⭐ 关键：按 index 排序，与分析循环（all_wsi_infos）顺序一致，确保 S 编号对应正确的切片
            sorted_file_infos = sorted(
                patient_state.get('file_infos') or file_infos,
                key=lambda x: x.get('index', 0),
            )
            wsi_summaries_parts = []
            total_chars = 0
            for idx, f_info in enumerate(sorted_file_infos, start=1):
                fid = f_info.get('file_id')
                part_name = f_info.get('part', f_info.get('filename', fid))
                _s_id = f"S{idx}"
                
                if not (patient_state.get('wsi_results', {}).get(fid) if fid else None):
                    logger.warning(f"  ⚠️ {part_name} ({_s_id}): 该切片分析失败，汇总中标记为未提供证据")
                    _failed_note = (
                        'Analysis of this slide failed; no evidence is available.'
                        if patient_state.get('lang') == 'en' else '该切片分析失败，未提供证据。'
                    )
                    wsi_summaries_parts.append(f"\n### 📋 {part_name}（{_s_id}）\n{_failed_note}\n")
                    continue

                if fid and fid in patient_state.get('wsi_results', {}):
                    wsi_result = patient_state['wsi_results'][fid]
                    stats = wsi_result.get('statistics', {})
                    wsi_summary_brief = wsi_result.get('wsi_summary', '')
                    is_roam_slide = (
                        wsi_result.get('strategy') == 'single_agent_roam'
                        or patient_state.get('analysis_strategy') == 'single_agent_roam'
                        or bool(wsi_result.get('trajectory'))
                    )
                    if is_roam_slide:
                        roam_stats = _roam_stats_from_result(wsi_result)
                        stats_line = (
                            f"**统计**: 单 Agent 游走 {roam_stats['fovs']} 个观察视野"
                            f"（独立取样 {roam_stats['independent'] or roam_stats['fovs']}，无红/蓝/绿框）"
                        )
                    else:
                        stats_line = (
                            f"**统计**: {stats.get('red_frames', 0)}红框, "
                            f"{stats.get('blue_frames', 0)}蓝框, "
                            f"{stats.get('green_frames', 0)}绿框"
                        )
                    
                    wsi_full_evidence = f"""
### 📋 {part_name}（{_s_id}）
{stats_line}

**切片结构化证据摘要（路径引用格式: {_s_id}-R?-B?-G?）：**
{wsi_summary_brief if wsi_summary_brief else '（无小结）'}
"""
                    wsi_summaries_parts.append(wsi_full_evidence)
                    total_chars += len(wsi_full_evidence)
                    logger.info(f"  📄 {part_name} ({_s_id}): {len(wsi_summary_brief)} 字符（结构化证据摘要）")
            
            all_wsi_summaries = '\n\n---\n'.join(wsi_summaries_parts) if wsi_summaries_parts else '暂无各切片证据'
            
            # 2. 调用 AI 生成最终报告（支持按需查阅详细路径）
            _lang = patient_state.get('lang', 'zh')
            logger.info(f"📝 正在调用 AI 生成患者级别最终报告...")
            final_prompt = get_patient_level_final_summary_prompt(folder_name, all_wsi_summaries, total_wsi, lang=_lang)
            
            prompt_chars = len(final_prompt)
            estimated_tokens = int(prompt_chars * 1.5)
            logger.info(f"{'='*60}")
            logger.info(f"📊 Patient Level 最终报告 Token 统计:")
            logger.info(f"   - 提示词总字符数: {prompt_chars:,}")
            logger.info(f"   - 各切片小结字符数: {total_chars:,}")
            logger.info(f"   - 估计 Token 数: ~{estimated_tokens:,}")
            logger.info(f"{'='*60}")
            
            if agent_stop_flags.get(room, False) or patient_state.get('cancel_requested') is True:
                _finish_patient_level_stop(socketio, room, patient_state)
                return
            ai_final_report = call_gpt_vision(final_prompt, None)
            if agent_stop_flags.get(room, False):
                _finish_patient_level_stop(socketio, room, patient_state)
                return
            
            # ⭐ 检查 Orchestrator 是否请求查阅详细信息（支持切片级 + ROI级 + 图片）
            # 模型常把示例里的 [NEED_DETAIL] NONE 原样抄进报告；NONE 视为无需回查。
            ai_final_report, needs_detail_lookup, detail_block = finalize_patient_report_text(
                ai_final_report
            )
            if needs_detail_lookup:
                logger.info(f"🔍 [Patient Level] Orchestrator 请求查阅详细信息，解析中...")
                logger.info(f"🔍 请求详情:\n{detail_block}")
                    
                # ===== 解析请求（新格式优先，旧格式兜底） =====
                detail_requests = []
                    
                # 新格式: 目标/Target | 需要/Need | 原因/Reason
                new_fmt = re.findall(
                    r'(?:目标|Target)\s*[:：]\s*(\S+)\s*\|\s*(?:需要|Need)\s*[:：]\s*(.+?)\s*\|\s*(?:原因|Reason)\s*[:：]\s*(.+?)$',
                    detail_block, re.MULTILINE | re.IGNORECASE
                )
                for target, need_raw, reason in new_fmt:
                    t = target.strip()
                    nl = need_raw.strip().lower()
                    if t.upper() == 'KNOWLEDGE':
                        need_type = need_raw.strip()
                    elif '图片' in nl or 'image' in nl:
                        need_type = 'analysis+image' if ('解读' in nl or 'analysis' in nl or '完整' in nl or 'full' in nl) else 'image'
                    else:
                        need_type = 'full_analysis'
                    detail_requests.append({'target': t, 'need_type': need_type, 'reason': reason.strip()})
                
                # 旧格式兜底: 切片/Slide | 原因/Reason
                if not detail_requests:
                    old_fmt = re.findall(
                        r'(?:切片|Slide)\s*[:：]\s*(.+?)(?:\s*\|\s*(?:原因|Reason)\s*[:：]\s*(.+?))?$',
                        detail_block, re.MULTILINE | re.IGNORECASE
                    )
                    for target, reason in old_fmt:
                        t = target.strip()
                        if t:
                            detail_requests.append({'target': t, 'need_type': 'full_analysis', 'reason': (reason or '').strip()})
                
                logger.info(f"🔍 解析到 {len(detail_requests)} 个查阅请求")
                
                if detail_requests:
                    detail_text_parts = []
                    image_paths_with_labels = []
                    
                    for req in detail_requests:
                        target = req['target']
                        need_type = req['need_type']
                        
                        # ⭐ 知识库查询请求（Target = KNOWLEDGE）
                        if target.upper() == 'KNOWLEDGE':
                            knowledge_key = need_type.strip().lower()
                            logger.info(f"📚 [NEED_DETAIL] 知识库查询: {knowledge_key}")
                            try:
                                knowledge_result = get_diagnostic_criteria(knowledge_key) if get_diagnostic_criteria else ''
                                if knowledge_result:
                                    _kb_header = "Knowledge Base Query Result" if _lang == 'en' else "知识库查询结果"
                                    detail_text_parts.append(f"### 📚 {_kb_header}: {knowledge_key}\n\n{knowledge_result}")
                                    logger.info(f"  📚 知识库返回: {len(knowledge_result)} 字符")
                                else:
                                    logger.warning(f"  ⚠️ 知识库未找到: {knowledge_key}")
                            except Exception as ke:
                                logger.warning(f"  ⚠️ 知识库查询失败: {ke}")
                            continue
                        
                        # 解析目标: S2 → (2, None), S2-R3-B1 → (2, 'R3-B1')
                        s_match = re.match(r'S(\d+)(?:-(.+))?', target, re.IGNORECASE)
                        slide_idx = int(s_match.group(1)) if s_match else None
                        roi_path = s_match.group(2) if s_match else None
                        
                        # 如果无法解析 S 编号，尝试按名称匹配（向后兼容）
                        matched_idx = None
                        if slide_idx and 1 <= slide_idx <= len(sorted_file_infos):
                            matched_idx = slide_idx - 1
                        else:
                            for i, fi in enumerate(sorted_file_infos):
                                pn = fi.get('part', fi.get('filename', ''))
                                if target in pn or pn in target:
                                    matched_idx = i
                                    break
                        
                        if matched_idx is None:
                            logger.warning(f"⚠️ 未找到匹配的切片: {target}")
                            continue
                        
                        f_info = sorted_file_infos[matched_idx]
                        fid = f_info.get('file_id')
                        part_name = f_info.get('part', f_info.get('filename', fid))
                        _s_id = f"S{matched_idx + 1}"
                        
                        wsi_result = (patient_state.get('wsi_results', {}).get(fid) or {}) if fid else {}
                        slide_tree_nodes = wsi_result.get('tree_nodes', []) if wsi_result else []
                        
                        # 提取文本分析
                        if need_type in ('full_analysis', 'analysis+image'):
                            if roi_path:
                                roi_text, roi_img = _extract_roi_detail(slide_tree_nodes, roi_path, lang=_lang)
                                detail_text_parts.append(f"### 📋 {part_name}（{_s_id}-{roi_path}）— ROI 详细解读\n\n{roi_text}")
                                logger.info(f"  📄 已抽取 {_s_id}-{roi_path}: {len(roi_text)} 字符")
                                if need_type == 'analysis+image' and roi_img:
                                    image_paths_with_labels.append((roi_img, f"{_s_id}-{roi_path}"))
                            else:
                                full_detail = build_path_summary_from_tree_nodes(slide_tree_nodes, global_analysis='', lang=_lang)
                                detail_text_parts.append(f"### 📋 {part_name}（{_s_id}）— 完整详细路径分析\n\n{full_detail}")
                                logger.info(f"  📄 已抽取 {part_name} ({_s_id}) 完整路径: {len(full_detail)} 字符")
                        
                        # 仅图片
                        elif need_type == 'image':
                            if roi_path:
                                _, roi_img = _extract_roi_detail(slide_tree_nodes, roi_path, lang=_lang)
                                if roi_img:
                                    image_paths_with_labels.append((roi_img, f"{_s_id}-{roi_path}"))
                                else:
                                    logger.warning(f"⚠️ 未找到 {_s_id}-{roi_path} 的图片路径")
                    
                    if detail_text_parts or image_paths_with_labels:
                        supplementary_details = '\n\n---\n\n'.join(detail_text_parts) if detail_text_parts else ''
                        
                        # 拼接图片（如有）
                        composed_image_base64 = None
                        if image_paths_with_labels:
                            composed_image_base64 = _compose_detail_images(image_paths_with_labels)
                            if composed_image_base64:
                                logger.info(f"🖼️ 已拼接 {len(image_paths_with_labels)} 张 ROI 图片")
                            else:
                                logger.warning(f"⚠️ 图片拼接失败或图片文件不存在")
                        
                        # 构建增强版摘要
                        _en = (_lang == 'en')
                        _detail_header = "## Supplementary: Requested Detailed Analysis" if _en else "## 补充信息：你请求查阅的详细分析"
                        _prev_header = "## Your Previous Assessment" if _en else "## 你之前的评估"
                        _img_note = ""
                        if composed_image_base64:
                            _img_note = ("\n\n📷 **The requested ROI images are attached to this message. Examine them carefully for your diagnosis.**"
                                         if _en else "\n\n📷 **你请求查看的 ROI 图片已附在本消息中，请仔细观察并用于诊断判断。**")
                        _instruction = (
                            f"Now you have the structured summaries AND the supplementary details you requested.{' The requested images are also provided.' if composed_image_base64 else ''} Please generate the complete final report directly — do NOT output [NEED_DETAIL] again."
                            if _en else
                            f"现在你同时拥有结构化摘要和你请求的补充详细信息。{'你请求的图片也已提供。' if composed_image_base64 else ''}请直接生成最终综合报告——不要再输出 [NEED_DETAIL]。"
                        )
                        
                        enriched_summaries = f"""{all_wsi_summaries}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{_detail_header}

{supplementary_details}{_img_note}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{_prev_header}

{detail_block}

⚠️ {_instruction}"""
                        supplement_prompt = get_patient_level_final_summary_prompt(
                            folder_name, enriched_summaries, total_wsi, lang=_lang
                        )
                        
                        supplement_chars = len(supplement_prompt)
                        logger.info(f"📊 第二轮调用: {len(detail_text_parts)} 个文本段 + {len(image_paths_with_labels)} 张图片 | {supplement_chars:,} 字符")
                        
                        if (
                            agent_stop_flags.get(room, False)
                            or patient_state.get('cancel_requested') is True
                        ):
                            _finish_patient_level_stop(socketio, room, patient_state)
                            return
                        ai_final_report = call_gpt_vision(supplement_prompt, composed_image_base64)
                        if agent_stop_flags.get(room, False):
                            _finish_patient_level_stop(socketio, room, patient_state)
                            return
                        logger.info(f"✅ [Patient Level] 第二轮报告生成完成")
                        ai_final_report, _, _ = finalize_patient_report_text(ai_final_report)
            
            if ai_final_report:
                logger.info(f"\n{'='*60}")
                logger.info(f"✅ AI 患者级别最终报告生成成功！")
                logger.info(f"{'='*60}")
                logger.info(ai_final_report[:1000] + "..." if len(ai_final_report) > 1000 else ai_final_report)
                
                # The public file is published only after the final Stop/commit
                # decision.  Until then the report lives in memory (and later
                # in the run-scoped staging directory).
                patient_state['ai_final_report'] = ai_final_report
            else:
                logger.warning("⚠️ AI 最终报告生成失败或仍包含查阅请求，未保存 AI 最终报告")
                
        except Exception as e:
            logger.warning(f"[Patient Level] AI 最终报告生成失败: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        
        if agent_stop_flags.get(room, False):
            _finish_patient_level_stop(socketio, room, patient_state)
            return

        # ⭐⭐⭐ 协作模式：综合报告协作确认（让用户审阅修改最终报告）⭐⭐⭐
        if mode == 'collaborative' and ai_final_report:
            logger.info(f"🤝 [Patient Level 协作模式] 等待用户确认综合报告...")
            patient_state['status'] = 'awaiting_patient_report_review'
            patient_state['pending_final_report'] = ai_final_report
            patient_state['patient_report_review_status'] = 'pending'

            # 先注册等待状态，再通知前端，避免极快确认时发生竞态。
            # wait_for_phase_confirmation() returns execute immediately unless
            # phase_requires_confirmation is set; that would silently issue the
            # patient report without a pathologist click.
            temp_state = {
                'phase_status': 'pending',
                'phase_confirmation': None,
                'phase_requires_confirmation': True,
                'current_phase': 6,
            }
            set_agent_state(room, temp_state)

            # 发送综合报告供用户审阅。WebSocket 只负责实时通知；
            # 最终确认由 Patient-level HTTP API 持久、可验证地接收。
            socketio.emit('agent.patient_report_review', {
                'patient_id': patient_id,
                'run_token': patient_state.get('run_token'),
                'folder_name': folder_name,
                'report': ai_final_report,
                'total_wsi': total_wsi,
                'mode': 'collaborative'
            }, to=room)
            
            confirmation = wait_for_phase_confirmation(socketio, room, temp_state, phase=6, timeout=1800)
            if (
                agent_stop_flags.get(room, False)
                or confirmation.get('action') == 'stopped'
            ):
                clear_agent_state(room)
                _finish_patient_level_stop(socketio, room, patient_state)
                return
            
            if confirmation['action'] == 'modify' and confirmation.get('modified_data'):
                # ⭐ 修复：同时支持 edited_report 和 report 键（前端使用 edited_report）
                modified_data = confirmation['modified_data']
                edited_report = modified_data.get('edited_report') or modified_data.get('report', ai_final_report)
                ai_final_report = edited_report
                patient_state['ai_final_report'] = ai_final_report
                logger.info(f"✏️ [Patient Level 协作模式] 用户修改了综合报告，长度: {len(ai_final_report)} 字符")
                
                # The edited draft is staged with the other final artifacts;
                # do not overwrite a previously published report yet.
            else:
                logger.info(f"✅ [Patient Level 协作模式] 用户确认综合报告")
            
            clear_agent_state(room)
            patient_state['patient_report_confirmed_at'] = datetime.now().isoformat()
            patient_state['patient_report_review_status'] = 'confirmed'
            patient_state.pop('pending_final_report', None)
            patient_state['status'] = 'finalizing'
        
        if agent_stop_flags.get(room, False):
            _finish_patient_level_stop(socketio, room, patient_state)
            return

        safe_run_token = re.sub(
            r'[^A-Za-z0-9_.-]+', '_',
            str(patient_state.get('run_token') or datetime.now().timestamp()),
        )[:160]
        staging_root = patient_output_dir / '.staging' / safe_run_token
        staging_mode_dir = staging_root / mode_subdir
        staging_mode_dir.mkdir(parents=True, exist_ok=True)
        unpublished_artifacts = []
        publish_pairs = []

        def _discard_unpublished_patient_artifacts():
            """Drop this run's staging tree, whether or not it was published."""
            if staging_root.exists():
                # Windows/WSL can still hold a just-written report open for a
                # moment, so a single rmtree loses the whole directory to a
                # transient lock and leaves .staging garbage behind.
                for attempt in range(3):
                    try:
                        shutil.rmtree(staging_root)
                        break
                    except Exception:
                        if attempt < 2:
                            time.sleep(0.3)
                else:
                    shutil.rmtree(staging_root, ignore_errors=True)
                if staging_root.exists():
                    logger.warning(
                        '[Patient Level] staging 目录清理失败（不影响已发布产物）: %s',
                        staging_root,
                    )
                    return
            try:
                staging_root.parent.rmdir()
            except OSError:
                # Another run of the same patient still owns a staging tree.
                pass

        if ai_final_report:
            staged_ai_report = staging_mode_dir / 'ai_final_report.txt'
            with open(staged_ai_report, 'w', encoding='utf-8') as f:
                f.write("# 患者级别 AI 综合报告\n")
                f.write(f"# 患者: {folder_name}\n")
                f.write(f"# 切片数: {total_wsi}\n")
                f.write(f"# 模式: {'协作模式' if mode == 'collaborative' else '自动模式'}\n")
                f.write(f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write(ai_final_report)
            unpublished_artifacts.append(staged_ai_report)
            publish_pairs.append((
                staged_ai_report,
                patient_output_dir / mode_subdir / 'ai_final_report.txt',
            ))

        # 生成患者级别报告（Word）- 传入 AI 报告
        from agent.report_generator import generate_patient_level_report
        patient_state['ai_final_report'] = ai_final_report  # 确保传入
        report_result = generate_patient_level_report(patient_state, staging_root)
        if report_result.get('success') and report_result.get('report_path'):
            staged_report_path = Path(report_result['report_path'])
            unpublished_artifacts.append(staged_report_path)
            publish_pairs.append((staged_report_path, patient_output_dir / staged_report_path.name))
        if agent_stop_flags.get(room, False):
            _discard_unpublished_patient_artifacts()
            _finish_patient_level_stop(socketio, room, patient_state)
            return
        
        # ⭐ 生成患者级别 HTML 报告
        patient_html_path = None
        try:
            wsi_results = patient_state.get('wsi_results', {})
            patient_html_path = generate_patient_level_html_report(
                patient_state=patient_state,
                output_dir=staging_mode_dir,
                file_infos=patient_state.get('file_infos') or file_infos,
                wsi_results=wsi_results
            )
            if patient_html_path:
                patient_html_path = Path(patient_html_path)
                unpublished_artifacts.append(patient_html_path)
                publish_pairs.append((
                    patient_html_path,
                    patient_output_dir / mode_subdir / patient_html_path.name,
                ))
        except Exception as e:
            logger.warning(f"[Patient Level] HTML 报告生成失败（不影响主流程）: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        if agent_stop_flags.get(room, False):
            _discard_unpublished_patient_artifacts()
            _finish_patient_level_stop(socketio, room, patient_state)
            return
        
        # ⭐ 保存 patient_summary.json（供缓存加载时恢复聊天中的汇总报告文本）
        try:
            import json as json_module_ps
            summary_data = {
                'patient_id': patient_id,
                'folder_name': folder_name,
                'mode': mode,
                'analysis_strategy': analysis_strategy,
                'summary': report_result.get('summary', ''),
                'report_url': f'/api/patient/{patient_id}/report/word' if report_result.get('success') else None,
                'html_report_url': f'/outputs/{patient_dir_name}/{mode_subdir}/patient_report.html' if patient_html_path else None,
                'saved_at': datetime.now().isoformat()
            }
            summary_dir = staging_mode_dir
            summary_dir.mkdir(parents=True, exist_ok=True)
            summary_path = summary_dir / 'patient_summary.json'
            with open(summary_path, 'w', encoding='utf-8') as sf:
                json_module_ps.dump(summary_data, sf, ensure_ascii=False, indent=2)
            unpublished_artifacts.append(summary_path)
            publish_pairs.append((
                summary_path,
                patient_output_dir / mode_subdir / 'patient_summary.json',
            ))
            logger.info(f"[Patient Level] ✅ patient_summary.json 已保存: {summary_path}")
        except Exception as e:
            logger.warning(f"[Patient Level] patient_summary.json 保存失败（不影响主流程）: {e}")

        # ⭐ 聚合各 WSI 的 analysis_tree.json → 患者级别树
        # 结构：Patient 根节点 → 各 WSI 分支 → 红→蓝→绿（与单张模式一致）
        patient_tree_path = None
        try:
            import json as json_module
            patient_tree = {
                'metadata': {
                    'patient_id': patient_id,
                    'folder_name': folder_name,
                    'total_wsi': total_wsi,
                    'analysis_time': datetime.now().isoformat(),
                    'ai_final_report': patient_state.get('ai_final_report', '')
                },
                'tree_nodes': []
            }
            
            for f_info in (patient_state.get('file_infos') or file_infos):
                fid = f_info.get('file_id')
                if not fid:
                    continue
                
                # ⭐ 优先从 wsi_results 获取正确的输出路径（新格式：文件名_时间戳）
                wsi_tree_path = None
                wsi_result_item = patient_state.get('wsi_results', {}).get(fid) or {}
                wsi_output_paths = wsi_result_item.get('output_paths', {})
                if wsi_output_paths:
                    wsi_output_dir = wsi_output_paths.get('output_dir')
                    if wsi_output_dir:
                        wsi_tree_path = Path(wsi_output_dir) / 'analysis_tree.json'
                
                # ⭐ 如果没有 output_paths，尝试搜索目录（兼容新旧格式）
                if not wsi_tree_path or not wsi_tree_path.exists():
                    # 新格式：wsi_{文件名}_{时间戳}，使用通配符搜索
                    search_dir = patient_output_dir / mode_subdir
                    if search_dir.exists():
                        matching_dirs = list(search_dir.glob(f'wsi_*'))
                        for d in matching_dirs:
                            candidate = d / 'analysis_tree.json'
                            if candidate.exists():
                                # 读取 JSON 检查 file_id 是否匹配
                                try:
                                    with open(candidate, 'r', encoding='utf-8') as check_f:
                                        check_data = json_module.load(check_f)
                                        if check_data.get('metadata', {}).get('file_id') == fid:
                                            wsi_tree_path = candidate
                                            break
                                except:
                                    pass
                
                # ⭐ 兼容旧路径格式
                if not wsi_tree_path or not wsi_tree_path.exists():
                    wsi_tree_path = patient_output_dir / mode_subdir / f'wsi_{fid}' / 'analysis_tree.json'
                if not wsi_tree_path.exists():
                    wsi_tree_path = Path(f"outputs/{fid}/{mode_subdir}/analysis_tree.json")
                if not wsi_tree_path.exists():
                    wsi_tree_path = Path(f"outputs/{fid}/analysis_tree.json")
                
                if analysis_strategy != 'single_agent_roam' and wsi_tree_path.exists():
                    with open(wsi_tree_path, 'r', encoding='utf-8') as tf:
                        wsi_tree = json_module.load(tf)
                    
                    # ⭐ 获取该 WSI 的缩略图 URL
                    wsi_thumbnail_url = wsi_tree.get('metadata', {}).get('thumbnail_url', '')
                    if not wsi_thumbnail_url:
                        # 使用 wsi_tree_path 的父目录来构建正确的 steps 路径
                        wsi_output_dir = wsi_tree_path.parent / 'steps'
                        if wsi_output_dir.exists():
                            thumb_patterns = list(wsi_output_dir.glob('thumbnail_*.jpg')) + list(wsi_output_dir.glob('step_0_*.jpg'))
                            if thumb_patterns:
                                # 从 wsi_tree_path 提取相对路径
                                wsi_dir_name = wsi_tree_path.parent.name  # 目录名（wsi_{文件名}_{时间戳}）
                                wsi_thumbnail_url = f'/outputs/{patient_dir_name}/{mode_subdir}/{wsi_dir_name}/steps/{thumb_patterns[0].name}'
                    
                    # ⭐ 使用 Slide 1/2/3 命名，而非 Part 1/2
                    wsi_idx_num = len(patient_tree['tree_nodes']) + 1
                    
                    patient_tree['tree_nodes'].append({
                        'roi_id': f'Slide {wsi_idx_num}',
                        'level': 'wsi',
                        'filename': f_info.get('filename', ''),
                        'part_name': f_info.get('part', f'Slide {wsi_idx_num}'),
                        'file_id': fid,
                        'thumbnail_url': wsi_thumbnail_url,
                        'children': wsi_tree.get('tree_nodes', []),
                        'analysis': {
                            'findings': wsi_tree.get('metadata', {}).get('global_analysis', ''),
                            'reasoning': ''
                        },
                        'metadata': wsi_tree.get('metadata', {})
                    })
                    logger.info(f"[Patient Level] ✅ 聚合 WSI 树: {f_info.get('part', fid)}, {len(wsi_tree.get('tree_nodes', []))} 个红框")
                elif analysis_strategy == 'single_agent_roam' and wsi_result_item:
                    trajectory = list(wsi_result_item.get('trajectory') or [])
                    wsi_idx_num = len(patient_tree['tree_nodes']) + 1
                    output_paths = wsi_result_item.get('output_paths') or {}
                    steps_path = Path(output_paths.get('steps_dir') or '')
                    overview_path = steps_path / 'overview.jpg' if steps_path else None
                    thumbnail_url = ''
                    if overview_path and overview_path.exists():
                        try:
                            rel = overview_path.relative_to(Path('outputs'))
                            thumbnail_url = '/outputs/' + '/'.join(rel.parts)
                        except ValueError:
                            thumbnail_url = ''
                    roam_children = []
                    for item in trajectory:
                        if not isinstance(item, dict) or not item.get('rect'):
                            continue
                        roam_children.append({
                            'roi_id': f"FOV-{item.get('step')}",
                            'level': 'roam_step',
                            'rect': item.get('rect'),
                            'zoom': item.get('zoom'),
                            'analysis': {
                                'findings': item.get('observation', ''),
                                'reasoning': item.get('why', ''),
                            },
                            'children': [],
                        })
                    patient_tree['tree_nodes'].append({
                        'roi_id': f'Slide {wsi_idx_num}',
                        'level': 'wsi_single_agent',
                        'filename': f_info.get('filename', ''),
                        'part_name': f_info.get('part', f'Slide {wsi_idx_num}'),
                        'file_id': fid,
                        'thumbnail_url': thumbnail_url,
                        'children': roam_children,
                        'analysis': {
                            'findings': wsi_result_item.get('report', ''),
                            'reasoning': 'single_agent_roam',
                        },
                        'metadata': {
                            'strategy': 'single_agent_roam',
                            'steps': len(roam_children),
                        },
                    })
                    logger.info(
                        '[Patient Level] ✅ 聚合 Single Agent 轨迹: %s, %s 个视野',
                        f_info.get('part', fid), len(roam_children),
                    )
                else:
                    logger.warning(f"[Patient Level] ⚠️ WSI 树文件不存在: {wsi_tree_path}")
            
            # 保存患者级别聚合树
            patient_tree_dir = staging_mode_dir
            patient_tree_dir.mkdir(parents=True, exist_ok=True)
            patient_tree_path = patient_tree_dir / 'patient_analysis_tree.json'
            with open(patient_tree_path, 'w', encoding='utf-8') as tf:
                json_module.dump(patient_tree, tf, ensure_ascii=False, indent=2)
            unpublished_artifacts.append(patient_tree_path)
            publish_pairs.append((
                patient_tree_path,
                patient_output_dir / mode_subdir / 'patient_analysis_tree.json',
            ))
            logger.info(f"[Patient Level] ✅ 患者级别聚合树已生成: {patient_tree_path}")
            
        except Exception as e:
            logger.warning(f"[Patient Level] 聚合树生成失败（不影响主流程）: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        
        # Commit completion atomically against the cooperative Stop endpoint.
        # A Stop accepted before this point wins and unpublished final files
        # are removed; after this point the terminal completion is authoritative.
        with _patient_worker_state_lock:
            publish_cancelled = (
                agent_stop_flags.get(room, False)
                or patient_state.get('cancel_requested') is True
            )
            if not publish_cancelled:
                _publish_patient_artifacts_atomically(
                    publish_pairs,
                    staging_mode_dir / '.publish_backup',
                )
                patient_state['status'] = 'completed'
                patient_state['completed_at'] = datetime.now().isoformat()
                patient_state['updated_at'] = patient_state['completed_at']
        if publish_cancelled:
            _discard_unpublished_patient_artifacts()
            _finish_patient_level_stop(socketio, room, patient_state)
            return
        _discard_unpublished_patient_artifacts()

        # 发送报告完成事件
        socketio.emit('agent.patient_report', {
            'patient_id': patient_id,
            'run_token': patient_state.get('run_token'),
            'folder_name': folder_name,
            'mode': mode,  # ⭐ 携带模式信息
            'analysis_strategy': analysis_strategy,
            'report_url': f'/api/patient/{patient_id}/report/word' if report_result.get('success') else None,
            'html_report_url': f'/outputs/{patient_dir_name}/{mode_subdir}/patient_report.html' if patient_html_path else None,
            'summary': report_result.get('summary', ''),
            'success': report_result.get('success', False),
            'tree_url': f'/api/agent/patient-analysis-tree/{patient_id}' if patient_tree_path else None
        }, to=room)
        
        socketio.emit('agent.patient_progress', {
            'percentage': 100,
            'completed_wsi': total_wsi,
            'total_wsi': total_wsi,
            'patient_id': patient_id,
            'run_token': patient_state.get('run_token'),
            'message': _msg('analysis_done', lang)
        }, to=room)
        
        logger.info(f"\n✅ Patient Level分析完成: {folder_name}")
        
    except Exception as e:
        if 'patient_state' in locals() and agent_stop_flags.get(room, False):
            _finish_patient_level_stop(socketio, room, patient_state)
            return
        logger.error(f"Patient Level分析失败: {e}")
        import traceback
        logger.error(traceback.format_exc())

        terminal_already_committed = False
        if 'patient_state' in locals():
            with _patient_worker_state_lock:
                terminal_already_committed = patient_state.get('status') in {
                    'completed', 'stopped', 'cancelled', 'failed'
                }
                if not terminal_already_committed:
                    patient_state['status'] = 'failed'
                    patient_state['updated_at'] = datetime.now().isoformat()
        if terminal_already_committed:
            logger.warning(
                '[Patient Level] 终态已提交，忽略终态后通知异常',
                exc_info=True,
            )
            return

        socketio.emit('agent.error', {
            'error': f'Patient Level分析失败: {str(e)}'
        }, to=room)


def regenerate_patient_level_report(socketio, room: str, patient_dir_name: str,
                                     mode_subdir: str = 'auto', lang: str = 'zh'):
    """
    从已保存的 pathology_report.txt 重新生成患者级别综合报告，无需重跑单切片分析。

    流程：
    1. 读取 patient_analysis_tree.json 获取切片顺序与元数据
    2. 逐张读取 wsi_*/pathology_report.txt 重建 all_wsi_summaries
    3. 调用更新后的 get_patient_level_final_summary_prompt + AI
    4. 覆盖写入 ai_final_report.txt / patient_report.html / patient_summary.json
    5. 通过 socketio 向前端推送进度与结果
    """
    import json as _json
    import re as _re
    from pathlib import Path
    from datetime import datetime
    from agent.gpt_client import call_gpt_vision

    outputs_dir = Path("outputs")
    patient_output_dir = outputs_dir / patient_dir_name
    mode_dir = patient_output_dir / mode_subdir
    _en = (lang == 'en')

    try:
        logger.info(f"\n{'='*60}")
        logger.info(f"🔄 [重新汇总] 开始: {patient_dir_name} / {mode_subdir}")
        logger.info(f"{'='*60}")

        # ── Step 1: 读取 patient_analysis_tree.json 获取切片顺序 ──────────
        tree_json_path = mode_dir / 'patient_analysis_tree.json'
        if not tree_json_path.exists():
            raise FileNotFoundError(f"找不到 patient_analysis_tree.json: {tree_json_path}")

        with open(tree_json_path, 'r', encoding='utf-8') as f:
            patient_tree = _json.load(f)

        meta = patient_tree.get('metadata', {})
        folder_name  = meta.get('folder_name', patient_dir_name)
        patient_id   = meta.get('patient_id', patient_dir_name)

        wsi_nodes = [n for n in patient_tree.get('tree_nodes', []) if n.get('level') == 'wsi']
        # 按 "Slide N" 数字升序排列，保证 S 编号与原分析一致
        def _slide_num(node):
            m = _re.search(r'\d+', node.get('roi_id', ''))
            return int(m.group()) if m else 999
        wsi_nodes.sort(key=_slide_num)
        total_wsi = len(wsi_nodes)

        socketio.emit('agent.patient_progress', {
            'percentage': 10,
            'message': f'{"Reading" if _en else "正在读取"} {total_wsi} {"slide reports..." if _en else "张切片报告..."}'
        }, to=room)

        # ── Step 2: 逐张读取 pathology_report.txt ────────────────────────
        wsi_summaries_parts = []
        for idx, node in enumerate(wsi_nodes, start=1):
            _s_id    = f"S{idx}"
            raw_part = node.get('part_name', f'Slide {idx}')
            part_name = raw_part.split(': ', 1)[1] if ': ' in raw_part else raw_part

            # 从 thumbnail_url 推断 wsi 输出目录
            thumbnail_url = node.get('metadata', {}).get('thumbnail_url', '')
            wsi_output_dir = None
            if thumbnail_url:
                # /outputs/.../wsi_xxx/steps/global_thumbnail.jpg  → wsi_xxx/
                wsi_path = Path(thumbnail_url.lstrip('/'))
                if wsi_path.parts[-2] == 'steps':
                    wsi_output_dir = wsi_path.parent.parent  # 去掉 steps/filename
            # 兜底：按文件名 glob 搜索
            if not wsi_output_dir or not (wsi_output_dir / 'pathology_report.txt').exists():
                filename = node.get('filename', '')
                safe_stem = Path(filename).stem.replace(' ', '_') if filename else ''
                candidates = list(mode_dir.glob(f'wsi_{safe_stem}*')) if safe_stem else []
                if candidates:
                    wsi_output_dir = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]

            report_txt_path = wsi_output_dir / 'pathology_report.txt' if wsi_output_dir else None
            if not report_txt_path or not report_txt_path.exists():
                logger.warning(f"[重新汇总] ⚠️ {part_name}({_s_id}): 找不到 pathology_report.txt，跳过")
                continue

            with open(report_txt_path, 'r', encoding='utf-8') as f:
                report_txt = f.read().strip()

            if not report_txt:
                logger.warning(f"[重新汇总] ⚠️ {part_name}({_s_id}): pathology_report.txt 为空，跳过")
                continue

            is_roam_node = (
                node.get('level') == 'wsi_single_agent'
                or (node.get('metadata') or {}).get('strategy') == 'single_agent_roam'
            )
            stats = node.get('metadata', {}).get('statistics', {})
            if is_roam_node:
                fov_n = stats.get('roam_steps', stats.get('steps', len(node.get('children') or [])))
                stats_line = f"**统计**: 单 Agent 游走 {fov_n} 个观察视野（无红/蓝/绿框）"
            else:
                red_n   = stats.get('total_red',   stats.get('red_frames',   0))
                blue_n  = stats.get('total_blue',  stats.get('blue_frames',  0))
                green_n = stats.get('total_green', stats.get('green_frames', 0))
                stats_line = f"**统计**: {red_n}红框, {blue_n}蓝框, {green_n}绿框"

            wsi_summaries_parts.append(
                f"### 📋 {part_name}（{_s_id}）\n"
                f"{stats_line}\n\n"
                f"**切片结构化证据摘要（路径引用格式: {_s_id}-R?-B?-G?）：**\n"
                f"{report_txt}"
            )
            logger.info(f"[重新汇总] ✅ {part_name}({_s_id}): {len(report_txt)} 字符")

        if not wsi_summaries_parts:
            raise ValueError("未读取到任何有效切片报告")

        all_wsi_summaries = '\n\n---\n'.join(wsi_summaries_parts)
        actual_wsi = len(wsi_summaries_parts)
        logger.info(f"[重新汇总] 已加载 {actual_wsi}/{total_wsi} 张切片报告，共 {len(all_wsi_summaries):,} 字符")

        all_reports_path = mode_dir / 'all_slide_reports.txt'
        with open(all_reports_path, 'w', encoding='utf-8') as f:
            f.write(all_wsi_summaries)
        logger.info(f"[重新汇总] 💾 all_slide_reports.txt 已保存 ({len(all_wsi_summaries):,} 字符)")

        socketio.emit('agent.patient_progress', {
            'percentage': 30,
            'message': (f'Loaded {actual_wsi}/{total_wsi} slides, calling AI...'
                        if _en else f'已加载 {actual_wsi}/{total_wsi} 张，正在调用 AI 生成综合报告...')
        }, to=room)

        # ── Step 3: 调用 AI（含 NEED_DETAIL 一次重试） ───────────────────
        final_prompt = get_patient_level_final_summary_prompt(
            folder_name, all_wsi_summaries, actual_wsi, lang=lang
        )
        logger.info(f"[重新汇总] 提示词 {len(final_prompt):,} 字符，调用 AI...")
        ai_report = call_gpt_vision(final_prompt, None)

        # 支持 NEED_DETAIL（一次补充查阅）。NONE / 空块视为无需回查。
        ai_report, needs_detail_lookup, detail_block = finalize_patient_report_text(ai_report)
        if needs_detail_lookup:
            logger.info(f"[重新汇总] AI 请求补充详情，从 analysis_tree.json 补全...")
            socketio.emit('agent.patient_progress', {
                'percentage': 55,
                'message': 'AI 请求补充详情，正在提取...' if not _en else 'AI requested detail, extracting...'
            }, to=room)

            detail_parts = []
            if detail_block:
                for req_line in detail_block.splitlines():
                    t_m = _re.search(r'(?:目标|Target)\s*[:：]\s*(\S+)', req_line, _re.I)
                    if not t_m:
                        continue
                    target = t_m.group(1).strip()
                    if target.upper() == 'KNOWLEDGE':
                        try:
                            from agent.knowledge_retrieval import get_diagnostic_criteria
                            kw = _re.search(r'(?:需要|Need)\s*[:：]\s*(.+?)(?:\s*\|)', req_line, _re.I)
                            key = kw.group(1).strip() if kw else 'who_2020_classification'
                            kb = get_diagnostic_criteria(key)
                            if kb:
                                detail_parts.append(f"### 📚 知识库: {key}\n{kb}")
                        except Exception:
                            pass
                        continue
                    s_m = _re.match(r'S(\d+)(?:-(.+))?', target, _re.I)
                    if not s_m:
                        continue
                    slide_idx = int(s_m.group(1))
                    roi_path  = s_m.group(2)
                    if 1 <= slide_idx <= len(wsi_nodes):
                        nd_node = wsi_nodes[slide_idx - 1]
                        nd_thumbnail = nd_node.get('metadata', {}).get('thumbnail_url', '')
                        nd_dir = None
                        if nd_thumbnail:
                            nd_path = Path(nd_thumbnail.lstrip('/'))
                            if nd_path.parts[-2] == 'steps':
                                nd_dir = nd_path.parent.parent
                        if nd_dir:
                            tree_f = nd_dir / 'analysis_tree.json'
                            if tree_f.exists():
                                with open(tree_f, 'r', encoding='utf-8') as tf:
                                    nd_tree = _json.load(tf)
                                nd_nodes = nd_tree.get('tree_nodes', [])
                                if roi_path:
                                    roi_txt, _ = _extract_roi_detail(nd_nodes, roi_path, lang=lang)
                                    detail_parts.append(f"### S{slide_idx}-{roi_path} 详细解读\n{roi_txt}")
                                else:
                                    full = build_path_summary_from_tree_nodes(nd_nodes, lang=lang)
                                    detail_parts.append(f"### S{slide_idx} 完整路径分析\n{full}")

            if detail_parts:
                _instr = ("Now generate the complete final report directly." if _en else
                          "现在你同时拥有以上补充详情，请直接生成最终综合报告——不要再输出 [NEED_DETAIL]。")
                enriched = (f"{all_wsi_summaries}\n\n━━━━━━━━━━━━━━\n\n"
                            f"## 补充详细信息\n\n" + '\n\n'.join(detail_parts) +
                            f"\n\n⚠️ {_instr}")
                supp_prompt = get_patient_level_final_summary_prompt(
                    folder_name, enriched, actual_wsi, lang=lang
                )
                ai_report = call_gpt_vision(supp_prompt, None)
                logger.info(f"[重新汇总] ✅ 第二轮报告生成完成")
                ai_report, still_needs, _ = finalize_patient_report_text(ai_report)
                if still_needs:
                    raise ValueError("AI 未能生成有效报告")

        if not ai_report:
            raise ValueError("AI 未能生成有效报告")

        socketio.emit('agent.patient_progress', {
            'percentage': 75,
            'message': 'AI 报告生成完成，正在保存...' if not _en else 'Report generated, saving...'
        }, to=room)

        # ── Step 4: 保存 ai_final_report.txt ─────────────────────────────
        ai_report_path = mode_dir / 'ai_final_report.txt'
        mode_dir.mkdir(parents=True, exist_ok=True)
        with open(ai_report_path, 'w', encoding='utf-8') as f:
            f.write(f"# 患者级别 AI 综合报告（重新汇总）\n")
            f.write(f"# 患者: {folder_name}\n")
            f.write(f"# 切片数: {actual_wsi}\n")
            f.write(f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(ai_report)
        logger.info(f"[重新汇总] 💾 ai_final_report.txt 已保存")

        # ── Step 5: 重新生成 HTML 报告 ────────────────────────────────────
        patient_html_path = None
        try:
            # 构造最小 patient_state，供 generate_patient_level_html_report 使用
            minimal_state = {
                'patient_id': patient_id,
                'folder_name': folder_name,
                'mode': 'collaborative' if mode_subdir == 'collab' else 'ai',
                'lang': lang,
                'ai_final_report': ai_report,
                'wsi_results': {},
                'completed_wsi': actual_wsi,
                'total_wsi': total_wsi,
                'statistics': {'completed_wsi': actual_wsi, 'total_wsi': total_wsi}
            }
            # 从各 wsi 目录补充 wsi_results（供 HTML 生成器渲染各切片小节）
            any_roam = False
            for idx2, nd in enumerate(wsi_nodes, start=1):
                fid2 = nd.get('file_id') or nd.get('metadata', {}).get('file_id', f'regen_{idx2}')
                thumb = nd.get('metadata', {}).get('thumbnail_url', '')
                nd_dir2 = None
                if thumb:
                    tp = Path(thumb.lstrip('/'))
                    if len(tp.parts) >= 2 and tp.parts[-2] == 'steps':
                        nd_dir2 = tp.parent.parent
                html_path2 = (nd_dir2 / 'pathology_report.html') if nd_dir2 else None
                is_roam_node = (
                    nd.get('level') == 'wsi_single_agent'
                    or (nd.get('metadata') or {}).get('strategy') == 'single_agent_roam'
                )
                trajectory = []
                traj_file = (nd_dir2 / 'trajectory.json') if nd_dir2 else None
                if traj_file and traj_file.exists():
                    try:
                        traj_payload = json.loads(traj_file.read_text(encoding='utf-8'))
                        trajectory = list(traj_payload.get('trajectory') or [])
                    except Exception:
                        trajectory = []
                if not trajectory and is_roam_node:
                    for child in nd.get('children') or []:
                        analysis = child.get('analysis') or {}
                        trajectory.append({
                            'step': str(child.get('roi_id') or '').replace('FOV-', '') or 0,
                            'label': child.get('roi_id') or '',
                            'observation': analysis.get('findings', ''),
                            'why': analysis.get('reasoning', ''),
                            'rect': child.get('rect'),
                            'zoom': child.get('zoom'),
                        })
                if is_roam_node or trajectory:
                    any_roam = True
                report_txt_path = (nd_dir2 / 'pathology_report.txt') if nd_dir2 else None
                wsi_summary = ''
                if report_txt_path and report_txt_path.exists():
                    try:
                        wsi_summary = report_txt_path.read_text(encoding='utf-8').strip()
                    except Exception:
                        wsi_summary = ''
                metadata_stats = dict((nd.get('metadata') or {}).get('statistics') or {})
                if trajectory and not metadata_stats.get('roam_steps'):
                    metadata_stats['roam_steps'] = len([
                        item for item in trajectory
                        if int(item.get('step') or 0) > 0
                    ])
                minimal_state['wsi_results'][fid2] = {
                    'part_name': (nd.get('part_name', '').split(': ', 1)[-1]),
                    'wsi_summary': wsi_summary,
                    'report': wsi_summary,
                    'tree_nodes': [] if (is_roam_node or trajectory) else (nd.get('children') or []),
                    'trajectory': trajectory,
                    'strategy': 'single_agent_roam' if (is_roam_node or trajectory) else 'agent_swarm',
                    'statistics': metadata_stats,
                    'output_paths': {
                        'output_dir': str(nd_dir2) if nd_dir2 else '',
                        'steps_dir': str(nd_dir2 / 'steps') if nd_dir2 else '',
                    },
                    'html_report_path': str(html_path2) if html_path2 and html_path2.exists() else None,
                }
            if any_roam:
                minimal_state['analysis_strategy'] = 'single_agent_roam'

            # 构造 file_infos
            regen_file_infos = []
            for idx2, nd in enumerate(wsi_nodes, start=1):
                fid2 = nd.get('file_id') or nd.get('metadata', {}).get('file_id', f'regen_{idx2}')
                regen_file_infos.append({
                    'file_id': fid2,
                    'part': nd.get('part_name', '').split(': ', 1)[-1],
                    'filename': nd.get('filename', ''),
                    'index': idx2
                })

            patient_html_path = generate_patient_level_html_report(
                patient_state=minimal_state,
                output_dir=mode_dir,
                file_infos=regen_file_infos,
                wsi_results=minimal_state['wsi_results']
            )
            logger.info(f"[重新汇总] 💾 patient_report.html 已保存")
        except Exception as e_html:
            logger.warning(f"[重新汇总] HTML 报告生成失败（不影响主流程）: {e_html}")

        # ── Step 6: 更新 patient_summary.json ────────────────────────────
        try:
            summary_path = mode_dir / 'patient_summary.json'
            existing = {}
            if summary_path.exists():
                with open(summary_path, 'r', encoding='utf-8') as f:
                    existing = _json.load(f)
            existing.update({
                'summary': ai_report,
                'html_report_url': f'/outputs/{patient_dir_name}/{mode_subdir}/patient_report.html' if patient_html_path else existing.get('html_report_url'),
                'saved_at': datetime.now().isoformat(),
                'regenerated': True
            })
            with open(summary_path, 'w', encoding='utf-8') as f:
                _json.dump(existing, f, ensure_ascii=False, indent=2)
            logger.info(f"[重新汇总] 💾 patient_summary.json 已更新")
        except Exception as e_sum:
            logger.warning(f"[重新汇总] patient_summary.json 更新失败: {e_sum}")

        # ── Step 7: 通知前端 ──────────────────────────────────────────────
        html_url = f'/outputs/{patient_dir_name}/{mode_subdir}/patient_report.html' if patient_html_path else None
        socketio.emit('agent.patient_report', {
            'patient_id': patient_id,
            'folder_name': folder_name,
            'mode': 'collaborative' if mode_subdir == 'collab' else (
                'single_agent_roam' if mode_subdir == 'single_agent_roam' else 'ai'
            ),
            'html_report_url': html_url,
            'summary': ai_report,
            'success': True,
            'regenerated': True
        }, to=room)

        socketio.emit('agent.patient_progress', {
            'percentage': 100,
            'message': '重新汇总完成！' if not _en else 'Re-aggregation complete!'
        }, to=room)

        logger.info(f"[重新汇总] ✅ 完成: {patient_dir_name}")

    except Exception as e:
        logger.error(f"[重新汇总] 失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        socketio.emit('agent.error', {
            'error': f'重新汇总失败: {str(e)}'
        }, to=room)


def run_single_wsi_for_patient(socketio, room: str, file_id: str, part_name: str,
                                folder_name: str, wsi_index: int, total_wsi: int,
                                mode: str = "ai", patient_id: str = None,
                                lang: str = 'zh',
                                analysis_strategy: str = 'agent_swarm',
                                diagnostic_question: str = '',
                                patient_event_sink=None,
                                collaboration_review_policy: str = 'staged') -> dict:
    """
    为Patient Level分析单张WSI
    
    ⭐ 重要：直接调用 run_agent 函数，使用完全相同的分析流程（红→蓝→绿三级下钻），
    只是传入 Patient Level 上下文，确保分析结果与单张模式完全一致。
    
    Args:
        patient_id: 患者 ID（用于确定正确的输出路径）
    
    Returns:
        dict: WSI分析结果，包含 pathology_findings, tree_nodes 等
    """
    logger.info(f"[Patient Level] 调用 run_agent 分析 {part_name}: {file_id}")
    
    # 构建 Patient Level 上下文（包含 patient_id 用于正确的输出路径）
    patient_level_context = {
        'part_name': part_name,
        'folder_name': folder_name,
        'wsi_index': wsi_index,
        'total_wsi': total_wsi,
        'patient_id': patient_id,  # ⭐ 关键：传入 patient_id 用于正确的输出路径
        'preserve_stop_flag': True,
        '_patient_event_sink': patient_event_sink,
    }
    
    # ⭐ 直接调用 run_agent，使用完全相同的分析流程
    # 传入 patient_level_context 参数，run_agent 会返回分析结果而不是发送 done 事件
    result = run_agent(
        socketio=socketio,
        room=room,
        file_id=file_id,
        max_steps=12,
        strategy="default",
        mode=mode,
        patient_level_context=patient_level_context,
        lang=lang,
        analysis_strategy=analysis_strategy,
        diagnostic_question=diagnostic_question,
        collaboration_review_policy=collaboration_review_policy,
    )
    
    return result


def get_candidate_rois_from_thumbnail_patient_level(thumb_path, dimensions, agent_state):
    """
    Patient Level版本：从缩略图获取候选ROI
    使用Patient Level专属提示词
    
    ⭐ 修复：使用正方形网格，保证 10x/40x/160x 放大倍率准确
    """
    from agent.gpt_client import call_gpt_vision
    import math
    
    part_name = agent_state.get('part_name', 'Slide 1')
    folder_name = agent_state.get('folder_name', 'Patient')
    wsi_index = agent_state.get('wsi_index', 1)
    total_wsi = agent_state.get('total_wsi', 1)
    organ = agent_state.get('organ', '未指定标本')
    _lang = agent_state.get('lang', 'zh')
    
    # 创建网格
    Image.MAX_IMAGE_PIXELS = None  # 允许处理大图像
    thumb_img = Image.open(thumb_path)
    thumb_w, thumb_h = thumb_img.size
    
    # 计算缩放比例
    scale_x = dimensions[0] / thumb_w
    scale_y = dimensions[1] / thumb_h
    
    # ⭐⭐⭐ 修复：放弃强制正方形，rect 宽高分别用 scale_x/scale_y 映射 ⭐⭐⭐
    # 之前使用 min(scale_x, scale_y) 统一缩放，导致：
    #   1. rect 与缩略图上的实际网格区域不对应
    #   2. 边缘格子的 rect 超出切片范围，LLM 看到空白区域
    #   3. 蓝框编号位置与前端显示不一致
    target_grid_count = 4  # 目标约 4x4 = 16 个网格
    cell_size = min(thumb_w, thumb_h) // target_grid_count
    
    # 根据正方形尺寸计算实际行列数（缩略图上仍然是视觉正方形网格）
    grid_cols = math.ceil(thumb_w / cell_size)
    grid_rows = math.ceil(thumb_h / cell_size)
    
    logger.info(f"[Patient Level] 网格: {cell_size}×{cell_size} 缩略图像素, {grid_rows}行×{grid_cols}列")
    logger.info(f"[Patient Level] 缩略图: {thumb_w}×{thumb_h}, scale_x={scale_x:.2f}, scale_y={scale_y:.2f}")
    
    grid_rois = []
    grid_id = 1
    for row in range(grid_rows):
        for col in range(grid_cols):
            x = col * cell_size
            y = row * cell_size
            
            # 确保不超出缩略图边界（边缘格子会被裁切）
            actual_w = min(cell_size, thumb_w - x)
            actual_h = min(cell_size, thumb_h - y)
            
            # ⭐ 修复：分别用 scale_x 和 scale_y 映射宽高，保持与缩略图一致
            orig_x = int(x * scale_x)
            orig_y = int(y * scale_y)
            orig_w = int(actual_w * scale_x)  # 用 actual_w（已裁切）× scale_x
            orig_h = int(actual_h * scale_y)  # 用 actual_h（已裁切）× scale_y
            
            grid_rois.append({
                'id': f'R{grid_id}',
                'grid_id': grid_id,
                'rect': {'x': orig_x, 'y': orig_y, 'w': orig_w, 'h': orig_h},  # ⭐ 与缩略图一致
                'thumb_rect': {'x': x, 'y': y, 'w': actual_w, 'h': actual_h}
            })
            grid_id += 1
    
    logger.info(f"[Patient Level] 生成 {len(grid_rois)} 个红框候选 (边缘格子已裁切)")
    
    # 在缩略图上标注网格
    annotated_path = annotate_thumbnail_grid(thumb_path, grid_rois)
    
    # 使用Patient Level提示词
    prompt = get_patient_level_red_prompt(
        total_boxes=len(grid_rois),
        part_name=part_name,
        folder_name=folder_name,
        wsi_index=wsi_index,
        total_wsi=total_wsi,
        organ=organ,
        lang=_lang
    )
    
    # 读取图像并转换为base64
    image_path = str(annotated_path) if annotated_path else str(thumb_path)
    image_base64 = encode_image_file_to_base64(image_path)
    
    response = _strip_structured_preamble(call_gpt_vision(prompt, image_base64))
    
    # 解析选择
    selected_ids = parse_selection_response(response, len(grid_rois))
    
    if not selected_ids:
        # 默认选择中间区域
        selected_ids = [5, 6, 9, 10]
    
    # 返回选中的ROI
    selected_rois = [roi for roi in grid_rois if roi['grid_id'] in selected_ids]
    
    return selected_rois[:5]  # 最多5个红框


def annotate_thumbnail_grid(thumb_path, grid_rois):
    """在缩略图上标注网格编号"""
    try:
        from PIL import ImageDraw, ImageFont
        Image.MAX_IMAGE_PIXELS = None  # 允许处理大图像
        
        img = Image.open(thumb_path)
        draw = ImageDraw.Draw(img)
        
        # ⭐ 自适应字体大小，与 filter_rois_by_gpt_vision 保持一致
        _img_max_dim = max(img.size[0], img.size[1])
        _font_size = max(20, int(_img_max_dim * 0.018))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", _font_size)
        except:
            font = ImageFont.load_default()
        logger.info(f"🔤 [annotate_thumbnail_grid] 画布={img.size[0]}×{img.size[1]}, 字体={_font_size}px")
        
        for roi in grid_rois:
            rect = roi['thumb_rect']
            grid_id = roi['grid_id']
            
            # 绘制边框
            draw.rectangle(
                [rect['x'], rect['y'], rect['x'] + rect['w'], rect['y'] + rect['h']],
                outline='red',
                width=2
            )
            
            # 绘制编号
            text_x = rect['x'] + 5
            text_y = rect['y'] + 5
            draw.text((text_x, text_y), str(grid_id), fill='red', font=font)
        
        annotated_path = str(thumb_path).replace('.jpg', '_annotated.jpg')
        img.save(annotated_path, quality=90)
        
        return annotated_path
        
    except Exception as e:
        logger.warning(f"标注缩略图失败: {e}")
        return None


def create_blue_grid(red_rect, grid_size=4):
    """在红框内创建蓝框网格"""
    cell_w = red_rect['w'] // grid_size
    cell_h = red_rect['h'] // grid_size
    
    blue_rois = []
    for row in range(grid_size):
        for col in range(grid_size):
            grid_id = row * grid_size + col + 1
            x = red_rect['x'] + col * cell_w
            y = red_rect['y'] + row * cell_h
            
            blue_rois.append({
                'id': f'B{grid_id}',
                'grid_id': grid_id,
                'rect': {'x': x, 'y': y, 'w': cell_w, 'h': cell_h}
            })
    
    return blue_rois


def annotate_grid_on_image(image_path, grid_rois):
    """在图像上标注网格"""
    try:
        from PIL import ImageDraw, ImageFont
        Image.MAX_IMAGE_PIXELS = None  # 允许处理大图像
        
        img = Image.open(image_path)
        draw = ImageDraw.Draw(img)
        
        # 计算缩放
        img_w, img_h = img.size
        if grid_rois:
            first_rect = grid_rois[0]['rect']
            # 找到网格的边界
            min_x = min(r['rect']['x'] for r in grid_rois)
            min_y = min(r['rect']['y'] for r in grid_rois)
            max_x = max(r['rect']['x'] + r['rect']['w'] for r in grid_rois)
            max_y = max(r['rect']['y'] + r['rect']['h'] for r in grid_rois)
            
            scale_x = img_w / (max_x - min_x) if max_x > min_x else 1
            scale_y = img_h / (max_y - min_y) if max_y > min_y else 1
        
        # ⭐ 自适应字体大小
        _img_max_dim = max(img_w, img_h)
        _font_size = max(16, int(_img_max_dim * 0.018))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", _font_size)
        except:
            font = ImageFont.load_default()
        
        for roi in grid_rois:
            rect = roi['rect']
            grid_id = roi['grid_id']
            
            # 转换到图像坐标
            x1 = int((rect['x'] - min_x) * scale_x)
            y1 = int((rect['y'] - min_y) * scale_y)
            x2 = int((rect['x'] + rect['w'] - min_x) * scale_x)
            y2 = int((rect['y'] + rect['h'] - min_y) * scale_y)
            
            draw.rectangle([x1, y1, x2, y2], outline='blue', width=2)
            draw.text((x1 + 3, y1 + 3), str(grid_id), fill='blue', font=font)
        
        annotated_path = str(image_path).replace('.jpg', '_grid.jpg')
        img.save(annotated_path, quality=90)
        
        return annotated_path
        
    except Exception as e:
        logger.warning(f"标注网格失败: {e}")
        return None


def parse_selection_response(response, max_count):
    """解析AI选择响应，提取编号列表（支持 Orchestrator 和 Sub-agent 格式）"""
    if not response:
        return []
    
    import re
    
    # 尝试多种模式匹配（按优先级排序）
    patterns = [
        # Orchestrator 格式：选择的红框区域 = [1, 3, 5]
        r'选择的红框区域\s*[=：:]\s*\[([\d,\s]+)\]',
        # Sub-agent Agent Loop 格式：选择的区域 = [1, 3, 5]
        r'选择的区域\s*[=：:]\s*\[([\d,\s]+)\]',
        # Agent Loop 格式：动作参数中的数组
        r'动作参数[：:].*?\[([\d,\s]+)\]',
        # 旧版格式：红框：[1, 2, 3]
        r'[红蓝绿]框[：:]\s*\[([\d,\s]+)\]',
        # 通用数组格式
        r'\[([\d,\s]+)\]',
        # 选择格式
        r'选择[：:]?\s*([\d,\s、]+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, response, re.DOTALL)
        if match:
            numbers_str = match.group(1)
            # 提取数字
            numbers = re.findall(r'\d+', numbers_str)
            result = [int(n) for n in numbers if int(n) <= max_count]
            if result:
                return result
    
    return []


def create_roi_overlay(roi, color='red', magnification=10):
    """创建ROI overlay对象"""
    color_map = {
        'red': '#FF0000',
        'blue': '#0066FF', 
        'green': '#00CC00'
    }
    
    return {
        'id': roi['id'],
        'original_id': roi.get('original_id') or roi.get('id'),
        'rect': roi['rect'],
        'color': color_map.get(color, color),
        'magnification': magnification,
        'label': ''  # ⭐ 不显示标签，保持界面干净
    }

# ================== End Patient Level ==================


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


def _incomplete_decision_retry_prompt(prompt, decision, frame_type='blue', lang='zh'):
    """Retry cue for an incomplete parse. Do not nudge D/E/B/C into A."""
    action = ((decision or {}).get('action') or '').upper()
    parse_status = (decision or {}).get('parse_status') or ''
    is_en = (lang or 'zh') == 'en'
    is_green = (frame_type or 'blue') == 'green'
    if is_en:
        box_name = 'green boxes' if is_green else 'blue boxes'
        box_example = 'green boxes: [1, 2, 3]' if is_green else 'blue boxes: [1, 2, 3]'
    else:
        box_name = '绿框' if is_green else '蓝框'
        box_example = f'{box_name}：[1, 2, 3]'

    if action == 'A' and parse_status in {'missing_selection', 'invalid_selection'}:
        if is_en:
            reminder = (
                f"⚠️ Important: you selected A(Drill-down). "
                f"Put the selection as {box_example} — a list of integers only."
            )
        else:
            reminder = (
                f"⚠️ 重要提醒：已选择 A(下钻)。请确保【选择结果】格式正确，"
                f"必须是 {box_example} 这样的纯数字列表！"
            )
    elif action == 'D' and parse_status == 'missing_parameter':
        if is_en:
            reminder = (
                "⚠️ Important: you selected D(Search). Fill in the search query only. "
                "Do not switch to A, and do not fill box indices.\n"
                "Keep Selected action: D, and give Action parameter: <search query>."
            )
        else:
            reminder = (
                "⚠️ 重要提醒：已选择 D(查库/Search)。请只补全检索问题，"
                "不要改成 A，也不要填写框编号。\n"
                "请保持 Selected action: D，并给出 Action parameter: <search query>。"
            )
    else:
        if is_en:
            reminder = (
                "⚠️ Important: in [Plan], write Selected action: A/B/C/D/E. "
                f"Fill the {box_name} index list only when the action is A(Drill-down); "
                "when the action is D, fill the search query only."
            )
        else:
            reminder = (
                "⚠️ 重要提醒：请在 [Plan] 中明确写出 Selected action: A/B/C/D/E。"
                f"只有选择 A(下钻) 时才填写{box_name}编号列表；"
                "选择 D 时只填写检索问题。"
            )
    return f"{prompt}\n\n{reminder}"


def _replanning_prompt_parts(replanning_context, lang='zh'):
    """Split a local D/E replan payload into prompt fragments. Never uses shared state."""
    replan = replanning_context if isinstance(replanning_context, dict) else {}
    kind = replan.get('kind', '')
    content = str(replan.get('content', '') or '')
    neighbor = content if kind == 'neighbor' else ''
    knowledge = content if kind == 'knowledge' else ''
    parent_extra = ''
    if kind == 'knowledge' and content:
        label = (
            "Knowledge reference (not image evidence)"
            if lang == 'en'
            else "知识参考（非图像证据）"
        )
        parent_extra = f"[{label}]\n{content}"
    return neighbor, knowledge, parent_extra


def drill_selection_guidance_note(lang='zh'):
    """Qualitative selection hint only. Do not name a count or hard-truncate."""
    lang = _normalize_output_language(lang)
    if lang == 'en':
        return (
            "\n\n💡 Selection principle: fewest boxes, highest diagnostic value. "
            "Select only regions essential to the current question; fewer is better. "
            "You do not have to drill at every level. Do not add extra boxes just in case. "
            "B(Stop) or [] remains valid."
        )
    return (
        "\n\n💡 选择原则：最少框、最高性价比。"
        "只选对回答当前问题必不可少的区域，能少则少，不必每层都下钻，不要为了保险再加框。"
        "仍可选择 B(停止) 或空列表。"
    )


def filter_rois_by_gpt_vision(slide, rois, thumbnail_size=(2000, 2000), max_select=None, parent_roi=None, wsi_name=None, return_analysis=False, agent_state=None, replanning_context=None):
    """
    使用AI-Vision从概览图中筛选出包含有效病理组织的网格
    
    ⭐ Agent Loop 关键函数：AI 的回复同时包含【思考过程】（分析）和【选择结果】
    当 return_analysis=True 时，返回完整的AI分析（用于红框分析+蓝框选择合并）
    
    Args:
        slide: OpenSlide对象
        rois: 候选ROI列表（包含rect信息）
        thumbnail_size: 用于生成概览图的缩略图尺寸
        max_select: 最大选择数量
        parent_roi: 父ROI信息（用于生成局部上下文概览图），如果为None则使用全片缩略图
        wsi_name: WSI文件名（用于创建调试图片子目录）
        return_analysis: 是否返回完整的 AI 分析响应（Agent Loop 模式需要）
        agent_state: Agent状态（用于判断是否为Patient Level模式）
        
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

        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(thumbnail)
        
        # 加载字体 - ⭐ 自适应字体大小
        # 缩略图（无 parent_roi）用较小系数 ~1.0%，钻取图（有 parent_roi）用 ~1.8%
        _img_max_dim = max(thumbnail.size[0], thumbnail.size[1])
        if parent_roi:
            _font_size = max(20, int(_img_max_dim * 0.018))  # 钻取图：大字体，LLM 能看清
        else:
            _font_size = max(18, int(_img_max_dim * 0.010))  # 缩略图：小字体，不遮挡组织
        try:
            font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" 
            font = ImageFont.truetype(font_path, _font_size)
        except:
            font = ImageFont.load_default()
        logger.info(f"🔤 [字体] 画布={thumbnail.size[0]}×{thumbnail.size[1]}, 字体={_font_size}px")
            
        # 全局和局部候选使用同一套宽松组织掩膜。这里只排除明显空白，
        # 不做形态学处理，避免误删细长、边缘和碎片化组织。
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
                
                # 如果组织占比低于阈值，视为无效区域，不参与AI筛选
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
        debug_file_id = str((agent_state or {}).get('file_id') or '').strip()
        if debug_file_id:
            # Use the opaque file identity rather than the display filename;
            # equal filenames from different users must not share a directory.
            debug_dir = debug_base_dir / debug_file_id
        elif wsi_name:
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
        
        # 构建 Prompt——使用规范化 Agent Loop 提示词
        valid_indices = list(roi_map.keys())
        
        # 如果所有候选仍低于阈值，安全终止本次下钻。绝不恢复未过滤候选，
        # 否则白色背景会被重新编号并送给模型。
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
                    'agent_action': 'C',
                    'agent_reason': (
                        "No candidate ROI met the "
                        f"{TISSUE_CONTENT_THRESHOLD:.0%} tissue-content threshold."
                    ),
                    # Legacy aliases retained for extension compatibility.
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
        organ = agent_state.get('organ', '未指定标本') if agent_state else '未指定标本'
        
        # ⭐ 判断是否为 Patient Level 模式
        is_patient_level = agent_state and agent_state.get('is_patient_level', False)
        
        # 根据frame_type使用对应的规范化提示词函数
        frame_type = parent_roi.get('frame_type', 'red') if parent_roi else 'red'
        # ⭐ 从 agent_state 获取语言设置
        _lang = agent_state.get('lang', 'zh') if agent_state else 'zh'
        _neighbor_ctx, _knowledge_ctx, _knowledge_parent = _replanning_prompt_parts(
            replanning_context, _lang
        )

        if frame_type == 'red' or not parent_roi:
            if is_patient_level:
                # ⭐ Patient Level 模式：使用专门的病理诊断报告收集证据提示词
                prompt = get_patient_level_red_prompt(
                    total_boxes=len(valid_indices),
                    part_name=agent_state.get('part_name', 'Slide 1'),
                    folder_name=agent_state.get('folder_name', 'Patient'),
                    wsi_index=agent_state.get('wsi_index', 1),
                    total_wsi=agent_state.get('total_wsi', 1),
                    organ=organ,
                    lang=_lang
                )
                logger.info(f"[Patient Level] 使用 Patient Level 红框提示词 (Part: {agent_state.get('part_name')}, lang={_lang})")
            else:
                # 使用规范化红框提示词
                prompt = get_red_selection_prompt(
                    len(valid_indices), organ, _lang,
                    agent_state.get('diagnostic_question', '') if agent_state else ''
                )
            # (有效编号列表在下方统一追加)
        elif frame_type == 'blue':
            parent_id = parent_roi.get('id', '?')
            
            if is_patient_level:
                # ⭐⭐⭐ Patient Level 模式：获取路径上下文（来自缩略图分析） ⭐⭐⭐
                parent_context = ""
                if agent_state:
                    # 方法1: 从 global_analysis 获取（缩略图级别的全局分析）
                    global_analysis = agent_state.get('global_analysis', '')
                    if isinstance(global_analysis, str) and global_analysis:
                        parent_context = f"【缩略图全局分析】\n{global_analysis}"
                    elif isinstance(global_analysis, dict):
                        reasoning = global_analysis.get('reasoning', '')
                        if reasoning:
                            parent_context = f"【缩略图全局分析】\n{reasoning}"
                    
                    # 方法2: 补充 thumbnail_analysis（如果有的话）
                    thumbnail_analysis = agent_state.get('thumbnail_analysis', {})
                    if thumbnail_analysis and isinstance(thumbnail_analysis, dict):
                        findings = thumbnail_analysis.get('findings', '')
                        if findings and findings not in parent_context:
                            parent_context += f"\n【缩略图发现】{findings}"
                
                # ⭐⭐⭐ 保存当前红框上下文到 agent_state，供绿框分析使用 ⭐⭐⭐
                if parent_context:
                    agent_state['current_red_context'] = {
                        'roi_id': parent_id,
                        'findings': parent_context,
                        'reasoning': parent_context
                    }
                    logger.info(f"[Patient Level] 蓝框获取到路径上下文 (长度: {len(parent_context)})")
                else:
                    logger.warning(f"[Patient Level] ⚠️ 蓝框未找到路径上下文")
                
                if _knowledge_parent:
                    parent_context = f"{parent_context}\n\n{_knowledge_parent}".strip()
                if _neighbor_ctx:
                    parent_context = f"{parent_context}\n\n{_neighbor_ctx}".strip()
                
                # ⭐ Patient Level 模式：使用病理诊断报告蓝框提示词（带上下文）
                prompt = get_patient_level_blue_prompt(
                    red_idx=parent_id,
                    num_blue_boxes=len(valid_indices),
                    part_name=agent_state.get('part_name', 'Slide 1'),
                    folder_name=agent_state.get('folder_name', 'Patient'),
                    organ=organ,
                    parent_context=parent_context,
                    wsi_index=agent_state.get('wsi_index', 1),
                    lang=_lang
                )
                logger.info(f"[Patient Level] 使用 Patient Level 蓝框提示词 (带路径上下文, lang={_lang})")
            else:
                # ⭐ Agent Loop 规范化提示词——传入缩略图全局分析作为路径上下文
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
                
                if _knowledge_parent:
                    parent_context = f"{parent_context}\n\n{_knowledge_parent}".strip()
                prompt = get_blue_selection_prompt_agent_loop(
                    red_idx=parent_id,
                    num_blue_boxes=len(valid_indices),
                    parent_context=parent_context,
                    neighbor_context=_neighbor_ctx,
                    knowledge_context=_knowledge_ctx,
                    organ=organ,
                    lang=_lang,
                    diagnostic_objective=agent_state.get('diagnostic_question', '') if agent_state else ''
                )
                logger.info(f"🔵 [Agent Loop] 使用规范化蓝框提示词 (红框 {parent_id}, 上下文长度={len(parent_context)}, 邻近上下文={'有' if _neighbor_ctx else '无'}, MCP循证上下文={'有' if _knowledge_ctx else '无'}, lang={_lang})")
        else:
            # 绿框选择（frame_type == 'green'）
            parent_id = parent_roi.get('id', '?')
            
            if is_patient_level:
                # ⭐⭐⭐ Patient Level 模式：获取路径上下文（红框+蓝框分析结果） ⭐⭐⭐
                parent_context = ""
                if agent_state:
                    # 方法1: 从 parent_roi 中提取蓝框的父红框信息，获取对应分析
                    blue_parent_red_id = parent_roi.get('parent_id', '') if parent_roi else ''
                    
                    # 方法2: 从 red_analyses 中查找对应红框的分析结果
                    red_analyses = agent_state.get('red_analyses', [])
                    for red_analysis in red_analyses:
                        if red_analysis.get('roi_id') == blue_parent_red_id or not blue_parent_red_id:
                            red_reasoning = red_analysis.get('reasoning', '') or red_analysis.get('findings', '')
                            if red_reasoning:
                                parent_context += f"【红框 {red_analysis.get('roi_id', '?')} 分析】\n{red_reasoning}\n\n"
                                break
                    
                    # 方法3: 从 current_red_context 和 current_blue_context 获取（备用）
                    if not parent_context:
                        red_context = agent_state.get('current_red_context', {})
                        if red_context:
                            red_findings = red_context.get('findings', '') or red_context.get('reasoning', '')
                            if red_findings:
                                parent_context += f"【红框分析】{red_findings}\n"
                    
                    blue_context = agent_state.get('current_blue_context', {})
                    if blue_context:
                        blue_findings = blue_context.get('findings', '') or blue_context.get('reasoning', '')
                        if blue_findings:
                            parent_context += f"【蓝框分析】{blue_findings}\n"
                    
                    # 方法4: 从 global_analysis 获取（作为最后的补充）
                    if not parent_context:
                        global_analysis = agent_state.get('global_analysis', '')
                        if isinstance(global_analysis, str) and global_analysis:
                            parent_context = f"【全局分析】\n{global_analysis}"
                        elif isinstance(global_analysis, dict):
                            reasoning = global_analysis.get('reasoning', '')
                            if reasoning:
                                parent_context = f"【全局分析】\n{reasoning}"
                
                # ⭐ 保存当前蓝框上下文到 agent_state
                if parent_context:
                    agent_state['current_blue_context'] = {
                        'roi_id': parent_id,
                        'findings': parent_context,
                        'reasoning': parent_context
                    }
                    logger.info(f"[Patient Level] 绿框获取到路径上下文 (长度: {len(parent_context)})")
                else:
                    logger.warning(f"[Patient Level] ⚠️ 绿框未找到路径上下文")
                
                if _knowledge_parent:
                    parent_context = f"{parent_context}\n\n{_knowledge_parent}".strip()
                if _neighbor_ctx:
                    parent_context = f"{parent_context}\n\n{_neighbor_ctx}".strip()
                
                # ⭐ Patient Level 模式：使用病理诊断报告绿框提示词（带上下文）
                prompt = get_patient_level_green_prompt(
                    blue_idx=parent_id,
                    num_green_boxes=len(valid_indices),
                    part_name=agent_state.get('part_name', 'Slide 1'),
                    folder_name=agent_state.get('folder_name', 'Patient'),
                    organ=organ,
                    parent_context=parent_context,
                    wsi_index=agent_state.get('wsi_index', 1),
                    lang=_lang
                )
                logger.info(f"[Patient Level] 使用 Patient Level 绿框提示词 (带路径上下文, lang={_lang})")
            else:
                # ⭐ Agent Loop 规范化提示词——传入完整路径上下文：缩略图 + 红框
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
                
                if _knowledge_parent:
                    parent_context = f"{parent_context}\n\n{_knowledge_parent}".strip()
                prompt = get_green_selection_prompt_agent_loop(
                    blue_idx=parent_id,
                    num_green_boxes=len(valid_indices),
                    parent_context=parent_context,
                    neighbor_context=_neighbor_ctx,
                    knowledge_context=_knowledge_ctx,
                    organ=organ,
                    lang=_lang,
                    diagnostic_objective=agent_state.get('diagnostic_question', '') if agent_state else ''
                )
                logger.info(f"🟢 [Agent Loop] 使用规范化绿框提示词 (蓝框 {parent_id}, 父红框={blue_parent_red_id if agent_state else '?'}, 上下文长度={len(parent_context)}, 邻近上下文={'有' if _neighbor_ctx else '无'}, MCP循证上下文={'有' if _knowledge_ctx else '无'}, lang={_lang})")

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

        prompt += drill_selection_guidance_note(_lang)
        logger.info("📋 [选择引导] 最少框、最高性价比（仅写入提示词，不硬截断）")

        # ⭐ 支持重试的AI调用：动作优先解析，失败 fail-closed，不猜前 5 格、不默认 A
        max_retries = 2
        response = None
        selected_indices = []
        decision = empty_mcp_decision('')

        for attempt in range(max_retries):
            if attempt == 0:
                _prep_elapsed = time.time() - _filter_start_time
                logger.info(f"⏱️ [计时] 准备工作完成，耗时: {_prep_elapsed:.2f}秒，即将调用 API...")
                logger.info("正在请求AI进行网格筛选...")
                raw_response = call_gpt_vision(prompt, image_base64)
            else:
                if mcp_decision_is_complete(decision):
                    break
                logger.warning(f"🔄 第{attempt+1}次重试AI筛选（上次解析失败）...")
                retry_prompt = _incomplete_decision_retry_prompt(
                    prompt, decision, frame_type=frame_type, lang=_lang
                )
                raw_response = call_gpt_vision(retry_prompt, image_base64)

            if not raw_response:
                logger.warning("⚠️ API 返回空响应（completion=0），跳过本次重试")
                continue
            response = _strip_structured_preamble(raw_response)
            logger.info(f"AI筛选响应: {response[:500]}...")

            decision = parse_mcp_response(response, valid_indices=valid_indices)
            action = (decision.get('action') or '').upper()
            selected_indices = list(decision.get('selected_indices') or [])
            if action in MCP_NON_DRILL_ACTIONS:
                selected_indices = []
                decision['selected_indices'] = []
            if mcp_decision_is_complete(decision):
                logger.info(f"✅ AI 选择了 {action}，选区={selected_indices}")
                break
            logger.warning(f"⚠️ 第{attempt+1}次解析失败，将重试...")

        action = (decision.get('action') or '').upper()
        selected_indices = list(decision.get('selected_indices') or [])
        if action in MCP_NON_DRILL_ACTIONS:
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

        if action in MCP_NON_DRILL_ACTIONS:
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
            logger.info(f"🔵 [Agent Loop] AI 选择 {mcp_action or '(none)'}，选中 {len(final_rois)} 个区域")

            if mcp_action in MCP_NON_DRILL_ACTIONS:
                final_rois = []
                selected_indices = []

            return {
                'selected_rois': final_rois,
                'analysis': response,
                'selection_indices': selected_indices,
                'agent_action': mcp_action,
                'agent_reason': mcp_reason,
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
        if return_analysis:
            return {
                'selected_rois': [],
                'analysis': '',
                'selection_indices': [],
                'agent_action': '',
                'agent_reason': 'filter_exception_fail_closed',
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
        # Patient Level 模式下用 output_dir（WSI 级别）而非 base_dir（患者级别），
        # 避免不同 WSI 的缓存互相覆盖
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
        
        # 缓存命名空间包含视野配置和过滤策略。旧缓存保留在磁盘上，
        # 但不会被当前算法误复用。
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
                
                # 验证缓存有效性：尺寸、版本、视野配置和过滤策略必须一致。
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
        
        # ⭐ 当前目录无缓存时，搜索同一 WSI 的历史缓存并复制过来。
        # ⚠️ 绝不能对 outputs 做全树递归 glob：历史 run 越积越多（每个 run 的
        # steps/、debug_images/、DZI 瓦片有数千文件），在 /mnt 挂载盘上一次
        # 递归扫描要几分钟，且每跑一次分析就更慢。这里只枚举固定两层深度、
        # 先用 run 目录名过滤同名 WSI，再按最新优先最多读少量 JSON。
        if not use_cache and wsi_name:
            try:
                _wsi_stem = Path(wsi_name).stem if wsi_name else ''
                if _wsi_stem:
                    outputs_root = Path("outputs")
                    if outputs_root.exists():
                        _search_started = time.time()
                        _cache_paths = []
                        for _run_dir in outputs_root.iterdir():
                            try:
                                if not _run_dir.is_dir() or _wsi_stem not in _run_dir.name:
                                    continue
                                _direct = _run_dir / "grid_rois_cache.json"
                                if _direct.exists():
                                    _cache_paths.append(_direct)
                                for _mode_dir in _run_dir.iterdir():
                                    if not _mode_dir.is_dir():
                                        continue
                                    _nested = _mode_dir / "grid_rois_cache.json"
                                    if _nested.exists():
                                        _cache_paths.append(_nested)
                            except OSError:
                                continue
                        _dated = []
                        for _cand in _cache_paths:
                            if _cand == cache_file:
                                continue
                            try:
                                _dated.append((_cand.stat().st_mtime, _cand))
                            except OSError:
                                continue
                        _dated.sort(key=lambda item: item[0], reverse=True)
                        _found_cache = None
                        # 只验证最新的少量候选；校验字段能保证正确性，数量上限只影响命中率。
                        for _mtime, candidate in _dated[:8]:
                            try:
                                with open(candidate, 'r', encoding='utf-8') as _cf:
                                    _cd = json.load(_cf)
                                # 历史缓存同样必须满足完整策略校验。
                                if (_cd.get('dimensions') == list(_slide_dims) 
                                    and _cd.get('cache_version') == CACHE_VERSION
                                    and _cd.get('view_profile') == VIEW_PROFILE_NAME
                                    and _cd.get('tissue_mask_policy') == TISSUE_MASK_POLICY
                                    and _cd.get('tissue_content_threshold')
                                    == TISSUE_CONTENT_THRESHOLD
                                    and _cd.get('grid_rois')):
                                    _found_cache = candidate
                                    break
                            except Exception:
                                continue
                        logger.info(
                            "⏱️ [历史缓存搜索] 定深扫描 %s 个候选，耗时 %.2f 秒",
                            len(_dated),
                            time.time() - _search_started,
                        )
                        
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
            
            # === 1. 生成候选框专用宽松组织掩膜 ===
            # 全局外接矩形和各网格占比必须使用同一掩膜，避免前后标准不一致。
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
            # ceil 多出的空白格子会被 20% 组织阈值自动过滤
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
                    
                    # 直接切片同一张宽松掩膜，保证外接矩形与网格过滤一致。
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
            
            # ⭐⭐⭐ 保存完整候选框列表（AI筛选前）用于协作模式的点击添加功能 ⭐⭐⭐
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
                    'tissue_content_threshold': tissue_threshold,
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
            max_select=None,  # ⭐ 不限制数量，让AI根据切片复杂度决定
            parent_roi=None,  # 全局筛选
            wsi_name=wsi_name,
            return_analysis=True,  # ⭐ 获取AI的完整响应（包含【思考过程】）
            agent_state=agent_state  # ⭐ 传入agent_state用于Patient Level提示词
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
                # 回退：从 debug_images 搜索（file_id 键优先，文件名键兜底）
                sample_name = wsi_name.replace('.svs', '').replace('.ndpi', '') if wsi_name else file_id
                _thumb_found = _find_debug_image(
                    "debug_gpt_global_*.jpg",
                    file_id=file_id,
                    sample_name=sample_name,
                )
                if _thumb_found:
                    agent_state['global_thumbnail_path'] = _thumb_found
                    logger.info(f"✅ 保存全局缩略图路径(回退glob): {_thumb_found}")
        
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
    """生成仅供候选框筛选使用的宽松组织掩膜。

    这里刻意不使用 Otsu、HSV 或形态学开闭运算。候选框阶段的目标是排除
    明显空白背景，而不是精确分割组织；形态学操作会误删细长、碎片化和
    切缘组织，造成假阴性候选框过滤。
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
        'original_id': roi.get('original_id') or roi.get('id'),
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
    
    # ⭐ 新格式：提取【Reason】+【Evidence】部分（Patient Level 使用，直接显示不转换）
    reason_match = re.search(r'【Reason】\s*([\s\S]*?)(?=【Plan】|$)', reasoning_text)
    if reason_match:
        reason_text = reason_match.group(1).strip()
        
        # 同时提取 Evidence 部分（如果有）- 直接显示，不做格式转换
        evidence_match = re.search(r'【Evidence】\s*([\s\S]*?)(?=【|$)', reasoning_text)
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
    """Parse the organ-agnostic ROI evidence contract."""
    import json
    import re

    confidence_to_percent = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}
    cleaned = _strip_structured_preamble(response or '')
    result = {
        'findings': '',
        'confidence': 50,
        'confidence_level': 2,
        # Absence of a classification field is not evidence that the model
        # explicitly judged the ROI uncertain.
        'pathology_type': 'unclassified',
        'key_features': [],
        'thinking': cleaned,
        'evidence': {},
        'evidence_confidence': None,
        'diagnosis_suggestion': '',
    }

    reason_match = re.search(
        r'(?:【Reason】|\[Reason\])\s*([\s\S]*?)'
        r'(?=【Plan】|\[Plan\]|【Evidence】|\[Evidence\]|'
        r'【路径证据汇总】|\[Path Evidence Summary\]|'
        r'【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
        cleaned,
    )
    if reason_match:
        result['thinking'] = reason_match.group(1).strip()

    evidence_match = re.search(
        r'(?:【Evidence】|\[Evidence\]|【路径证据汇总】|'
        r'\[Path Evidence Summary\])\s*([\s\S]*?)'
        r'(?=【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
        cleaned,
    )
    if evidence_match:
        evidence_text = evidence_match.group(1).strip()

        def extract_field(field_name):
            match = re.search(
                rf'[-•]?\s*{field_name}[：:]\s*([\s\S]*?)'
                rf'(?=\n[-•]\s*[A-Za-z_]+[：:]|$)',
                evidence_text,
                re.IGNORECASE,
            )
            return match.group(1).strip() if match else ''

        summary = extract_field('evidence')
        limitations = extract_field('limitations_or_counterevidence')
        if not summary:
            summary = evidence_text
        if summary:
            result['findings'] = summary[:2000]
            result['evidence']['summary'] = summary[:4000]
        if limitations:
            result['evidence']['limitations_or_counterevidence'] = limitations[:4000]

        confidence_match = re.search(
            r'(?:[-•]\s*)?confidence[：:]\s*(\d)',
            evidence_text,
            re.IGNORECASE,
        )
        if confidence_match:
            level = min(4, max(0, int(confidence_match.group(1))))
            result['confidence_level'] = level
            result['confidence'] = confidence_to_percent[level]
            result['evidence_confidence'] = level

        type_match = re.search(
            r'(?:[-•]\s*)?pathology_type[：:]\s*'
            r'(malignant|benign|uncertain)',
            evidence_text,
            re.IGNORECASE,
        )
        if type_match:
            result['pathology_type'] = type_match.group(1).lower()

        features_match = re.search(
            r'(?:[-•]\s*)?(?:key_findings|key_features)[：:]\s*'
            r'([\s\S]*?)(?=\n[-•]\s*[A-Za-z_]+[：:]|$)',
            evidence_text,
            re.IGNORECASE,
        )
        if features_match:
            feature_text = features_match.group(1).strip()
            quoted = re.findall(r'["\']([^"\']+)["\']', feature_text)
            result['key_features'] = quoted or [
                item.strip()
                for item in re.split(r'[,，;；]', feature_text.strip('[]'))
                if item.strip()
            ]

    diagnosis_match = re.search(
        r'(?:【诊断建议】|\[Diagnostic [Ii]mpression\])\s*([\s\S]*?)$',
        cleaned,
    )
    if diagnosis_match:
        result['diagnosis_suggestion'] = diagnosis_match.group(1).strip()

    if not result['findings']:
        conclusion_match = re.search(
            r'【分析结论】\s*([\s\S]*?)$',
            cleaned,
        )
        if conclusion_match:
            conclusion = conclusion_match.group(1)
            findings_match = re.search(
                r'[-•]\s*findings[：:]\s*([\s\S]*?)'
                r'(?=\n[-•]|$)',
                conclusion,
                re.IGNORECASE,
            )
            if findings_match:
                result['findings'] = findings_match.group(1).strip()
            confidence_match = re.search(
                r'[-•]\s*(?:confidence_level|confidence)[：:]\s*(\d+)',
                conclusion,
                re.IGNORECASE,
            )
            if confidence_match:
                raw_confidence = int(confidence_match.group(1))
                if raw_confidence <= 4:
                    level = min(4, max(0, raw_confidence))
                    result['confidence_level'] = level
                    result['confidence'] = confidence_to_percent[level]
                    result['evidence_confidence'] = level
                else:
                    percent = min(100, max(0, raw_confidence))
                    result['confidence'] = percent
                    result['confidence_level'] = min(4, round(percent / 25))
                    result['evidence_confidence'] = result['confidence_level']

    if not result['findings']:
        try:
            json_match = re.search(r'\{[\s\S]*\}', cleaned)
            if json_match:
                parsed = json.loads(json_match.group())
                result['findings'] = str(
                    parsed.get('findings')
                    or parsed.get('evidence')
                    or ''
                ).strip()
                result['thinking'] = str(
                    parsed.get('thinking')
                    or parsed.get('reasoning')
                    or result['thinking']
                )
                result['diagnosis_suggestion'] = str(
                    parsed.get('diagnosis_suggestion')
                    or parsed.get('diagnostic_impression')
                    or ''
                )
                raw_pathology_type = str(parsed.get('pathology_type') or '').strip().lower()
                if raw_pathology_type in {'malignant', 'benign', 'uncertain'}:
                    result['pathology_type'] = raw_pathology_type
                raw_confidence = parsed.get(
                    'confidence_level',
                    parsed.get('confidence'),
                )
                if raw_confidence is not None:
                    numeric = int(float(raw_confidence))
                    if numeric <= 4:
                        result['confidence_level'] = min(4, max(0, numeric))
                        result['confidence'] = confidence_to_percent[
                            result['confidence_level']
                        ]
                    else:
                        result['confidence'] = min(100, max(0, numeric))
                        result['confidence_level'] = min(
                            4,
                            round(result['confidence'] / 25),
                        )
                    result['evidence_confidence'] = result['confidence_level']
                if result['findings']:
                    result['evidence'] = {'summary': result['findings'][:4000]}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    if not result['findings']:
        result['findings'] = result['thinking'][:2000] or cleaned[:2000]
        if result['findings']:
            result['evidence'] = {'summary': result['findings'][:4000]}

    return result


def analyze_roi_parallel(
    slide,
    roi,
    zoom_level,
    save_dir=None,
    parent_context=None,
    is_patient_level=False,
    patient_level_info=None,
    lang='zh',
    agent_state=None,
):
    """Analyze one ROI without introducing an organ- or benchmark-specific task."""
    try:
        lang = _normalize_output_language(lang)
        roi_image, _ = extract_roi_at_zoom_level(slide, roi, zoom_level)

        image_path = None
        if save_dir:
            save_dir = Path(save_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            image_path = save_dir / f"{roi['id']}_{zoom_level}x.jpg"
            roi_image.save(image_path, 'JPEG', quality=90)

        buffer = io.BytesIO()
        if roi_image.size[0] > 2000 or roi_image.size[1] > 2000:
            roi_image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        roi_image.save(buffer, format='JPEG', quality=95)
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

        context_text = ''
        if parent_context and zoom_level > 10:
            context_text = build_context_prompt(
                zoom_level,
                parent_context,
                agent_state=agent_state,
                lang=lang,
            )

        if is_patient_level:
            info = patient_level_info or {}
            if lang == 'en':
                case_context = (
                    f"Case: {info.get('folder_name', 'Patient')} | "
                    f"Slide: {info.get('part_name', 'Slide 1')} | "
                    f"Specimen: {info.get('organ', 'unspecified')}\n"
                )
            else:
                case_context = (
                    f"病例：{info.get('folder_name', 'Patient')} | "
                    f"切片：{info.get('part_name', 'Slide 1')} | "
                    f"标本：{info.get('organ', '未指定标本')}\n"
                )
            context_text = case_context + context_text

        if zoom_level == 160:
            prompt = _build_generic_terminal_evidence_prompt(
                roi,
                context_text,
                lang=lang,
                objective=agent_state or '',
            )
        else:
            objective = get_active_diagnostic_objective(agent_state or '', lang)
            if lang == 'en':
                prompt = f"""You are PathCopilot SubAgent reviewing ROI {roi.get('id', 'unknown')} at {zoom_level}×.

## Current objective and answer contract
{objective}

## Upstream context
{context_text or 'No upstream context was recorded.'}

Describe only directly visible morphology at this scale. Separate observation from inference, identify relevant mimics and limitations, and do not introduce an organ-specific checklist. Do not extrapolate a selected field to the whole slide.

[Reason]
(Direct observations and scale-aware interpretation.)

[Evidence]
- evidence: (task-relevant evidence with ROI provenance)
- limitations_or_counterevidence: (specific uncertainty, mimic, or sampling limitation)
- confidence: N (0-4)"""
            else:
                prompt = f"""你是 PathCopilot SubAgent，正在复核 {zoom_level}× ROI {roi.get('id', 'unknown')}。

## 当前目标与回答契约
{objective}

## 上游上下文
{context_text or '未记录上游上下文。'}

只描述当前倍率下可直接观察的形态；区分观察与推断，说明相关模拟病变及局限性，不得加入器官专项检查表，也不得把主动选择的局部视野外推为全切片结论。

【Reason】
（直接观察与符合当前倍率的解释。）

【Evidence】
- evidence：（与当前任务相关并可追溯到 ROI 的证据）
- limitations_or_counterevidence：（具体不确定性、模拟病变或取材局限）
- confidence：N（0-4）"""

        response = _strip_structured_preamble(call_gpt_vision(prompt, image_base64))
        if not response:
            raise RuntimeError('Empty model response')
        parsed = parse_structured_response(response, zoom_level)

        return {
            'roi_id': roi['id'],
            'zoom_level': zoom_level,
            'success': True,
            'findings': parsed.get('findings', ''),
            'confidence': parsed.get('confidence', 50),
            'confidence_level': parsed.get('confidence_level', 2),
            'pathology_type': parsed.get('pathology_type', 'unclassified'),
            'key_features': parsed.get('key_features', []),
            'image_path': str(image_path) if image_path else None,
            'parent_id': roi.get('parent_id'),
            'rect': roi.get('rect'),
            'reasoning': response,
            'thinking': parsed.get('thinking', response),
            'evidence': parsed.get('evidence', {}),
            'evidence_confidence': parsed.get('evidence_confidence'),
            'secondary_review': parsed.get('secondary_review'),
            'secondary_review_verdict': parsed.get('secondary_review_verdict'),
            'diagnosis_suggestion': parsed.get('diagnosis_suggestion', ''),
            'context_used': context_text,
        }
    except Exception as exc:
        logger.error(f"并行分析失败: {exc}")
        return {
            'roi_id': roi.get('id', 'unknown'),
            'zoom_level': zoom_level,
            'success': False,
            'findings': f"Analysis failed: {exc}" if lang == 'en' else f"分析失败: {exc}",
            'confidence': 0,
            'confidence_level': 0,
            'pathology_type': 'unclassified',
            'key_features': [],
            'image_path': None,
            'parent_id': roi.get('parent_id'),
            'rect': roi.get('rect'),
            'reasoning': f"Analysis failed: {exc}" if lang == 'en' else f"分析失败: {exc}",
            'evidence': {},
            'evidence_confidence': None,
            'diagnosis_suggestion': '',
            'context_used': '',
        }


def _emit_green_analysis_result(socketio, room, ga, index=1):
    """Show one finished 160× green-box result in the workspace immediately.

    Green boxes are analyzed in a thread pool.  If the UI only receives the
    start notice until the whole pool joins, the Subagent pane stays on
    “开始分析绿框” even after individual 160× write-ups are ready.
    """
    if not socketio or not isinstance(ga, dict):
        return
    green_roi_id = str(ga.get('roi_id') or '').strip()
    if not green_roi_id:
        return
    parent_blue_id = str(ga.get('parent_id') or '').strip()
    if not parent_blue_id and '-' in green_roi_id:
        parent_blue_id = '-'.join(green_roi_id.split('-')[:-1])
    observation = ga.get('reasoning') or ga.get('findings') or ''
    findings = ga.get('findings') or observation
    socketio.emit('agent.green_analysis', {
        'parent_blue_id': parent_blue_id,
        'green_id': green_roi_id,
        'rect': ga.get('rect') or {},
        'observation': observation,
        'findings': findings,
        'confidence': ga.get('confidence', 0.5),
        'success': ga.get('success', True),
    }, to=room)
    socketio.emit('agent.message', {
        'type': 'assistant',
        'content': f"🟢 {green_roi_id} 分析结果：\n{findings}",
        'reasoning': observation,
        'level': 'green',
        'roi_id': green_roi_id,
        'parent_id': parent_blue_id,
        'index': index,
        'rect': ga.get('rect'),
        'is_result': True,
    }, to=room)
    try:
        socketio.sleep(0)
    except Exception:
        pass


def execute_multiscale_roi_analysis(socketio, room, slide, roi_info, step, file_id, steps_dir, agent_state):
    """
    执行多尺度循证分析（并行版本）：
    
    协作模式下分两阶段：
    - Phase 1（本函数并行执行）：分析红框 + 筛选蓝框，返回结果供后续确认
    - Phase 2（确认后由 execute_blue_green_phase 执行）：分析蓝框 + 筛选绿框 + 分析绿框
    
    自动模式下一次性完成所有阶段。
    """
    try:
        import concurrent.futures

        if _agent_stop_requested(room):
            logger.info(f"⛔ 红框 {roi_info.get('id', '?')} 启动前已取消")
            return _stopped_multiscale_result(roi_info)
        
        is_collaborative = agent_state.get('mode') == 'collaborative'
        
        # ⭐ 调试：打印模式
        logger.info(f"🔍 [调试] execute_multiscale_roi_analysis: mode={agent_state.get('mode')}, is_collaborative={is_collaborative}")
        
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
        # ⭐ Agent Loop 关键：蓝框选择的 AI 响应同时包含【红框分析】和【蓝框选择】
        logger.info(f"父红框 {roi_info['id']} 坐标: x={roi_info['rect']['x']}, y={roi_info['rect']['y']}, w={roi_info['rect']['w']}, h={roi_info['rect']['h']}")
        # 4x4网格 = 16个蓝框候选
        blue_rois = subdivide_roi_into_grid(roi_info, grid_rows=4, grid_cols=4, child_zoom_level=40)
        logger.info(f"红框 {roi_info['id']} → 生成16个蓝框（4×4网格），AI将根据复杂度选择")
        
        # 创建保存目录
        roi_save_dir = steps_dir / roi_info['id']
        
        # ⭐ Agent Loop 模式：AI 看到红框的蓝框网格图，一个回复中同时给出分析和选择
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

        if _agent_stop_requested(room):
            logger.info(f"⛔ 红框 {roi_info['id']} 的 10× 请求返回后停止；不执行动作或下钻")
            return _stopped_multiscale_result(roi_info)

        def _replan_red(replanning_context):
            logger.info(
                f"🔄 [Agent Loop] 红框 {roi_info['id']}: 回注"
                f"{replanning_context.get('kind', '')}上下文后重新规划一次"
            )
            return filter_rois_by_gpt_vision(
                slide, blue_rois, (2000, 2000), None, roi_info_for_blue, wsi_name,
                return_analysis=True,
                agent_state=agent_state,
                replanning_context=replanning_context,
            )

        blue_result, agent_action_result, red_tool_trace = resolve_mcp_action_with_one_replan(
            initial_result=blue_result,
            roi_info=roi_info,
            agent_state=agent_state,
            replan_callback=_replan_red,
            socketio=socketio,
            room=room,
        )

        if _agent_stop_requested(room):
            logger.info(f"⛔ 红框 {roi_info['id']} 规划完成后停止；不生成蓝框任务")
            return _stopped_multiscale_result(roi_info)
        
        # 根据 Agent Loop 动作决定下一步
        if agent_action_result['action'] == 'backtrack':
            logger.warning(f"⏪ [Agent Loop] 红框 {roi_info['id']} 触发回溯: {agent_action_result['reason']}")
            return {
                'finding': f"回溯: {agent_action_result['reason']}",
                'confidence': 0,
                'next_action': 'backtrack',
                'tools_results': [],
                'tree_nodes': [],
                'backtrack_reason': agent_action_result['reason']
            }
        
        if agent_action_result['action'] == 'stop':
            logger.info(f"⏹️ [Agent Loop] 红框 {roi_info['id']} 停止下钻: {agent_action_result['reason']}")
            selected_blue_rois = []
        else:
            selected_blue_rois = agent_action_result['selected_rois']
        
        red_analysis_text = _strip_structured_preamble(blue_result['analysis'])  # ⭐ 仅保留正式结构化分析
        
        logger.info(f"✅ 蓝框选择完成: {len(blue_rois)} → {len(selected_blue_rois)}")
        logger.info(f"\n💭 [AI观察 - 红框{roi_info['id']}]")
        logger.info(red_analysis_text[:500] + "..." if len(red_analysis_text) > 500 else red_analysis_text)
        
        # ⭐ Agent Loop 关键：从蓝框选择响应构建红框分析结果
        # 响应中的【思考过程】就是红框分析，【选择结果】就是蓝框选择
        # ⭐ 解析AI响应为结构化数据（保留原始文本，同时提取思考过程和选择结果）
        def parse_ai_response(text):
            """解析AI响应，提取思考过程和选择结果"""
            thinking = ""
            selection = ""
            
            # 同时兼容旧版【思考过程】与新版 Agent Loop 的 Reason 标记。
            # 语言清理会把误输出的英文草稿裁到首个 Reason，因此这里必须
            # 能继续提取正式理由，避免界面只剩空摘要。
            thinking_match = re.search(
                r'(?:【思考过程】|【Reason】|\[Reason\])\s*'
                r'(.*?)(?=【选择结果】|【Plan】|\[Plan\]|【Evidence】|\[Evidence\]|$)',
                text,
                re.DOTALL,
            )
            if thinking_match:
                thinking = thinking_match.group(1).strip()
            
            # 提取旧版选择结果，或新版 Agent Loop 的 Plan 段。
            selection_match = re.search(
                r'(?:【选择结果】|【Plan】|\[Plan\])\s*'
                r'(.*?)(?=【Evidence】|\[Evidence\]|$)',
                text,
                re.DOTALL,
            )
            if selection_match:
                selection = selection_match.group(1).strip()
            
            return {
                'thinking': thinking,
                'selection': selection,
                'raw': text
            }
        
        parsed = parse_ai_response(red_analysis_text)
        red_thinking_text = _strip_structured_preamble(parsed['thinking'])
        
        # ⭐⭐⭐ 核心修复：区分"选择理由"和"形态学分析"
        # - selection_reasoning: AI为什么选择这些蓝框的理由（用于显示）
        # - findings: 应该是形态学分析（Agent Loop 模式下红框没有单独分析，设为简短摘要）
        # ⭐ 提取关键信息作为红框findings（而不是完整的选择理由）
        red_finding_summary = ''
        if red_thinking_text:
            # 从思考过程中提取第一句话作为摘要
            first_sentence = red_thinking_text.split('。')[0] if '。' in red_thinking_text else red_thinking_text[:100]
            # 过滤掉"区域X、Y、Z"这种选择格式的内容
            if not any(kw in first_sentence for kw in ['区域', '编号', '选择', '我选择']):
                red_finding_summary = first_sentence + '。' if not first_sentence.endswith('。') else first_sentence
            else:
                red_finding_summary = f'AI正在分析红框 {roi_info["id"]}，已选择 {len(selected_blue_rois)} 个蓝框进行详细分析'
        else:
            red_finding_summary = f'AI正在分析红框 {roi_info["id"]}，已选择 {len(selected_blue_rois)} 个蓝框进行详细分析'
        
        red_analysis = {
            'findings': red_finding_summary,  # ⭐ 简短摘要，不是完整的选择理由
            'selection_reasoning': red_thinking_text,  # ⭐ AI选择蓝框的完整理由（用于调试/报告）
            'reasoning': red_analysis_text,  # 完整AI推理过程（用于调试）
            'thinking': red_thinking_text,  # 思考过程
            'selection': parsed['selection'],  # 选择结果
            'confidence': 75,  # 默认置信度
            'pathology_type': 'analysis',
            'key_features': [],
            'image_path': None,  # 可后续补充
            'plan_action': blue_result.get('agent_action') or blue_result.get('mcp_action') or '',
            'selected_indices': list(blue_result.get('selection_indices') or []),
            'runtime_action': _effective_action_letter(agent_action_result),
            'mcp_tool_trace': red_tool_trace,
        }
        
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
        
        # ⭐ 发送蓝框选取完成消息（协作模式下标记为需要确认）
        # ⭐ Bug修复：包含蓝框的regions数据，用于前端显示和点击聚焦
        blue_regions_info = []
        for i, blue_roi in enumerate(selected_blue_rois, 1):
            blue_regions_info.append({
                'index': i,
                'roi_id': blue_roi['id'],
                'rect': blue_roi['rect'],
                'frame_type': 'blue'
            })
        
        # ⭐ 新方案：Phase 1 时就生成 confirmation_id，用户可任意顺序确认
        import uuid as uuid_module
        confirmation_id = str(uuid_module.uuid4())[:8] if is_collaborative else None
        
        socketio.emit('agent.message', {
            'type': 'system',
            'content': f"✅ {roi_info['id']} 蓝框选取完成: 已选择 {len(selected_blue_rois)} 个蓝框 (40×)",
            'level': 'blue',
            'roi_id': roi_info['id'],  # ⭐ 父红框ID
            'rect': roi_info['rect'],   # ⭐ 父红框坐标（用于点击聚焦）
            'regions': blue_regions_info,  # ⭐ 蓝框坐标列表，用于前端显示
            'confirmation_id': confirmation_id,  # ⭐ 新增：直接发送confirmation_id
            'metadata': {
                'action': 'selected', 
                'count': len(selected_blue_rois), 
                'parent_id': roi_info['id'],
                'requires_confirmation': is_collaborative  # ⭐ 协作模式需要确认
            }
        }, to=room)
        
        # ⭐ Patient Level 模式：发送红框分析结果到前端（用于更新左侧分析树）
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
        
        # ⭐ 协作模式：Phase 1 完成，返回结果（用户可任意顺序确认）
        if is_collaborative:
            # 发送红框分析结果到左侧面板（层级显示）
            socketio.emit('agent.message', {
                'type': 'assistant',
                'content': f"🔴 红框分析结果：\n{red_analysis.get('findings', '无分析结果')}",
                'reasoning': red_analysis.get('reasoning', red_analysis.get('findings', '')),  # ⭐ AI思考过程
                'level': 'red',
                'roi_id': roi_info['id'],
                'rect': roi_info['rect'],
                'is_result': True  # 标记为结果消息
            }, to=room)
            logger.info(f"🔴 红框分析完成: {red_analysis.get('findings', '无分析结果')[:100]}...")
            
            # ⭐ 新方案：注册待确认状态（用户可任意顺序确认）
            red_leaf = _stopped_red_leaf(roi_info, red_analysis)
            pending_entry = {
                'status': 'pending',
                'roi_info': roi_info,
                'step': step,
                'red_analysis': red_analysis,
                'red_leaf': red_leaf,
                'selected_blue_rois': selected_blue_rois,
                'roi_save_dir': roi_save_dir,
                'wsi_name': wsi_name
            }
            pending_lock = agent_state.get('pending_blue_lock')
            agent_state.setdefault('pending_blue_confirmations', {})
            if pending_lock:
                with pending_lock:
                    agent_state['pending_blue_confirmations'][confirmation_id] = pending_entry
            else:
                agent_state['pending_blue_confirmations'][confirmation_id] = pending_entry
            
            logger.info(f"🤝 协作模式 Phase 1 完成，confirmation_id={confirmation_id}，用户可任意顺序确认")
            logger.info(f"🔍 [调试] 当前 pending_blue_confirmations 数量: {len(agent_state.get('pending_blue_confirmations', {}))}")
            return {
                'phase': 1,
                'confirmation_id': confirmation_id,
                'roi_info': roi_info,
                'step': step,
                'red_analysis': red_analysis,
                'red_leaf': red_leaf,
                'selected_blue_rois': selected_blue_rois,
                'roi_save_dir': roi_save_dir,
                'wsi_name': wsi_name,
                'tree_nodes': [red_leaf] if not selected_blue_rois else [],
            }
        
        # ⭐ 自动模式：继续执行，请求确认（实际上自动模式不会阻塞）
        red_total = len(agent_state.get('candidate_rois', []))
        blue_progress_info = {
            'red_index': step,  # 当前红框索引（1-based）
            'red_total': red_total,
            'brief_finding': f"AI在红框#{step}中选择了{len(selected_blue_rois)}个蓝框"
        }
        selected_blue_rois = request_user_confirmation(
            socketio, room, agent_state, 'blue', roi_info, selected_blue_rois, slide,
            progress_info=blue_progress_info
        )

        if _agent_stop_requested(room):
            logger.info(f"⛔ 红框 {roi_info['id']} 确认后停止；保留已完成 10× 观察")
            return _stopped_multiscale_result(roi_info, red_analysis)
        
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
            red_leaf = _stopped_red_leaf(roi_info, red_analysis)
            return {
                'finding': red_analysis.get('findings', ''),
                'confidence': red_analysis.get('confidence', 0.5),
                'next_action': 'stop' if agent_action_result['action'] == 'stop' else 'move_to_next_roi',
                'tools_results': [],
                'tree_nodes': [red_leaf]
            }
        
        # ⭐⭐⭐ Agent Loop 改进：删除冗余的 analyze_roi_parallel (40x)
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
                if _agent_stop_requested(room):
                    logger.info(f"⛔ 红框 {roi_info['id']} 停止；不再启动后续蓝框")
                    return _stopped_multiscale_result(roi_info, red_analysis)
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

                if _agent_stop_requested(room):
                    logger.info(f"⛔ 蓝框 {blue_roi['id']} 的 40× 请求返回后停止；不生成绿框任务")
                    return _stopped_multiscale_result(roi_info, red_analysis)

                blue_neighbor_context = get_completed_sibling_context(
                    blue_roi,
                    selected_blue_rois,
                    blue_analyses,
                    max_neighbors=3,
                )

                def _replan_blue(replanning_context, _blue=blue_roi, _greens=green_rois, _parent=blue_roi_for_green):
                    logger.info(
                        f"🔄 [Agent Loop] 蓝框 {_blue['id']}: 回注"
                        f"{replanning_context.get('kind', '')}上下文后重新规划一次"
                    )
                    return filter_rois_by_gpt_vision(
                        slide,
                        _greens,
                        (2000, 2000),
                        None,
                        _parent,
                        wsi_name,
                        return_analysis=True,
                        agent_state=agent_state,
                        replanning_context=replanning_context,
                    )

                green_result, green_action_result, _blue_tool_trace = resolve_mcp_action_with_one_replan(
                    initial_result=green_result,
                    roi_info=blue_roi,
                    agent_state=agent_state,
                    replan_callback=_replan_blue,
                    socketio=socketio,
                    room=room,
                    neighbor_context_override=blue_neighbor_context,
                )

                if _agent_stop_requested(room):
                    logger.info(f"⛔ 蓝框 {blue_roi['id']} 规划完成后停止；不发布迟到结果")
                    return _stopped_multiscale_result(roi_info, red_analysis)

                if green_action_result['action'] == 'backtrack':
                    logger.warning(
                        f"⏪ [Agent Loop] 蓝框 {blue_roi['id']} 触发回溯，"
                        "不写入诊断树，继续其他同级蓝框"
                    )
                    skipped_count += 1
                    continue

                if green_action_result['action'] == 'stop':
                    selected_greens = []
                else:
                    selected_greens = list(green_action_result.get('selected_rois') or [])
                
                # ⭐ 从 debug_images 目录查找带网格线的蓝框图片（filter_rois_by_gpt_vision 已生成）
                # file_id 键优先：保存端用的就是 debug_images/{file_id}/，
                # 只按文件名键找会把旧目录的陈年文件（甚至死链接）写进 analysis_tree.json。
                blue_image_path = None
                try:
                    blue_image_path = _find_debug_image(
                        f"debug_gpt_{blue_roi['id']}_*candidates_*.jpg",
                        file_id=(agent_state or {}).get('file_id'),
                        sample_name=Path(wsi_name).stem if wsi_name else '',
                    )
                    if blue_image_path:
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
                    'plan_action': green_result.get('agent_action') or green_result.get('mcp_action') or '',
                    'selected_indices': list(green_result.get('selection_indices') or []),
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
                    logger.info(f"  ⊙ {blue_roi['id']}: 不需要160x放大（【判断】B）")
                    logger.info(f"  📤 已发送蓝框完成事件: {blue_roi['id']} (无需160x)")
                    skipped_count += 1
                else:
                    logger.info(f"  ✓ {blue_roi['id']}: 需要160x放大，选中 {len(selected_greens)} 个绿框")
            
            logger.info(f"{'─'*40}")
            logger.info(f"✅ 蓝框 Reason+Plan 完成: {len(selected_blue_rois) - skipped_count}/{len(selected_blue_rois)} 蓝框需要放大")
            logger.info(f"{'─'*40}")

            if _agent_stop_requested(room):
                logger.info(f"⛔ 红框 {roi_info['id']} 停止；跳过绿框汇总与任务创建")
                return _stopped_multiscale_result(roi_info, red_analysis)
            
            # 合并所有选中的绿框
            selected_green_rois = []
            red_total = len(agent_state.get('candidate_rois', []))
            for idx, green_list in enumerate(green_filter_results):
                # ⭐ 人机协作模式：请求用户确认每个蓝框下的绿框选择（带进度信息）
                if agent_state.get('mode') == 'collaborative' and green_list:
                    parent_blue = selected_blue_rois[idx] if idx < len(selected_blue_rois) else None
                    if parent_blue:
                        green_progress_info = {
                            'red_index': step,  # 当前红框索引
                            'red_total': red_total,
                            'blue_index': idx + 1,  # 当前蓝框索引（1-based）
                            'blue_total': len(selected_blue_rois),
                            'brief_finding': f"在蓝框#{idx+1}中选择了{len(green_list)}个绿框"
                        }
                        green_list = request_user_confirmation(
                            socketio, room, agent_state, 'green', parent_blue, green_list, slide,
                            progress_info=green_progress_info
                        )
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
        
        # ⭐ Agent Loop：收集蓝框证据到证据池
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

        if _agent_stop_requested(room):
            logger.info(f"⛔ 红框 {roi_info['id']} 停止；不创建 160× 线程池")
            return _stopped_multiscale_result(roi_info, red_analysis)
        
        # 注意：红框是并行分析的，不在这里发送绿框进度（会被其他线程覆盖）
        
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(selected_green_rois))) as executor:
                green_analysis_futures = []
                future_meta = {}
                for green_idx, green_roi in enumerate(selected_green_rois, 1):
                    if _agent_stop_requested(room):
                        break
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
                    
                    # ⭐ Patient Level 模式传递额外信息
                    pl_is_patient_level = agent_state.get('is_patient_level', False)
                    pl_info = {
                        'part_name': agent_state.get('part_name', 'Slide 1'),
                        'folder_name': agent_state.get('folder_name', 'Patient'),
                        'organ': agent_state.get('organ', '未指定标本')
                    } if pl_is_patient_level else None
                    
                    def _analyze_green_if_active(
                        _green_roi=green_roi,
                        _parent_context=parent_context,
                        _pl_is_patient_level=pl_is_patient_level,
                        _pl_info=pl_info,
                    ):
                        if _agent_stop_requested(room):
                            return None
                        return analyze_roi_parallel(
                            slide,
                            _green_roi,
                            160,
                            roi_save_dir,
                            parent_context=_parent_context,
                            is_patient_level=_pl_is_patient_level,
                            patient_level_info=_pl_info,
                            lang=agent_state.get('lang', 'zh'),
                            agent_state=agent_state,
                        )

                    green_analysis_futures.append(
                        executor.submit(_analyze_green_if_active)
                    )
                    future_meta[green_analysis_futures[-1]] = green_idx
                logger.info(f"⏳ 等待 {len(green_analysis_futures)} 个绿框分析任务完成...")
                green_analyses = []
                for future in concurrent.futures.as_completed(green_analysis_futures):
                    result = future.result()
                    if _agent_stop_requested(room):
                        for pending_future in green_analysis_futures:
                            pending_future.cancel()
                        return _stopped_multiscale_result(roi_info, red_analysis)
                    if result is not None:
                        green_analyses.append(result)
                        _emit_green_analysis_result(
                            socketio,
                            room,
                            result,
                            future_meta.get(future, len(green_analyses)),
                        )
            
            logger.info(f"✅ 绿框分析完成！共 {len(green_analyses)} 个绿框")
        except RuntimeError as e:
            if "cannot schedule new futures after" in str(e):
                logger.warning(f"⚠️ 线程池被中断（可能是程序重启），跳过当前红框: {roi_info['id']}")
                return  # 优雅退出
            raise  # 其他RuntimeError继续抛出
        logger.info(f"\n📊 绿框分析结果汇总:")
        
        # ⭐ Agent Loop：收集绿框证据到证据池
        for ga in green_analyses:
            collect_evidence(agent_state, ga, {'id': ga['roi_id'], 'rect': ga.get('rect', {})}, level='green')
        
        # ⭐ 将分析结果中的image_path合并回绿框ROI对象
        for ga in green_analyses:
            for roi in selected_green_rois:
                if roi['id'] == ga['roi_id'] and ga.get('image_path'):
                    roi['image_path'] = ga['image_path']
                    logger.debug(f"  ✅ 绿框 {roi['id']} image_path: {ga['image_path']}")
        
        # 绿框分析结果已在每个 future 完成时推到前端；这里只写日志。
        for ga in green_analyses:
            logger.info(f"  🟢 {ga['roi_id']}: {str(ga.get('findings') or '')[:100]}...")
        
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

    if agent_state and agent_state.get('diagnostic_question'):
        objective = get_active_diagnostic_objective(agent_state, lang)
        if _is_en:
            context_text += f"=== Current diagnostic objective (highest priority) ===\n{objective}\n\n"
        else:
            context_text += f"=== 当前诊断目标（最高优先级）===\n{objective}\n\n"
    
    # 注入缩略图全局分析，用于约束需要宏观空间信息的判断。
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
        # A stopped run has no final diagnostic product.  Emitting the same
        # terminal event keeps the UI/runtime lifecycle intact, but it must
        # not create a report (or synthesize a diagnosis) that could be
        # mistaken for a completed analysis.
        if status == 'stopped':
            socketio.emit('agent.done', {
                'type': 'done',
                'file_id': file_id,
                'report_path': None,
                'steps': agent_state.get('current_step', 0),
                'roi_count': len(agent_state.get('analyzed_rois', [])),
                'status': 'stopped',
                'summary': {
                    'status': 'stopped',
                    'diagnosis': None,
                    'confidence': None,
                },
            }, to=room)
            logger.info(f"病理分析已停止: {file_id}；未生成最终报告")
            return

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


# ⭐⭐⭐ ReRead Agent - 根据用户反馈重新解读 ROI ⭐⭐⭐
def reread_roi_analysis(session_id: str, roi_id: str, phase: int, 
                        original_analysis: str, user_instruction: str) -> dict:
    """
    ReRead Agent: 根据用户反馈重新解读指定的 ROI 区域
    
    与普通文本重写不同，这个 Agent 会：
    1. 从 agent_state 中获取原始 ROI 的图像
    2. 结合用户的反馈指令，重新进行视觉分析
    3. 返回新的分析结果
    
    Args:
        session_id: 会话ID
        roi_id: ROI ID (如 R11, R2-B3 等)
        phase: 阶段 (2=红框10x, 3=蓝框40x, 4=绿框160x)
        original_analysis: 原始 AI 分析文本
        user_instruction: 用户的修改建议
    
    Returns:
        dict: {
            'success': bool,
            'new_analysis': str,  # 新的分析结果
            'error': str  # 如果失败
        }
    """
    logger.info(f"🔄 [ReRead Agent] 启动重新解读: {roi_id} (Phase {phase})")
    logger.info(f"   用户指令: {user_instruction}")
    
    try:
        # 1. 获取 agent_state
        state = get_agent_state(session_id)
        if not state:
            return {'success': False, 'error': '会话不存在或已过期'}
        
        # 2. 获取 slide 对象
        slide = state.get('slide')
        if not slide:
            logger.warning(f"[ReRead Agent] 没有 slide 对象，使用纯文本模式")
            return reread_text_only(roi_id, phase, original_analysis, user_instruction)
        
        # 3. 查找对应的 ROI 信息
        roi_info = find_roi_info(state, roi_id, phase)
        if not roi_info:
            logger.warning(f"[ReRead Agent] 找不到 {roi_id} 的 ROI 信息，使用纯文本模式")
            return reread_text_only(roi_id, phase, original_analysis, user_instruction)
        
        rect = roi_info.get('rect')
        if not rect:
            logger.warning(f"[ReRead Agent] {roi_id} 没有 rect 信息，使用纯文本模式")
            return reread_text_only(roi_id, phase, original_analysis, user_instruction)
        
        # 4. 提取 ROI 图像
        logger.info(f"[ReRead Agent] 提取 {roi_id} 图像: rect={rect}")
        try:
            roi_image = safe_read_region(
                slide,
                (rect['x'], rect['y']), 
                0, 
                (rect['w'], rect['h'])
            ).convert('RGB')
            
            # 缩放到合适大小（不超过 1024）
            max_size = 1024
            w, h = roi_image.size
            if max(w, h) > max_size:
                scale = max_size / max(w, h)
                new_w, new_h = int(w * scale), int(h * scale)
                roi_image = roi_image.resize((new_w, new_h), Image.LANCZOS)
            
            # 转为 base64
            buffered = io.BytesIO()
            roi_image.save(buffered, format="JPEG", quality=90)
            roi_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
            
            logger.info(f"[ReRead Agent] 图像提取成功: {roi_image.size}")
            
        except Exception as e:
            logger.error(f"[ReRead Agent] 图像提取失败: {e}")
            return reread_text_only(roi_id, phase, original_analysis, user_instruction)
        
        # 5-6. 构建带图像的 ReRead 提示词并调用（与磁盘重建路径共用）
        return _reread_vision_call(
            roi_id, phase, original_analysis, user_instruction, roi_base64
        )
        
    except Exception as e:
        logger.error(f"❌ [ReRead Agent] 重新解读失败: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


def _reread_vision_call(roi_id, phase, original_analysis, user_instruction, roi_base64):
    """带图像的 ReRead 调用（在线 state 路径与磁盘重建路径共用）。"""
    zoom_desc = {2: '10x低倍镜', 3: '40x高倍镜', 4: '160x油镜'}.get(phase, f'Phase{phase}')

    reread_prompt = f"""你是一位资深病理学专家，正在协助病理医生进行诊断分析。

## 任务背景
病理医生正在审核 AI 对区域 **{roi_id}** 的分析结果，并提出了修改建议。
请你仔细查看这张 **{zoom_desc}** 下的病理图像，结合医生的建议重新进行分析。

## 原始 AI 分析
{original_analysis}

## 病理医生的反馈建议
**{user_instruction}**

## 你的任务
1. 仔细观察图像中的组织学特征
2. 充分考虑病理医生的专业建议
3. 如果医生指出了具体问题，重点关注并分析
4. 如果医生要求"重新解读"，请独立客观地重新分析，不要简单重复原始分析
5. 使用专业的病理学术语，保持简洁准确

## 重新解读
请直接输出新的分析结果（无需重复医生的建议）："""

    logger.info(f"[ReRead Agent] 调用 GPT Vision API...")
    new_analysis = call_gpt_vision(reread_prompt, roi_base64)

    if not new_analysis:
        return {'success': False, 'error': 'AI 返回空结果'}

    logger.info(f"✅ [ReRead Agent] {roi_id} 重新解读完成，新分析长度={len(new_analysis)}")

    return {
        'success': True,
        'new_analysis': new_analysis,
        'method': 'vision'  # 标记使用了图像分析
    }


def _find_roi_rect_in_saved_trees(file_id, roi_id):
    """从最新的 analysis_tree.json 里找该 ROI 的全局坐标。

    定深枚举（run/、run/<mode>/、patient/<wsi>/<mode>/），剪掉瓦片等大目录，
    最新优先、限量读取——与 get_analysis_tree 的兜底同一套纪律，不做全树递归。
    """
    outputs_dir = Path('outputs')
    if not outputs_dir.exists():
        return None

    def _prunable(name):
        return name.endswith('_files') or name in ('steps', 'debug_images', 'roi_images')

    candidates = []
    try:
        level1 = [d for d in outputs_dir.iterdir() if d.is_dir()]
    except OSError:
        return None
    for d1 in level1:
        f1 = d1 / 'analysis_tree.json'
        if f1.is_file():
            candidates.append(f1)
        if _prunable(d1.name):
            continue
        try:
            level2 = [d for d in d1.iterdir() if d.is_dir() and not _prunable(d.name)]
        except OSError:
            continue
        for d2 in level2:
            f2 = d2 / 'analysis_tree.json'
            if f2.is_file():
                candidates.append(f2)
            try:
                level3 = [d for d in d2.iterdir() if d.is_dir() and not _prunable(d.name)]
            except OSError:
                continue
            for d3 in level3:
                f3 = d3 / 'analysis_tree.json'
                if f3.is_file():
                    candidates.append(f3)

    dated = []
    for p in candidates:
        try:
            dated.append((p.stat().st_mtime, p))
        except OSError:
            continue
    dated.sort(key=lambda item: item[0], reverse=True)

    wanted = str(roi_id or '')

    def _walk(nodes):
        for node in nodes or []:
            if str(node.get('roi_id') or node.get('id') or '') == wanted:
                return node.get('rect')
            hit = _walk(node.get('children'))
            if hit:
                return hit
        return None

    for _mtime, path in dated[:24]:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if str(doc.get('metadata', {}).get('file_id') or '') != str(file_id):
            continue
        rect = _walk(doc.get('tree_nodes'))
        if isinstance(rect, dict) and rect.get('x') is not None:
            return rect
    return None


def reread_roi_from_disk(file_id, roi_id, phase, original_analysis, user_instruction):
    """运行状态已被清理时（如患者级切片完成后）的重读。

    引用消息对应的 ROI 坐标持久化在 analysis_tree.json 里，WSI 原图也在本地：
    直接按坐标从原图重新取图，走与在线重读完全相同的视觉分析；
    任一环节取不到，再退化为纯文本重读。
    """
    try:
        rect = _find_roi_rect_in_saved_trees(file_id, roi_id) if file_id else None
        slide_path = get_wsi_file_path(file_id) if file_id else None
        if not rect or not slide_path or not Path(str(slide_path)).exists():
            logger.info(
                f"[ReRead Agent] 磁盘重建不可用 (rect={bool(rect)}, slide={bool(slide_path)})，退化为纯文本重读"
            )
            return reread_text_only(roi_id, phase, original_analysis, user_instruction)

        slide = openslide.OpenSlide(str(slide_path))
        try:
            roi_image = safe_read_region(
                slide,
                (int(rect['x']), int(rect['y'])),
                0,
                (int(rect['w']), int(rect['h'])),
            ).convert('RGB')
        finally:
            slide.close()

        max_size = 1024
        w, h = roi_image.size
        if max(w, h) > max_size:
            scale = max_size / max(w, h)
            roi_image = roi_image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buffered = io.BytesIO()
        roi_image.save(buffered, format='JPEG', quality=90)
        roi_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
        logger.info(f"[ReRead Agent] 磁盘重建图像成功: {roi_id} {roi_image.size}")

        return _reread_vision_call(
            roi_id, phase, original_analysis, user_instruction, roi_base64
        )
    except Exception as e:
        logger.warning(f"[ReRead Agent] 磁盘重读失败，退化为纯文本: {e}")
        return reread_text_only(roi_id, phase, original_analysis, user_instruction)


def reread_text_only(roi_id: str, phase: int, original_analysis: str, user_instruction: str) -> dict:
    """
    纯文本模式的重新解读（当无法获取图像时的回退方案）
    使用 call_gpt_vision(prompt, None) 来调用 API（API key 已正确配置）
    """
    logger.info(f"[ReRead Agent] 使用纯文本模式重新解读 {roi_id}")
    
    zoom_desc = {2: '10x低倍镜', 3: '40x高倍镜', 4: '160x油镜'}.get(phase, f'Phase{phase}')
    
    prompt = f"""你是一位病理学专家，正在协助病理医生修改 AI 的分析结果。

## 原始分析
区域: {roi_id} ({zoom_desc})
{original_analysis}

## 病理医生的修改建议
{user_instruction}

## 任务
请根据病理医生的专业建议，仅针对 **{roi_id}** 这一个区域进行修改。注意：
1. 只修改 {roi_id} 的分析内容，不要涉及其他区域
2. 充分尊重医生的专业意见
3. 如果医生要求"重新解读"，请重新组织分析语言
4. 保持专业术语和简洁风格
5. 直接输出修改后的分析（无需额外说明）

## {roi_id} 修改后的分析："""

    try:
        # ⭐ 使用 call_gpt_vision 的纯文本模式（传入 image_base64=None）
        # 这样可以复用已配置好的 API key 轮询机制
        new_analysis = call_gpt_vision(prompt, None)
        
        if new_analysis and new_analysis != '{"selected_grids": []}':
            return {
                'success': True,
                'new_analysis': new_analysis,
                'method': 'text_only'
            }
        return {'success': False, 'error': 'AI 返回空结果'}
    except Exception as e:
        logger.error(f"[ReRead Agent] 纯文本模式调用失败: {e}")
        return {'success': False, 'error': str(e)}


def find_roi_info(state: dict, roi_id: str, phase: int) -> dict:
    """
    从 agent_state 中查找指定 ROI 的信息
    
    Args:
        state: agent_state
        roi_id: ROI ID
        phase: 阶段
    
    Returns:
        dict: ROI 信息，包含 rect 等
    """
    # 根据 phase 确定要查找的数据源
    if phase == 2:
        # 红框分析结果
        analyses = state.get('red_analyses', [])
        for item in analyses:
            if item.get('roi_id') == roi_id:
                return item
        # 也查找 candidate_rois
        candidates = state.get('candidate_rois', [])
        for item in candidates:
            if item.get('id') == roi_id:
                return item
                
    elif phase == 3:
        # 蓝框分析结果
        analyses = state.get('blue_analyses', [])
        for item in analyses:
            if item.get('roi_id') == roi_id:
                return item
        # 查找 selected_blue_rois
        blue_rois = state.get('selected_blue_rois', [])
        for item in blue_rois:
            if item.get('id') == roi_id:
                return item
                
    elif phase == 4:
        # 绿框分析结果
        analyses = state.get('green_analyses', [])
        for item in analyses:
            if item.get('roi_id') == roi_id:
                return item
        # 查找 selected_green_rois
        green_rois = state.get('selected_green_rois', [])
        for item in green_rois:
            if item.get('id') == roi_id:
                return item
    
    # 通用查找：遍历所有可能的字段
    for key in ['subagent_results', 'phase_results', 'tree_nodes']:
        items = state.get(key, [])
        for item in items:
            if item.get('roi_id') == roi_id or item.get('id') == roi_id:
                return item
    
    return None
