# -*- coding: utf-8 -*-
"""Action-first A–E parse contract.

This module must stay free of OpenSlide, model clients, and network imports so
hygiene tests can load ``agent_loop_prompts.py`` in isolation.
"""

from __future__ import annotations

import ast
import json
import logging
import re

logger = logging.getLogger(__name__)

MCP_NON_DRILL_ACTIONS = frozenset({"B", "C", "D", "E"})
MCP_ALL_ACTIONS = frozenset({"A", "B", "C", "D", "E"})
_MCP_CN_ACTION_NAMES = {
    "下钻": "A",
    "停止": "B",
    "回溯": "C",
    "查库": "D",
    "查邻": "E",
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

SELECTION_ABSENT = "absent"
SELECTION_EMPTY = "explicit_empty"
SELECTION_VALID = "valid"
SELECTION_INVALID = "invalid"


def _loads_json_object(text):
    if not text or not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value


def _strip_wrapping_fence(text: str) -> str:
    stripped = (text or "").strip()
    match = re.match(r"^```(?:json)?\s*\n?([\s\S]*?)\n?```$", stripped, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped


def _is_mcp_json_dict(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = set(obj)
    if keys <= {"answer"} or (len(obj) == 1 and "answer" in obj):
        return False
    return bool(keys & {"Reason", "Plan", "Evidence", "Selected action"})


def _format_action_parameter_line(value) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if (
            len(stripped) >= 2
            and stripped[0] == stripped[-1]
            and stripped[0] in {'"', "'"}
        ):
            inner = stripped[1:-1].strip()
            if inner.startswith("[") and inner.endswith("]"):
                stripped = inner
        return f"Action parameter: {stripped}"
    if isinstance(value, (list, dict)):
        return f"Action parameter: {json.dumps(value, ensure_ascii=False)}"
    return f"Action parameter: {value}"


def _mcp_json_to_tagged(obj: dict) -> str:
    parts = []
    if "Reason" in obj:
        reason = obj["Reason"]
        if isinstance(reason, dict):
            reason = "\n".join(f"{key}: {value}" for key, value in reason.items())
        parts.append(f"【Reason】\n{reason}")
    if "Plan" in obj:
        plan = obj["Plan"]
        if isinstance(plan, str) and _MCP_PLAN_SCALAR_RE.match(plan.strip()):
            lines = [f"Selected action: {plan.strip()}"]
            if "Action parameter" in obj:
                lines.append(_format_action_parameter_line(obj["Action parameter"]))
            plan = "\n".join(lines)
        elif isinstance(plan, dict):
            action_val = None
            for key in ("Selected action", "action", "Action"):
                cand = plan.get(key)
                if isinstance(cand, str) and _MCP_PLAN_SCALAR_RE.match(cand.strip()):
                    action_val = cand.strip()
                    break
            if action_val:
                lines = [f"Selected action: {action_val}"]
                param = None
                if "Action parameter" in obj:
                    param = obj["Action parameter"]
                elif "Action parameter" in plan:
                    param = plan["Action parameter"]
                elif "parameter" in plan:
                    param = plan["parameter"]
                if param is not None:
                    lines.append(_format_action_parameter_line(param))
                plan = "\n".join(lines)
            else:
                lines = []
                for key, value in plan.items():
                    if isinstance(value, (list, dict)):
                        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
                    else:
                        lines.append(f"{key}: {value}")
                if "Action parameter" in obj and "Action parameter" not in plan:
                    lines.append(_format_action_parameter_line(obj["Action parameter"]))
                plan = "\n".join(lines)
        parts.append(f"【Plan】\n{plan}")
    if "Evidence" in obj:
        evidence = obj["Evidence"]
        if isinstance(evidence, dict):
            evidence = "\n".join(f"- {key}: {value}" for key, value in evidence.items())
        elif isinstance(evidence, list):
            chunks = []
            for item in evidence:
                if isinstance(item, dict):
                    chunks.append("\n".join(f"- {key}: {value}" for key, value in item.items()))
                else:
                    chunks.append(str(item))
            evidence = "\n".join(chunks)
        parts.append(f"【Evidence】\n{evidence}")
    if "Selected action" in obj and "Plan" not in obj:
        lines = [f"Selected action: {obj['Selected action']}"]
        if "Action parameter" in obj:
            lines.append(_format_action_parameter_line(obj["Action parameter"]))
        parts.append("【Plan】\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _coerce_mcp_json_to_text(response: str) -> str:
    if not response:
        return ""
    stripped = _strip_wrapping_fence(response)
    obj = _loads_json_object(stripped)
    if _is_mcp_json_dict(obj):
        tagged = _mcp_json_to_tagged(obj)
        if tagged:
            return tagged
    return response


def parse_index_list_literal(literal: str, valid_indices=None):
    if not literal:
        return []
    cleaned = literal.replace("、", ",").strip()
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
    return match.group(1).strip() if match else ""


def extract_explicit_action(text: str) -> str:
    if not text:
        return ""
    match = _MCP_ACTION_LINE_RE.search(text)
    if match:
        return match.group("letter").upper()
    match = _MCP_ACTION_JSON_RE.search(text)
    if match:
        return match.group("letter").upper()
    match = _MCP_PLAN_JSON_SCALAR_RE.search(text)
    if match:
        return match.group("letter").upper()
    match = _MCP_NESTED_ACTION_JSON_RE.search(text)
    if match:
        return match.group("letter").upper()
    match = _MCP_CN_ACTION_NAME_RE.search(text)
    if match:
        return _MCP_CN_ACTION_NAMES[match.group("name")]
    stripped = text.strip()
    match = _MCP_PLAN_SCALAR_RE.match(stripped)
    if match:
        return match.group("letter").upper()
    first_line = stripped.splitlines()[0].strip()
    match = _MCP_PLAN_SCALAR_RE.match(first_line)
    if match:
        return match.group("letter").upper()
    return ""


def _classify_index_literal(literal: str, valid_indices=None):
    if not literal:
        return [], SELECTION_ABSENT
    stripped = literal.replace("、", ",").strip()
    if stripped == "[]":
        return [], SELECTION_EMPTY
    raw = parse_index_list_literal(stripped, valid_indices=None)
    if not raw:
        return [], SELECTION_INVALID
    selected = parse_index_list_literal(stripped, valid_indices)
    if selected:
        return selected, SELECTION_VALID
    return [], SELECTION_INVALID


def _extract_labeled_selection(text: str, valid_indices=None):
    if not text:
        return [], SELECTION_ABSENT
    found_invalid = False
    for pattern in _MCP_LABELED_INDEX_RES:
        match = pattern.search(text)
        if not match:
            continue
        selected, status = _classify_index_literal(match.group(1), valid_indices)
        if status in (SELECTION_VALID, SELECTION_EMPTY):
            return selected, status
        if status == SELECTION_INVALID:
            found_invalid = True
    if found_invalid:
        return [], SELECTION_INVALID
    return [], SELECTION_ABSENT


def _extract_labeled_indices(text: str, valid_indices=None):
    selected, status = _extract_labeled_selection(text, valid_indices)
    if status in (SELECTION_VALID, SELECTION_EMPTY):
        return selected
    return []


def _extract_bracket_selection(plan_text: str, valid_indices=None):
    if not plan_text:
        return [], SELECTION_ABSENT
    match = _MCP_INDEX_LIST_RE.search(plan_text)
    if not match:
        return [], SELECTION_ABSENT
    return _classify_index_literal(match.group(0), valid_indices)


def empty_mcp_decision(response: str = "") -> dict:
    return {
        "reason": "",
        "plan_action": "",
        "plan_params": None,
        "action_parameter": None,
        "evidence": {},
        "raw_response": response or "",
        "action": "",
        "selected_indices": [],
        "inferred_stop": False,
        "parse_status": "failed",
    }


def _action_parameter_from_plan(action, selected_indices, plan_text, tagged):
    if action == "A":
        return list(selected_indices or [])
    blob = plan_text or tagged or ""
    match = _MCP_ACTION_PARAMETER_RE.search(blob)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value == "[]":
        return None
    return value


def mcp_decision_is_complete(decision: dict) -> bool:
    if not decision:
        return False
    if decision.get("parse_status") in {
        "invalid_selection",
        "missing_selection",
        "missing_parameter",
    }:
        return False
    action = (decision.get("action") or "").upper()
    return action in MCP_ALL_ACTIONS


def mcp_response_is_stop(response: str) -> bool:
    if not response:
        return False
    rl = response.lower()
    if re.search(r"(?:选择动作|selected\s*action|action)\s*[：:]\s*[bcde]\b", rl):
        return True
    if re.search(r"\bb\s*[\(（]\s*(?:stop|停止)", rl):
        return True
    cues = [
        "已足够", "足以回答", "已足以", "已能回答", "已可回答", "已经可以回答",
        "无需下钻", "无需进一步", "不需要下钻", "不需要进一步", "不必下钻",
        "已明确", "已可作答", "已可确定", "当前放大已", "当前倍率已", "已可诊断",
        "no need to drill", "no further magnification", "no further drill",
        "need not drill", "no drill-down", "no drill down", "not necessary to drill",
        "already answered", "already determined", "already definitive",
        "definitively characteristic", "definitively a", "unambiguously",
        "sufficient to answer", "evidence is sufficient", "enough evidence to answer",
        "can be answered at this", "answerable at this magnification",
        "current magnification is sufficient", "this magnification is sufficient",
    ]
    return any(cue in rl for cue in cues)


def parse_mcp_response(response: str, valid_indices=None) -> dict:
    """Parse one Sub-agent / Orchestrator reply into an action and its parameters.

    Contract: decide the action first. If the action is B/C/D/E, no number
    from Reason, Evidence, confidence, or the answer JSON may become a
    drill-down index.
    """
    result = empty_mcp_decision(response)
    if not response:
        return result

    tagged = _coerce_mcp_json_to_text(response)
    result["reason"] = _extract_tagged_section(tagged, "Reason")

    plan_text = _extract_tagged_section(tagged, "Plan")
    evidence_text = _extract_tagged_section(tagged, "Evidence")
    if not evidence_text:
        evidence_match = re.search(r"【证据摘要】\s*([\s\S]*?)$", tagged)
        if evidence_match:
            evidence_text = evidence_match.group(1).strip()

    action = extract_explicit_action(plan_text) or extract_explicit_action(tagged)
    if not action:
        action = extract_explicit_action(response)

    selected_indices = []
    parse_status = "failed"
    inferred_stop = False

    if action in MCP_NON_DRILL_ACTIONS:
        selected_indices = []
        parse_status = "ok"
    elif action == "A":
        selected_indices, sel_status = _extract_labeled_selection(plan_text, valid_indices)
        if sel_status == SELECTION_ABSENT:
            selected_indices, sel_status = _extract_bracket_selection(plan_text, valid_indices)
        if sel_status == SELECTION_ABSENT:
            selected_indices, sel_status = _extract_labeled_selection(tagged, valid_indices)
        if sel_status == SELECTION_ABSENT:
            selected_indices, sel_status = _extract_labeled_selection(response, valid_indices)
        if sel_status == SELECTION_INVALID:
            selected_indices = []
            parse_status = "invalid_selection"
        elif sel_status == SELECTION_ABSENT:
            selected_indices = []
            parse_status = "missing_selection"
        else:
            parse_status = "ok"
    else:
        selected_indices = _extract_labeled_indices(tagged, valid_indices)
        if not selected_indices:
            selected_indices = _extract_labeled_indices(response, valid_indices)
        if selected_indices:
            action = "A"
            parse_status = "inferred_drill"
        elif mcp_response_is_stop(tagged) or mcp_response_is_stop(response):
            action = "B"
            selected_indices = []
            inferred_stop = True
            parse_status = "inferred_stop"
            logger.info("🛟 [MCP兜底] 未解析到动作但检测到停止语义 → 判定 B(Stop)")

    if action in MCP_NON_DRILL_ACTIONS:
        selected_indices = []

    action_parameter = _action_parameter_from_plan(
        action, selected_indices, plan_text, tagged
    )
    if action == "D" and parse_status == "ok" and not action_parameter:
        parse_status = "missing_parameter"
    result["action"] = action
    result["plan_action"] = action
    result["selected_indices"] = selected_indices
    result["action_parameter"] = action_parameter
    result["inferred_stop"] = inferred_stop
    result["parse_status"] = parse_status
    if action == "A":
        result["plan_params"] = selected_indices
    elif action in ("B", "C", "D", "E"):
        result["plan_params"] = action_parameter

    if evidence_text:
        for field in [
            "evidence",
            "limitations_or_counterevidence",
            "confidence",
            "pathology_type",
            "key_findings",
        ]:
            field_match = re.search(
                rf"[-•]\s*{field}[：:]\s*(.+?)(?=\n[-•]|$)",
                evidence_text,
                re.IGNORECASE,
            )
            if field_match:
                result["evidence"][field] = field_match.group(1).strip()

    return result


def parse_mcp_selection(text, valid_indices=None):
    """Return only the drill-down indices from an action-first parse."""
    return list(
        parse_mcp_response(text, valid_indices=valid_indices).get("selected_indices") or []
    )
