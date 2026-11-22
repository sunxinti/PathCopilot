# -*- coding: utf-8 -*-
"""API-native, single-WSI secondary visual review.

This module is intentionally independent from the outbound PubMed/Web MCP
client.  The main Orchestrator may expose one OpenAI-compatible function tool
after every first-pass red-blue-green path in a WSI has completed.  A selected
terminal claim is then reviewed from its saved multiscale images before the
single-slide summary is synthesized.

The tool is optional and fail-open for execution, but fail-closed for the
selected claim: a planned review that cannot run is not treated as a confirmed
positive.  No patient-level or cross-slide visual review is performed here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from PIL import Image, ImageDraw

try:
    from .gpt_client import call_agent_model, call_gpt_vision
except ImportError:  # 兼容将 agent/ 直接加入 sys.path 的旧入口。
    from gpt_client import call_agent_model, call_gpt_vision

logger = logging.getLogger(__name__)


SECONDARY_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "secondary_review",
        "description": (
            "Independently re-review one precise, focal, image-checkable claim "
            "from a terminal WSI ROI when that claim could materially change "
            "the current single-slide diagnostic task and is isolated or "
            "meaningfully discordant with other evidence."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "roi_id": {
                    "type": "string",
                    "description": "Exact terminal ROI evidence ID.",
                },
                "claim": {
                    "type": "string",
                    "description": "One precise focal claim copied from the terminal evidence.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why the claim is material to the current task and "
                        "isolated or discordant."
                    ),
                },
                "answer_if_confirmed": {
                    "type": "string",
                    "description": "Main single-slide answer if the claim is confirmed.",
                },
                "answer_if_rejected": {
                    "type": "string",
                    "description": "Main single-slide answer if the claim is rejected.",
                },
                "supporting_roi_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact supporting evidence IDs; must include roi_id.",
                },
                "conflicting_roi_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact conflicting evidence IDs; GLOBAL may identify "
                        "the whole-slide observation."
                    ),
                },
                "image_checkable": {
                    "type": "boolean",
                    "description": "Whether saved morphology images can adjudicate the claim.",
                },
                "requires_nonvisual_data": {
                    "type": "boolean",
                    "description": (
                        "Whether adjudication requires clinical data, stains, "
                        "biomarkers, molecular data, or a whole-organ denominator."
                    ),
                },
            },
            "required": [
                "roi_id",
                "claim",
                "reason",
                "answer_if_confirmed",
                "answer_if_rejected",
                "supporting_roi_ids",
                "conflicting_roi_ids",
                "image_checkable",
                "requires_nonvisual_data",
            ],
        },
    },
}


def _enabled() -> bool:
    return os.getenv("PATHCOPILOT_SECONDARY_REVIEW", "1").strip().lower() not in {
        "", "0", "false", "off", "no"
    }


def secondary_review_limit() -> int:
    """Return the bounded review budget for one WSI (default: three)."""
    try:
        value = int(os.getenv("PATHCOPILOT_SECONDARY_REVIEW_MAX", "3"))
    except (TypeError, ValueError):
        value = 3
    return min(8, max(0, value))


def secondary_review_honor_rule(lang: str = "zh") -> str:
    """Rule injected into both slide- and patient-level synthesis prompts."""
    if str(lang or "").lower().startswith("en"):
        return (
            "For a claim selected for secondary review, only CONFIRMED may be "
            "reported or counted as a confirmed positive. REJECTED, "
            "INDETERMINATE, SKIP, and NOT_RUN mean that the nominated first-pass "
            "claim remains unconfirmed; preserve it only as an audited prior "
            "observation or limitation. A high first-pass confidence cannot "
            "override that adjudication. Unselected findings are not assigned "
            "a review verdict."
        )
    return (
        "对于已被选中进行二次复核的主张，只有 CONFIRMED 才能作为已确认阳性写入或计数。"
        "REJECTED、INDETERMINATE、SKIP、NOT_RUN 均表示该次被提名的首次主张未获确认；"
        "首次内容仅作为审计记录或局限保留，原始高置信度不得覆盖该裁决。"
        "未被选中复核的发现不附加复核 verdict。"
    )


def _normalize_lang(lang: str) -> str:
    return "en" if str(lang or "").lower().startswith("en") else "zh"


def _terminal_refs(agent_state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Index terminal green analyses by reference, without copying them."""
    refs: Dict[str, Dict[str, Any]] = {}

    def add(node: Mapping[str, Any]) -> None:
        if not isinstance(node, Mapping):
            return
        analysis = node.get("analysis") or {}
        if not isinstance(analysis, dict) or analysis.get("success") is False:
            return
        roi_id = str(
            analysis.get("roi_id")
            or node.get("roi_id")
            or node.get("id")
            or ""
        ).strip()
        if not roi_id:
            return
        parent_id = str(
            analysis.get("parent_id")
            or node.get("parent_id")
            or "-".join(roi_id.split("-")[:2])
        ).strip()
        refs.setdefault(
            roi_id,
            {
                "analysis": analysis,
                "roi": {
                    "id": roi_id,
                    "parent_id": parent_id,
                    "rect": analysis.get("rect") or node.get("rect") or {},
                    "image_path": node.get("image_path"),
                },
            },
        )

    for red in agent_state.get("tree_nodes", []) or []:
        for blue in red.get("children", []) or []:
            for green in blue.get("children", []) or []:
                add(green)
    return refs


def _analysis_text(analysis: Any) -> str:
    if not isinstance(analysis, Mapping):
        return str(analysis or "")
    values = [
        analysis.get("findings"),
        analysis.get("diagnosis_suggestion"),
        analysis.get("reasoning"),
    ]
    evidence = analysis.get("evidence")
    if evidence:
        values.append(
            json.dumps(evidence, ensure_ascii=False)
            if isinstance(evidence, (dict, list))
            else str(evidence)
        )
    return "\n".join(str(value) for value in values if value)[:12000]


def _evidence_index(
    agent_state: Mapping[str, Any], refs: Mapping[str, Mapping[str, Any]]
) -> Dict[str, str]:
    index = {
        str(roi_id): _analysis_text(ref.get("analysis", {}))
        for roi_id, ref in refs.items()
    }
    index["GLOBAL"] = _analysis_text(agent_state.get("global_analysis"))

    def add(node: Mapping[str, Any], fallback_id: str = "") -> None:
        if not isinstance(node, Mapping):
            return
        analysis = node.get("analysis") or node
        if not isinstance(analysis, Mapping) or analysis.get("success") is False:
            return
        evidence_id = str(
            analysis.get("roi_id")
            or node.get("roi_id")
            or node.get("id")
            or fallback_id
            or ""
        ).strip()
        if evidence_id:
            index.setdefault(evidence_id, _analysis_text(analysis))

    for red in agent_state.get("tree_nodes", []) or []:
        add(red)
        for blue in red.get("children", []) or []:
            add(blue)
    return index


def _planning_prompt(
    agent_state: Mapping[str, Any],
    refs: Mapping[str, Mapping[str, Any]],
    diagnostic_question: str,
    limit: int,
) -> str:
    terminal = []
    for roi_id, ref in refs.items():
        analysis = ref.get("analysis", {})
        terminal.append(
            {
                "roi_id": roi_id,
                "parent_id": ref.get("roi", {}).get("parent_id"),
                "rect": ref.get("roi", {}).get("rect"),
                "first_pass": _analysis_text(analysis)[:5000],
                "confidence": analysis.get(
                    "evidence_confidence",
                    analysis.get("confidence_level", analysis.get("confidence")),
                ),
            }
        )

    upstream = []
    for red in agent_state.get("tree_nodes", []) or []:
        upstream.append(
            {
                "evidence_id": red.get("roi_id") or red.get("id"),
                "level": "red",
                "text": _analysis_text(red.get("analysis", {}))[:1600],
            }
        )
        for blue in red.get("children", []) or []:
            upstream.append(
                {
                    "evidence_id": blue.get("roi_id") or blue.get("id"),
                    "level": "blue",
                    "text": _analysis_text(blue.get("analysis", {}))[:1600],
                }
            )
    bundle = {
        "whole_slide": {
            "evidence_id": "GLOBAL",
            "text": _analysis_text(agent_state.get("global_analysis"))[:3500],
        },
        "upstream_evidence": upstream[:100],
        "terminal_evidence": terminal,
    }
    evidence_text = json.dumps(bundle, ensure_ascii=False, indent=2)
    if _normalize_lang(agent_state.get("lang", "zh")) == "en":
        return f"""You are the main PathCopilot Orchestrator after all first-pass paths for ONE WSI have completed.

## Current single-slide diagnostic task
{diagnostic_question}

## Available evidence tree
{evidence_text}

## Available API-native tool
You may call `secondary_review` on a terminal ROI. It independently reviews the exact target with saved parent-scale context and the marked whole-slide location. Never imitate a tool call in message text.

Call it only when ALL are true: (1) the claim could materially change this single-slide answer; (2) it is isolated or meaningfully discordant with sibling, nearby, parent-scale, or other independent evidence, or a plausible mimic remains; (3) it is precise and image-checkable; and (4) a second visual review can resolve or appropriately mark the uncertainty.

Do not call it for incidental findings, already concordant evidence, hidden checklists, whole-organ denominators, clinical data, stains, biomarkers, molecular results, or patient-level inference. Do not introduce an organ, diagnosis, grade, stage, invasion target, or named finding that neither the task nor evidence raised. Prefer zero calls. Maximum calls: {limit}.

For each call, reuse the target terminal evidence's diagnostic wording, list exact supporting and conflicting evidence IDs, include the target in supporting_roi_ids, and state how the single-slide answer differs if confirmed versus rejected. If no call is justified, make no tool call and reply briefly NO_SECONDARY_REVIEW."""
    return f"""你是 PathCopilot 主 Orchestrator。当前这一张 WSI 的所有首次分析路径均已完成。

## 当前单片诊断任务
{diagnostic_question}

## 可用证据树
{evidence_text}

## 可用的 API 原生工具
你可以针对终末 ROI 原生调用 `secondary_review`。工具将结合已保存的父级倍率路径和标出目标位置的全切片定位图，独立复核这一精确局灶主张。不得在普通文本中模仿工具调用。

只有以下条件全部满足时才调用：(1) 主张可能实质性改变本张切片的主要结论；(2) 主张孤立或与同级、邻近、父级倍率或其他独立证据存在有意义的不一致，或仍存在合理模拟病变；(3) 主张明确且可由图像复核；(4) 二次视觉复核能够解决或恰当标记不确定性。

不得用于偶然发现、已有一致证据的主张、隐藏检查清单、需要全器官分母的问题、临床资料、特殊染色、生物标志物、分子结果或患者级推断。不得引入当前任务和现有证据都未提出的器官、诊断、分级、分期、浸润目标或特定发现。原则上优先不调用。最多调用 {limit} 次。

每次调用必须沿用目标终末证据的诊断性表述，列出准确的支持/冲突证据 ID，并在 supporting_roi_ids 中包含目标 ROI；同时说明确认与否定时本张切片主要答案分别如何改变。若无合理调用，不调用工具并简短回复 NO_SECONDARY_REVIEW。"""


def _parse_plan(
    model_message: Any, valid_roi_ids: Iterable[str], limit: int
) -> list[Dict[str, Any]]:
    """Accept only API-native tool_calls; ignore JSON imitations in text."""
    if not isinstance(model_message, Mapping) or limit <= 0:
        return []
    allowed = {str(value) for value in valid_roi_ids}
    selected = []
    seen = set()
    tool_calls = model_message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return []
    for raw_call in tool_calls:
        function = raw_call.get("function") if isinstance(raw_call, Mapping) else None
        if not isinstance(function, Mapping) or function.get("name") != "secondary_review":
            continue
        raw_arguments = function.get("arguments") or {}
        try:
            item = raw_arguments if isinstance(raw_arguments, dict) else json.loads(str(raw_arguments))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        roi_id = str(item.get("roi_id") or "").strip()
        supporting = item.get("supporting_roi_ids")
        conflicting = item.get("conflicting_roi_ids")
        required_text = {
            key: str(item.get(key) or "").strip()
            for key in (
                "claim",
                "reason",
                "answer_if_confirmed",
                "answer_if_rejected",
            )
        }
        if (
            roi_id not in allowed
            or roi_id in seen
            or not all(required_text.values())
            or not isinstance(supporting, list)
            or not isinstance(conflicting, list)
            or not isinstance(item.get("image_checkable"), bool)
            or not isinstance(item.get("requires_nonvisual_data"), bool)
        ):
            continue
        selected.append(
            {
                "roi_id": roi_id,
                **{key: value[:1200] for key, value in required_text.items()},
                "supporting_roi_ids": [str(value).strip() for value in supporting if str(value).strip()][:100],
                "conflicting_roi_ids": [str(value).strip() for value in conflicting if str(value).strip()][:100],
                "image_checkable": item["image_checkable"],
                "requires_nonvisual_data": item["requires_nonvisual_data"],
                "tool_call_id": str(raw_call.get("id") or ""),
            }
        )
        seen.add(roi_id)
        if len(selected) >= limit:
            break
    return selected


def _plan_reviews(
    agent_state: Mapping[str, Any],
    refs: Mapping[str, Mapping[str, Any]],
    diagnostic_question: str,
    limit: int,
) -> list[Dict[str, Any]]:
    if not refs or limit <= 0:
        return []
    messages = [
        {
            "role": "system",
            "content": (
                "You are the sole PathCopilot Orchestrator controlling the optional "
                "secondary-review function. Make conservative evidence-linked native "
                "tool decisions. Never imitate a tool call in message text."
            ),
        },
        {
            "role": "user",
            "content": _planning_prompt(agent_state, refs, diagnostic_question, limit),
        },
    ]
    try:
        model_message, error = call_agent_model(
            messages,
            max_tokens=4096,
            call_type="secondary_review_plan",
            tools=[SECONDARY_REVIEW_TOOL],
            tool_choice="auto",
            return_message=True,
        )
    except Exception as exc:
        logger.warning("[secondary_review] planning call failed: %s", exc)
        return []
    if error:
        logger.warning("[secondary_review] planning failed: %s", error)
        return []
    return _parse_plan(model_message, refs.keys(), limit)


def _first_existing(*paths: Any) -> Optional[str]:
    for candidate in paths:
        try:
            if candidate and Path(candidate).exists():
                return str(candidate)
        except (OSError, TypeError, ValueError):
            continue
    return None


def _node_image(agent_state: Mapping[str, Any], wanted_id: str) -> Optional[str]:
    for red in agent_state.get("tree_nodes", []) or []:
        red_id = str(red.get("roi_id") or red.get("id") or "")
        if red_id == wanted_id:
            return red.get("image_path") or (red.get("analysis") or {}).get("image_path")
        for blue in red.get("children", []) or []:
            blue_id = str(blue.get("roi_id") or blue.get("id") or "")
            if blue_id == wanted_id:
                return blue.get("image_path") or (blue.get("analysis") or {}).get("image_path")
    return None


def _image_paths(
    agent_state: Mapping[str, Any], ref: Mapping[str, Any], roi_id: str, root: Path
) -> Dict[str, Optional[str]]:
    analysis = ref.get("analysis", {})
    roi = ref.get("roi", {})
    parent_id = str(roi.get("parent_id") or "-".join(roi_id.split("-")[:2]))
    red_id = roi_id.split("-")[0]
    global_analysis = agent_state.get("global_analysis")
    global_image = global_analysis.get("image_path") if isinstance(global_analysis, Mapping) else None
    return {
        "thumbnail": _first_existing(
            root / "global_thumbnail.jpg",
            agent_state.get("global_thumbnail_path"),
            global_image,
            root / "step_0_thumbnail.jpg",
        ),
        "red": _first_existing(root / f"red_{red_id}.jpg", _node_image(agent_state, red_id)),
        "blue": _first_existing(root / f"blue_{parent_id}.jpg", _node_image(agent_state, parent_id)),
        "green": _first_existing(
            analysis.get("image_path"),
            roi.get("image_path"),
            root / f"green_{roi_id}.jpg",
            root / red_id / f"{roi_id}_160x.jpg",
            root / f"{roi_id}_160x.jpg",
        ),
    }


def _normalise_answer(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


def _gate(
    tool_call: Mapping[str, Any],
    agent_state: Mapping[str, Any],
    refs: Mapping[str, Mapping[str, Any]],
    image_paths: Mapping[str, Optional[str]],
) -> Dict[str, Any]:
    roi_id = str(tool_call.get("roi_id") or "")
    evidence_index = _evidence_index(agent_state, refs)
    valid_ids = set(evidence_index)
    support = {str(value) for value in tool_call.get("supporting_roi_ids", []) if value}
    conflict = {str(value) for value in tool_call.get("conflicting_roi_ids", []) if value}
    invalid = sorted((support | conflict) - valid_ids)
    other_support = sorted((support & valid_ids) - {roi_id})
    counterfactuals_differ = bool(
        _normalise_answer(tool_call.get("answer_if_confirmed"))
        and _normalise_answer(tool_call.get("answer_if_rejected"))
        and _normalise_answer(tool_call.get("answer_if_confirmed"))
        != _normalise_answer(tool_call.get("answer_if_rejected"))
    )
    claim = str(tool_call.get("claim") or "").strip()
    context_count = sum(bool(image_paths.get(key)) for key in ("thumbnail", "red", "blue"))
    checks = {
        "material_to_single_slide_answer": counterfactuals_differ,
        "target_has_linked_first_pass_support": roi_id in support and bool(evidence_index.get(roi_id, "").strip()),
        "isolated_or_evidence_conflict": not other_support or bool(conflict & valid_ids),
        "precise_image_checkable_claim": 3 <= len(claim) <= 1200 and bool(tool_call.get("image_checkable")) and not bool(tool_call.get("requires_nonvisual_data")),
        "target_and_context_images_available": bool(image_paths.get("green")) and context_count >= 1,
        "all_evidence_ids_valid": not invalid,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "passed": not failed,
        "failed_checks": failed,
        "checks": checks,
        "metrics": {
            "supporting_evidence_ids": sorted(support & valid_ids),
            "other_supporting_evidence_ids": other_support,
            "conflicting_evidence_ids": sorted(conflict & valid_ids),
            "invalid_evidence_ids": invalid,
            "context_image_count": context_count,
            "available_images": {key: bool(image_paths.get(key)) for key in ("thumbnail", "red", "blue", "green")},
        },
    }


def _marked_thumbnail(
    thumbnail: str,
    rect: Mapping[str, Any],
    slide_dimensions: Any,
    output_path: Path,
) -> Optional[str]:
    try:
        if not thumbnail or not Path(thumbnail).exists() or not rect or not slide_dimensions:
            return None
        slide_w, slide_h = float(slide_dimensions[0]), float(slide_dimensions[1])
        if slide_w <= 0 or slide_h <= 0:
            return None
        image = Image.open(thumbnail).convert("RGB")
        thumb_w, thumb_h = image.size
        sx, sy = thumb_w / slide_w, thumb_h / slide_h
        x = int(float(rect.get("x", 0)) * sx)
        y = int(float(rect.get("y", 0)) * sy)
        width = max(6, int(float(rect.get("w", 0)) * sx))
        height = max(6, int(float(rect.get("h", 0)) * sy))
        pad = max(8, int(min(thumb_w, thumb_h) * 0.02))
        box = [
            max(0, x - pad),
            max(0, y - pad),
            min(thumb_w - 1, x + width + pad),
            min(thumb_h - 1, y + height + pad),
        ]
        draw = ImageDraw.Draw(image)
        for offset in range(3):
            draw.rectangle(
                [box[0] - offset, box[1] - offset, box[2] + offset, box[3] + offset],
                outline=(255, 0, 0),
            )
        draw.text((box[0], max(0, box[1] - 14)), "REVIEW", fill=(255, 0, 0))
        image.save(output_path, "JPEG", quality=90)
        return str(output_path)
    except Exception as exc:
        logger.debug("[secondary_review] thumbnail marking failed: %s", exc)
        return None


def _review_prompt(
    roi_id: str,
    rect: Mapping[str, Any],
    diagnostic_question: str,
    claim: str,
    first_pass: str,
    lang: str,
) -> str:
    position = f"x={rect.get('x')}, y={rect.get('y')}" if rect else ""
    if _normalize_lang(lang) == "en":
        return f"""You are an independent senior pathologist performing a SECOND LOOK of one focal claim from a WSI agent.

Current single-slide diagnostic task: {diagnostic_question}
Candidate ROI: {roi_id} {position}
Claim to adjudicate: {claim}
First-pass evidence: {first_pass or 'Not recorded'}

Images are ordered as marked whole-slide location → red lower-power context → blue intermediate-power context → green target 160× field; unavailable context levels are omitted. Review independently across scales.

Judge only the exact visible claim. Check cross-scale consistency, location, quality, representativeness, and plausible mimics/artifacts. Do not introduce an organ, disease, grade, stage, invasion target, biomarker, or special finding absent from the task and evidence. A selected high-power field may establish a local feature, but not a whole-slide proportion, extent, grade, or patient-level diagnosis.

CONFIRMED requires direct concordant evidence for the exact claim. REJECTED means the images contradict it or support a mimic/artifact. INDETERMINATE means the images cannot resolve it.

Give a concise reason, then end with exactly one separate line:
VERDICT: CONFIRMED
VERDICT: REJECTED
VERDICT: INDETERMINATE"""
    return f"""你是独立资深病理医生，正在对 WSI Agent 的一个局灶主张进行二次复核。

当前单片诊断任务：{diagnostic_question}
候选 ROI：{roi_id} {position}
待裁决主张：{claim}
首次分析证据：{first_pass or '未记录'}

图像按以下顺序排列：标出目标位置的全切片定位图 → 红框低倍上下文 → 蓝框中倍上下文 → 绿框目标 160× 视野；不可用层级会省略。请跨倍率独立复核。

只裁决上述可见的精确主张。核对跨倍率一致性、位置、质量、代表性及合理模拟病变/伪影。不得引入任务和证据中不存在的器官、病种、分级、分期、浸润目标、生物标志物或特殊发现。主动选择的高倍视野可确认局部特征，但不能单独证明全片比例、范围、分级或患者级诊断。

CONFIRMED 表示该精确主张有直接且一致的证据；REJECTED 表示图像反对该主张或更支持模拟/伪影；INDETERMINATE 表示现有图像不能解决。

先给出简洁理由，最后必须单独一行写三者之一：
二次复核结论: CONFIRMED
二次复核结论: REJECTED
二次复核结论: INDETERMINATE"""


def _run_review(
    roi_id: str,
    ref: Mapping[str, Any],
    image_paths: Mapping[str, Optional[str]],
    diagnostic_question: str,
    claim: str,
    compose_images: Callable[[list], Optional[str]],
    lang: str,
    slide_dimensions: Any,
    steps_dir: Path,
) -> Dict[str, str]:
    try:
        thumbnail = image_paths.get("thumbnail")
        if thumbnail:
            safe_roi_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", roi_id)
            thumbnail = _marked_thumbnail(
                thumbnail,
                ref.get("roi", {}).get("rect", {}),
                slide_dimensions,
                steps_dir / f"_review_mark_{safe_roi_id}.jpg",
            ) or thumbnail
        ordered = []
        for key, label in (
            ("thumbnail", "Whole-slide location (red mark)"),
            ("red", "Lower-power parent context"),
            ("blue", "Intermediate-power parent context"),
            ("green", f"Target 160x field: {roi_id}"),
        ):
            path = thumbnail if key == "thumbnail" else image_paths.get(key)
            if path and Path(path).exists():
                ordered.append((path, label))
        if not image_paths.get("green") or len(ordered) < 2:
            return {"verdict": "SKIP", "reason": "target image or contextual image is unavailable"}
        composed = compose_images(
            [(path, f"[{index}] {label}") for index, (path, label) in enumerate(ordered, 1)]
        )
        if not composed:
            return {"verdict": "SKIP", "reason": "image composition failed"}
        first_pass = _analysis_text(ref.get("analysis", {}))[:6000]
        response = call_gpt_vision(
            _review_prompt(
                roi_id,
                ref.get("roi", {}).get("rect", {}),
                diagnostic_question,
                claim,
                first_pass,
                lang,
            ),
            composed,
        )
        if not response:
            return {"verdict": "SKIP", "reason": "empty review response"}
        match = re.search(
            r"(?:二次复核结论|VERDICT)\s*[:：]\s*(CONFIRMED|REJECTED|INDETERMINATE)",
            str(response),
            re.IGNORECASE,
        )
        if not match:
            return {"verdict": "SKIP", "reason": "unparseable verdict: " + str(response).strip()[:400]}
        return {"verdict": match.group(1).upper(), "reason": str(response).strip()[:1600]}
    except Exception as exc:
        logger.warning("[secondary_review] execution failed: %s", exc)
        return {"verdict": "SKIP", "reason": f"exception: {exc}"}


def _apply_review(
    analysis: Dict[str, Any], review: Mapping[str, Any], claim: str, roi_id: str, lang: str
) -> None:
    """Preserve first pass, then add adjudication to the live terminal record."""
    evidence = analysis.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        analysis["evidence"] = evidence
    verdict = str(review.get("verdict") or "SKIP").upper()
    reason = str(review.get("reason") or "")[:1600]
    record = {
        "verdict": verdict,
        "claim": str(claim or ""),
        "reason": reason,
        "roi_id": str(roi_id),
    }
    evidence["secondary_review"] = record
    analysis["secondary_review"] = record
    analysis["secondary_review_verdict"] = verdict

    if _normalize_lang(lang) == "en":
        note = (
            f"[Secondary review: {verdict}] ROI {roi_id}'s nominated claim "
            + ("was independently confirmed." if verdict == "CONFIRMED" else "is not accepted as a confirmed conclusion.")
        )
    else:
        note = (
            f"【二次复核：{verdict}】ROI {roi_id} 的候选主张"
            + ("经独立复核确认。" if verdict == "CONFIRMED" else "不作为已确认结论采纳。")
        )
    original_reasoning = str(analysis.get("reasoning") or "")
    if verdict != "CONFIRMED":
        record["first_pass_snapshot"] = {
            "reasoning": original_reasoning,
            "findings": str(analysis.get("findings") or ""),
            "diagnosis_suggestion": str(analysis.get("diagnosis_suggestion") or ""),
            "confidence": analysis.get("confidence"),
            "confidence_level": analysis.get("confidence_level"),
            "evidence_confidence": analysis.get("evidence_confidence"),
        }
        analysis["secondary_review_claim_status"] = "not_confirmed"
        downgraded = 0 if verdict == "REJECTED" else 1
        for key in ("confidence_level", "evidence_confidence"):
            if key in analysis:
                try:
                    analysis[key] = min(int(analysis.get(key) or 0), downgraded)
                except (TypeError, ValueError):
                    analysis[key] = downgraded
        if "confidence" in analysis:
            try:
                current = float(analysis.get("confidence") or 0)
                analysis["confidence"] = min(current, downgraded if current <= 4 else downgraded * 25)
            except (TypeError, ValueError):
                analysis["confidence"] = downgraded
    analysis["reasoning"] = "\n\n".join(
        value for value in (original_reasoning, note, reason[:800]) if value
    )[:16000]


def _emit_tool_event(
    socketio: Any,
    room: Any,
    call_id: str,
    status: str,
    arguments: Mapping[str, Any],
    result: Optional[Mapping[str, Any]] = None,
    error: str = "",
) -> None:
    if not socketio or not room:
        return
    payload = {
        "call_id": call_id,
        "status": status,
        "phase": "secondary_review",
        "kind": "api_native",
        "action": "function_call",
        "server": "pathcopilot_runtime",
        "tool": "secondary_review",
        "roi_id": str(arguments.get("roi_id") or ""),
        "arguments": dict(arguments),
    }
    if result is not None:
        payload["result"] = dict(result)
    if error:
        payload["error"] = str(error)
    socketio.emit("agent.native_tool", payload, to=room)


def orchestrate_secondary_reviews(
    agent_state: Dict[str, Any],
    slide: Any,
    steps_dir: Any,
    diagnostic_question: str,
    compose_images: Callable[[list], Optional[str]],
    socketio: Any = None,
    room: Any = None,
    limit: Optional[int] = None,
) -> list[Dict[str, Any]]:
    """Review selected focal claims for one WSI before its summary is built."""
    if not _enabled():
        agent_state["secondary_review_plan"] = []
        agent_state["secondary_review_results"] = []
        return []
    limit = secondary_review_limit() if limit is None else min(8, max(0, int(limit)))
    refs = _terminal_refs(agent_state)
    plan = _plan_reviews(agent_state, refs, str(diagnostic_question or "").strip(), limit)
    if not plan:
        # Zero calls is a valid Orchestrator decision. Do not manufacture a
        # NOT_RUN verdict and do not alter any first-pass claim.
        agent_state["secondary_review_plan"] = []
        agent_state["secondary_review_results"] = []
        logger.info("[secondary_review] Orchestrator selected zero native calls")
        return []

    root = Path(steps_dir)
    lang = _normalize_lang(agent_state.get("lang", "zh"))
    results = []
    audited_plan = []
    for item in plan:
        roi_id = item["roi_id"]
        ref = refs.get(roi_id)
        call_id = item.get("tool_call_id") or f"secondary_review_{uuid.uuid4().hex}"
        public_arguments = {key: value for key, value in item.items() if key != "tool_call_id"}
        _emit_tool_event(socketio, room, call_id, "running", public_arguments)
        try:
            paths = _image_paths(agent_state, ref or {}, roi_id, root) if ref else {}
            if not ref:
                gate = {"passed": False, "failed_checks": ["target_roi_exists"]}
                outcome = {
                    "tool_status": "rejected_by_gate",
                    "verdict": "NOT_RUN",
                    "review_reason": "target ROI is absent from terminal evidence",
                    "gate": gate,
                }
            else:
                gate = _gate(item, agent_state, refs, paths)
                if not gate["passed"]:
                    review = {
                        "verdict": "NOT_RUN",
                        "reason": "code gate: " + ", ".join(gate["failed_checks"]),
                    }
                    _apply_review(ref["analysis"], review, item["claim"], roi_id, lang)
                    outcome = {
                        "tool_status": "rejected_by_gate",
                        "verdict": "NOT_RUN",
                        "review_reason": review["reason"],
                        "gate": gate,
                    }
                else:
                    review = _run_review(
                        roi_id,
                        ref,
                        paths,
                        str(diagnostic_question or ""),
                        item["claim"],
                        compose_images,
                        lang,
                        getattr(slide, "dimensions", None),
                        root,
                    )
                    _apply_review(ref["analysis"], review, item["claim"], roi_id, lang)
                    outcome = {
                        "tool_status": "executed",
                        "verdict": review.get("verdict", "SKIP"),
                        "review_reason": review.get("reason", ""),
                        "gate": gate,
                    }
        except Exception as exc:
            review = {"verdict": "NOT_RUN", "reason": f"execution exception: {exc}"}
            if ref and isinstance(ref.get("analysis"), dict):
                _apply_review(ref["analysis"], review, item["claim"], roi_id, lang)
            outcome = {
                "tool_status": "execution_error",
                "verdict": "NOT_RUN",
                "review_reason": review["reason"],
                "gate": {"passed": False, "failed_checks": ["tool_execution"]},
            }
        result = {**public_arguments, **outcome}
        results.append(result)
        audited_plan.append({**public_arguments, "gate": outcome.get("gate", {})})
        if outcome.get("tool_status") == "executed":
            _emit_tool_event(socketio, room, call_id, "succeeded", public_arguments, result=outcome)
        else:
            _emit_tool_event(
                socketio,
                room,
                call_id,
                "failed",
                public_arguments,
                result=outcome,
                error=outcome.get("review_reason", "review was not run"),
            )
        logger.info("[secondary_review] %s -> %s", roi_id, outcome.get("verdict"))

    agent_state["secondary_review_plan"] = audited_plan
    agent_state["secondary_review_results"] = results
    return results


__all__ = [
    "SECONDARY_REVIEW_TOOL",
    "orchestrate_secondary_reviews",
    "secondary_review_honor_rule",
    "secondary_review_limit",
]
