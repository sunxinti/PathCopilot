# -*- coding: utf-8 -*-
"""
Agent循环逻辑模块
实现WSI Agent的感知→行动→推理循环
模拟病理医生工作流：缩略图预筛选 → 多尺度ROI分析 → 病理学工具集成 → 迭代推理
"""

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

def safe_read_region(slide, location, level, size):
    """线程安全的 OpenSlide read_region 封装"""
    with _openslide_lock:
        return slide.read_region(location, level, size)

def safe_get_slide_metadata(slide):
    """线程安全地读取当前 OpenSlide 对象的元数据。

    不按 ``id(slide)`` 跨对象缓存：OpenSlide 对象释放后 Python 可能复用
    对象 ID，进而把上一张 WSI 的尺寸误用于下一张 WSI。Patient Level
    多切片并行、长进程尤其不能保留这种缓存。
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
    from .knowledge_retrieval import (
        get_knowledge_for_report, extract_diagnostic_keywords, search_pubmed,
        format_knowledge_context, search_grading_criteria, get_diagnostic_criteria
    )
except ImportError:
    try:
        from gpt_client import call_gpt_vision, call_gemini_mcp
        from report_generator import generate_detailed_word_report, generate_report_with_images
        from knowledge_retrieval import (
            get_knowledge_for_report, extract_diagnostic_keywords, search_pubmed,
            format_knowledge_context, search_grading_criteria, get_diagnostic_criteria
        )
    except ImportError:
        # 如果都失败，提供一个空的实现
        def call_gpt_vision(prompt, image_base64):
            logger.warning("Vision API不可用，返回空结果")
            return '{"selected_grids": []}'
        def call_gemini_mcp(messages, model=None, max_tokens=65536, max_retries=3):
            logger.warning("MCP API不可用，返回空结果")
            return None, "API不可用"
        # 知识检索占位
        get_knowledge_for_report = None
        search_grading_criteria = None
        get_diagnostic_criteria = None

try:
    from .secondary_review import (
        orchestrate_secondary_reviews,
        secondary_review_honor_rule,
    )
except ImportError:
    from secondary_review import (
        orchestrate_secondary_reviews,
        secondary_review_honor_rule,
    )

# 真正的出站 MCP：官方 SDK ClientSession 连接 allowlist 中的 FastMCP
# stdio server。此接口与历史的本地 Python registry 不同。
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
    'generating_patient_report': {'zh': '正在生成患者级别综合报告...', 'en': 'Generating patient-level comprehensive report...'},
    'analysis_done':        {'zh': '分析完成！',                          'en': 'Analysis complete!'},
    'phase1_start':         {'zh': 'Phase 1: 正在分析缩略图，识别候选区域...', 'en': 'Phase 1: Analyzing thumbnail, identifying candidates...'},
    'phase2_start':         {'zh': 'Phase 2: 正在分析 {n} 个红框区域...', 'en': 'Phase 2: Analyzing {n} red-box regions...'},
    'phase3_start':         {'zh': 'Phase 3: 正在分析 {n} 个蓝框区域...', 'en': 'Phase 3: Analyzing {n} blue-box regions...'},
    'phase4_start':         {'zh': 'Phase 4: 正在分析 {n} 个绿框区域...', 'en': 'Phase 4: Analyzing {n} green-box regions...'},
    'phase5_start':         {'zh': 'Phase 5: 正在生成诊断报告...',        'en': 'Phase 5: Generating diagnostic report...'},
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


def get_output_paths(file_id: str, patient_id: str, filename: str = None) -> dict:
    """
    获取 Patient Level Auto 单张切片的输出路径。
    
    命名格式：{原始文件名}_{分析时间戳}，例如：sample_001_20260211_143052
    
    Args:
        file_id: WSI 文件 ID
        patient_id: 患者 ID
        filename: 原始文件名（可选，用于生成可读的目录名）
    
    Returns:
        dict: {
            'output_dir': Path,      # 主输出目录
            'steps_dir': Path,       # 步骤图片目录
            'report_html': str,      # 离线 HTML 审计报告路径
            'report_txt': str,       # 文本报告路径
            'mode_subdir': str,      # 固定为 'auto'
            'dir_name': str,         # 目录名（文件名_时间戳）
        }
    """
    if not patient_id:
        raise ValueError("patient_id is required for Patient Level evaluation")
    mode_subdir = 'auto'
    
    # 生成可读的目录名：文件名_时间戳_numX
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if filename:
        safe_name = sanitize_filename(filename)
    else:
        safe_name = f"{file_id[:8]}" if file_id else "unknown"
    
    patient_dir_name = patient_id if patient_id.startswith('patient_') else f"patient_{patient_id}"
    base_dir = Path("outputs") / patient_dir_name
    mode_dir = base_dir / mode_subdir
    # A cache miss starts a new attempt. Retain older evidence, including rejected caches.
    run_num = 1
    while True:
        dir_name = f"{safe_name}_{timestamp}_num{run_num}"
        output_dir = mode_dir / f"wsi_{dir_name}"
        try:
            output_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            run_num += 1

    report_html = f'/outputs/{patient_dir_name}/{mode_subdir}/wsi_{dir_name}/pathology_report.html'
    report_txt = f'/outputs/{patient_dir_name}/{mode_subdir}/wsi_{dir_name}/pathology_report.txt'
    
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


def _extract_plan_tool_decision(response: str, valid_indices=None) -> dict:
    """Parse Plan action via parse_mcp_response; keep explicit D/E query lines."""
    text = str(response or '')
    decision = parse_mcp_response(text, valid_indices=valid_indices)
    action = (decision.get('action') or '').upper()
    result = {
        'mcp_action': action,
        'mcp_reason': '',
        'search_query': '',
        'neighbor_query': '',
        'parse_status': decision.get('parse_status', 'failed'),
        'selected_indices': list(decision.get('selected_indices') or []),
        'action_parameter': decision.get('action_parameter'),
    }
    if not text.strip():
        return result

    plan_match = re.search(
        r'(?:【|\[)Plan(?:】|\])\s*([\s\S]*?)'
        r'(?=(?:【|\[)Evidence(?:】|\])|【证据摘要】|$)',
        text,
        re.IGNORECASE,
    )
    plan_text = plan_match.group(1).strip() if plan_match else ''
    parameter_scopes = [plan_text] if plan_text else []
    if text not in parameter_scopes:
        parameter_scopes.append(text)

    def _first_value(patterns):
        for scope in parameter_scopes:
            for pattern in patterns:
                match = re.search(pattern, scope, re.IGNORECASE | re.MULTILINE)
                if not match:
                    continue
                value = match.group(1).strip().strip("\"'").strip()
                value = re.split(
                    r'\n\s*(?:选择理由|Rationale|(?:【|\[)Evidence)',
                    value,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip()
                if value:
                    return value
        return ''

    reason = _first_value([
        r'^\s*(?:选择理由|Rationale)\s*[：:]\s*(.+)$',
        r'^\s*(?:回溯原因|Backtrack\s+reason|停止原因|Stop\s+reason)\s*[：:]\s*(.+)$',
    ])
    if not reason and action in {'B', 'C', 'D', 'E'}:
        param = decision.get('action_parameter')
        if isinstance(param, str) and param.strip():
            reason = param.strip().splitlines()[0].strip()
    result['mcp_reason'] = reason

    if action == 'D':
        query = _first_value([
            r'^\s*(?:检索问题|检索查询|搜索问题|search_query|search\s+query|query)\s*[：:]\s*(.+)$',
            r'^\s*-?\s*D(?:\([^\n)]*\))?\s*[：:]\s*(.+)$',
            r'^\s*(?:动作参数|Action\s+parameters?)\s*[：:]\s*(.+)$',
        ])
        if not query and isinstance(decision.get('action_parameter'), str):
            query = decision['action_parameter'].strip().splitlines()[0].strip()
        if query.lower() in {'specific query', 'query'} or query in {'需要检索的具体问题', '检索问题'}:
            query = ''
        result['search_query'] = query
        if query and result['parse_status'] == 'missing_parameter':
            result['parse_status'] = 'ok'
        elif not query and result['parse_status'] == 'ok':
            result['parse_status'] = 'missing_parameter'
    elif action == 'E':
        query = _first_value([
            r'^\s*(?:空间关系|邻区问题|neighbor_query|neighbor\s+query|spatial\s+relationship)\s*[：:]\s*(.+)$',
            r'^\s*-?\s*E(?:\([^\n)]*\))?\s*[：:]\s*(.+)$',
            r'^\s*(?:动作参数|Action\s+parameters?)\s*[：:]\s*(.+)$',
        ])
        if not query and isinstance(decision.get('action_parameter'), str):
            query = decision['action_parameter'].strip().splitlines()[0].strip()
        if query.lower() in {'spatial relationship description', 'neighbor query'} or query in {'需要参考的空间关系描述', '空间关系描述'}:
            query = ''
        result['neighbor_query'] = query

    return result


def _sanitize_external_search_query(query: str, agent_state: dict) -> str:
    """机械清洗外部检索词：去运行标识和路径，不改写医学内容。"""
    cleaned = re.sub(r'\s+', ' ', str(query or '')).strip()
    if not cleaned:
        return ''

    state = agent_state or {}
    sensitive_values = []
    for key in ('patient_id', 'file_id', 'filename', 'wsi_name', 'file_path'):
        value = str(state.get(key, '') or '').strip()
        if not value:
            continue
        sensitive_values.append(value)
        try:
            path_value = Path(value)
            sensitive_values.extend([path_value.name, path_value.stem])
        except Exception:
            pass
    for value in sorted(set(sensitive_values), key=len, reverse=True):
        if value:
            cleaned = re.sub(re.escape(value), ' ', cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r'(?i)\b[A-Z]:\\[^\s]+', ' ', cleaned)
    cleaned = re.sub(r'(?<!\w)/(?:mnt|home|Users|tmp|var|opt)/[^\s]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ,;:-')
    return cleaned[:500].strip()


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
    r'(?:动作参数|Action parameter|检索问题|检索查询|搜索问题|'
    r'search_query|search\s+query)\s*[：:]\s*(.*?)'
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
    if action == 'D' and parse_status == 'ok' and not action_parameter:
        parse_status = 'missing_parameter'
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
            'action': 'continue' | 'stop' | 'backtrack' | 'search' | 'neighbor',
            'selected_rois': 选中的 ROI 列表,
            'reason': 动作原因,
            'search_result': 知识检索结果（如果动作是 D）
        }
    """
    mcp_action = mcp_result.get('mcp_action')
    mcp_reason = mcp_result.get('mcp_reason', '')
    selected_rois = list(mcp_result.get('selected_rois') or [])
    analysis = mcp_result.get('analysis', '')
    
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
    
    # ===== D(查库)：检索外部知识库 =====
    elif mcp_action == 'D':
        logger.info(f"🔍 [D(查库)] {roi_id}: {mcp_reason[:50] if mcp_reason else '知识检索'}")
        
        # 查询必须来自本次 Plan 的 D 参数。参数缺失时不得用固定文案、
        # 病例标识或诊断问题代填，也不得启动 MCP/本地检索。
        search_query = _sanitize_external_search_query(
            mcp_result.get('search_query', ''),
            agent_state,
        )
        if not search_query:
            logger.warning(f"⚠️ [D(查库)] {roi_id}: Plan 缺少有效检索问题，未调用工具")
            return {
                'action': 'stop',
                'selected_rois': [],
                'reason': 'D(查库)缺少有效检索问题，当前层停止',
                'search_result': '',
                'retrieval_transport': 'not-called-missing-query',
                'tool_status': 'not_run_missing_query',
            }
        
        # 发送检索消息到前端
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"🔍 {roi_id}: AI 请求检索外部知识\n查询: {search_query}",
                'level': 'info'
            }, to=room)
        
        # PUMCH 默认通过官方 MCP Client → allowlisted FastMCP servers。
        # PubMed 为首选；无可用结果或协议失败时，才调用固定生物医学来源
        # (PubMed / Europe PMC / OpenAlex) 的 Web Search MCP。两者均失败时
        # 保留历史 Python 检索作为 fail-open，避免单张 WSI 因外部服务中断。
        search_result = None
        retrieval_transport = 'legacy-local'
        mcp_errors = []
        try:
            use_outbound_mcp = bool(
                call_mcp_tool
                and is_outbound_mcp_enabled
                and is_outbound_mcp_enabled()
            )
            if use_outbound_mcp:
                mcp_attempts = (
                    (
                        'pubmed',
                        'search_pubmed',
                        {'query': search_query, 'max_results': 3},
                    ),
                    (
                        'web_search',
                        'search_pathology_web',
                        {'query': search_query, 'context_size': 'medium'},
                    ),
                )
                for server_name, tool_name, arguments in mcp_attempts:
                    mcp_call_id = f"mcp_{uuid.uuid4().hex}"
                    if socketio and room:
                        socketio.emit('agent.mcp_tool', {
                            'call_id': mcp_call_id,
                            'status': 'running',
                            'phase': 'knowledge_retrieval',
                            'kind': 'outbound_mcp',
                            'action': 'tools/call',
                            'server': server_name,
                            'tool': tool_name,
                            'roi_id': roi_id,
                            'query': search_query,
                            'count': 1,
                        }, to=room)
                    try:
                        outcome = call_mcp_tool(
                            server_name,
                            tool_name,
                            arguments,
                        )
                        structured = getattr(outcome, 'structured_content', None)
                        count = None
                        if isinstance(structured, dict):
                            count = structured.get('count')
                            candidate = (
                                structured.get('context')
                                or json.dumps(structured, ensure_ascii=False)
                            )
                        else:
                            candidate = getattr(outcome, 'content', '') or str(outcome)
                        candidate = str(candidate or '').strip()
                        usable = bool(candidate) and not (
                            isinstance(count, (int, float)) and count <= 0
                        )
                        if socketio and room:
                            socketio.emit('agent.mcp_tool', {
                                'call_id': mcp_call_id,
                                'status': 'succeeded' if usable else 'empty',
                                'phase': 'knowledge_retrieval',
                                'kind': 'outbound_mcp',
                                'action': 'tools/call',
                                'server': server_name,
                                'tool': tool_name,
                                'roi_id': roi_id,
                                'query': search_query,
                                'count': 1 if usable else 0,
                            }, to=room)
                        if usable:
                            search_result = candidate
                            retrieval_transport = f'mcp:{server_name}'
                            break
                        mcp_errors.append(f'{server_name}.{tool_name}: no results')
                    except Exception as exc:
                        mcp_errors.append(f'{server_name}.{tool_name}: {exc}')
                        logger.warning(
                            f"⚠️ MCP 工具 {server_name}.{tool_name} 失败: {exc}"
                        )
                        if socketio and room:
                            socketio.emit('agent.mcp_tool', {
                                'call_id': mcp_call_id,
                                'status': 'failed',
                                'phase': 'knowledge_retrieval',
                                'kind': 'outbound_mcp',
                                'action': 'tools/call',
                                'server': server_name,
                                'tool': tool_name,
                                'roi_id': roi_id,
                                'query': search_query,
                                'error': str(exc),
                                'failed': 1,
                            }, to=room)

            if not search_result:
                if callable(search_grading_criteria):
                    search_result = search_grading_criteria(search_query)
                elif callable(get_knowledge_for_report):
                    search_result = get_knowledge_for_report(search_query)
                else:
                    detail = '; '.join(mcp_errors) if mcp_errors else '检索接口不可用'
                    search_result = f"知识检索失败: {detail}"
                    retrieval_transport = 'failed'
                if retrieval_transport != 'failed':
                    retrieval_transport = (
                        'local-fallback-after-mcp'
                        if mcp_errors else 'legacy-local'
                    )

            logger.info(
                f"🔍 知识检索完成 (transport={retrieval_transport}): "
                f"{str(search_result)[:100]}..."
            )
        except Exception as exc:
            logger.error(f"知识检索失败: {exc}")
            search_result = f"知识检索失败: {exc}"
            retrieval_transport = 'failed'

        # 工具结果必须回注给发起 D 的同一个 Sub-agent，再规划一次。
        return {
            'action': 'knowledge',
            'selected_rois': [],
            'reason': '知识检索完成',
            'search_query': search_query,
            'search_result': search_result,
            'retrieval_transport': retrieval_transport,
            'tool_status': 'ok' if retrieval_transport != 'failed' else 'failed',
        }
    
    # ===== E(查邻)：搜索相邻区域 =====
    elif mcp_action == 'E':
        neighbor_query = str(mcp_result.get('neighbor_query', '') or '').strip()
        logger.info(
            f"🔎 [E(查邻)] {roi_id}: "
            f"{neighbor_query[:50] if neighbor_query else (mcp_reason[:50] if mcp_reason else '空间检索')}"
        )
        
        if socketio and room:
            socketio.emit('agent.message', {
                'type': 'info',
                'content': f"🔎 {roi_id}: AI 请求搜索相邻区域\n关系: {neighbor_query or mcp_reason}",
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
            'selected_rois': selected_rois,
            'reason': mcp_reason or '空间检索',
            'neighbor_query': neighbor_query,
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

    D/E are bounded to one tool/context round-trip; requesting either action
    again keeps the completed observation as a B leaf rather than inventing
    a drill-down selection.
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

# ================== Patient Level 专属提示词 ==================

WHO_2020_ENDOMETRIAL_CLASSIFICATION_ZH = """
## ⚠️ WHO 2020 子宫内膜癌组织学分类参考（判定组织学类型时参照）

1. 子宫内膜样癌
完全由腺体细胞组成，通常表现为腺样或绒毛腺管状结构，伴有拥挤复杂的分支结构。组织学分级沿用FIGO分级：G1、G2、G3三级。当腺上皮细胞呈现明显异型时，分级可提高1级。可伴有鳞状细胞分化或分泌性变化。WHO 2020：G1/G2 = 低级别癌，G3 = 高级别癌。

2. 子宫浆液性癌
复杂的乳头和/或腺样结构，伴有弥漫而明显的核多形性。多有p53突变，Ki-67指数较高。可与子宫内膜样癌混合存在，浆液性成分超过10%时表现为浆液性癌生物学行为。浆液性癌分化程度不再区分。子宫浆液性上皮内癌（SEIC）即使局限于子宫内膜亦可发生远处转移。

3. 子宫透明细胞癌
多呈实性片状、腺管样、微囊状或乳头状排列，乳头通常短而圆，间质透明变。癌细胞呈柱状、多角形、鞋钉状或扁平状，细胞质透明或嗜酸性，细胞核多形性，中度至重度异型性。

4. 子宫内膜未分化癌和去分化癌
未分化癌：未分化的上皮性恶性肿瘤，细胞大小一致，成片排列，无巢状或腺样结构，核分裂象多见。去分化癌：由处于内膜表层分化较好的子宫内膜样癌和其下方的未分化癌组成。

5. 癌肉瘤
多形性上皮细胞与间叶分化区域混杂。癌性成分通常为浆液性癌或内膜样癌，也可为透明细胞癌和未分化癌。间质成分最常由无特殊分化的高级别肉瘤组成，也可见异源性分化（横纹肌肉瘤、软骨肉瘤等）。本质为上皮来源单细胞克隆去分化，属上皮性癌。肉瘤成分>50%时称以肉瘤为主的癌肉瘤。

6. 混合性腺癌
由2种或以上不同组织类型子宫内膜癌组成，其中至少1种为透明细胞癌或浆液性癌，每种成分必须超过10%。最常见为子宫内膜样癌和浆液性癌的混合。

7. 中肾管癌和中肾管样癌（罕见，<1%）
中肾管样腺癌起源于Muller上皮向中肾管方向分化；中肾管腺癌起源于中肾管残迹。组织学形态高度多样，同一肿瘤内可同时出现管状、乳头状、实性、梭形细胞、网状（retiform）、性索样、肾小球样等多种生长模式。典型者腺腔内可见嗜酸性胶体样物质的小腺体和小管。IHC：ER/PR阴性，P53野生型，GATA3弥漫阳性，TTF-1核阳性，CD10腔面阳性。

8. 胃肠型黏液性癌
主要由黏液分泌上皮形成的胃型或肠型腺体组成，可含有杯状细胞和丰富的嗜伊红或透明胞质，细胞核异型性可高可低。

9. 鳞状细胞癌
表现为尖锐湿疣样外观，因角化呈现白色切面。
此外，子宫神经内分泌肿瘤具有神经内分泌形态，分高、低两级。低级别类似胃肠道类癌；高级别分小细胞和大细胞两种类型。
"""

WHO_2020_ENDOMETRIAL_CLASSIFICATION_EN = """
## ⚠️ WHO 2020 Endometrial Cancer Histological Classification Reference (Consult when determining histological type)

1. Endometrioid carcinoma
Composed entirely of glandular cells, typically showing glandular or villoglandular architecture with crowded and complex branching structures. FIGO grading: G1, G2, G3. Grade may be raised by 1 when glandular epithelial cells show marked atypia. May show squamous differentiation or secretory changes. WHO 2020: G1/G2 = low-grade, G3 = high-grade.

2. Serous carcinoma
Complex papillary and/or glandular architecture with diffuse and prominent nuclear pleomorphism. Most have p53 mutations and high Ki-67 index. May coexist with endometrioid carcinoma; serous component >10% determines serous biological behavior. No longer subgraded. Serous endometrial intraepithelial carcinoma (SEIC) can metastasize even when confined to the endometrium.

3. Clear cell carcinoma
Solid, tubulocystic, or papillary architecture; papillae typically short and rounded with hyalinized stroma. Tumor cells columnar, polygonal, hobnail, or flat; cytoplasm clear or eosinophilic; moderate to severe nuclear pleomorphism.

4. Undifferentiated and dedifferentiated carcinoma
Undifferentiated: uniform cells in sheets, no nesting or glandular structures, frequent mitoses. Dedifferentiated: well-differentiated endometrioid carcinoma at the surface with underlying undifferentiated carcinoma.

5. Carcinosarcoma
Pleomorphic epithelial cells admixed with mesenchymal differentiation. Carcinomatous component usually serous or endometrioid, can also be clear cell or undifferentiated. Sarcomatous component most commonly high-grade without specific differentiation, may show heterologous differentiation (rhabdomyosarcoma, chondrosarcoma, etc.). Monoclonal epithelial-origin neoplasm. Sarcomatous component >50% = sarcoma-predominant carcinosarcoma.

6. Mixed cell adenocarcinoma
2 or more different histological types, at least one being clear cell or serous carcinoma, each component >10%. Most common: endometrioid + serous carcinoma.

7. Mesonephric and mesonephric-like carcinoma (rare, <1%)
Mesonephric-like adenocarcinoma originates from Mullerian epithelium differentiating toward mesonephric pattern; mesonephric carcinoma from mesonephric remnants. Highly diverse architecture: a single tumor may simultaneously exhibit tubular, papillary, solid, spindle cell, retiform, sex cord-like, and glomeruloid patterns. Typical: small glands/tubules with eosinophilic colloid-like luminal material. IHC: ER/PR negative, p53 wild-type, GATA3 diffuse positive, TTF-1 nuclear positive, CD10 luminal positive.

8. Mucinous carcinoma, intestinal type
Composed of gastric or intestinal-type glands with mucin-secreting epithelium, may contain goblet cells and abundant eosinophilic or clear cytoplasm. Nuclear atypia variable.

9. Squamous cell carcinoma
May be associated with chronic inflammation, prolonged pyometra, or prior radiation; HPV association unclear. May show condyloma-like appearance with keratinized white cut surface.

In addition, neuroendocrine tumors of the uterus have neuroendocrine morphology: low-grade (similar to GI carcinoid) and high-grade (small cell and large cell types).
"""


def _get_patient_level_diagnostic_objective(wsi_index=1, slide_index=1, lang='zh'):
    """Patient Level 诊断目标 + 溯源规则（版本 A：证据收集版，供红/蓝/绿/绿分析/WSI汇总共用）"""
    _s = f"S{slide_index}"
    if lang == 'en':
        return f"""## Diagnostic Objective (dimensions the Patient Level final report must cover)

You are currently analyzing Slide #{wsi_index}, labeled as **{_s}**.
You will consolidate this slide's evidence to contribute to the final standardized pathology report. At every analysis stage, your observations should focus on collecting evidence along the following dimensions.

### Traceability & Uncertainty Rules (mandatory)

- **Every diagnostic conclusion (including negative findings) must be traced to an evidence anchor**: S?-R?-B?-G? (if the full path applies; if evidence is obtained at the blue box level, S?-R?-B? is acceptable).
- **Every diagnostic conclusion must include a confidence score**:
  · 0 (insufficient evidence, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features fairly characteristic)
  · 4 (definitive, features typical and unambiguous)
  Example: "- confidence: 3 (probable, features fairly characteristic)"
- If a dimension has no corresponding field of view or cannot be assessed: write **"Not assessed (no corresponding evidence)"**. **Never guess.**
- **Never use "common clinical experience" as evidence.** Do not infer staging or invasion from a single risk factor.
- "No significant findings / not involved" is also critical diagnostic evidence — must be explicitly recorded.

{WHO_2020_ENDOMETRIAL_CLASSIFICATION_EN}

### Patient Level core report items

The Patient Level evaluation contains **14 predefined diagnostic questions**: **13 image-assessable diagnostic dimensions** supported by slide evidence, plus **1 patient-level FIGO staging recommendation** synthesized across all slides and available case context. Specimen information and limitations/recommendations are supplementary report sections and are not counted among the 14 questions.

### Image-assessable diagnostic dimensions

⚠️ The current slide may not cover all dimensions below. Your goal is evidence collection — only report what this slide can provide, for the Orchestrator to integrate later.

**(Specimen type: e.g. total hysterectomy with bilateral salpingo-oophorectomy — for reference only, use actual specimen)**

1. **Histological type**: Based on morphological features (glandular architecture, growth pattern, cytology, etc.), make an **open-ended judgment using WHO/CAP terminology** — do NOT "pick from a preset list". If atypical or multiple possibilities exist: give preferred diagnosis + differential diagnoses + supporting/opposing evidence + recommended additional tests. (⚠️ Refer to the WHO 2020 classification reference above for typing criteria.)

2. **Grading**: FIGO Grade 1/2/3 (if applicable) or High-grade / Low-grade (if applicable); Nuclear grade (1/2/3): nuclear size variation, chromatin, nucleolar features.

3. **Tumor site and size**: Location (fundus/corpus/lower segment/diffuse); maximum diameter (cm).

4. **Myometrial invasion**: Invasion depth (mm/cm) + total wall thickness → ratio (<50% / ≥50%); serosal involvement.

5. **Cervical involvement** (⚠️ must distinguish): Mucosal/glandular involvement only vs **stromal invasion** (depth in mm). 

6. **Parametrium**: Involved / not involved / not applicable (non-radical surgery).

7. **LVSI (lymphovascular space invasion)** (⚠️ per-slide reporting required):
   - Conclusion: absent / suspicious / definite
   - If definite: **count per slide** (consistent with pathology reporting convention):
     - focal: definite foci **<5 per slide** (and most slides negative)
     - extensive: definite foci **≥5 per slide** on any single slide
     - ⚠️ Do NOT add foci across slides to reach the "extensive" threshold
   - MELF pattern (microcystic, elongated, fragmented): present / absent / not assessed
   - This slide: definite foci count + suspicious foci count + location anchors (S?-R?-B?-G?)

8. **Perineural invasion (PNI)**: present / absent / not assessed.

9. **Lower uterine segment**: involved / not involved / not assessed.

10. **Bilateral adnexa** (describe separately): Ovaries: negative / metastatic / benign changes; Fallopian tubes: negative / involved / other.

11. **Vaginal margin/resection margin**: negative / positive / not applicable (non-radical).

12. **Non-neoplastic endometrium**: background endometrial status + EIN/atypical hyperplasia.

13. **Other findings/lesions**: leiomyomas (number, size), adenomyosis, chronic cervicitis, cysts, and other additional findings."""

    return f"""## 诊断目标（Patient Level 最终报告需涵盖的维度）

你当前接收到的是第 {wsi_index} 张 Slide，命名为 **{_s}**。
你将整合当前切片证据，目的是为最终汇总生成标准化病理诊断报告。在每个分析阶段，你的观察都应围绕以下维度收集证据。

### 溯源与不确定性规则（硬性要求）

- **每项诊断结论（含否定结论）必须溯源到证据锚点**：S?-R?-B?-G?（若路径全适用；若截至蓝框已得到证据，可标注 S?-R?-B?）。
- **每项诊断结论（含否定结论）必须标注置信度**：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例："- confidence: 3 (较确定，特征较明确)"
- 若某项没有对应视野或无法判断：写 **"未评估（无对应证据）"**，**严禁猜测**。
- **严禁将"常见情况/经验常识"当证据**。不可从单一风险因素推断侵袭或分期。
- "未见特殊/未累及"也是重要诊断证据，必须明确记录。

{WHO_2020_ENDOMETRIAL_CLASSIFICATION_ZH}

### Patient Level 核心报告项目

Patient Level 评估包含 **14 个预设诊断问题**：其中 **13 个可由图像评估的诊断维度**由各切片提供形态学证据，另有 **1 个患者级 FIGO 分期建议**基于全部切片及可用病例信息综合形成。标本信息和局限性/建议属于补充报告章节，不计入这 14 个问题。

### 图像可评估的诊断维度

⚠️ 当前切片不一定覆盖下方所有维度。你以收集证据为目的，只需写出当前切片能提供的证据，为后续 Orchestrator 整合报告做准备。

**（标本类型：如全子宫及双侧输卵管卵巢切除标本——仅供参考，以实际为准）**

1. **组织学类型**
- **首选组织学类型（WHO/CAP 术语）**：[开放式写出——必须符合上方 WHO 2020 分类参考]
- 形态学依据：[腺体结构、生长模式、细胞形态等关键特征]
- **鉴别诊断（如不唯一）**：[写出最有可能的鉴别诊断]
  - 支持证据：[要点 + 锚点]
  - 反对证据/不足：[要点 + 锚点/缺失项]
- 建议补充：[IHC/分子/额外取材]
- 证据锚点：[S?-R?-B?-G?]
- confidence: [N (标签)]

2. **分级**：FIGO 分级 1/2/3（若适用）或 High-grade / Low-grade（若适用）；核分级（1/2/3）：核大小变异、染色质、核仁特征。

3. **肿瘤部位与大小**：位置（宫底/体部/下段/弥漫）；最大直径（cm）。

4. **肌层浸润**：浸润深度（mm/cm）+ 肌壁总厚度 → 占比（<50% / ≥50%）；是否累及浆膜面。

5. **宫颈受累**（⚠️ 必须区分）：仅黏膜/腺体累及 vs **间质浸润**（浸润深度 mm）。

6. **宫旁组织**：是否累及（非根治术写"未取材/不适用"）。

7. **LVSI（脉管侵犯）**（⚠️ 需逐切片报告）：
   - 结论：无 / 可疑 / 明确有；若"明确有"：局灶 focal（≤4 灶）vs 广泛 extensive（≥5 灶）
   - MELF 模式（microcystic, elongated, fragmented）：有 / 无 / 未评估
   - 本切片明细：definite 灶数 + suspicious 灶数 + 位置锚点（S?-R?-B?-G?）

8. **神经侵犯（PNI）**：有 / 无 / 未评估。

9. **子宫下段**：是否累及。

10. **双侧附件**（左右分别描述）：卵巢：阴性 / 转移累及 / 良性改变（囊肿、内膜异位等）；输卵管：阴性 / 累及 / 其他（系膜囊肿、慢性炎等）。

11. **阴道壁断端/切缘**：阴性 / 阳性 / 不适用（非根治术）。

12. **非癌性子宫内膜**：背景内膜状态 + EIN/非典型增生有无。

13. **其他病变/发现**：肌瘤（数量、大小）、腺肌症、慢性宫颈炎、囊肿等额外发现。"""


def get_patient_level_red_prompt(total_boxes, part_name="Slide 1", folder_name="Patient", wsi_index=1, total_wsi=1, lang='zh'):
    """Patient Level 红框提示词 - MCP Loop 格式 + Evidence-Based Exploration (双语)"""
    _diag_obj = _get_patient_level_diagnostic_objective(wsi_index=wsi_index, slide_index=wsi_index, lang=lang)
    if lang == 'en':
        return f"""You are **PathCopilot Orchestrator**, executing **Patient Level multi-slide joint analysis**.

## System Architecture
- **Orchestrator (you)**: global path planning, task scheduling, evidence aggregation, final diagnosis
- **Sub-agent**: local analysis per field of view, executing MCP Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current phase: Global path planning (thumbnail → select red boxes)
🏥 Patient: {folder_name} | Slide: **{part_name}** ({wsi_index}/{total_wsi})
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

[Evidence] (for the Meta phase — evidence consolidation)
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
- **Sub-agent**：负责各视野的局部分析，执行 MCP Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前阶段：全局路径规划（缩略图 → 选择红框）
🏥 患者: {folder_name} | 切片: **{part_name}** (第 {wsi_index}/{total_wsi} 张)
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
💡 选择原则：在计算预算允许范围内，选择诊断价值最高的 Top-N 区域，最大化证据收集效率。 

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


def get_patient_level_blue_prompt(red_idx, num_blue_boxes, part_name="Slide 1", folder_name="Patient", parent_context="", wsi_index=1, lang='zh'):
    """Patient Level 蓝框提示词 - MCP Loop 格式 + Evidence-Based Exploration (双语)"""
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
- **Sub-agent (you)**: observe and analyze the current field of view, plan the next action via Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current position: 10× low-power (Red box R{red_idx}) → select blue boxes
🏥 Patient: {folder_name} | Slide: **{part_name}**
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

[Evidence] (for the Meta phase — evidence consolidation)
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
- **Sub-agent（你）**：负责当前视野的观察分析和路径规划，执行 Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前位置：10× 低倍镜（红框 R{red_idx}）→ 选择蓝框
🏥 患者: {folder_name} | 切片: **{part_name}**
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

💡 选择原则：在计算预算允许范围内，选择诊断价值最高的 Top-N 区域，最大化证据收集效率。 
⚠️ 注意：选择的区域必须是纯数字列表，如 [1, 5, 9]"""


def get_patient_level_green_prompt(blue_idx, num_green_boxes, part_name="Slide 1", folder_name="Patient", parent_context="", wsi_index=1, lang='zh'):
    """Patient Level 绿框提示词 - MCP Loop 格式 + Evidence-Based Exploration (双语)"""
    _diag_obj = _get_patient_level_diagnostic_objective(wsi_index=wsi_index, slide_index=wsi_index, lang=lang)
    if lang == 'en':
        context_section = ""
        if parent_context:
            context_section = f"""
## 📋 Path Context (inherited from parent analysis)

Your task is to confirm or supplement evidence at higher magnification, based on the results of the parent path.

**Parent path output:**
{parent_context}

Combine this with the parent analysis to seek higher-resolution evidence (e.g. nuclear atypia, mitotic figures, vascular invasion).
"""
        return f"""You are **PathCopilot Sub-agent**, dispatched by the **Orchestrator** to execute Patient Level analysis.

## System Architecture
- **Orchestrator**: task scheduling, evidence aggregation, final diagnosis
- **Sub-agent (you)**: observe and analyze the current field of view, plan the next action via Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current position: 40× medium-power (Blue box B{blue_idx}) → select green boxes
🏥 Patient: {folder_name} | Slide: **{part_name}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_section}
{_diag_obj}

## Key Judgment

Can the current 40× view already answer the diagnostic question for this slide?
- **B(Stop)**: evidence is sufficient (e.g. "no tumor involving cervix", "leiomyoma, benign", "no significant findings")
- **A(Drill-down)**: 160× needed (suspected LVSI, grading or invasion depth requires more detail)

⚠️ **40× Medium-Power Stage Rules**:
Within this multiscale workflow, the 40× stage is used primarily to evaluate tissue architecture, growth patterns, relationships with surrounding tissue, and regions requiring further verification. Final confirmation of cellular lineage and malignant cytological features is performed at 160×. Different cellular components may show similar cellular or hyperchromatic appearances at the current magnification; therefore:

1. **Use evidence-graded language**: In the Reason section, prioritize morphology directly visible in the current field. Before 160× verification, do not describe a cellular area as “definite tumor invasion,” “definite carcinoma nests,” or “definite malignant cells.” Use terms such as “suspicious,” “favors,” or “requires high-power confirmation.”

2. **Record relevant differential diagnoses**: For cellular or architecturally complex areas, list relevant alternatives according to the anatomical site and tissue context, including inflammatory aggregates, solid epithelial or other tumor components, and stromal or smooth-muscle cross/oblique sections. State the supporting evidence and limitations visible in the current field.

3. **Confidence ceiling**: This stage does not provide final cytological classification. Before 160× verification, interpretations concerning malignancy or tumor identity must not exceed confidence 2 and must be marked as requiring high-power confirmation.

4. **Independent downstream verification**: The 40× description may provide location and architectural context for the downstream 160× field, but it does not substitute for direct high-power observation or predetermine the final conclusion.

## Current Task

Blue box B{blue_idx} has been divided into **{num_green_boxes} green box candidates** (numbered 1–{num_green_boxes}).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 Output Format

[Reason]
1. Region nature: (based on slide site {part_name}, identify the tissue type)
2. Observations: (describe histological features)
3. Evidence collection: (what evidence does this view provide?)
4. Evidence assessment: is current evidence sufficient to answer this slide's diagnostic question?

[Plan]
Selected action: A(Drill-down) / B(Stop) / C(Backtrack) / D(Knowledge) / E(Neighbor)
Action parameter:
- A(Drill-down): selected green box regions = [list of indices]
- B(Stop): reason why current evidence is sufficient (e.g. "no significant findings", "benign confirmed")
- C(Backtrack): reason + suggested new position
- D(Knowledge): specific query
- E(Neighbor): spatial relationship description
Rationale: (explain your action choice)

[Evidence]
- Main findings: (findings along the current analysis path)
- Confidence: · 0 / · 1 / · 2 / · 3 / · 4

⚠️ Note: if choosing B(Stop), use []"""

    # ⭐ 构建路径上下文部分
    context_section = ""
    if parent_context:
        context_section = f"""
## 📋 路径上下文（继承自上级分析）

你的任务是基于上级路径的分析结果，在高倍镜下确认或补充证据。

**上级路径输出：**
{parent_context}

请结合上级分析，在当前视野中寻找更高分辨率的证据（如细胞异型性、核分裂象、脉管侵犯等）。
"""
    
    return f"""你是 **PathCopilot Sub-agent（子智能体）**，由 **Orchestrator（编排器）** 调度执行 Patient Level 分析。

## 系统架构
- **Orchestrator**：负责任务调度、证据整合、最终诊断
- **Sub-agent（你）**：负责当前视野的观察分析和路径规划，执行 Agent Loop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前位置：40× 中倍镜（蓝框 B{blue_idx}）→ 选择绿框
🏥 患者: {folder_name} | 切片: **{part_name}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_section}
{_diag_obj}

## 关键判断

当前 40× 视野是否已能回答本切片的诊断问题？
- **B(停止)**：证据已足够（如"宫颈未见肿瘤累及"、"平滑肌瘤，良性"、"未见特殊"）
- **A(下钻)**：需要 160× 确认（疑似脉管侵犯、分化程度/浸润深度需更多细节）

⚠️ **40× 中倍镜阶段规则**：
在本多尺度分析流程中，40× 主要用于观察组织架构、生长模式和病变与周围组织的关系，并筛选需要进一步核验的区域；涉及细胞谱系和恶性细胞学特征的最终确认由 160× 视野完成。不同性质的细胞成分在当前倍率下可能呈现相似的细胞密集或深染外观，因此：

1. **使用证据分级语言**：在 Reason 中优先描述当前视野可直接观察到的形态学特征。对于尚未经过 160× 核验的细胞密集区域，不得直接写成“明确肿瘤浸润”“明确癌巢”或“明确恶性细胞”；可使用“可疑”“倾向”或“需要高倍确认”等表述。

2. **记录相关鉴别诊断**：对细胞密集或结构复杂的区域，应结合解剖部位和组织背景列出相关鉴别，包括炎细胞聚集、实性上皮性或其他肿瘤成分，以及间质或平滑肌横切/斜切等，并说明当前视野中可见的支持或限制因素。

3. **置信度上限**：本阶段不承担最终细胞学定性。涉及恶性或肿瘤性质的判断在完成 160× 核验前最高为 confidence 2，并应明确标记为待高倍确认。

4. **下级独立核验**：40× 描述可为 160× 下级视野提供定位和组织架构背景，但不能替代下级视野的直接观察，也不能预先决定其最终结论。

## 当前任务

蓝框 B{blue_idx} 区域已被划分为 **{num_green_boxes} 个绿框候选**（编号 1-{num_green_boxes}）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 输出格式（严格按此格式输出）

【Reason】
1. 区域性质：（基于切片部位 {part_name}，判断当前视野的组织类型）
2. 观察发现：（描述组织学特征）
3. 证据收集：（针对本切片诊断任务，当前视野能提供哪些证据？）
4. 证据评估：当前证据是否足够回答本切片的诊断问题？

【Plan】
选择动作：A(下钻) / B(停止) / C(回溯) / D(查库) / E(查邻)
动作参数：
- A(下钻)：选择的绿框区域 = [编号列表]
- B(停止)：说明当前证据已足够的原因（如"未见特殊"、"良性确认"）
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

💡 选择原则：在计算预算允许范围内，选择诊断价值最高的 Top-N 区域，最大化证据收集效率。 
⚠️ 注意：如选 B(停止)，区域列表填 [] 
⚠️ 注意：若选择区域，选择的区域必须是纯数字列表，如 [1, 5, 9]"""


def get_patient_level_green_analysis_prompt(roi_id, parent_id, part_name="Slide 1", folder_name="Patient", parent_context="", wsi_index=1, lang='zh'):
    """Patient Level 绿框解读提示词 - 160× 高倍镜细胞学分析 (双语)"""
    _diag_obj = _get_patient_level_diagnostic_objective(wsi_index=wsi_index, slide_index=wsi_index, lang=lang)
    if lang == 'en':
        context_section = ""
        if parent_context:
            context_section = f"""
## 📋 Path Context (inherited from the complete analysis path)

The following is the full analysis path leading to the current view. Use this context for your high-power observation:

{parent_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        return f"""You are **PathCopilot Sub-agent**, performing the final high-power observation in **Patient Level analysis**.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current position: **160× high-power** (Green box {roi_id}) | Parent region: {parent_id}
🏥 Patient: {folder_name} | Slide: **{part_name}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_section}
You are observing this region under **160× high-power magnification**. This is the terminal magnification in the analysis path.

{_diag_obj}

## 160× High-Power Morphologic Verification Framework

⚠️ **Independent evidence principle**:
The parent red- and blue-box descriptions may be used to locate the field and understand its low- and medium-power context, but they do not substitute for morphology directly visible in the current 160× field and must not predetermine its interpretation.

Record the cellular and architectural features directly visible in the current field. The Orchestrator will subsequently integrate this observation with lower-power and other-path evidence. Do not extrapolate whole-slide tumor proportions, invasion depth, stage, or patient-level conclusions from a single 160× field.

1. **Assessability**
First determine whether the field is adequate for evaluation. Record sectioning, folding, crush, fixation, staining, contamination, or tissue-loss artifacts that may affect interpretation. If a key structure cannot be assessed reliably, state that the evidence is insufficient.

2. **Lineage and differentiation evidence**
Use positive morphologic evidence to determine whether the cells favor epithelial, mesenchymal, hematolymphoid, inflammatory/histiocytic, or indeterminate differentiation.

If an epithelial lesion is proposed, describe the visible epithelial differentiation evidence, such as glandular, cribriform or papillary architecture, mucin or keratinization, intercellular connections, or nests/solid arrangements with epithelial cytomorphology.

Do not infer epithelial origin solely by excluding other cell types. Hyperchromatic clusters, single-file arrangements, or cellular density alone do not establish epithelial malignancy.

3. **Atypia and malignant-behavior evidence**
Record the presence and assessability of architectural disorder or destructive invasion; nuclear size, shape, membrane, and chromatin abnormalities; loss of polarity or abnormal differentiation; definite atypical mitoses; tumor-associated necrosis; and growth discordant with the surrounding tissue. Do not require a mechanical fixed number of features; integrate their concordance, specificity, and reproducibility.

4. **Relevant mimics and alternative explanations**
According to the anatomic site and tissue background, assess relevant alternatives, including smooth-muscle or other stromal cells in cross/oblique section; lymphoid, plasma-cell, or histiocytic aggregates; reparative, degenerative, or hormonal changes; decidualized stroma or paraganglion-like structures; fallopian-tube folds or other section overlap; and squamous differentiation, morules, or other benign/reactive structures.

When a solid or ovoid cell aggregate is actually present in a smooth-muscle-rich background, the following labels may be used as structured observation aids:
- [SM-1] Whether continuity or transition with longitudinal spindle cells is visible in the same or an adjacent field;
- [SM-2] Whether the cytoplasm has a fibrillary or myofibrillar quality;
- [SM-3] Whether assessable nuclei show blunt-ended or other smooth-muscle-like contours;
- [SM-4] Whether the aggregate merges parallel to the surrounding stroma or instead shows an expansile/destructive interface.

These are descriptive evidence domains, not diagnostic votes. Record only assessable items and the relevant counterevidence. No individual item or fixed number of items automatically establishes smooth-muscle or epithelial origin; unresolved or conflicting findings should remain indeterminate.

For each relevant differential, describe both supporting and opposing visible evidence. Do not presume a benign or malignant interpretation, and do not complete the classification automatically from a single feature.


5. **Integrated judgment**
- When the findings are concordant and major mimics are reasonably excluded, provide the supported interpretation.
- When positive evidence is insufficient, a key structure is not visible, or a plausible alternative remains unresolved, report "suspicious" or "indeterminate" and state the limitation.
- If an isolated or discordant finding could materially affect the patient-level conclusion, flag it for review during patient-level synthesis rather than treating it as confirmed from this field alone.
- If immunohistochemistry, special stains, or molecular testing is required for confirmation, state this explicitly; the proposed test is not itself evidence for the current morphologic conclusion.

6. **Tissue architecture and differentiation**: glandular differentiation, solid growth, and growth pattern visible in the current field. Do not infer whole-tumor proportions from this field alone.

7. **Cytoplasmic features**: cytoplasmic amount and eosinophilic, basophilic, clear, mucinous, or other directly visible features relevant to classification.

⚠️ **LVSI Diagnostic Caution**: LVSI can be called "definite" only when an endothelial-lined vascular/lymphatic space containing a tumor embolus is unequivocally visible and retraction artifact has been excluded. If the endothelial lining is not clearly seen, or intraluminal cells may represent inflammatory cells, report "suspicious" or "indeterminate", not definitive.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 📝 Output Format

[Reason]
Under 160× high-power field (ROI: {roi_id}), the following features are observed:
- **Assessability**:
- **Positive lineage/differentiation evidence**:
- **Atypia or malignant-behavior evidence**:
- **Relevant mimics and their supporting/opposing evidence**:
- **Current-field interpretation**:
- **Unresolved uncertainty**:
- **LVSI, if assessable**: (definite / suspicious / indeterminate / absent — describe endothelial lining, luminal content, and whether retraction artifact is excluded)

[Evidence] (evidence collected for the final report)
- Main findings: (findings along the current analysis path to this field)
- Confidence: choose one:
  · 0 (insufficient, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features fairly characteristic)
  · 4 (definitive, features typical and unambiguous)"""

    # ⭐ 构建路径上下文部分
    context_section = ""
    if parent_context:
        context_section = f"""
## 📋 路径上下文（继承自上级分析路径）

以下是你到达当前视野的完整分析路径，请基于这些上下文进行高倍镜观察：

{parent_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    return f"""你是 **PathCopilot Sub-agent（子智能体）**，正在执行 **Patient Level 分析** 的最终高倍镜观察。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前位置：**160× 高倍镜**（绿框 {roi_id}）| 父区域: {parent_id}
🏥 患者: {folder_name} | 切片: **{part_name}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{context_section}
你正在 **160× 高倍镜** 下观察该区域。这是整个分析路径的最终放大。

1. **观察**：在 160× 高倍镜下观察当前视野，收集细胞学证据
2. **汇总**：结合上方路径上下文（10×、40× 的分析），为 Orchestrator 整合本路径的证据结论

{_diag_obj}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 🔬 160× 高倍镜证据核验
⚠️ **独立证据原则**：
上级红框和蓝框的描述仅用于定位当前视野及提供低、中倍结构背景，不能替代当前 160× 视野内的形态学证据，也不能预先决定本视野的诊断方向。

请独立记录当前视野可直接观察到的细胞和组织学特征。当前视野的结论随后由 Orchestrator 与低倍、中倍及其他路径证据综合；不得由单个 160× 视野外推全片肿瘤比例、浸润深度、分期或患者级结论。

## 高倍形态学核验框架

1. **可评估性**
首先判断当前视野是否足以评价。记录可能影响判断的切面、折叠、挤压、固定、染色、污染或组织缺失等因素。若关键结构无法可靠辨认，应明确报告证据不足。

2. **细胞谱系与分化证据**
根据当前视野的正向形态学证据，判断所见细胞更支持上皮性、间叶性、淋巴造血性、炎症/组织细胞性或暂不能确定。
若提出上皮性病变，应描述可见的上皮分化依据，例如腺腔、筛状或乳头状结构、黏液或角化、细胞间连接、具有上皮形态的巢状或实性排列等。

不得仅通过排除其他细胞类型推导上皮来源；“深染细胞成团”“单行排列”或“细胞密集”本身均不足以证明上皮性恶性。

3. **异型与恶性行为证据**
分别记录以下证据是否存在及其可辨认程度：
- 结构紊乱或破坏性浸润；
- 核大小、形态、核膜和染色质异常；
- 极性丧失或异常分化；
- 明确异常核分裂；
- 与肿瘤结构相关的坏死；
- 与周围组织关系不相称的生长方式。

不要求机械地满足固定数量的特征。应根据各项证据的一致性、特异性和可重复性综合判断。

4. **主要模拟病变与替代解释**
根据当前解剖部位和组织背景，评估合理的替代解释，包括但不限于：
- 平滑肌或其他间质细胞的横切/斜切；
- 炎细胞、浆细胞或组织细胞聚集；
- 修复性、退行性或激素相关改变；
- 蜕膜化间质、副神经节样结构；
- 输卵管黏膜皱襞或其他切面重叠；
- 鳞状分化、桑葚体，以及其他可能与肿瘤巢混淆的结构。

当富含平滑肌的组织背景中实际出现实性或卵圆细胞团时，可使用以下标签辅助结构化记录：
- [SM-1] 同一视野或邻近区域是否可见纵切梭形细胞与该细胞团移行或连续；
- [SM-2] 胞质是否呈纤维丝状或肌原纤维样质感；
- [SM-3] 可评估的细胞核是否呈钝圆核端或其他平滑肌样轮廓；
- [SM-4] 细胞团是否与周围间质平行移行，或相反呈现推挤性/破坏性界面。

上述项目是形态学证据观察维度，不是诊断投票。仅记录当前视野中可评估的项目及相关反证；任何单项或固定数量的项目均不能自动确定平滑肌或上皮来源。证据冲突或关键关系无法辨认时，应保留来源不确定性。

对每项实际相关的鉴别，应同时描述支持该解释和反对该解释的可见证据。不得预设其一定为良性或恶性，也不得仅因某一形态特征存在而自动完成分类。


5. **综合判断**
- 证据一致且能够合理排除主要模拟病变时，可给出相应判断；
- 正向证据不足、关键结构不可见，或存在合理且未解决的替代解释时，应报告“可疑”或“无法确定”，并说明局限；
- 对可能影响患者级主要结论、但在当前路径中孤立或与其他证据不一致的发现，应标记为患者级汇总时需要复核的事项，不得仅由本视野视为确认结论；
- 需要免疫组化、特殊染色或分子检测才能确认时，应明确说明；建议的检测本身不构成当前形态学结论的证据。


6. **组织结构与分化**（→ 用于分级判定）：腺体分化程度、实性生长比例

7. **细胞质特征**（→ 用于分型鉴别）：胞质量、嗜酸性/透明性

8. **LVSI 识别**：依据当前视野内可见的脉管结构、腔内细胞及其与管壁的关系进行判断，并与收缩裂隙、炎细胞团和组织拖带等替代解释鉴别。
只有同时满足以下形态学条件时，才可报告"确认阳性"：
(1) 腔内存在与周围肿瘤细胞学一致的**成团癌栓/肿瘤细胞团**；且
(2) 可辨识**内皮衬覆/独立管壁结构**；并已
(3) **排除收缩伪影/炎细胞团**。
腔内 RBC 或蛋白样内容可作为识别脉管腔的辅助线索，但既非必要条件，也不能单独证明 LVSI。应结合内皮或管壁结构、腔内肿瘤细胞团及伪影鉴别综合判断；关键关系无法可靠辨认时，报告“可疑/无法确定”并说明局限，必要时建议辅助检查。

9. **其他特殊发现**：淋巴细胞浸润、肿瘤性坏死、间质反应等

## 📝 输出格式（严格按此格式输出）

【Reason】
【当前视野观察】
- 可评估性：
- 细胞谱系/分化的正向证据：
- 异型或恶性行为证据：
- 主要模拟病变及其支持/反对证据：
- 当前视野结论：
- 未解决的不确定性：
- LVSI（如可评价）：

【Evidence】（为最终报告收集的证据）
- 主要发现: （当前路径下钻至当前视野下的证据发现）
- 置信度: 输出格式为 "N (标签)"，N 取值如下：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
  示例：- 置信度: 3 (较确定，特征较明确)"""


def build_path_summary_from_tree_nodes(tree_nodes, global_analysis='', lang='zh'):
    """从 tree_nodes 构建带 R-B-G ID 的完整路径证据文本（用于 Patient Level 单张WSI汇总 prompt）。
    参照 generate_final_pathology_report 的逻辑，保留完整 reasoning + global_analysis。
    Patient Level 绿框按诊断维度输出结构化证据，不使用旧单片题号槽位。
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


def get_patient_level_final_summary_prompt(folder_name, all_wsi_summaries, total_wsi, lang='zh'):
    """Patient Level 最终综合报告提示词 - 版本B：全患者证据整合 → 结构化病理报告 (双语)"""
    if lang == 'en':
        return f"""You are the **Orchestrator**, integrating evidence from all slides/WSIs to produce a **standardized pathology diagnostic report (Patient Level)**.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏥 Patient: {folder_name} | Slides analyzed: **{total_wsi}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Structured Evidence Summaries from All Slides
(Each slide has been independently analyzed and distilled into key conclusions with S-R-B-G path traceability, where S=Slide number, e.g. S1-R2-B3-G1)

{all_wsi_summaries}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Step 0: Assess Evidence Sufficiency

Review all slide summaries above. For each diagnostic dimension, determine whether the structured summaries provide enough information to generate the report.

**Core principles:**
- **Trust the sub-agent's conclusions and confidence levels.** A low confidence score (e.g., 2 = "possible") is the sub-agent's honest professional judgment based on the morphology they observed — it is NOT a signal that you need to double-check their work. Low confidence alone must NEVER trigger NEED_DETAIL.
- **This step is contradiction-driven, not doubt-driven.** Only request details when the collected evidence is genuinely insufficient to write the report — not because a finding seems uncertain.

**When to trigger NEED_DETAIL:**
- Slides give **directly contradictory conclusions** on the same diagnostic dimension (e.g., S2 says "stromal invasion present" but S5 says "no invasion"), AND the contradiction cannot be resolved from the summaries alone.
- A summary is **missing critical information** that makes it impossible to produce the report (e.g., a slide mentions "atypical features" but provides no details about what was observed).

**When NOT to trigger NEED_DETAIL:**
- A sub-agent reports low confidence — that is their valid conclusion, respect it.
- A finding is marked "suspicious" or "possible" — this is expected for borderline morphology.
- You simply want more certainty — the report should reflect the actual evidence, including uncertainty.

- If evidence is sufficient to generate the report → proceed directly to Step 1-3.
- If a genuine cross-slide contradiction or critical information gap exists → output ONLY the following block and STOP (do NOT generate the report):

```
[NEED_DETAIL]
- Target: S2 | Need: full_analysis | Reason: LVSI evidence contradicts across regions, need full path details
- Target: S3-R2-B1-G3 | Need: analysis+image | Reason: need to confirm stromal invasion in this green box
- Target: S1-R1-B2 | Need: image | Reason: nuclear atypia uncertain, need visual confirmation
- Target: KNOWLEDGE | Need: who_2020_classification | Reason: need to confirm histological classification criteria for typing
[/NEED_DETAIL]
```

**Target format** (from coarse to fine):
- `SX` → full detailed path analysis for entire slide X
- `SX-RY` → red box RY and all its sub-paths in slide X
- `SX-RY-BZ` → blue box BZ and all its sub-paths
- `SX-RY-BZ-GW` → specific green box analysis
- `KNOWLEDGE` → query the knowledge base (see knowledge query types below)

**Need types**:
- `full_analysis`: text analysis only
- `analysis+image`: text analysis + the actual ROI slide image for visual confirmation
- `image`: ROI slide image only
- Knowledge query types (when Target = `KNOWLEDGE`):
  - `who_2020_classification`: WHO 2020 endometrial cancer histological classification — all types, diagnostic criteria, and differential points
  - `figo_staging_2023`: FIGO 2023 staging criteria
  - `molecular_classification`: TCGA/WHO molecular classification (POLEmut/dMMR/p53abn/NSMP)
  - `lvsi_criteria`: LVSI assessment and stratification criteria
  - `endometrioid adenocarcinoma` / `serous carcinoma` / `clear cell carcinoma` / `carcinosarcoma` / `undifferentiated_dedifferentiated carcinoma`: specific tumor type diagnostic criteria

⚠️ Only request details when cross-slide contradictions or critical information gaps make report generation impossible. Prefer specific ROI targets (`SX-RY-BZ`) over whole slides (`SX`) to minimize cost.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 1. Traceability & Uncertainty Rules (mandatory, must follow)

- **Every diagnostic conclusion (including negatives) must trace to evidence anchors**: `S?-R?-B?-G?`.
  Multiple slides/ROIs → list multiple anchors.
- **If no corresponding field of view or cannot assess**: write **"Not assessed (no corresponding evidence)"**. **Never guess.**
- **Never use "common clinical experience" as evidence.** Do not infer staging or invasion from a single risk factor.
- **If evidence is insufficient to determine uniquely**: must provide
  preferred diagnosis + differential diagnoses (2-4) + supporting/opposing points + recommended supplementary tests (IHC/molecular/sampling).
- **Every diagnostic conclusion must include a confidence score**:
  · 0 (insufficient evidence, unable to assess)
  · 1 (uncertain, more information needed)
  · 2 (possible, differential diagnosis exists)
  · 3 (probable, features fairly characteristic)
  · 4 (definitive, features typical and unambiguous)
- **Fixed output structure per field**:
  `Conclusion: …` → `Morphological basis: …` → `Evidence anchors: S?-R?-B?-G?` → `confidence: N (label)`
- **Confidence-weighted evidence integration**: When synthesizing evidence across multiple ROIs/slides for the same diagnostic dimension:
  · Findings with confidence ≥ 3 (probable/definitive) carry substantially greater weight than those with confidence ≤ 2 (uncertain/possible). Consistent, reproducible high-confidence conclusions should anchor the diagnosis.
  · Low-confidence findings should be recorded as differential clues or caveats, but must NOT override or dilute a well-supported high-confidence conclusion — unless they present specific, compelling morphological counter-evidence (not merely "alternative possibility").
  · When two findings directly conflict, prefer the one with higher confidence, more complete evidence chain (more anchors, higher-magnification confirmation), and greater reproducibility across ROIs.
  · **Integration of conflicting evidence**: When different ROIs or slides support different histological types, do not determine the preferred diagnosis solely by counting selected ROIs or applying a simple majority rule. Red, blue, and green boxes are purposefully selected multiscale samples and may be nested or repeatedly represent the same lesion. Integrate tissue representativeness, low- and medium-power architecture, concordance across magnifications, morphological specificity, reasonable exclusion of major mimics, and spatial relationships across slides. If the conflict remains unresolved, report a preferred diagnosis together with the relevant differential diagnoses, explicitly preserving the uncertainty and recommending appropriate ancillary evaluation.
- **Secondary-review adjudication**: {secondary_review_honor_rule('en')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{WHO_2020_ENDOMETRIAL_CLASSIFICATION_EN}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 2. Item-by-item Diagnosis (must cover all, no omission)

### 2.1 Specimen Information
- Specimen type: [e.g. total hysterectomy with bilateral salpingo-oophorectomy — use actual]
- Sampling scope: [uterine corpus / cervix / parametrium / adnexa / margins / lymph nodes / ascites, as applicable]

### 2.2 Histological Type (open-ended judgment; do not select from a preset list)

⚠️ **Rules for integrating histological typing evidence**:

1. **Evidence source**: Representative main-tumor regions from primary-organ slides are the principal evidence source for histological typing. Ovarian or fallopian-tube lesions confirmed to represent metastases may provide corroborating evidence. If the origin of an adnexal lesion remains uncertain or its morphology differs from that of the primary site, document the evidence separately and retain the relevant primary-site differential diagnosis; do not presume that either interpretation is necessarily correct or incorrect.

2. **Traceability**: Histological-type summaries produced by individual Sub-agents serve as evidence references only. The patient-level conclusion must be traceable to specific slides, analysis paths, and morphological findings. Do not introduce a new histological type that was not raised in any single-slide report and lacks direct morphological support.

3. **Integration of strong signals from primary-organ slides**: Integrate typing evidence from all primary-organ slides, giving priority to findings from representative tumor regions that are consistent across magnifications, reproducible across fields or slides, and supported with higher confidence. The number of supporting slides may contribute evidence of consistency but must not be the sole adjudication criterion. A typing direction supported only by an isolated focus, without corroboration from another representative region or independent evidence, must not be promoted to the preferred diagnosis solely on that basis; retain it as an important differential consideration and state the required ancillary verification.

4. **Composite diagnostic labels**: Do not create a diagnosis such as “mixed carcinoma,” “mixed type,” or “dual phenotype” solely because different slides support different individual histological types. Such a composite diagnosis may be considered only when distinct histological components within the same representative tumor each have clear, independent morphological support and satisfy the applicable WHO diagnostic criteria. Otherwise, report a preferred histological type and the principal differential diagnoses, with the evidence source for each.

⚠️ Refer to the **WHO 2020 Classification Reference** above for morphological criteria.

- **Preferred histological type (WHO/CAP terminology)**: [open-ended — must conform to the WHO 2020 classification reference above]
- Morphological basis: [glandular architecture, growth pattern, cytology]
- **Differential diagnoses (if not unique)**: [2-4]
  - Supporting evidence: [points + anchors]
  - Opposing evidence/gaps: [points + anchors/missing items]
- Recommended supplementary: [IHC/molecular/additional sampling]
- Evidence anchors: [S?-R?-B?-G?]
- confidence: [N (label)]

### 2.3 Grading
- FIGO Grade 1/2/3 (if applicable) or High-grade / Low-grade (if applicable)
- Nuclear grade: [1/2/3 or not assessed]
- Morphological basis: [...]
- Evidence anchors: [S?-R?-B?-G?]
- confidence: [N (label)]

### 2.4 Tumor Site and Size
- Site: [fundus / corpus / lower segment / diffuse / not assessed]
- Maximum diameter: [ ] cm (if no data: "not provided")
- Evidence anchors: [...]

### 2.5 Myometrial Invasion
- Myometrial invasion: [absent / present / not assessed]
- Invasion depth: [ ] mm/cm
- Total wall thickness: [ ] mm/cm
- Ratio: [ ]% (<50% = FIGO IA; ≥50% = FIGO IB)
- Serosal involvement: [absent / present / not assessed]
- Morphological basis: [...]
- Evidence anchors: [S?-R?-B?-G?]
- confidence: [N (label)]

### 2.6 Cervical Involvement (⚠️ must distinguish mucosal vs stromal)
- Cervical mucosal/glandular involvement: [absent / present / not assessed]
- **Cervical stromal invasion**: [absent / present / not assessed]
- Stromal invasion depth: [ ] mm
- Morphological basis: [...]
- Evidence anchors: [S?-R?-B?-G?]
- confidence: [N (label)]

### 2.7 Parametrium
- Parametrium: [not involved / involved / not assessed / not applicable (non-radical surgery)]
- Evidence anchors: [...]

### 2.8 LVSI (⚠️ must integrate per-slide reports)
- **Overall conclusion**: [absent / suspicious / present]
- If "present": focal (≤4 foci) vs extensive (≥5 foci)
- MELF pattern: [present / absent / not assessed]
- **Per-slide LVSI detail (must list each slide)**:
  - S1 ([slide name]): definite = [ ] foci; suspicious = [ ] foci; location: [S1-R?-B?-G?]
  - S2 ([slide name]): definite = [ ] foci; suspicious = [ ] foci; location: [S2-R?-B?-G?]
  - ... (list all slides)
- Integration basis: [brief description of cross-slide LVSI distribution]
- confidence: [N (label)]

### 2.9 Perineural Invasion (PNI)
- PNI: [absent / present / not assessed]
- Evidence anchors: [...]

### 2.10 Lower Uterine Segment
- Involvement: [absent / present / not assessed]
- Evidence anchors: [...]

### 2.11 Bilateral Adnexa (describe left and right separately)
- Left ovary: [negative / metastatic / benign changes] — Evidence anchors: [...]
- Right ovary: [...]
- Left fallopian tube: [...]
- Right fallopian tube: [...]

### 2.12 Vaginal Cuff / Resection Margin
- Margin: [negative / positive / not assessed / not applicable (non-radical)]
- Evidence anchors: [...]

### 2.13 Non-neoplastic Endometrium
- Background endometrium: [atrophic / proliferative / secretory / other / not assessed]
- EIN/atypical hyperplasia: [absent / present / not assessed]
- Evidence anchors: [...]

### 2.14 Other Findings/Lesions
- [leiomyomas (number, size) / adenomyosis / chronic cervicitis / cysts / other additional findings]
- Evidence anchors: [...]

### 2.15 FIGO Staging Recommendation
- Stage: [IA / IB / II / IIIA / IIIB / IIIC1 / IIIC2 / IVA / IVB]
- Staging basis: [list key supporting evidence and anchors for each criterion]
- If insufficient information: write "Cannot assess (reason: ...)"
- confidence: [N (label)]

### 2.16 Limitations and Recommendations
- Which items are "not assessed / low confidence" and why
- Recommended supplementary: [IHC / molecular testing / additional sampling / clinical information]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 3. Output Format (follow this order)

**[Final Diagnosis (Detailed Review Version)]**
Strictly follow Section 2 format, item by item (2.1-2.16). Each item must contain: conclusion → morphological basis → evidence anchors → confidence. Full traceability for clinical review.

**[Final Diagnosis (One-sentence Integrated Format)]**
Integrate into a standardized clinical pathology diagnostic statement grouped by specimen site:
1. Begin with "(Specimen site)" and group by specimen;
2. Main specimen: histological type + grade + depth of myometrial invasion + LVSI + cervical involvement + parametrial status;
3. List each adnexal specimen individually (left/right ovary, left/right fallopian tube), including negative findings (e.g., "unremarkable");
4. ALL other findings from Section 2.14 (adenomyosis, chronic inflammation, cysts, etc.) MUST be included — do NOT omit any;
5. Reference format: (Specimen site) [histological diagnosis], [invasion depth], [LVSI status]; [parametrial status]; [cervical status]; [left ovary], [right ovary], [left fallopian tube], [right fallopian tube]; [other findings]."""

    return f"""你是 **Orchestrator**，将整合所有切片/WSI 的证据，输出**标准化病理诊断报告（Patient Level）**。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏥 患者: {folder_name} | 已分析切片数: **{total_wsi}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 以下是所有切片的结构化证据摘要
（每张切片已独立完成分析并提炼关键结论与 S-R-B-G 路径追溯，S=切片编号，如 S1-R2-B3-G1）

{all_wsi_summaries}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 第零步：评估证据充分性

审阅以上所有切片的结构化证据摘要。针对每个诊断维度，判断当前收集的证据是否足以生成报告。

**核心原则：**
- **信任子代理同事给出的结论和置信度。** 低置信度（如 2 = "可能"）是子代理基于所观察形态学特征做出的专业判断，而非需要你复核的信号。低置信度本身绝不构成触发 NEED_DETAIL 的理由。
- **本步骤以整合驱动，而非以怀疑驱动。** 仅在已收集的证据确实不足以撰写报告时才请求详情——而非因为某个发现看起来不够确定。

**触发 NEED_DETAIL 的条件：**
- 多张切片对同一诊断维度给出了**直接矛盾的结论**（如 S2 称"间质浸润阳性"而 S5 称"无浸润"），且仅凭摘要无法判断矛盾原因。
- 摘要**缺失关键信息**导致无法生成报告（如某切片提到"异型特征"但未说明具体观察到的形态）。

**不应触发 NEED_DETAIL 的情况：**
- 子代理给出低置信度 —— 那是他们的合理结论，应予尊重。
- 某发现标注为"可疑"或"可能" —— 这是边界性形态的正常表述。
- 你仅仅想要更高确定性 —— 报告应如实反映证据现状，包括不确定性。

- 如果证据足以生成报告 → 直接进入第 1-3 步。
- 如果存在真正的跨切片矛盾或关键信息缺失 → 仅输出以下格式并**停止**（不要生成报告）：

```
[NEED_DETAIL]
- 目标: S2 | 需要: 完整解读 | 原因: 该切片 LVSI 证据矛盾，需查看全部路径细节
- 目标: S3-R2-B1-G3 | 需要: 解读+图片 | 原因: 需确认该绿框区域是否存在间质浸润
- 目标: S1-R1-B2 | 需要: 图片 | 原因: 核异型性不确定，需直接观察该蓝框图像
- 目标: KNOWLEDGE | 需要: who_2020_classification | 原因: 需要确认组织学分型标准以鉴别混合性腺癌
[/NEED_DETAIL]
```

**目标格式**（从粗到细）：
- `SX` → 查阅整张切片 X 的全部路径详细解读
- `SX-RY` → 查阅切片 X 中红框 RY 及其所有子路径
- `SX-RY-BZ` → 查阅蓝框 BZ 及其所有子路径
- `SX-RY-BZ-GW` → 查阅特定绿框的解读
- `KNOWLEDGE` → 查询知识库（见下方知识查询类型）

**需要类型**：
- `完整解读`：仅查阅文本分析
- `解读+图片`：查阅文本分析 + 该 ROI 的切片图像（用于视觉确认）
- `图片`：仅查看 ROI 切片图像
- 知识查询类型（目标为 `KNOWLEDGE` 时）：
  - `who_2020_classification`：WHO 2020 子宫内膜癌组织学分类——各类型定义、诊断要点与鉴别
  - `figo_staging_2023`：FIGO 2023 分期标准
  - `molecular_classification`：TCGA/WHO 分子分型（POLEmut/dMMR/p53abn/NSMP）
  - `lvsi_criteria`：LVSI 评估与分层标准
  - `endometrioid adenocarcinoma` / `serous carcinoma` / `clear cell carcinoma` / `carcinosarcoma` / `undifferentiated_dedifferentiated carcinoma`：特定肿瘤类型诊断标准


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 1. 溯源与优先规则（硬性要求，必须遵守）

- **下方你的每一个诊断结论（含否定结论）都必须溯源到证据锚点**：`S?-R?-B?-G?`，若多张切片/多个 ROI 时列出多个锚点方便病理医生审阅，例如在每一句话后像参考文献一样插入[证据锚点]。
- **若某项没有对应视野或无法判断**：写 **"未评估（无对应证据）"**，**严禁猜测**。
- **若证据不足以唯一确定某个问题的回答**：必须采用谨慎的语气，例如怀疑是..  + 建议补充(IHC/分子/取材)。
- **每一项诊断结论（含否定结论）必须标注置信度**：置信度来自于你汇总多切片中的置信度：
  · 0 (证据不足，无法判断)
  · 1 (不确定，需要更多信息)
  · 2 (可能，存在鉴别诊断)
  · 3 (较确定，特征较明确)
  · 4 (确定，特征典型且无歧义)
- **每个字段的输出结构固定为**：
  `结论：…` → `依据：…` → `证据锚点：S?-R?-B?-G?` → `confidence: N (标签)`
- **二次复核裁决**：{secondary_review_honor_rule('zh')}

- **优先规则**：
各个Sub-agent均已对每张切片做出高质量的总结供你参考，他们罗列了支持证据的数量，并标注了你选择的优先度，你应该充分考量。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 2. 逐项诊断（必须全部覆盖，不可遗漏）

### 2.1 标本信息
- 标本类型：[如：全子宫及双侧输卵管卵巢切除标本——以实际为准] 
- 取材范围：[子宫体/宫颈/宫旁/附件/断端/淋巴结/腹水等，按实际列出]

### 2.2 组织学类型（开放式判断，不要从预设列表选答案）

⚠️ **分型证据整合规则**：

1. **证据来源**：原发器官切片中具有代表性的主肿瘤区域，是组织学分型的主要证据来源。明确为转移灶的卵巢、输卵管等附件病变可作为辅助印证；若附件病变的来源尚未确定或与原发部位形态不一致，应分别记录其证据并保留原发部位鉴别，不得预设其中一方必然正确或错误。

2. **证据可溯源性**：各 Sub-agent 对组织学类型的归纳仅作为证据参考。患者级结论必须能够追溯到具体切片、分析路径及形态学依据，不得产生所有单片报告均未提出且缺乏形态学支持的新组织学类型。

3. **原发切片强信号整合**：应综合所有原发器官切片的分型证据，优先考虑来自代表性肿瘤区域、跨倍率一致、可在不同视野或切片中复现且 confidence 较高的结论。支持某一方向的切片数量可以作为一致性证据，但不得作为唯一裁决依据。若某一分型方向仅由孤立局灶支持，且缺乏其他代表性区域或独立证据印证，不得仅凭该局灶发现将其升格为首选诊断；应保留为重要鉴别线索，并说明需要的辅助验证。

4. **复合诊断标签**：不得仅因不同切片分别支持不同的单一组织学类型，就自行合成为“混合性癌”“混合型”或“双表型”。只有在同一代表性肿瘤中，不同组织学成分各自具有清晰、独立的形态学证据，并符合适用的 WHO 诊断标准时，才可考虑相应的复合诊断；否则应报告首选组织学类型和主要鉴别诊断，并注明各自的证据来源。


- **首选组织学类型（WHO术语）**：
- 形态学依据：充分思考后给出首选组织学类型的各个切片的支持证据以及溯源[证据锚点]。
- **鉴别诊断（若不唯一）**：[写出最有可能的鉴别诊断] 
  - 支持证据：[要点 + 证据锚点]
  - 反对证据/不足：[要点 + 证据锚点/缺失项]
- 建议补充：[IHC/分子/额外取材]
- confidence: [N (标签)]

### 2.3 分级
- FIGO 分级 1/2/3（若适用）或 High-grade / Low-grade（若适用）[证据锚点]
- 核分级：[1/2/3 或 未评估]
- 形态学依据：[…]
- confidence: [N (标签)]


### 2.4 肿瘤部位与大小
- 部位：[宫底/体部/下段/弥漫/未评估] 
- 最大直径：[ ] cm（无数据写"未提供"）[证据锚点]


### 2.5 肌层浸润
- 肌层浸润：[无 / 有 / 未评估]
- 浸润深度：[ ] mm/cm
- 肌壁总厚度：[ ] mm/cm
- 占比：[ ]%（<50% = FIGO IA；≥50% = FIGO IB）
- 浆膜受累：[无 / 有 / 未评估]
- 证据：[…] [证据锚点]
- confidence: [N (标签)]

### 2.6 宫颈受累（⚠️ 必须区分黏膜 vs 间质）
- 宫颈黏膜/腺体累及：[无 / 有 / 未评估]
- **宫颈间质浸润**：[无 / 有 / 未评估]
- 间质浸润深度：[ ] mm
- 证据：[…][证据锚点]
- confidence: [N (标签)]

### 2.7 宫旁组织
- 宫旁：[未累及 / 累及 / 未评估 / 不适用（非根治术）] [证据锚点]

### 2.8 LVSI（⚠️ 需整合所有切片的逐切片报告）
- **总结论**：[无 / 可疑 / 有]
- 若"有"：**按切片计数**（与病理报告规范一致）：
  - 局灶 focal：单张切片内 definite 灶数 **＜5 个/切片**（且多数切片阴性）
  - 广泛 extensive：任一切片内 definite 灶数 **≥5 个/切片**
  - ⚠️ **禁止跨切片累加**：不得将多张切片的灶数相加后判断为"广泛"
- MELF 模式：[有 / 无 / 未评估]

- **切片级 LVSI 明细（必须逐张列出）**：
  - S1（[切片名]）：definite = [ ] 灶；suspicious = [ ] 灶；位置：[S1-R?-B?-G?]
  - S2（[切片名]）：definite = [ ] 灶；suspicious = [ ] 灶；位置：[S2-R?-B?-G?]
  - …（逐张列完）

- 整合依据：[简述跨切片 LVSI 分布特征，注明每张切片是否 <5 或 ≥5 灶]
- confidence: [N (标签)]

### 2.9 神经侵犯
- PNI：[无 / 有 / 未评估] [证据锚点]

### 2.10 子宫下段
- 累及：[无 / 有 / 未评估][证据锚点]

### 2.11 双侧附件（左右分别描述）
- 左卵巢：[阴性 / 转移累及 / 良性改变] — 证据锚点：[…]
- 右卵巢：[…]
- 左输卵管：[…]
- 右输卵管：[…]

### 2.12 阴道壁断端/切缘
- 断端：[阴性 / 阳性 / 未评估 / 不适用（非根治术）] [证据锚点]

### 2.13 非癌性子宫内膜
- 背景内膜：[萎缩 / 增生 / 分泌期 / 其他 / 未评估] [证据锚点]
- EIN/非典型增生：[无 / 有 / 未评估]

### 2.14 其他病变/发现
**注意一定不可遗漏单张切片中给出的其他病变或者发现的结论**
- [例如肌瘤（数量、大小）/ 腺肌症 / 慢性宫颈炎 / 囊肿等额外发现]
- 分病变写出支持证据锚点：[…]

### 2.15 FIGO 分期建议
- 分期：[IA / IB / II / IIIA / IIIB / IIIC1 / IIIC2 / IVA / IVB]
- 分期依据：[逐条列出支持该分期的关键证据及锚点]
- 若信息不足：写"无法评估（原因：…）"
- confidence: [N (标签)]

### 2.16 局限性与建议
- 哪些信息缺失导致"未评估/低置信度"
- 建议补充：[IHC / 分子检测 / 额外取材 / 临床信息]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 3. 输出格式（按此顺序）

**【最终诊断（详细审阅版本）】**
严格按照以上第 2 节格式逐项输出（2.1-2.16），每项均含：结论 → 证据 → 证据锚点 → confidence。做到完全溯源、证据追踪，供临床医生审阅。

**【最终诊断（一句话整合式）】**
按标本部位整合为一段标准临床病理报告诊断语言。要求：
1. 以"（标本部位）"开头，按标本分组；
2. 主要标本：组织学诊断 + 分级 + 肌层浸润深度 + LVSI + 宫颈累及情况 + 宫旁累及情况；
3. 每个附件标本（左/右卵巢、左/右输卵管）逐一列出诊断，包括阴性发现（如"未见特殊"）；
4. 2.14节中的所有其他病变（腺肌症、慢性炎症、囊肿等）不得遗漏；
5. 参考格式：（标本部位）[组织学诊断]，[浸润深度]，[LVSI情况]；[宫旁情况]；[宫颈情况]；[左卵巢情况]，[右卵巢情况]，[左输卵管情况]，[右输卵管情况]；[其他病变]。"""

# ================== End Patient Level 提示词 ==================

# Runtime state is keyed by Socket.IO room. Parallel Patient Level workers use
# private rooms so their per-slide state and stop flags cannot overwrite one
# another.
agent_stop_flags = {}
active_agent_states = {}
_agent_state_lock = threading.RLock()


def get_agent_state(room):
    """获取指定room的agent_state（用于socket事件处理器）"""
    with _agent_state_lock:
        return active_agent_states.get(room)

def set_agent_state(room, agent_state):
    """设置指定room的agent_state"""
    with _agent_state_lock:
        active_agent_states[room] = agent_state

def clear_agent_state(room):
    """清理指定room的agent_state"""
    with _agent_state_lock:
        active_agent_states.pop(room, None)


def _signal_api_interrupt():
    """Unblock threads waiting on the OpenRouter gate. Does not abort HTTP."""
    try:
        from .gpt_client import request_api_interrupt
    except ImportError:
        try:
            from gpt_client import request_api_interrupt
        except ImportError:
            return
    try:
        request_api_interrupt()
    except Exception:
        logger.debug("API interrupt signal was unavailable", exc_info=True)


def _shutdown_thread_pool(executor, *, wait):
    """Shut down a pool without waiting for in-flight work when wait=False."""
    if executor is None:
        return
    if wait:
        executor.shutdown(wait=True)
        return
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)


# ==================== Agent Loop 功能：空间搜索（Spatial Search） ====================

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

# 每个 WSI worker 使用独立缓存。不同切片会复用 R1/B1/G1 等 ROI ID，
# 因此进程级共享缓存会造成跨切片图像串用。
_roi_cache_local = threading.local()


def _get_parent_roi_cache():
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
    _parent_roi_cache = _get_parent_roi_cache()
    
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

def generate_html_visual_report(agent_state, file_id, output_dir, final_report_text, total_red, total_blue, total_green, report_title=None, lang=None):
    """
    生成HTML可视化病理报告 - 完全按照 3.Report_Generator.py 格式
    
    Args:
        agent_state: Agent状态字典
        file_id: 文件ID
        output_dir: 输出目录
        final_report_text: Patient Level 单片结构化证据摘要
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
        'label_organ':          'Slide site' if is_en else '切片部位',
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
        sample_name = Path(filename).stem if filename else file_id
        organ_display = (
            agent_state.get('part_name')
            or agent_state.get('folder_name')
            or sample_name
        )
        
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
            # 如果是列表，将其转换为字符串。
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
        
        # ⭐ 在多倍镜分析部分末尾插入 Phase 2 (蓝框) 和 Phase 3 (绿框) 修改记录
        
        
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


def generate_patient_level_html_report(patient_state: dict, output_dir: Path, file_infos: list, wsi_results: dict) -> str:
    """
    生成患者级别 HTML 可视化报告 - 合并各切片的完整 HTML 报告 + 底部综合诊断
    
    ⭐ 结构（rbind 上下合并）：
    ┌──────────────────────────────────────┐
    │  Patient Level 综合病理报告 (Header)   │
    ├──────────────────────────────────────┤
    │  📊 总体统计                          │
    ├──────────────────────────────────────┤
    │  切片1: Slide 1: 宫颈1 - xxx.svs      │
    │  [该切片完整的 HTML 报告内容]          │
    ├──────────────────────────────────────┤
    │  切片2: Slide 2: 内膜1 - yyy.svs      │
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
        
        # 统计所有 WSI 的红/蓝/绿框总数
        total_red = 0
        total_blue = 0
        total_green = 0
        
        for file_info in file_infos:
            fid = file_info.get('file_id')
            if fid and fid in wsi_results:
                result = wsi_results[fid]
                if result:
                    stats = result.get('statistics', {})
                    total_red += stats.get('red_frames', 0)
                    total_blue += stats.get('blue_frames', 0)
                    total_green += stats.get('green_frames', 0)
        
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
                <p>{'WSI schedule' if _en else 'WSI 调度'}: {patient_state.get('wsi_schedule', 'parallel')}</p>
                <p>{'API max concurrency' if _en else 'API 并发上限'}: {patient_state.get('api_max_concurrency', '')}</p>
                <p>{'Generated' if _en else '生成时间'}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S' if _en else '%Y年%m月%d日 %H:%M:%S')}</p>
            </div>
        </div>
        
        <div class="content">
            <!-- 总体统计 -->
            <div class="section">
                <h2 class="section-title">📊 {'Overall Statistics' if _en else '总体统计'}</h2>
                <div class="stats-grid">
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
            wsi_red = wsi_stats.get('red_frames', 0)
            wsi_blue = wsi_stats.get('blue_frames', 0)
            wsi_green = wsi_stats.get('green_frames', 0)
            
            # ⭐ 切片分隔标题
            html_content += f"""
            <!-- ==================== 切片 {wsi_idx}: {part_name} ==================== -->
            <div class="wsi-section">
                <div class="wsi-section-header" onclick="toggleWsiSection(this)">
                    📋 {'Slide' if _en else '切片'} {wsi_idx}/{completed_wsi}: {part_name} - {filename} ({wsi_red}{'R' if _en else '红'}/{wsi_blue}{'B' if _en else '蓝'}/{wsi_green}{'G' if _en else '绿'})
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
            wsi_body_content = extract_wsi_report_content(wsi_html_path) if fid and wsi_html_path.exists() else None
            
            if wsi_body_content:
                # ⭐ 成功读取：嵌入完整的报告内容（包含缩略图、路径、分析、诊断报告）
                logger.info(f"[Patient Level] ✅ 成功嵌入 {part_name} 的完整 HTML 报告 ({len(wsi_body_content)} chars)")
                html_content += f"""
                    <div style="padding: 20px;">
                        {wsi_body_content}
                    </div>
"""
            else:
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

# 注意这是WSI-level的单张切片的汇总，对每一张进行汇总后至1567函数进行Patient-level的汇总
def generate_final_pathology_report(agent_state, file_id, output_dir, socketio=None, room=None, report_title=None, is_patient_level=True):
    """
    聚合所有层级分析结果，生成最终病理报告
    
    Args:
        agent_state: Agent状态字典
        file_id: 文件ID
        output_dir: 输出目录
        socketio: SocketIO实例（可选，用于实时推送）
        room: WebSocket房间ID（可选）
        report_title: 报告标题（可选，默认"最终病理诊断报告"，Patient Level 下使用"单张切片病理诊断报告"）
        is_patient_level: 必须为 True；本评测仅生成 Patient Level 单片证据摘要
    
    Returns:
        dict: 最终报告
    """
    if not is_patient_level:
        raise ValueError("PATIENT_LEVEL_REPORT_REQUIRED")

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
                    if isinstance(evidence, dict):
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
        
        # ⭐⭐⭐ 注入全局缩略图分析（Orchestrator 的全局架构观察）
        # 缩略图是唯一能看到全切片架构的视角，对 Q4（浸润深度）等宏观判断至关重要
        global_analysis_text = agent_state.get('global_analysis', '')
        if isinstance(global_analysis_text, dict):
            global_analysis_text = global_analysis_text.get('reasoning', '') or global_analysis_text.get('analysis', '')
        elif isinstance(global_analysis_text, list):
            global_analysis_text = '\n'.join([str(item) for item in global_analysis_text])
        if global_analysis_text:
            _ga_label = "Thumbnail Global Analysis (Orchestrator — full slide overview)" if _is_en else "缩略图全局分析（Orchestrator — 全切片架构观察）"
            all_summaries = f"🔭 **{_ga_label}**:\n{global_analysis_text}\n\n{'─' * 60}\n\n{all_summaries}"
        
        # ⭐⭐⭐ MCP Protocol: Plan阶段 - LLM自主决定是否需要工具
        # 使用标准MCP接口实现 Plan→Action→Reason→Meta 工作流
        
        # 第二阶段：Reason + Meta - 生成最终报告（WHO分类已嵌入prompt，无需额外工具调用）
        # Patient Level 单片执行结果统一使用结构化证据摘要格式
        part_name = agent_state.get('part_name', '')
        folder_name = agent_state.get('folder_name', '')
        
        if is_patient_level:
            # ⭐⭐⭐ Patient Level 专用：版本 A 结构化证据摘要（供最终 Patient Level 聚合直接使用）
            # 诊断维度和溯源规则由 _get_patient_level_diagnostic_objective 提供，此处不重复
            _slide_index = agent_state.get('wsi_index', 1)
            _s = f"S{_slide_index}"
            _diag_obj = _get_patient_level_diagnostic_objective(wsi_index=_slide_index, slide_index=_slide_index, lang=lang)
            if lang == 'en':
                report_prompt = f"""You are **PathCopilot Orchestrator**, executing the **Meta phase: Structured Evidence Summary**.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Current phase: Meta (single WSI → structured evidence summary for Patient Level aggregation)
🏥 Patient: {folder_name} | Slide: **{part_name}** (Slide ID: **{_s}**)
📊 Analysis stats: {total_red} red / {total_blue} blue / {total_green} green boxes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{_diag_obj}

## Complete Path Tree Evidence (with R-B-G Traceability)

{all_summaries}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Your Task

**Slide site: {part_name}** | **Slide ID: {_s}**

Based on the **Diagnostic Objective** above (13 dimensions), summarize this slide's diagnostic contribution into a **structured evidence summary** for the patient-level final report.
⚠️ **"No significant findings / not involved" is also critical diagnostic evidence!**
⚠️ **Every path reference MUST include this slide's ID prefix `{_s}-` (e.g. {_s}-R1-B2-G3).**
⚠️ Only describe what you **actually observed** in this slide. Do not fabricate information.
⚠️ **Secondary-review adjudication**: {secondary_review_honor_rule('en')}
⚠️ **Confidence-weighted integration**: When multiple ROIs provide evidence for the same diagnostic dimension, findings with confidence ≥ 3 should receive greater weight when supported by specific morphology, traceable paths, and reproducibility. Findings with confidence ≤ 2 should be retained as differential clues or uncertainty; specific morphological counter-evidence must not be disregarded solely because of a lower confidence score. When findings directly conflict, integrate morphological specificity, tissue representativeness, cross-scale consistency, and completeness of the evidence chain.
⚠️ **Slide source and evidence-role statement**: The documented specimen site for this slide is **{part_name}**. Use this specimen information together with the tissue actually visible on the slide to state which diagnostic dimensions this slide can directly inform.

  · For histological typing, consider whether tumor is directly present in this slide, the specimen site, tissue representativeness, morphological evidence quality, and the spatial relationship to other slides. Do not assign evidence priority automatically from site-name keywords or a preset hierarchy.
  · Concordant morphology across sites may provide mutually supporting evidence. When findings are discordant, report the difference and its morphological basis, and allow the Patient Level synthesis to consider reasonable explanations including synchronous lesions, metastasis, direct extension, sampling variation, and benign or reactive mimics.
  · For site-specific dimensions such as parametrial involvement, adnexal status, or margin status, answer only from evidence actually visible in the corresponding slide. Report "not assessed" or "indeterminate" when evidence is insufficient.
  · Report LVSI, PNI, and other focal findings as observed, without increasing or decreasing their weight solely because of the specimen site.

## 📝 Output Format (strictly follow)

**[Reason]**
1. Key evidence integration: (summarize important findings from each path, citing {_s}-R-B-G IDs, including detailed morphological descriptions, conclusions, and confidence)
2. Slide diagnostic task: (based on the evidence above and the Diagnostic Objective, determine which questions this slide at site {part_name} can answer)

**[Structured Evidence Summary]**
For the **13 image-assessable diagnostic dimensions defined above**, report only the dimensions for which this slide has supporting evidence — skip inapplicable dimensions. For each reported dimension, provide: dimension name → detailed conclusion (include key morphological features, not just a label) + path evidence ({_s}-R?-B?-G?) + confidence: 0(insufficient evidence, unable to assess) / 1(uncertain, more information needed) / 2(possible, differential diagnosis exists) / 3(probable, features fairly characteristic) / 4(definitive, features typical and unambiguous).
⚠️ Each dimension's conclusion must be **substantive and detailed**, for use in the final aggregated report.
⚠️ **Evidence marker rule**: Prefix a finding with `🔴` only when it has confidence ≥ 3, includes specific directly observed morphological evidence, and provides a traceable path anchor. The marker identifies a higher-certainty local finding for Patient Level review; it does not by itself override conflicting evidence or determine the final patient-level conclusion.

Example: `🔴 Histological type: [type], {_s}-R?-B?-G?, confidence: 3/4`

⚠️ **Structured comparison of multiple histological typing directions**:

If the slide contains evidence supporting two or more histological typing directions, present the morphological evidence for each direction separately. Do not omit conflicting evidence or merge distinct diagnoses into a new unsupported label. Final patient-level typing is performed by the Patient Level aggregation layer using evidence from all slides.

  📌 Typing evidence comparison (for Patient Level aggregation):

· Direction A: [type]
  - Supporting ROI count: N (sampling record only; not independent votes) | Paths: {_s}-R?-B?-G?, ...
    - Highest confidence: X
  - Directly observed key morphological evidence:
    ①...  ②...
  - Opposing evidence or principal limitations:
    ①...  ②...

· Direction B: [type] (if applicable)
  - Supporting ROI count: M (sampling record only; not independent votes) | Paths: {_s}-R?-B?-G?, ...
    - Highest confidence: Y
  - Directly observed key morphological evidence:
    ①...  ②...
  - Opposing evidence or principal limitations:
      ①...  ②...

· Slide-level evidence weighting:
  - If one direction is more strongly supported, explain this using tumor representativeness, cross-scale consistency, morphological specificity, reproducibility, and confidence; do not decide solely from ROI counts or assertive wording.
  - Local findings that satisfy the evidence-marker rule may retain `🔴`, but the marker does not constitute the final patient-level adjudication.
  - If the directions remain comparable or the conflict is unresolved, retain all relevant directions and state the uncertainty.

  · Source declaration: [Primary·Main typing] or [Adnexal·Supplementary typing] (based on {part_name})

If all ROIs support only one typing direction, use the standard output format. Distinct histological diagnoses must be listed separately rather than merged into one ambiguous direction.
"""
            else:
                report_prompt = f"""你是 **PathCopilot Orchestrator（编排器）**，正在执行 **汇总阶段：结构化证据摘要**。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 当前阶段：Meta（单张 WSI → 结构化证据摘要，供 Patient Level 聚合使用）
🏥 患者: {folder_name} | 切片: **{part_name}**（切片编号: **{_s}**）
📊 分析统计: {total_red} 红框, {total_blue} 蓝框, {total_green} 绿框
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{_diag_obj}

## 完整路径树证据（带 R-B-G 路径追溯）

{all_summaries}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 你的任务

**切片部位：{part_name}** | **切片编号: {_s}**

围绕**上方「诊断目标」**中定义的 13 个图像可评估诊断维度，将本切片的诊断贡献整理为 **结构化证据摘要**，供 Patient Level 最终综合报告使用。

⚠️ **"未见特殊/未累及"也是重要的诊断证据！**
⚠️ **所有路径引用必须加上本切片编号前缀 `{_s}-`（如 {_s}-R1-B2-G3）！**
⚠️ 只描述你在本切片中**实际观察到的**内容，**禁止编造**任何未提供的信息。
⚠️ **二次复核裁决**：{secondary_review_honor_rule('zh')}
⚠️ **置信度加权整合**：当本切片内多个 ROI 对同一诊断维度提供证据时，confidence ≥ 3 且具有明确形态学依据、路径可溯源性和可重复性的发现应赋予较高权重。confidence ≤ 2 的发现应作为鉴别线索或不确定性记录；若其包含具体的形态学反面证据，不得仅因 confidence 较低而忽略。证据直接冲突时，应结合形态学特异性、组织代表性、跨倍率一致性和证据链完整性进行综合判断。
⚠️ **切片来源与证据角色声明**：本切片的记录取材部位为 **{part_name}**。请结合该取材信息和当前切片实际可见的组织学证据，说明本切片能够为哪些诊断维度提供直接证据。

  · 对组织学类型的判断，应综合考虑肿瘤是否直接存在于本切片、取材部位、组织代表性、形态学证据质量及与其他切片的空间关系，不得仅根据部位名称或预设的证据层级自动决定权重。
  · 若本切片与其他部位切片的组织学表现一致，可作为相互支持的证据；若不一致，应如实记录差异及其形态学依据，并在 Patient Level 汇总时评估同步病变、转移、直接蔓延、取材差异及良性或反应性模拟病变等合理解释。
  · 对本切片取材部位相关的专属维度（如宫旁累及、附件状态或切缘状态），仅根据当前切片实际可见的证据作答；证据不足时报告“未评估”或“无法确定”。
  · LVSI、PNI 及其他局灶性发现按本切片实际所见报告，不因取材部位预先升高或降低其证据权重。

⚠️ **「组织学类型」维度的多方向证据整理**：

若本切片内存在两个或以上组织学分型方向的证据，应分别呈现各方向的形态学依据，不得隐去冲突证据，也不得将不同诊断方向合并成一个未经支持的新标签。最终患者级分型由 Patient Level 汇总层结合全部切片完成。

📌 分型证据比较（供 Patient Level 汇总参考）：

  · 方向 A：[类型名称]
  - 支持 ROI 数：N 个（仅作取样记录，不作为独立投票）| 路径：{_s}-R?-B?-G?, ...
    - 最高 confidence：X
  - 直接观察到的核心形态学依据：
    ①...  ②...
  - 反对证据或主要局限：
    ①...  ②...

  · 方向 B：[类型名称]（如有）
  - 支持 ROI 数：M 个（仅作取样记录，不作为独立投票）| 路径：{_s}-R?-B?-G?, ...
    - 最高 confidence：Y
  - 直接观察到的核心形态学依据：
    ①...  ②...
  - 反对证据或主要局限：
      ①...  ②...

· 本切片证据权重说明：
  - 若一个方向获得更充分支持，应根据肿瘤区域的代表性、跨倍率一致性、形态学特异性、可重复性及 confidence 说明原因；不得仅依据 ROI 数量或措辞坚定程度决定。
  - 符合上方证据标记规则的局部发现可保留 `🔴`，但该标记不构成患者级最终裁决。
  - 若不同方向的证据接近或冲突尚未解决，应全部保留并明确不确定性。

  · 来源声明：[原发·主分型] 或 [附件·辅助分型]（根据 {part_name} 决定）

若所有 ROI 仅支持一个分型方向，可按常规结构输出。不同组织学诊断必须分别列出，不得合并为一个含混的诊断方向。


## 📝 输出格式（严格按此格式输出）

**【Reason】**
1. 关键证据整合：（逐条整合各路径的重要发现，标注 {_s}-R-B-G 路径 ID，包含详细的形态学描述，结论，和置信度；
你必须充分总结该切片所有的路径信息，尤其是各个sub-agents给出的高置信线索都要重视并且呈现；经过充分的线索证据汇总思考后为最终的patient-level提供清晰的证据参考。）

2. 本切片诊断任务：（基于上述证据和上方「诊断目标」，确定部位 {part_name} 的本切片能够回答哪些问题）

**【结构化证据摘要】**
针对**上方「诊断目标」中的 13 个图像可评估诊断维度**，仅报告本切片有证据支撑的维度，跳过不适用的维度。每个维度给出：维度名称 → 详细结论（包含关键形态学特征，而非仅一个诊断名称）+ 路径依据（{_s}-R?-B?-G?）+ 置信度：0(证据不足，无法判断) / 1(不确定，需要更多信息) / 2(可能，存在鉴别诊断) / 3(较确定，特征较明确) / 4(确定，特征典型且无歧义)。
⚠️ 每个维度的结论必须**内容详实**，供最终汇总报告使用。
⚠️ **证据标记规则**：仅当某项发现的 confidence ≥ 3、包含当前视野直接观察到的具体形态学依据，并具有可追溯的分析路径锚点时，才在对应维度结论前加 `🔴`。该标记仅表示需要 Patient Level 汇总层重点审阅的较高确信度局部证据；其本身不能覆盖冲突证据，也不能直接决定患者级最终结论。

示例：`🔴 组织学类型：[类型]，{_s}-R?-B?-G?，confidence: 3/4`
"""
        
        # 调用AI生成报告
        logger.info(f"\n{'='*80}")
        logger.info("📝 正在调用AI生成最终病理报告...")
        logger.info(f"{'='*80}")
        from agent.gpt_client import call_gpt_vision
        
        # 纯文本，不需要图像
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
        part_name = agent_state.get('part_name') or agent_state.get('folder_name') or filename
        
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
        report_lines.append(f"| **切片部位** | {part_name} |")
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
            
            # 红框 MCP Loop 信息
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
                
                # 蓝框 MCP Loop 信息
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
                    
                    # 绿框 MCP Loop 信息
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
                'mode': agent_state.get('mode'),
                'patient_id': agent_state.get('patient_id'),
                'organ': part_name,
                'part_name': part_name,
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
                            'key_features': green_node.get('analysis', {}).get('key_features', []),
                            'secondary_review': green_node.get('analysis', {}).get('secondary_review'),
                            'secondary_review_verdict': green_node.get('analysis', {}).get('secondary_review_verdict'),
                            'secondary_review_claim_status': green_node.get('analysis', {}).get('secondary_review_claim_status'),
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


def run_agent(socketio, room, file_id: str, max_steps: int = 12, strategy: str = "default",
              patient_level_context: dict = None, lang: str = 'zh') -> dict:
    """
    运行WSI Agent循环
    步骤0：缩略图级别ROI识别
    步骤1-N：多尺度ROI分析循环
    
    Args:
        socketio: SocketIO实例
        room: 事件通道 ID
        file_id: WSI文件ID
        max_steps: 最大步数
        strategy: 分析策略
        patient_level_context: 必需的 Patient Level 上下文，包含:
            - part_name: 切片部位名称
            - folder_name: 患者文件夹名
            - wsi_index: 当前WSI索引
            - total_wsi: 总WSI数量
        lang: 语言设置 ('zh' 中文, 'en' 英文)
    
    Returns:
        dict: 分析结果（仅Patient Level模式返回），包含pathology_findings等
    """
    if not patient_level_context:
        raise ValueError(
            "PATIENT_LEVEL_CONTEXT_REQUIRED: this evaluation only supports "
            "patient-level runs; use run_patient_level_agent()."
        )
    slide = None
    try:
        logger.info(f"启动病理Agent (Patient Level Auto): file_id={file_id}, room={room}, max_steps={max_steps}")
        
        # ⭐ 启动 LLM 日志会话
        from .llm_logger import start_log_session, end_log_session
        from .gpt_client import set_current_session, clear_current_session
        
        log_metadata = {
            "mode": "ai",
            "strategy": strategy,
            "max_steps": max_steps,
            "room": room
        }
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
        
        # 重置停止标志
        agent_stop_flags[room] = False
        
        # 获取WSI文件路径（需要先获取文件名用于命名输出目录）
        file_path = get_wsi_file_path(file_id)
        if not file_path:
            emit_error(socketio, room, f"WSI文件不存在: {file_id}")
            return
        
        # 提取原始文件名（从 uploaded_files 获取用户上传时的原始名称）
        try:
            from file_registry import get_file
            original_filename = get_file(file_id).get('filename', '')
        except Exception:
            original_filename = ''
        # 如果拿不到原始文件名，用磁盘文件名兜底
        filename = original_filename if original_filename else os.path.basename(file_path)
        
        # ⭐⭐⭐ 创建输出目录（使用文件名+时间戳命名，更直观可读）⭐⭐⭐
        patient_id = patient_level_context.get('patient_id') if patient_level_context else None
        paths = get_output_paths(file_id, patient_id=patient_id, filename=filename)
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
            'patient_id': patient_id,
            'file_id': file_id,  # ⭐ 文件ID，用于输出路径等
            'filename': filename,
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
            'lang': lang,   # ⭐ 语言设置: 'zh' 或 'en'
            # ⭐ ROI计数器（用于简化命名: R1, R2, B1, B2, G1, G2）
            'r_roi_counter': 0,
            'b_roi_counter': 0,
            'g_roi_counter': 0,
            # ⭐ MCP Loop高级功能
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
            # 单片二次复核只使用本张 WSI 的 13 个图像可评估诊断维度；
            # 患者级 FIGO 汇总不属于单片视觉复核范围。
            agent_state['diagnostic_question'] = _get_patient_level_diagnostic_objective(
                wsi_index=agent_state['wsi_index'],
                slide_index=agent_state['wsi_index'],
                lang=lang,
            )
            logger.info(f"[Patient Level] 启用Patient Level上下文: {agent_state['part_name']}")
        
        # 保存运行状态，供并行事件投影与停止标志查询。
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
            slide = None
            return
        
        # ⭐ 为选中的红框分配简单ID (R1, R2, R3...)
        assign_simple_roi_ids(candidate_rois, 'R', agent_state)
        logger.info(f"🔴 红框ID分配: {[roi['id'] for roi in candidate_rois]}")
        
        # ⭐ Patient Level: 发送简化ID后的红框列表 + 全局分析理由
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
        
        # ⭐ Agent Loop: 构建空间索引（用于空间搜索功能）
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
                
                # 注意：红框是并行分析的，这里不发送进度
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
                
                # 发射红框完成事件。
                if roi_analysis_result and 'finding' in roi_analysis_result :
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
        
        # 收集所有并行红框结果。
        all_results = []
        
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        wait_for_red_workers = True
        try:
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
                    wait_for_red_workers = False
                    _signal_api_interrupt()
                    _shutdown_thread_pool(executor, wait=False)
                    emit_done(socketio, room, file_id, agent_state, status='stopped')
                    slide.close()
                    slide = None
                    return
                
                completed_count += 1
                result_data = future.result()
                all_results.append(result_data)
                
                # 自动模式：立即处理结果
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
        except KeyboardInterrupt:
            wait_for_red_workers = False
            _signal_api_interrupt()
            _shutdown_thread_pool(executor, wait=False)
            raise
        finally:
            if wait_for_red_workers:
                _shutdown_thread_pool(executor, wait=True)
        
        logger.info(f"✅ 所有 {len(candidate_rois)} 个红框并行分析完成！")

        # 每张 WSI 的首次红→蓝→绿路径全部完成后，由单片 Orchestrator
        # 自主决定是否原生调用 secondary_review。该步骤必须在关闭
        # OpenSlide 和生成单片证据摘要之前完成；患者级汇总不在此看片。
        try:
            orchestrate_secondary_reviews(
                agent_state=agent_state,
                slide=slide,
                steps_dir=steps_dir,
                diagnostic_question=agent_state.get('diagnostic_question', ''),
                compose_images=_compose_detail_images,
                socketio=socketio,
                room=room,
            )
        except Exception as review_error:
            # 二次复核是可选控制步骤，其自身异常不得使已完成的 WSI
            # 首轮分析失败。尚未形成原生工具调用时不制造 NOT_RUN 裁决。
            logger.warning(f"[secondary_review] 单片复核编排异常，保留首轮证据: {review_error}")
            agent_state.setdefault('secondary_review_plan', [])
            agent_state.setdefault('secondary_review_results', [])
        
        slide.close()
        slide = None
        
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
                wsi_summary_error = None
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
                    wsi_summary_error = str(e)
                    logger.warning(f"[Patient Level] WSI 报告生成失败: {e}")
                    wsi_summary = f"【切片部位】{patient_level_context.get('part_name', 'Slide')}\n【主要诊断】分析过程中出现错误\n【本切片诊断小结】需重新分析"
                
                if pl_report and pl_report.get('error'):
                    wsi_summary_error = pl_report['error']
                elif not (wsi_summary or '').strip() or '{"selected_grids"' in wsi_summary:
                    wsi_summary_error = 'empty model response'
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
                    'wsi_summary_error': wsi_summary_error,
                    'secondary_review_plan': agent_state.get('secondary_review_plan', []),
                    'secondary_review_results': agent_state.get('secondary_review_results', []),
                    'statistics': {
                        'red_frames': red_count,
                        'blue_frames': blue_count,
                        'green_frames': green_count
                    },
                    'output_paths': agent_state.get('output_paths', {}),
                }
            
    except Exception as e:
        logger.error(f"Agent执行失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        emit_error(socketio, room, f"Agent执行失败: {str(e)}")
        raise
    finally:
        if slide is not None:
            try:
                slide.close()
            except Exception as close_err:
                logger.warning(f"关闭 OpenSlide 失败: {close_err}")
            slide = None
        # 清理停止标志
        if room in agent_stop_flags:
            del agent_stop_flags[room]
            clear_agent_state(room)
        
        # ⭐ 结束 LLM 日志会话
        try:
            summary = {
                "total_rois": len(agent_state.get('analyzed_rois', [])) if 'agent_state' in dir() else 0,
                "mode": "ai",
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


def _load_patient_ai_caches(patient_dir, patient_id, requested):
    """Load verified slide evidence from this patient's AI-only output directory."""
    auto_dir = patient_dir / 'auto'
    caches = {}
    # Keep paths local even when a cache directory or artifact is a symlink.
    if auto_dir.resolve() != Path('outputs').resolve() / patient_dir.name / 'auto':
        return caches
    for tree_path in sorted(auto_dir.glob('wsi_*/analysis_tree.json'), reverse=True):
        try:
            if tree_path.resolve() != auto_dir.resolve() / tree_path.parent.name / tree_path.name:
                continue
            tree = json.loads(tree_path.read_text(encoding='utf-8'))
            meta = tree['metadata']
            fid = meta['file_id']
            info = requested.get(fid)
            if not info or meta.get('filename') != info['filename'] or meta.get('part_name') != info['part']:
                continue
            if meta.get('patient_id', patient_id) != patient_id:
                continue
            mode = meta.get('mode')
            if mode is None:
                # Older trees have no mode field. The original thumbnail must point
                # back into this exact patient's auto run, not to a copied collab run.
                origin = '/' + (tree_path.parent / 'steps').as_posix() + '/'
                if not str(meta.get('thumbnail_url', '')).startswith(origin):
                    continue
            elif mode not in ('ai', 'auto'):
                continue
            summary = meta.get('wsi_summary')
            nodes = tree.get('tree_nodes')
            if not isinstance(summary, str) or not summary.strip() or not isinstance(nodes, list):
                continue
            if fid in caches:
                continue
            stats = meta.get('statistics', {})
            caches[fid] = {
                'pathology_findings': [], 'tree_nodes': nodes, 'all_overlays': [],
                'wsi_summary': summary, 'from_cache': True, 'cache_metadata': meta,
                'statistics': {f'{level}_frames': stats.get(f'total_{level}', 0)
                               for level in ('red', 'blue', 'green')},
                'output_paths': {'output_dir': tree_path.parent, 'steps_dir': tree_path.parent / 'steps'},
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Cannot reuse slide cache %s: %s", tree_path, exc)
    return caches


def _matching_ai_patient_tree(auto_dir, patient_id, folder_name, infos, caches):
    """Return the existing aggregate only if it represents exactly these cached slides."""
    tree_path = auto_dir / 'patient_analysis_tree.json'
    report_path = auto_dir / 'ai_final_report.txt'
    try:
        if any(p.resolve() != auto_dir.resolve() / p.name for p in (tree_path, report_path)):
            return None
        tree = json.loads(tree_path.read_text(encoding='utf-8'))
        meta = tree['metadata']
        nodes = tree['tree_nodes']
        if (meta.get('patient_id') != patient_id or meta.get('folder_name') != folder_name
                or meta.get('mode', 'ai') != 'ai' or len(nodes) != len(infos)):
            return None
        for node, info in zip(nodes, infos):
            cache = caches[info['file_id']]
            if (node.get('file_id') != info['file_id'] or node.get('part_name') != info['part']
                    or node.get('filename') != info['filename'] or node.get('metadata') != cache['cache_metadata']
                    or node.get('children') != cache['tree_nodes']):
                return None
        header, _, body = report_path.read_text(encoding='utf-8').partition('\n\n')
        if ('# 模式: Patient Level Auto' not in header.splitlines()
                or f'# 患者: {folder_name}' not in header.splitlines()
                or not body.strip() or body.strip() != str(meta.get('ai_final_report') or '').strip()):
            return None
        return tree
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _patient_wsi_report_text(wsi_result: dict) -> str:
    """Read the per-slide text report used by the Patient Level UI."""
    if not wsi_result:
        return ''
    if wsi_result.get('from_cache'):
        return wsi_result.get('wsi_summary', '')
    output_dir = (wsi_result.get('output_paths') or {}).get('output_dir')
    if output_dir:
        report_path = Path(str(output_dir)) / 'pathology_report.txt'
        if report_path.exists():
            try:
                return report_path.read_text(encoding='utf-8')
            except Exception:
                pass
    return wsi_result.get('wsi_summary', '') or ''


def _run_patient_automatic_slide_stage(
    socketio,
    room: str,
    patient_id: str,
    patient_state: dict,
    all_wsi_infos: list,
    total_wsi: int,
    folder_name: str,
    skip_file_ids: set,
    lang: str,
    wsi_schedule: str = 'parallel',
) -> bool:
    """Run Patient Level WSI analyses with a selectable scheduler.

    ``parallel`` (default) submits every pending slide together, matching the
    manuscript evaluation protocol. ``sequential`` runs one slide to completion
    before starting the next. Both paths call the same
    ``run_single_wsi_for_patient()`` kernel; intra-slide red/blue/green work is
    unchanged. Each WSI still receives a private worker room. Patient-level
    synthesis is performed by the caller after this stage has settled, using
    manifest/index order rather than completion order.
    """
    schedule = str(wsi_schedule or 'parallel').strip().lower()
    if schedule not in ('sequential', 'parallel'):
        raise ValueError(
            "wsi_schedule must be 'sequential' or 'parallel', "
            f"got {wsi_schedule!r}"
        )
    is_parallel = schedule == 'parallel'

    patient_dir_name = (
        patient_id if patient_id.startswith('patient_')
        else f'patient_{patient_id}'
    )
    skip_file_ids = set(skip_file_ids or ())
    resolved_by_index = {}
    cached_infos = []
    pending_infos = []

    for idx in range(total_wsi):
        info = dict(all_wsi_infos[idx] if idx < len(all_wsi_infos) else {})
        info['index'] = idx
        info.setdefault('filename', f'WSI_{idx + 1}')
        info.setdefault('part', f'Slide {idx + 1}')
        resolved_by_index[idx] = info
        if info.get('file_id') in skip_file_ids:
            cached_infos.append(info)
        else:
            pending_infos.append(info)

    def flag_slide_workers_stop():
        agent_stop_flags[room] = True
        for worker_idx in range(total_wsi):
            agent_stop_flags[
                f'{room}:patient:{patient_id}:wsi:{worker_idx}'
            ] = True

    # Cached slides are complete already, but the UI still needs the same
    # lifecycle event used for newly analyzed slides.
    for info in cached_infos:
        file_id = info['file_id']
        cached_result = patient_state.get('wsi_results', {}).get(file_id) or {}
        output_paths = cached_result.get('output_paths') or {}
        socketio.emit('agent.wsi_completed', {
            'index': info['index'],
            'file_id': file_id,
            'filename': info['filename'],
            'part': info['part'],
            'mode': 'ai',
            'parallel': is_parallel,
            'wsi_schedule': schedule,
            'from_cache': True,
            'html_report_url': output_paths.get('report_html', ''),
            'report_txt': _patient_wsi_report_text(cached_result),
            'result': {
                'findings_count': len(
                    cached_result.get('pathology_findings', [])
                ),
                'statistics': cached_result.get('statistics', {}),
            },
        }, to=room)

    def analyze_one(raw_info):
        info = dict(raw_info)
        idx = int(info['index'])
        file_id = info.get('file_id')
        if not file_id:
            raise ValueError(f"Missing file_id for slide: {info['part']}")

        worker_room = f'{room}:patient:{patient_id}:wsi:{idx}'
        socketio.emit('agent.wsi_started', {
            'index': idx,
            'file_id': file_id,
            'filename': info['filename'],
            'part': info['part'],
            'patient_id': patient_id,
            'mode': 'ai',
            'parallel': is_parallel,
            'wsi_schedule': schedule,
        }, to=room)
        socketio.sleep(0)

        # ROI caches and LLM log sessions are worker-local, so identical ROI
        # ids from different slides cannot cross-contaminate one another.
        clear_roi_cache()
        try:
            result = run_single_wsi_for_patient(
                socketio=socketio,
                room=worker_room,
                file_id=file_id,
                part_name=info['part'],
                folder_name=folder_name,
                wsi_index=idx + 1,
                total_wsi=total_wsi,
                patient_id=patient_id,
                lang=lang,
            )
            return info, result
        finally:
            clear_agent_state(worker_room)
            agent_stop_flags.pop(worker_room, None)

    completed = len(cached_infos)
    failures = []

    def record_success(info, wsi_result):
        idx = int(info['index'])
        file_id = info['file_id']
        resolved_by_index[idx] = info
        patient_state.setdefault('wsi_results', {})[file_id] = wsi_result
        output_paths = wsi_result.get('output_paths') or {}
        report_url = output_paths.get('report_html') or (
            f'/outputs/{patient_dir_name}/auto/'
            f'wsi_{file_id}/pathology_report.html'
        )
        socketio.emit('agent.wsi_completed', {
            'index': idx,
            'file_id': file_id,
            'filename': info['filename'],
            'part': info['part'],
            'mode': 'ai',
            'parallel': is_parallel,
            'wsi_schedule': schedule,
            'html_report_url': report_url,
            'report_txt': _patient_wsi_report_text(wsi_result),
            'result': {
                'findings_count': len(
                    wsi_result.get('pathology_findings', [])
                ),
                'tree_nodes': wsi_result.get('tree_nodes', []),
            },
        }, to=room)

    def record_failure(info, exc):
        failures.append(
            info.get('file_id') or f'missing-{info["index"]}'
        )
        logger.exception(
            '[Patient Level] WSI analysis failed: %s',
            info.get('filename', info.get('index')),
        )
        socketio.emit('agent.wsi_completed', {
            'index': info['index'],
            'file_id': info.get('file_id') or '',
            'filename': info['filename'],
            'part': info['part'],
            'mode': 'ai',
            'parallel': is_parallel,
            'wsi_schedule': schedule,
            'error': str(exc),
        }, to=room)

    def mark_slide_progress():
        nonlocal completed
        completed += 1
        patient_state['completed_wsi'] = completed
        socketio.emit('agent.patient_progress', {
            'percentage': 10 + completed / max(total_wsi, 1) * 80,
            'completed_wsi': completed,
            'total_wsi': total_wsi,
            'message': _msg(
                'slide_done', lang, done=completed, total=total_wsi
            ),
        }, to=room)
        socketio.sleep(0)
        if agent_stop_flags.get(room, False):
            flag_slide_workers_stop()

    def settle_analyzed_slide(info, wsi_result):
        if not wsi_result:
            raise RuntimeError('WSI analysis returned no result')
        record_success(info, wsi_result)

    if pending_infos:
        try:
            if schedule == 'sequential':
                # One WSI finishes completely before the next starts. Intra-slide
                # red/blue/green concurrency is unchanged.
                for pending_info in pending_infos:
                    if agent_stop_flags.get(room, False):
                        flag_slide_workers_stop()
                        break
                    info = dict(pending_info)
                    try:
                        info, wsi_result = analyze_one(info)
                        settle_analyzed_slide(info, wsi_result)
                    except KeyboardInterrupt:
                        flag_slide_workers_stop()
                        raise
                    except Exception as exc:
                        record_failure(info, exc)
                    mark_slide_progress()
            else:
                import concurrent.futures

                # Every WSI pipeline may progress concurrently. OpenSlide reads
                # remain serialized by _openslide_lock, while model/network
                # waits overlap. Do not use a context-managed executor: its
                # implicit shutdown(wait=True) would block Ctrl+C on in-flight
                # HTTP or semaphore waiters.
                pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, len(pending_infos)),
                    thread_name_prefix='patient-wsi-auto',
                )
                wait_for_workers = True
                try:
                    futures = {
                        pool.submit(analyze_one, info): info
                        for info in pending_infos
                    }
                    for future in concurrent.futures.as_completed(futures):
                        submitted_info = futures[future]
                        info = dict(submitted_info)
                        try:
                            info, wsi_result = future.result()
                            settle_analyzed_slide(info, wsi_result)
                        except KeyboardInterrupt:
                            flag_slide_workers_stop()
                            try:
                                from agent.gpt_client import request_api_interrupt
                                request_api_interrupt()
                            except Exception:
                                try:
                                    from gpt_client import request_api_interrupt
                                    request_api_interrupt()
                                except Exception:
                                    pass
                            wait_for_workers = False
                            try:
                                pool.shutdown(wait=False, cancel_futures=True)
                            except TypeError:
                                pool.shutdown(wait=False)
                            raise
                        except Exception as exc:
                            record_failure(info, exc)
                        mark_slide_progress()
                except KeyboardInterrupt:
                    flag_slide_workers_stop()
                    try:
                        from agent.gpt_client import request_api_interrupt
                        request_api_interrupt()
                    except Exception:
                        try:
                            from gpt_client import request_api_interrupt
                            request_api_interrupt()
                        except Exception:
                            pass
                    wait_for_workers = False
                    try:
                        pool.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        pool.shutdown(wait=False)
                    raise
                finally:
                    if wait_for_workers:
                        pool.shutdown(wait=True)
        except KeyboardInterrupt:
            flag_slide_workers_stop()
            try:
                from agent.gpt_client import request_api_interrupt
                request_api_interrupt()
            except Exception:
                try:
                    from gpt_client import request_api_interrupt
                    request_api_interrupt()
                except Exception:
                    pass
            raise

    # Preserve the original anatomical/index order even though futures finish
    # out of order. This keeps patient-level synthesis reproducible.
    resolved_infos = [
        resolved_by_index[idx] for idx in sorted(resolved_by_index)
    ]
    all_wsi_infos[:] = resolved_infos
    patient_state['file_infos'] = resolved_infos
    ordered_findings = []
    for info in resolved_infos:
        file_id = info.get('file_id')
        result = patient_state.get('wsi_results', {}).get(file_id) or {}
        findings = result.get('pathology_findings', [])
        if findings:
            ordered_findings.append({
                'part': info.get('part', ''),
                'filename': info.get('filename', ''),
                'findings': findings,
            })
    patient_state['aggregated_findings'] = ordered_findings
    patient_state['error_file_ids'] = failures

    if agent_stop_flags.get(room, False):
        return False
    return True


def _write_patient_schedule_metadata(
    output_dir,
    schedule,
    api_max_concurrency,
    *,
    cached,
    html_report_rewritten=False,
):
    """Record this run's scheduler settings without rewriting diagnostic reports."""
    try:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            'wsi_schedule': schedule,
            'api_max_concurrency': api_max_concurrency,
            'cached': bool(cached),
            'html_report_rewritten': bool(html_report_rewritten),
            'written_at': datetime.now().isoformat(),
        }
        path = output_dir / 'run_schedule.json'
        temporary_path = output_dir / 'run_schedule.json.tmp'
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        temporary_path.replace(path)
    except Exception as exc:
        logger.warning('[Patient Level] run_schedule.json 写入失败: %s', exc)


def run_patient_level_agent(socketio, room: str, patient_id: str,
                            file_infos: list, folder_name: str,
                            lang: str = 'zh', cache_action: str = 'rerun_all',
                            skip_file_ids: list = None,
                            wsi_schedule: str = 'parallel',
                            api_max_concurrency=None) -> dict:
    """
    运行 Patient Level Auto 多 WSI 分析。
    
    Args:
        socketio: SocketIO实例
        room: 事件通道 ID
        patient_id: 患者ID
        file_infos: 已就绪的 WSI 信息列表 [{file_id, filename, part, index}, ...]
        folder_name: 允许进入模型的患者/标本标识
        lang: 语言设置 ('zh' 中文, 'en' 英文)
    """
    try:
        total_wsi = len(file_infos)
        if total_wsi == 0:
            raise ValueError("At least one WSI is required")
        missing_ids = [info.get('part', info.get('filename', 'unknown')) for info in file_infos if not info.get('file_id')]
        if missing_ids:
            raise ValueError(f"All WSI files must be registered before evaluation: {missing_ids}")
        logger.info(f"\n{'='*60}")
        logger.info(f"🏥 启动Patient Level分析")
        logger.info(f"   患者: {folder_name}")
        logger.info(f"   切片数: {total_wsi}")
        logger.info("   模式: Patient Level Auto")
        schedule = str(wsi_schedule or 'parallel').strip().lower()
        if schedule not in ('sequential', 'parallel'):
            raise ValueError(
                "wsi_schedule must be 'sequential' or 'parallel', "
                f"got {wsi_schedule!r}"
            )
        try:
            from agent.gpt_client import (
                freeze_api_concurrency,
                require_api_max_concurrency,
            )
        except ImportError:
            try:
                from gpt_client import (
                    freeze_api_concurrency,
                    require_api_max_concurrency,
                )
            except ImportError:
                freeze_api_concurrency = None
                require_api_max_concurrency = None
        if require_api_max_concurrency is None:
            if api_max_concurrency is None:
                api_max_concurrency = 3
            else:
                api_max_concurrency = int(api_max_concurrency)
                if api_max_concurrency < 1:
                    raise ValueError("api_max_concurrency must be an integer >= 1")
        else:
            api_max_concurrency = require_api_max_concurrency(api_max_concurrency)
            freeze_api_concurrency()
        logger.info(f"   WSI调度: {schedule}")
        logger.info(f"   API并发上限: {api_max_concurrency}")
        logger.info(f"{'='*60}\n")
        
        # 创建患者输出目录（前端生成的 patient_id 已带 'patient_' 前缀）
        patient_dir_name = patient_id if patient_id.startswith('patient_') else f"patient_{patient_id}"
        patient_output_dir = Path(f"outputs/{patient_dir_name}")
        patient_output_dir.mkdir(parents=True, exist_ok=True)
        
        # 所有切片在启动前均已注册；按 manifest 顺序稳定汇总。
        all_wsi_infos = sorted(file_infos, key=lambda x: x.get('index', 0))
        
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
            'wsi_schedule': schedule,
            'api_max_concurrency': api_max_concurrency,
        }
        
        requested_ids = set(skip_file_ids or ())
        skip_file_ids = set()
        if cache_action == 'use_selected' and requested_ids:
            requested = {info['file_id']: info for info in all_wsi_infos if info['file_id'] in requested_ids}
            caches = _load_patient_ai_caches(patient_output_dir, patient_id, requested)
            patient_state['wsi_results'].update(caches)
            skip_file_ids = set(caches)
            patient_state['completed_wsi'] = len(caches)
            logger.info("[Patient Level] Reusing %d verified AI-only WSI caches", len(caches))
            if len(caches) == total_wsi:
                auto_dir = patient_output_dir / 'auto'
                tree = _matching_ai_patient_tree(auto_dir, patient_id, folder_name, all_wsi_infos, caches)
                if tree is not None:
                    tree_path = auto_dir / 'patient_analysis_tree.json'
                    meta = tree['metadata']
                    if meta.get('wsi_schedule') != schedule or meta.get('api_max_concurrency') != api_max_concurrency:
                        meta.update(wsi_schedule=schedule, api_max_concurrency=api_max_concurrency)
                        tree_path.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding='utf-8')
                    _write_patient_schedule_metadata(auto_dir, schedule, api_max_concurrency,
                                                     cached=True, html_report_rewritten=False)
                    socketio.emit('agent.patient_level_all_cached', {
                        'cached_slides': [
                            dict(info, cached_at=caches[info['file_id']]['cache_metadata'].get('analysis_time', ''),
                                 matched_tree_path=str(caches[info['file_id']]['output_paths']['output_dir']
                                                       / 'analysis_tree.json'))
                            for info in all_wsi_infos
                        ],
                        'mode': 'ai', 'folder_name': folder_name, 'patient_id': patient_id,
                        'tree_url': str(tree_path),
                    }, to=room)
                    socketio.sleep(0)
                    return {
                        'patient_id': patient_id, 'patient_identifier': folder_name,
                        'status': 'completed', 'success': True, 'cached': True,
                        'output_dir': str(auto_dir), 'error_file_ids': [],
                        'wsi_schedule': schedule, 'api_max_concurrency': api_max_concurrency,
                    }
                # The slide summaries are reusable; regenerate only the patient report.
        elif cache_action not in ('rerun_all', 'use_selected'):
            raise ValueError("cache_action must be 'rerun_all' or 'use_selected'")

        # 发送初始进度
        skipped_count = len(skip_file_ids)
        start_msg = _msg('patient_start_total', lang, folder_name=folder_name, total_wsi=total_wsi)
        if skipped_count > 0:
            start_msg = _msg('patient_start_skipped', lang, folder_name=folder_name, skipped=skipped_count, remaining=total_wsi - skipped_count)
        
        socketio.emit('agent.patient_progress', {
            'percentage': 5 + skipped_count / total_wsi * 80 if skipped_count > 0 else 5,
            'completed_wsi': skipped_count,
            'total_wsi': total_wsi,
            'message': start_msg
        }, to=room)
        socketio.sleep(0)
        
        # Default schedule is parallel (manuscript protocol). sequential runs
        # the same kernel one WSI at a time. Synthesis always waits until this
        # stage has settled.
        slide_stage_complete = _run_patient_automatic_slide_stage(
            socketio=socketio,
            room=room,
            patient_id=patient_id,
            patient_state=patient_state,
            all_wsi_infos=all_wsi_infos,
            total_wsi=total_wsi,
            folder_name=folder_name,
            skip_file_ids=skip_file_ids,
            lang=lang,
            wsi_schedule=schedule,
        )
        if not slide_stage_complete:
            logger.info('[Patient Level] 自动模式已停止，不执行患者级汇总')
            return {
                'patient_id': patient_id,
                'patient_identifier': folder_name,
                'status': 'stopped',
                'success': False,
                'output_dir': str(patient_output_dir / 'auto'),
                'error_file_ids': list(patient_state.get('error_file_ids', [])),
                'wsi_schedule': schedule,
                'api_max_concurrency': api_max_concurrency,
            }
        file_infos = list(all_wsi_infos)
        
        # 所有WSI分析完成，生成综合报告
        logger.info(f"\n{'='*60}")
        logger.info(f"所有WSI分析完成，开始生成患者级别综合报告")
        logger.info(f"{'='*60}")
        
        mode_subdir = 'auto'
        
        socketio.emit('agent.patient_progress', {
            'percentage': 90,
            'completed_wsi': total_wsi,
            'total_wsi': total_wsi,
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
            sorted_file_infos = sorted(file_infos, key=lambda x: x.get('index', 0))
            wsi_summaries_parts = []
            total_chars = 0
            for idx, f_info in enumerate(sorted_file_infos, start=1):
                fid = f_info.get('file_id')
                part_name = f_info.get('part', f_info.get('filename', fid))
                _s_id = f"S{idx}"
                
                _slide_result = patient_state.get('wsi_results', {}).get(fid) if fid else None
                if not _slide_result or _slide_result.get('wsi_summary_error'):
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
                    
                    wsi_full_evidence = f"""
### 📋 {part_name}（{_s_id}）
**统计**: {stats.get('red_frames', 0)}红框, {stats.get('blue_frames', 0)}蓝框, {stats.get('green_frames', 0)}绿框

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
            
            ai_final_report = call_gpt_vision(final_prompt, None)
            
            # ⭐ 检查 Orchestrator 是否请求查阅详细信息（支持切片级 + ROI级 + 图片）
            if ai_final_report and '[NEED_DETAIL]' in ai_final_report and '[/NEED_DETAIL]' in ai_final_report:
                logger.info(f"🔍 [Patient Level] Orchestrator 请求查阅详细信息，解析中...")
                
                import re
                need_detail_match = re.search(r'\[NEED_DETAIL\](.*?)\[/NEED_DETAIL\]', ai_final_report, re.DOTALL)
                if need_detail_match:
                    detail_block = need_detail_match.group(1).strip()
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
                            
                            ai_final_report = call_gpt_vision(supplement_prompt, composed_image_base64)
                            logger.info(f"✅ [Patient Level] 第二轮报告生成完成")
            
            if ai_final_report and '[NEED_DETAIL]' not in ai_final_report and '{"selected_grids"' not in ai_final_report:
                logger.info(f"\n{'='*60}")
                logger.info(f"✅ AI 患者级别最终报告生成成功！")
                logger.info(f"{'='*60}")
                logger.info(ai_final_report[:1000] + "..." if len(ai_final_report) > 1000 else ai_final_report)
                
                # 保存 AI 报告到文件
                ai_report_path = patient_output_dir / mode_subdir / 'ai_final_report.txt'
                ai_report_path.parent.mkdir(parents=True, exist_ok=True)
                with open(ai_report_path, 'w', encoding='utf-8') as f:
                    f.write(f"# 患者级别 AI 综合报告\n")
                    f.write(f"# 患者: {folder_name}\n")
                    f.write(f"# 切片数: {total_wsi}\n")
                    f.write("# 模式: Patient Level Auto\n")
                    f.write(f"# WSI调度: {schedule}\n")
                    f.write(f"# API并发上限: {api_max_concurrency}\n")
                    f.write(f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                    f.write(ai_final_report)
                logger.info(f"💾 AI 报告已保存: {ai_report_path}")
                
                patient_state['ai_final_report'] = ai_final_report
            else:
                logger.warning("⚠️ AI 最终报告生成失败或仍包含查阅请求，未保存 ai_final_report.txt")
                
        except Exception as e:
            logger.warning(f"[Patient Level] AI 最终报告生成失败: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        
        # 生成患者级别报告（Word）- 传入 AI 报告
        from agent.report_generator import generate_patient_level_report
        patient_state['ai_final_report'] = ai_final_report  # 确保传入
        report_result = generate_patient_level_report(patient_state, patient_output_dir)
        
        # ⭐ 生成患者级别 HTML 报告
        patient_html_path = None
        try:
            wsi_results = patient_state.get('wsi_results', {})
            patient_html_path = generate_patient_level_html_report(
                patient_state=patient_state,
                output_dir=patient_output_dir / mode_subdir,
                file_infos=file_infos,
                wsi_results=wsi_results
            )
        except Exception as e:
            logger.warning(f"[Patient Level] HTML 报告生成失败（不影响主流程）: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        
        # ⭐ 保存 patient_summary.json（供缓存加载时恢复聊天中的汇总报告文本）
        try:
            import json as json_module_ps
            summary_data = {
                'patient_id': patient_id,
                'folder_name': folder_name,
                'mode': 'ai',
                'wsi_schedule': schedule,
                'api_max_concurrency': api_max_concurrency,
                'summary': report_result.get('summary', ''),
                'report_path': str(report_result.get('path', '') or report_result.get('report_path', '')),
                'html_report_path': str(patient_html_path or ''),
                'saved_at': datetime.now().isoformat()
            }
            summary_dir = patient_output_dir / mode_subdir
            summary_dir.mkdir(parents=True, exist_ok=True)
            summary_path = summary_dir / 'patient_summary.json'
            with open(summary_path, 'w', encoding='utf-8') as sf:
                json_module_ps.dump(summary_data, sf, ensure_ascii=False, indent=2)
            logger.info(f"[Patient Level] ✅ patient_summary.json 已保存: {summary_path}")
        except Exception as e:
            logger.warning(f"[Patient Level] patient_summary.json 保存失败（不影响主流程）: {e}")
        _write_patient_schedule_metadata(
            patient_output_dir / mode_subdir,
            schedule,
            api_max_concurrency,
            cached=False,
            html_report_rewritten=bool(patient_html_path),
        )

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
                    'ai_final_report': patient_state.get('ai_final_report', ''),
                    'mode': 'ai',
                    'wsi_schedule': schedule,
                    'api_max_concurrency': api_max_concurrency,
                },
                'tree_nodes': []
            }
            
            for f_info in file_infos:
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
                
                # Only aggregate a tree returned by the current slide run or verified cache.
                if wsi_tree_path and wsi_tree_path.is_file():
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
                else:
                    logger.warning(f"[Patient Level] ⚠️ WSI 树文件不存在: {wsi_tree_path}")
            
            # 保存患者级别聚合树
            patient_tree_dir = patient_output_dir / mode_subdir
            patient_tree_dir.mkdir(parents=True, exist_ok=True)
            patient_tree_path = patient_tree_dir / 'patient_analysis_tree.json'
            with open(patient_tree_path, 'w', encoding='utf-8') as tf:
                json_module.dump(patient_tree, tf, ensure_ascii=False, indent=2)
            logger.info(f"[Patient Level] ✅ 患者级别聚合树已生成: {patient_tree_path}")
            
        except Exception as e:
            logger.warning(f"[Patient Level] 聚合树生成失败（不影响主流程）: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        
        # 发送报告完成事件
        socketio.emit('agent.patient_report', {
            'patient_id': patient_id,
            'folder_name': folder_name,
            'mode': 'ai',
            'report_path': str(report_result.get('path', '') or report_result.get('report_path', '')),
            'html_report_path': str(patient_html_path or ''),
            'summary': report_result.get('summary', ''),
            'success': report_result.get('success', False),
            'analysis_tree_path': str(patient_tree_path or '')
        }, to=room)
        
        socketio.emit('agent.patient_progress', {
            'percentage': 100,
            'completed_wsi': total_wsi,
            'total_wsi': total_wsi,
            'message': _msg('analysis_done', lang)
        }, to=room)
        
        logger.info(f"\n✅ Patient Level分析完成: {folder_name}")
        
        error_file_ids = list(patient_state.get('error_file_ids', []))
        error_file_ids += [
            fid for fid, r in (patient_state.get('wsi_results') or {}).items()
            if isinstance(r, dict) and r.get('wsi_summary_error') and fid not in error_file_ids
        ]
        ai_report_ok = (bool(ai_final_report) and '[NEED_DETAIL]' not in ai_final_report
                        and '{"selected_grids"' not in ai_final_report)
        status = 'partial' if (error_file_ids or not ai_report_ok) else 'completed'
        return {
            'patient_id': patient_id,
            'patient_identifier': folder_name,
            'status': status,
            'success': status == 'completed' and bool(report_result.get('success', False)),
            'cached': False,
            'output_dir': str(patient_output_dir / 'auto'),
            'report_txt': str(patient_output_dir / 'auto' / 'ai_final_report.txt'),
            'report_docx': str(report_result.get('path', '') or report_result.get('report_path', '')),
            'report_html': str(patient_html_path or ''),
            'analysis_tree': str(patient_tree_path or ''),
            'error_file_ids': error_file_ids,
            'wsi_schedule': schedule,
            'api_max_concurrency': api_max_concurrency,
        }
        
    except Exception as e:
        logger.error(f"Patient Level分析失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
        socketio.emit('agent.error', {
            'error': f'Patient Level分析失败: {str(e)}'
        }, to=room)
        raise



def run_single_wsi_for_patient(socketio, room: str, file_id: str, part_name: str,
                                folder_name: str, wsi_index: int, total_wsi: int,
                                patient_id: str, lang: str = 'zh') -> dict:
    """
    为Patient Level分析单张WSI
    
    ⭐ 重要：直接调用 run_agent 函数，使用完全相同的分析流程（红→蓝→绿三级下钻），
    只是传入 Patient Level 上下文，即切片，部位信息等，确保分析结果与单张模式完全一致。
    
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
        'patient_id': patient_id  # ⭐ 关键：传入 patient_id 用于正确的输出路径
    }
    
    # ⭐ 直接调用 run_agent，使用完全相同的分析流程
    # 传入 patient_level_context 参数，run_agent 会返回分析结果而不是发送 done 事件
    result = run_agent(
        socketio=socketio,
        room=room,
        file_id=file_id,
        max_steps=12,
        strategy="default",
        patient_level_context=patient_level_context,
        lang=lang
    )
    
    return result


def create_roi_overlay(roi, color='red', magnification=10):
    """创建ROI overlay对象"""
    color_map = {
        'red': '#FF0000',
        'blue': '#0066FF', 
        'green': '#00CC00'
    }
    
    return {
        'id': roi['id'],
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
        from file_registry import get_file
        
        file_info = get_file(file_id)
        if file_info:
            file_path = file_info['file_path']
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


def parse_mcp_selection(text, valid_indices=None):
    """Return drill-down indices from the unified parser. B/C/D/E are []."""
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
        agent_state: Agent状态（用于判断是否为Patient Level模式）
        replanning_context: 当前视野本地的 D/E 工具回注，不写入共享 agent_state
        
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
            
        # 全局/局部候选使用同一张宽松掩膜：只剔除明显空白，不做形态学，
        # 避免 Otsu/HSV/开闭运算把细条、浅染或边缘组织当成假阴性删掉。
        # 失败时直接安全终止：不能把 tissue_mask=None 当成“跳过过滤”，
        # 也不能 raise 到本函数外层 except（外层会把全部未过滤框加回去）。
        try:
            _tissue_start = time.time()
            thumb_arr = np.array(thumbnail)
            tissue_mask = generate_relaxed_candidate_tissue_mask(thumb_arr)
            _tissue_elapsed = time.time() - _tissue_start
            mask_h, mask_w = tissue_mask.shape
            mode_name = "局部模式" if parent_roi else "全局模式"
            logger.info(f"⏱️ [计时] 宽松组织预过滤耗时: {_tissue_elapsed:.2f}秒（{mode_name}）")
        except Exception as e:
            logger.error(f"组织掩膜生成失败: {e}")
            if return_analysis:
                return {
                    'selected_rois': [],
                    'analysis': '',
                    'selection_indices': [],
                    'mcp_action': 'C',
                    'mcp_reason': '候选组织掩膜生成失败，未恢复未过滤框。',
                    'image_path': persistent_image_path,
                    'tissue_prefilter_empty': True,
                }
            return []

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
                threshold = 0.15

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
        
        # 全部候选仍低于阈值时安全终止：不恢复空白候选，也不调用模型。
        # 蓝/绿通过 mcp_action=C 走回溯；红框全局筛选则得到空列表。
        if not valid_indices:
            logger.warning(
                f"[组织过滤-安全终止] {len(rois)} 个候选均低于15%组织占比；"
                "不恢复空白候选，也不调用模型。"
            )
            if return_analysis:
                return {
                    'selected_rois': [],
                    'analysis': '',
                    'selection_indices': [],
                    'mcp_action': 'C',
                    'mcp_reason': '所有候选组织占比均低于15%，未恢复空白候选。',
                    'image_path': persistent_image_path,
                    'tissue_prefilter_empty': True,
                }
            return []
        
        # 切片部位只使用 part_name
        
        # ⭐ 判断是否为 Patient Level 模式
        is_patient_level = agent_state and agent_state.get('is_patient_level', False)
        if not is_patient_level:
            raise ValueError("PATIENT_LEVEL_CONTEXT_REQUIRED")
        
        # 根据frame_type使用对应的规范化提示词函数
        frame_type = parent_roi.get('frame_type', 'red') if parent_roi else 'red'
        # ⭐ 从 agent_state 获取语言设置
        _lang = agent_state.get('lang', 'zh') if agent_state else 'zh'

        if frame_type == 'red' or not parent_roi:
            if is_patient_level:
                # ⭐ Patient Level 模式：使用专门的病理诊断报告收集证据提示词
                prompt = get_patient_level_red_prompt(
                    total_boxes=len(valid_indices),
                    part_name=agent_state.get('part_name', 'Slide 1'),
                    folder_name=agent_state.get('folder_name', 'Patient'),
                    wsi_index=agent_state.get('wsi_index', 1),
                    total_wsi=agent_state.get('total_wsi', 1),
                    lang=_lang
                )
                logger.info(f"[Patient Level] 使用 Patient Level 红框提示词 (Part: {agent_state.get('part_name')}, lang={_lang})")
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
                
                # ⭐ Patient Level 模式：使用病理诊断报告蓝框提示词（带上下文）
                prompt = get_patient_level_blue_prompt(
                    red_idx=parent_id,
                    num_blue_boxes=len(valid_indices),
                    part_name=agent_state.get('part_name', 'Slide 1'),
                    folder_name=agent_state.get('folder_name', 'Patient'),
                    parent_context=parent_context,
                    wsi_index=agent_state.get('wsi_index', 1),
                    lang=_lang
                )
                logger.info(f"[Patient Level] 使用 Patient Level 蓝框提示词 (带路径上下文, lang={_lang})")
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
                
                # ⭐ Patient Level 模式：使用病理诊断报告绿框提示词（带上下文）
                prompt = get_patient_level_green_prompt(
                    blue_idx=parent_id,
                    num_green_boxes=len(valid_indices),
                    part_name=agent_state.get('part_name', 'Slide 1'),
                    folder_name=agent_state.get('folder_name', 'Patient'),
                    parent_context=parent_context,
                    wsi_index=agent_state.get('wsi_index', 1),
                    lang=_lang
                )
                logger.info(f"[Patient Level] 使用 Patient Level 绿框提示词 (带路径上下文, lang={_lang})")

        # D(查库)/E(查邻) 的结果只在同一视野的第二次规划中注入。
        # 外部资料用于校准鉴别诊断，不作为当前切片的形态学阳性证据。
        _local_replan = replanning_context if isinstance(replanning_context, dict) else {}
        _replan_kind = str(_local_replan.get('kind', '') or '').strip().lower()
        _replan_content = str(_local_replan.get('content', '') or '').strip()
        _neighbor_ctx = _replan_content if _replan_kind == 'neighbor' else ''
        _knowledge_ctx = _replan_content if _replan_kind == 'knowledge' else ''
        if _neighbor_ctx:
            if _lang == 'en':
                prompt += (
                    "\n\n[Adjacent Region Reference — result of E(Neighbor)]\n"
                    f"{_neighbor_ctx}\nDo not repeat E(Neighbor) for this view."
                )
            else:
                prompt += (
                    "\n\n【邻近区域参考——E(查邻)结果】\n"
                    f"{_neighbor_ctx}\n同一视野不要再次选择 E(查邻)。"
                )
        if _knowledge_ctx:
            if _lang == 'en':
                prompt += (
                    "\n\n[External Evidence — result of D(Knowledge) MCP tool call]\n"
                    f"{_knowledge_ctx}\nUse this evidence to refine the differential "
                    "diagnosis; do not treat it as slide-level proof and do not "
                    "repeat D(Knowledge) for this view."
                )
            else:
                prompt += (
                    "\n\n【外部循证参考——D(查库) MCP 工具结果】\n"
                    f"{_knowledge_ctx}\n请用该证据校准鉴别诊断，但不得把外部资料"
                    "当作当前切片的形态学证据；同一视野不要再次选择 D(查库)。"
                )
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
        # D 缺查询使用专项提醒，不得改成 A，也不得用前 5 格回退。
        max_retries = 2
        response = None
        selected_indices = []
        decision = _empty_mcp_decision('')
        plan_decision = _extract_plan_tool_decision('')

        for attempt in range(max_retries):
            if attempt == 0:
                _prep_elapsed = time.time() - _filter_start_time
                logger.info(f"⏱️ [计时] 准备工作完成，耗时: {_prep_elapsed:.2f}秒，即将调用 API...")
                logger.info("正在请求AI进行网格筛选...")
                request_prompt = prompt
            else:
                if _mcp_decision_is_complete(decision):
                    break
                logger.warning(f"🔄 第{attempt+1}次重试AI筛选（上次解析失败）...")
                previous_action = plan_decision.get('mcp_action', '')
                if previous_action == 'D' and (
                    not plan_decision.get('search_query')
                    or plan_decision.get('parse_status') == 'missing_parameter'
                ):
                    if _lang == 'en':
                        reminder = (
                            "\n\nYou selected D(Knowledge), but no search question was provided. "
                            "State one explicit line in [Plan]: Search query: ..."
                        )
                    else:
                        reminder = (
                            "\n\n你选择了 D(查库)，但没有给出检索问题。"
                            "请在【Plan】中用一行明确写出：检索问题：..."
                        )
                elif _lang == 'en':
                    reminder = (
                        "\n\nImportant: keep the selection list in the required numeric "
                        "format, for example: Blue boxes: [1, 2, 3]."
                    )
                else:
                    reminder = (
                        "\n\n⚠️ 重要提醒：请确保【选择结果】格式正确，"
                        "必须是 蓝框：[1, 2, 3] 这样的纯数字列表！"
                    )
                request_prompt = prompt + reminder

            response = call_gpt_vision(request_prompt, image_base64)
            if not response:
                logger.warning(f"⚠️ API 返回空响应（completion=0），跳过本次重试")
                continue
            logger.info(
                f"AI筛选响应{'(重试)' if attempt else ''}: {response[:500]}..."
            )

            decision = parse_mcp_response(response, valid_indices=valid_indices)
            plan_decision = _extract_plan_tool_decision(
                response, valid_indices=valid_indices
            )
            action = (plan_decision.get('mcp_action') or decision.get('action') or '').upper()
            selected_indices = list(decision.get('selected_indices') or [])
            if action in _MCP_NON_DRILL_ACTIONS:
                selected_indices = []
                decision['selected_indices'] = []
            decision['action'] = action
            if action == 'D':
                decision['parse_status'] = plan_decision.get(
                    'parse_status', decision.get('parse_status')
                )
            else:
                decision['parse_status'] = decision.get('parse_status') or plan_decision.get(
                    'parse_status', 'failed'
                )
            if _mcp_decision_is_complete(decision):
                logger.info(f"✅ AI 选择了 {action}，选区={selected_indices}")
                break
            logger.warning(f"⚠️ 第{attempt+1}次解析失败，将重试...")

        action = (plan_decision.get('mcp_action') or decision.get('action') or '').upper()
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
            mcp_action = action
            mcp_reason = plan_decision.get('mcp_reason', '')
            logger.info(
                f"🔵 [MCP Loop] AI 选择 {mcp_action or '(none)'}，选中 {len(final_rois)} 个区域"
            )
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
                'search_query': plan_decision.get('search_query', ''),
                'neighbor_query': plan_decision.get('neighbor_query', ''),
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
                'search_query': '',
                'neighbor_query': '',
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
        
        # ⭐ 缓存版本控制：算法变更时自动失效旧缓存
        CACHE_VERSION = "v13_relaxed_tissue_15pct"
        
        # 检查缓存是否存在且有效
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                
                # 验证缓存有效性：检查 WSI 尺寸 + 缓存版本
                cached_dimensions = cache_data.get('dimensions', [])
                current_dimensions = list(_slide_dims)
                cached_version = cache_data.get('cache_version', '')
                
                if cached_dimensions == current_dimensions and cached_version == CACHE_VERSION:
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
            tissue_threshold = 0.15  # 与正常流程保持一致
            
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
            
            # 外接矩形和格子占比使用同一张宽松、无形态学候选掩膜。
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
            # ceil 多出的空白格子会被 15% 组织阈值自动过滤
            grid_cols = max(1, math.ceil(tissue_width / cell_size_initial))
            grid_rows = max(1, math.ceil(tissue_height / cell_size_initial))
            
            # 重新计算 cell_size：取 max 确保覆盖完整，格子保持正方形
            cell_size = max(tissue_width / grid_cols, tissue_height / grid_rows)
            
            total_grids = grid_rows * grid_cols
            logger.info(f"自适应网格: cell_size={int(cell_size)}x{int(cell_size)}px, "
                        f"{grid_rows}行x{grid_cols}列（共{total_grids}格）, TARGET_N={TARGET_N}")
            
            grid_rois = []
            all_red_overlays = []
            tissue_threshold = 0.15  # ⭐ 组织占比阈值：低于15%视为空白
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
                    
                    # 切同一张候选掩膜，与外接矩形判定保持一致
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
            
            # 保存完整候选框列表，供缓存、审计与二次复核定位使用。
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

    注意：候选框预过滤不得调用本函数。候选过滤请使用
    generate_relaxed_candidate_tissue_mask（无形态学，避免假阴性）。
    
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
    
    # ⭐⭐⭐ 修复：使用实际的列宽和行高，均匀覆盖整个父框 ⭐⭐⭐
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
        'thinking': response  # 保存完整的思考过程
    }
    
    # ===== 方法1: 解析新格式【Reason】+【Evidence/路径证据汇总】（MCP Loop 统一架构）=====
    reason_match = re.search(
        r'(?:【Reason】|\[Reason\])\s*([\s\S]*?)(?=【Plan】|\[Plan\]|【Evidence】|\[Evidence\]|【路径证据汇总】|\[Path Evidence Summary\]|【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
        response)
    evidence_match = re.search(
        r'(?:【Evidence】|\[Evidence\])\s*([\s\S]*?)(?=【Plan】|\[Plan\]|【路径证据汇总】|\[Path Evidence Summary\]|【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
        response)
    path_evidence_match = re.search(
        r'(?:【路径证据汇总】|\[Path Evidence Summary\])\s*([\s\S]*?)(?=【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
        response)
    
    if reason_match:
        default['thinking'] = reason_match.group(1).strip()
    
    if evidence_match:
        evidence_text = evidence_match.group(1)
        
        # 从【Evidence】中提取各字段
        # evidence (新字段，映射到 findings)
        evidence_content_match = re.search(
            r'[-•]\s*(?:evidence|主要发现|main\s+findings?)[：:]\s*(.+?)(?=\n[-•]|$)',
            evidence_text,
            re.IGNORECASE | re.DOTALL)
        if evidence_content_match:
            default['findings'] = evidence_content_match.group(1).strip()
        
        # confidence (新的5级量表，0-4)
        conf_match = re.search(
            r'[-•]\s*(?:confidence|置信度)[：:]\s*(\d)',
            evidence_text,
            re.IGNORECASE)
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
    if path_evidence_match:
        pe_text = path_evidence_match.group(1).strip()
        q_evidences = re.findall(r'Q\d_evidence[：:]\s*[（(]?([\s\S]*?)(?=Q\d_evidence|confidence|\n-\s*confidence|$)', pe_text, re.IGNORECASE)
        if q_evidences:
            default['findings'] = ' | '.join(e.strip().rstrip(')-） ') for e in q_evidences if e.strip())[:500]
        elif pe_text:
            default['findings'] = pe_text[:500]
        
        conf_values = re.findall(r'confidence[：:]\s*["\']?(\d)', pe_text, re.IGNORECASE)
        if conf_values:
            max_conf = max(int(c) for c in conf_values)
            max_conf = min(4, max(0, max_conf))
            default['confidence_level'] = max_conf
            default['confidence'] = CONFIDENCE_LEVEL_TO_PERCENT[max_conf]
        
        diag_match = re.search(r'(?:【诊断建议】|\[Diagnostic [Ii]mpression\])\s*([\s\S]*?)$', response)
        if diag_match:
            diag_text = diag_match.group(1).strip()
            if diag_text:
                default['findings'] = (default.get('findings', '') + ' | ' + diag_text)[:500] if default.get('findings') else diag_text[:500]
        
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
    
    return default


def analyze_roi_parallel(slide, roi, zoom_level, save_dir=None, parent_context=None, 
                         is_patient_level=False, patient_level_info=None, lang='zh',
                         agent_state=None):
    """
    并行分析 Patient Level 单片执行器产生的终末 160× ROI。
    
    Args:
        slide: OpenSlide对象
        roi: ROI信息字典
        zoom_level: 放大倍率
        save_dir: 保存目录路径
        parent_context: 父级分析结果上下文（用于Context Passing）
        is_patient_level: 必须为 True
        patient_level_info: Patient Level 模式的附加信息，包含:
            - part_name: 切片部位名称
            - folder_name: 患者/文件夹名称
    
    Returns:
        dict: {'roi_id', 'zoom_level', 'findings', 'confidence', 'pathology_type', 'key_features', 'image_path', 'parent_id', 'rect'}
    """
    try:
        if not is_patient_level or zoom_level != 160:
            raise ValueError("PATIENT_LEVEL_GREEN_CONTEXT_REQUIRED")

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
        
        # ⭐⭐⭐ 根据模式选择不同的提示词 ⭐⭐⭐
        # ========== Patient Level 模式：以结构化病理报告为导向 ==========
        pl_info = patient_level_info or {}
        part_name = pl_info.get('part_name', 'Slide 1')
        folder_name = pl_info.get('folder_name', 'Patient')
        
        wsi_index = pl_info.get('wsi_index', 1)
        
        prompt = get_patient_level_green_analysis_prompt(
            roi_id=roi['id'],
            parent_id=roi.get('parent_id', '未知'),
            part_name=part_name,
            folder_name=folder_name,
            parent_context=context_text,
            wsi_index=wsi_index,
            lang=lang
        )
        logger.info(f"🟢 [Patient Level] {roi['id']}: 使用 Patient Level 绿框解读提示词 (lang={lang})")
        response = call_gpt_vision(prompt, image_base64)
        
        # ⭐ 解析完整的 Reason 响应（【思考过程】+【分析结论】）
        parsed = parse_structured_response(response, zoom_level)
        
        # 160× 绿框：提取结构化 evidence 与 diagnosis_suggestion。
        evidence = {}
        diagnosis_suggestion = ""
        if zoom_level == 160:
            _pe_match = re.search(
                r'(?:【路径证据汇总】|\[Path Evidence Summary\]|【Evidence】|\[Evidence\])\s*([\s\S]*?)(?=【诊断建议】|\[Diagnostic [Ii]mpression\]|$)',
                response)
            if _pe_match:
                _pe_text = _pe_match.group(1).strip()
                evidence['path_evidence_summary'] = _pe_text
                _cm = re.search(r'(?:confidence|置信度)[：:]\s*(\d)', _pe_text, re.IGNORECASE)
                if _cm:
                    parsed['confidence'] = int(_cm.group(1)) / 4.0 * 100
            _diag_match = re.search(r'(?:【诊断建议】|\[Diagnostic [Ii]mpression\])\s*([\s\S]*?)$', response)
            if _diag_match:
                diagnosis_suggestion = _diag_match.group(1).strip()
        
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
    执行 Patient Level Auto 的红框、蓝框和绿框多尺度分析。
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
        
        # MCP Loop: D(查库)/E(查邻) 均最多在当前调用内回注并重规划一次。
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
            first_sentence = parsed['thinking'].split('。')[0] if '。' in parsed['thinking'] else parsed['thinking'][:100]
            if not any(kw in first_sentence for kw in ['区域', '编号', '选择', '我选择']):
                red_finding_summary = first_sentence + '。' if not first_sentence.endswith('。') else first_sentence
            else:
                red_finding_summary = _extract_findings_from_reasoning(red_analysis_text)
        else:
            red_finding_summary = _extract_findings_from_reasoning(red_analysis_text)

        red_runtime_action = _effective_action_letter(mcp_action_result)
        
        red_analysis = {
            'findings': red_finding_summary,
            'selection_reasoning': parsed['thinking'],
            'reasoning': red_analysis_text,
            'thinking': parsed['thinking'],
            'selection': parsed['selection'],
            'confidence': 75,
            'pathology_type': 'analysis',
            'key_features': [],
            'image_path': None,
            'plan_action': blue_result.get('mcp_action', ''),
            'selected_indices': list(blue_result.get('selection_indices') or []),
            'runtime_action': red_runtime_action,
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
        
        # 发射蓝框选取完成事件，并携带区域坐标供本地审计。
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
            'roi_id': roi_info['id'],  # ⭐ 父红框ID
            'rect': roi_info['rect'],   # ⭐ 父红框坐标（用于点击聚焦）
            'regions': blue_regions_info,  # ⭐ 蓝框坐标列表，用于前端显示
            'metadata': {
                'action': 'selected', 
                'count': len(selected_blue_rois), 
                'parent_id': roi_info['id']
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
        
        # ⭐⭐⭐ MCP Loop 改进：删除冗余的 analyze_roi_parallel (40x)
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

                def _replan_blue(replanning_context, _blue_roi=blue_roi, _green_rois=green_rois, _blue_roi_for_green=blue_roi_for_green):
                    logger.info(
                        f"  🔄 [MCP] 蓝框 {_blue_roi['id']}: 回注"
                        f"{replanning_context.get('kind', '')}上下文后重新规划一次"
                    )
                    return filter_rois_by_gpt_vision(
                        slide, _green_rois, (2000, 2000), None,
                        _blue_roi_for_green, wsi_name,
                        return_analysis=True, agent_state=agent_state,
                        replanning_context=replanning_context,
                    )

                green_result, green_action, blue_tool_trace = resolve_mcp_action_with_one_replan(
                    initial_result=green_result,
                    roi_info=blue_roi,
                    agent_state=agent_state,
                    replan_callback=_replan_blue,
                    socketio=socketio,
                    room=room,
                )

                if green_action['action'] == 'backtrack':
                    logger.warning(
                        f"⏪ [MCP] 蓝框 {blue_roi['id']} 触发回溯，"
                        "不写入诊断树，继续其他同级蓝框"
                    )
                    skipped_count += 1
                    continue
                
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
                if green_action['action'] in ('backtrack', 'stop'):
                    selected_greens = []
                else:
                    selected_greens = green_action['selected_rois']
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
                    'pathology_type': 'uncertain',
                    'key_features': [],
                    'no_green_needed': len(selected_greens) == 0,
                    'plan_action': green_result.get('mcp_action', ''),
                    'selected_indices': list(green_result.get('selection_indices') or []),
                    'runtime_action': _effective_action_letter(green_action),
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
                    logger.info(f"  ⊙ {blue_roi['id']}: 不需要160x放大（【判断】B）")
                    logger.info(f"  📤 已发送蓝框完成事件: {blue_roi['id']} (无需160x)")
                    skipped_count += 1
                else:
                    logger.info(f"  ✓ {blue_roi['id']}: 需要160x放大，选中 {len(selected_greens)} 个绿框")
            
            logger.info(f"{'─'*40}")
            logger.info(f"✅ 蓝框 Reason+Plan 完成: {len(selected_blue_rois) - skipped_count}/{len(selected_blue_rois)} 蓝框需要放大")
            logger.info(f"{'─'*40}")
            
            # 合并所有选中的绿框
            selected_green_rois = []
            for idx, green_list in enumerate(green_filter_results):
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
        
        green_analyses = []
        try:
            if selected_green_rois:
                executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(selected_green_rois)
                )
                wait_for_green_workers = True
                try:
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
                        
                        # ⭐ Patient Level 模式传递额外信息
                        pl_is_patient_level = agent_state.get('is_patient_level', False)
                        pl_info = {
                            'part_name': agent_state.get('part_name', 'Slide 1'),
                            'folder_name': agent_state.get('folder_name', 'Patient'),
                        } if pl_is_patient_level else None
                        
                        green_analysis_futures.append(
                            executor.submit(analyze_roi_parallel, slide, green_roi, 160, roi_save_dir, 
                                          parent_context=parent_context,
                                          is_patient_level=pl_is_patient_level,
                                          patient_level_info=pl_info,
                                          lang=agent_state.get('lang', 'zh'),
                                          agent_state=agent_state)
                        )
                    logger.info(f"⏳ 等待 {len(green_analysis_futures)} 个绿框分析任务完成...")
                    green_analyses = []
                    for future in green_analysis_futures:
                        green_analyses.append(future.result())
                except KeyboardInterrupt:
                    wait_for_green_workers = False
                    _signal_api_interrupt()
                    _shutdown_thread_pool(executor, wait=False)
                    raise
                finally:
                    if wait_for_green_workers:
                        _shutdown_thread_pool(executor, wait=True)
            
            logger.info(f"✅ 绿框分析完成！共 {len(green_analyses)} 个绿框")
            
            # ⭐ Patient Level 模式：发送绿框分析结果到前端（用于更新左侧分析树）
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
    
    ⭐ 传递完整的树结构上下文（Reason + Plan + Action），而不是截断的 findings
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
    
    # ⭐ 注入缩略图全局分析
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
