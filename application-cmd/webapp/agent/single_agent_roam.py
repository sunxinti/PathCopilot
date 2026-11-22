# -*- coding: utf-8 -*-
"""Independent single-Agent WSI navigation route.

This module intentionally does not import or emulate the red/blue/green
Subagent tree.  One model navigates one WSI, one FOV at a time, then produces
one evidence-linked report from its own trajectory.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import io
import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from typing import Any

import openslide
from PIL import Image, ImageDraw, ImageFont


logger = logging.getLogger(__name__)
_ZOOMS = {10, 40, 160}
_COLORS = {10: (255, 0, 0), 40: (0, 102, 255), 160: (0, 180, 0)}
_ROAM_STATE_LOCK = threading.RLock()
_ACTIVE_ROAM_STATES: dict[str, dict[str, Any]] = {}


def _independent_fov_count(trajectory: list[dict[str, Any]]) -> int:
    """Count independently sampled spatial locations, not zooms on one path."""
    locations: set[tuple[int, int]] = set()
    for item in trajectory:
        if item.get("rect") is None:
            continue
        try:
            locations.add((round(float(item.get("x"))), round(float(item.get("y")))))
        except (TypeError, ValueError):
            continue
    return len(locations)


def _calibrate_sparse_report(report: str, trajectory: list[dict[str, Any]], lang: str) -> str:
    """Fail closed when a subtype conclusion comes from only one spatial path."""
    text = str(report or "")
    if _independent_fov_count(trajectory) >= 2:
        return text
    replacements = (
        (
            ("病理分型", "形态学倾向（未确立）"),
            ("形态学高度典型", "形态学特征具有提示性"),
            ("完全符合", "形态学倾向"),
            ("高度符合", "形态学倾向"),
            ("高度支持", "倾向支持"),
            ("确诊为", "形态学倾向于"),
            ("确定为", "形态学倾向于"),
        )
        if lang != "en" else
        (
            ("Final diagnosis", "Morphologic impression (not established)"),
            ("Diagnosis", "Morphologic impression"),
            ("diagnostic of", "morphologically favors"),
            ("definitively confirms", "supports"),
            ("consistent with", "favors"),
            ("establishes", "supports"),
            ("definite ", "favored "),
        )
    )
    for source, target in replacements:
        text = text.replace(source, target)
    notice = (
        "**结论强度校准**：本次仅覆盖一个独立空间路径；特定组织学亚型只能作为形态学倾向，需更多独立区域及必要的免疫组化/分子证据确认。\n\n"
        if lang != "en" else
        "**Conclusion calibration**: only one independent spatial path was sampled. A specific histologic subtype is favored, not established, pending additional independent regions and appropriate ancillary testing.\n\n"
    )
    return notice + text


class RoamProtocolError(ValueError):
    """The model did not return the strict navigation contract."""


def get_roam_snapshot(room: str) -> dict[str, Any] | None:
    """Return a public, process-local UI snapshot for one Socket room.

    Only fields already emitted through ``agent.roam_*`` are retained.  The
    snapshot is used after a browser refresh; it is not a second diagnostic
    memory and never contains model messages, prompts, slide handles or API
    credentials.
    """
    key = str(room or "").strip()
    if not key:
        return None
    with _ROAM_STATE_LOCK:
        state = _ACTIVE_ROAM_STATES.get(key)
        return copy.deepcopy(state) if isinstance(state, dict) else None


def _replace_roam_state(room: str, payload: dict[str, Any]) -> None:
    with _ROAM_STATE_LOCK:
        _ACTIVE_ROAM_STATES[str(room)] = copy.deepcopy(payload)


def _append_roam_timeline(room: str, kind: str, payload: dict[str, Any]) -> None:
    public_payload = copy.deepcopy(payload)
    with _ROAM_STATE_LOCK:
        state = _ACTIVE_ROAM_STATES.get(str(room))
        if not isinstance(state, dict):
            return
        state.setdefault("timeline", []).append({
            "kind": str(kind),
            "payload": public_payload,
        })
        state["current_step"] = int(public_payload.get("step") or 0)
        if kind == "action":
            state["current_action"] = public_payload
        elif kind == "step":
            state["current_action"] = None


def _finish_roam_state(room: str, *, status: str, stopped_by: str = "") -> None:
    with _ROAM_STATE_LOCK:
        state = _ACTIVE_ROAM_STATES.get(str(room))
        if not isinstance(state, dict):
            return
        state["active"] = False
        state["status"] = str(status)
        state["stopped_by"] = str(stopped_by or "")
        state["current_action"] = None


def _emit(
    socketio,
    event: str,
    payload: dict[str, Any],
    room: str,
    *,
    patient_event_sink=None,
) -> None:
    socketio.emit(event, payload, to=room)
    if callable(patient_event_sink) and (
        event.startswith('agent.roam_') or event == 'agent.progress'
    ):
        patient_event_sink(event, payload)


def _image_b64(image: Image.Image, *, quality: int = 88, max_side: int = 1536) -> str:
    rgb = image.convert("RGB")
    if max(rgb.size) > max_side:
        scale = max_side / max(rgb.size)
        rgb = rgb.resize(
            (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
            Image.Resampling.LANCZOS,
        )
    stream = io.BytesIO()
    rgb.save(stream, format="JPEG", quality=quality)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _font(size: int):
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/mnt/c/Windows/Fonts/arialbd.ttf",
    ):
        try:
            if Path(candidate).is_file():
                return ImageFont.truetype(candidate, size)
        except Exception:
            logger.debug("Cannot load roam grid font %s", candidate, exc_info=True)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _agent_overview_cache_path(file_id: str) -> Path | None:
    token = str(file_id or "").strip()
    if not token:
        return None
    return Path("outputs") / token / "cached_thumbnail_2000.jpg"


def _overview_source_image(slide, long_side: int = 2000, file_id: str = "") -> Image.Image:
    """Reuse the exact 2000px overview Swarm builds for a single WSI.

    Same behaviour as the Swarm single-WSI path in filter_rois_by_gpt_vision:
    read the shared outputs/{file_id}/cached_thumbnail_2000.jpg if present,
    otherwise call get_thumbnail once and cache the result for next time.
    """
    from . import agent_loop as loop

    cache_path = _agent_overview_cache_path(file_id)
    if cache_path and cache_path.is_file():
        started = time.monotonic()
        image = Image.open(cache_path).convert("RGB")
        logger.info(
            "[Single Agent] 使用已有 2000px 概览缓存: %s size=%s 耗时=%.1fs",
            cache_path, image.size, time.monotonic() - started,
        )
        return image

    started = time.monotonic()
    with loop._openslide_lock:
        image = slide.get_thumbnail((long_side, long_side)).convert("RGB")
    logger.info(
        "[Single Agent] get_thumbnail 完成: %s size=%s 耗时=%.1fs",
        file_id or "?", image.size, time.monotonic() - started,
    )

    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(cache_path, "JPEG", quality=90)
            logger.info("[Single Agent] 已写入 2000px 概览缓存: %s", cache_path)
        except Exception:
            logger.debug("Saving overview cache failed", exc_info=True)
    return image


def _make_overview(slide, long_side: int = 2000, file_id: str = "") -> Image.Image:
    logger.info("[Single Agent] 开始生成整片概览: %s", file_id or "?")
    image = _overview_source_image(slide, long_side, file_id=file_id)
    draw = ImageDraw.Draw(image)
    font = _font(max(12, image.width // 90))
    for value in range(0, 1001, 100):
        x = int(value / 1000 * image.width)
        y = int(value / 1000 * image.height)
        draw.line([(x, 0), (x, image.height)], fill=(235, 180, 0), width=1)
        draw.line([(0, y), (image.width, y)], fill=(235, 180, 0), width=1)
        if value % 200 == 0:
            draw.text((min(x + 2, image.width - 35), 2), str(value), fill=(210, 0, 0), font=font)
            draw.text((2, min(y + 2, image.height - 18)), str(value), fill=(210, 0, 0), font=font)
    return image


def _overview_with_visited(
    overview: Image.Image,
    visited: list[dict[str, Any]],
    dimensions: tuple[int, int],
) -> Image.Image:
    if not visited:
        return overview
    width, height = dimensions
    image = overview.copy()
    draw = ImageDraw.Draw(image)
    font = _font(max(12, image.width // 95))
    for index, item in enumerate(visited, 1):
        rect = item["rect"]
        x0 = int(rect["x"] / width * image.width)
        y0 = int(rect["y"] / height * image.height)
        x1 = int((rect["x"] + rect["w"]) / width * image.width)
        y1 = int((rect["y"] + rect["h"]) / height * image.height)
        color = _COLORS.get(item["zoom"], (255, 150, 0))
        draw.rectangle((x0, y0, x1, y1), outline=color, width=3)
        draw.text((x0 + 3, y0 + 2), str(index), fill=color, font=font)
    return image


def _fov_side(max_dimension: int, zoom: int) -> float:
    return (max_dimension / 5.0) * (10.0 / zoom)


def _render_fov(slide, metadata: dict[str, Any], x_norm: int, y_norm: int, zoom: int):
    from . import agent_loop as loop

    width, height = metadata["dimensions"]
    side = int(max(64, min(_fov_side(max(width, height), zoom), max(width, height))))
    center_x = x_norm / 1000.0 * width
    center_y = y_norm / 1000.0 * height
    x = max(0, min(int(round(center_x - side / 2)), max(0, width - 1)))
    y = max(0, min(int(round(center_y - side / 2)), max(0, height - 1)))
    w = max(1, min(side, width - x))
    h = max(1, min(side, height - y))
    target = 1536
    level = 0
    for candidate, downsample in enumerate(metadata["level_downsamples"]):
        if max(w, h) / float(downsample) >= target:
            level = candidate
        else:
            break
    downsample = float(metadata["level_downsamples"][level])
    read_w = max(1, int(math.ceil(w / downsample)))
    read_h = max(1, int(math.ceil(h / downsample)))
    crop_at = time.monotonic()
    image = loop.safe_read_region(slide, (x, y), level, (read_w, read_h)).convert("RGB")
    logger.info(
        "[Single Agent] FOV crop %s× level=%s read=%sx%s 耗时=%.1fs",
        zoom, level, read_w, read_h, time.monotonic() - crop_at,
    )
    if max(image.size) > target:
        scale = target / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return image, {"x": x, "y": y, "w": w, "h": h}


def parse_roam_action(response: str) -> dict[str, Any]:
    """Parse one strict navigation action without truthy-string coercion."""
    text = str(response or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    decoder = json.JSONDecoder()
    payload = None
    for match in re.finditer(r"\{", text):
        try:
            candidate, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise RoamProtocolError("response does not contain a JSON object")
    observation = payload.get("observation")
    sufficient = payload.get("sufficient")
    next_step = payload.get("next")
    if not isinstance(observation, str) or not observation.strip():
        raise RoamProtocolError("observation must be a non-empty string")
    if type(sufficient) is not bool:
        raise RoamProtocolError("sufficient must be a JSON boolean")
    if not isinstance(next_step, dict):
        raise RoamProtocolError("next must be an object")
    action = str(next_step.get("action") or "").strip().lower()
    if action not in {"look", "stop"}:
        raise RoamProtocolError("next.action must be look or stop")
    if sufficient and action != "stop":
        raise RoamProtocolError("sufficient=true requires next.action=stop")
    result = {
        "observation": observation.strip(),
        "sufficient": sufficient,
        "action": action,
        "why": str(next_step.get("why") or "").strip(),
    }
    if action == "look":
        for key in ("x", "y"):
            value = next_step.get(key)
            if type(value) is not int or not 0 <= value <= 1000:
                raise RoamProtocolError(f"next.{key} must be an integer from 0 to 1000")
            result[key] = value
        zoom = next_step.get("zoom")
        if type(zoom) is not int or zoom not in _ZOOMS:
            raise RoamProtocolError("next.zoom must be exactly 10, 40, or 160")
        result["zoom"] = zoom
    else:
        result.update({"x": None, "y": None, "zoom": None})
    return result


def _language(lang: str) -> str:
    return "en" if str(lang or "").lower().startswith("en") else "zh"


def _navigation_prompt(lang: str, question: str) -> str:
    if lang == "en":
        goal = question or "Provide an organ-agnostic morphological assessment of this WSI."
        return f"""You are one pathologist autonomously navigating one H&E whole-slide image. No Subagents exist in this route. You may inspect one FOV at a time. The overview uses normalized coordinates 0-1000 (x right, y down). Choose zoom only from 10, 40, 160.

Diagnostic goal: {goal}

Scale discipline: use 10x for architecture, distribution and representative-region localization; 40x for growth pattern and tissue relationships; 160x for directly visible cytology and local structures. Each FOV response is an evidence note, not the final diagnosis: do not name a specific histologic subtype or organ-specific diagnosis from one FOV unless directly visible positive features support it and the cross-scale observations agree; otherwise retain it only as a differential. Do not infer a lineage merely by exclusion. A selected high-power FOV cannot by itself establish whole-slide proportions, grade, invasion extent or a patient-level conclusion. If morphology is insufficient, preserve uncertainty.

Return strict JSON only:
{{"observation":"current visible morphology","sufficient":false,"next":{{"action":"look","x":500,"y":500,"zoom":10,"why":"reason"}}}}
When no further FOV is useful, set sufficient=true and next.action="stop"."""
    goal = question or "对本张 WSI 进行器官中立的形态学评估。"
    return f"""你是独立运行的单一病理 Agent，正在自主游走一张 H&E 全切片；本路线不存在 Subagent。你每次只能查看一个视野。概览采用 0-1000 归一化坐标（x 向右、y 向下），倍率只能选择 10、40、160。

诊断目标：{goal}

倍率纪律：10×用于整体架构、分布与代表性区域定位；40×用于生长方式和组织关系；160×用于当前视野中可直接辨认的细胞学和局灶结构。每个视野的输出只是证据记录，不是最终诊断：除非当前视野存在可直接指认的正向特征且跨倍率证据一致，不得凭单个视野命名特定组织学亚型或器官特异诊断；否则只能保留为鉴别方向。不得仅靠排除法反推谱系；定向选择的单个高倍视野不能单独决定全片比例、分级、浸润范围或患者级结论。证据不足时必须保留不确定性。

只返回严格 JSON：
{{"observation":"当前可见形态","sufficient":false,"next":{{"action":"look","x":500,"y":500,"zoom":10,"why":"原因"}}}}
若继续观察已无价值，将 sufficient 设为 true，并令 next.action="stop"。"""


def _current_scale_guidance(zoom: int, lang: str) -> str:
    """Return a short, organ-agnostic contract for the FOV being viewed."""
    if lang == "en":
        if zoom == 10:
            return (
                "Current FOV role (10x): describe architecture, distribution, boundaries and "
                "tissue relationships. Do not make definitive cytologic, lineage, grade or "
                "whole-slide extent claims from this FOV."
            )
        if zoom == 40:
            return (
                "Current FOV role (40x): describe growth pattern and tissue relationships. "
                "Do not label dense dark areas as definite carcinoma nests or malignant cells, "
                "and do not finalize lineage or grade before 160x verification. Use suspicious, "
                "favored, or requires high-power confirmation when the key cellular relationship "
                "is not directly resolved."
            )
        return (
            "Current FOV role (160x): first assess image adequacy, then record directly visible "
            "positive differentiation clues, cytologic/behavioral abnormalities, and relevant "
            "mimics. Record specific histologic subtypes as differentials rather than conclusions "
            "unless diagnostic positive features are directly visible and cross-scale evidence agrees. "
            "Preserve uncertainty when these relationships are not resolved; this selected "
            "FOV still cannot establish whole-slide proportions, grade or invasion extent alone."
        )
    if zoom == 10:
        return (
            "当前视野角色（10×）：描述整体架构、分布、边界及组织关系；不得凭本视野作出"
            "确定的细胞性质、谱系、分级或全片范围结论。"
        )
    if zoom == 40:
        return (
            "当前视野角色（40×）：描述生长方式和组织关系；不得把深染密集区直接写成明确癌巢或"
            "恶性细胞，也不得在 160× 核验前确定谱系或分级。关键细胞关系不能直接辨认时，应使用"
            "“可疑 / 倾向 / 需高倍确认”等证据分级语言。"
        )
    return (
        "当前视野角色（160×）：先核验图像可评估性，再记录直接可见的正向分化线索、细胞学或"
        "恶性行为证据及当前相关模拟物；缺乏诊断性正向特征或跨倍率一致性时，特定组织学亚型只能"
        "列作鉴别、不能写成当前视野结论。关键关系不能辨认时保留不确定性。该定向高倍视野仍不能"
        "单独决定全片比例、分级或浸润范围。"
    )


def _trajectory_text(trajectory: list[dict[str, Any]], lang: str) -> str:
    heading = "Collected observations:" if lang == "en" else "已采集观察："
    lines = [heading]
    for item in trajectory:
        if item.get("rect"):
            location = f"{item['zoom']}x @({item['x']},{item['y']})"
        else:
            location = "overview"
        lines.append(f"- [{location}] {item['observation']}")
    return "\n".join(lines)


def _call_navigation(messages, *, metadata: dict[str, Any]):
    from .gpt_client import call_agent_model

    last_error = None
    current = list(messages)
    for attempt in range(2):
        response, error = call_agent_model(
            current,
            max_tokens=4096,
            max_retries=3,
            call_type="single_agent_roam",
            metadata={**metadata, "protocol_attempt": attempt + 1},
        )
        if error:
            last_error = error
            continue
        try:
            return parse_roam_action(response)
        except RoamProtocolError as exc:
            last_error = str(exc)
            current = current + [
                {"role": "assistant", "content": str(response or "")},
                {
                    "role": "user",
                    "content": (
                        "Your response violated the JSON navigation contract: "
                        f"{exc}. Return one corrected JSON object only."
                    ),
                },
            ]
    raise RoamProtocolError(last_error or "navigation call failed")


def _write_report_html(
    output_dir: Path,
    report: str,
    trajectory: list[dict[str, Any]],
    filename: str,
) -> None:
    cards = []
    for item in trajectory:
        step_image = item.get("image")
        image_html = (
            f'<img src="steps/{html.escape(step_image)}" alt="FOV">'
            if step_image else '<img src="steps/overview.jpg" alt="Overview">'
        )
        cards.append(
            "<article>"
            f"<h3>{html.escape(item['label'])}</h3>{image_html}"
            f"<p>{html.escape(item['observation'])}</p>"
            f"<small>{html.escape(item.get('why') or '')}</small>"
            "</article>"
        )
    document = f"""<!doctype html><html><head><meta charset="utf-8"><title>Single Agent Roam</title>
<style>body{{font-family:system-ui;margin:32px;background:#faf8f4;color:#29241f}}main{{max-width:1100px;margin:auto}}.route,.roam-route{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}article,.report{{background:white;border:1px solid #e6ded3;border-radius:14px;padding:16px;box-shadow:0 3px 16px #0000000b}}img{{width:100%;border-radius:8px}}pre{{white-space:pre-wrap;font-family:inherit;line-height:1.6}}</style></head><body><div class="content"><main>
<h1>Single Agent Roam</h1><p>{html.escape(filename)}</p><section class="route roam-route">{''.join(cards)}</section>
<section class="report"><h2>Final report</h2><pre>{html.escape(report)}</pre></section></main></div><div class="footer"></div></body></html>"""
    (output_dir / "pathology_report.html").write_text(document, encoding="utf-8")


def run_single_agent_roam(
    *,
    socketio,
    room: str,
    file_id: str,
    max_steps: int = 12,
    lang: str = "zh",
    diagnostic_question: str = "",
    specimen_context: str = "",
    runtime_run_id: str = "",
    patient_level_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the isolated single-Agent navigation route for one WSI."""
    from . import agent_loop as loop
    from .gpt_client import call_agent_model

    lang = _language(lang)
    step_limit = max(1, min(int(max_steps or 12), 30))
    file_path = loop.get_wsi_file_path(file_id)
    if not file_path:
        raise FileNotFoundError(f"WSI file not found for {file_id}")
    filename = Path(file_path).name
    patient_context = dict(patient_level_context or {})
    patient_event_sink = patient_context.get('_patient_event_sink')
    paths = loop.get_output_paths(
        file_id,
        "single_agent_roam",
        patient_context.get('patient_id'),
        filename=filename,
    )
    output_dir = Path(paths["output_dir"])
    steps_dir = Path(paths["steps_dir"])
    if not patient_context.get('patient_id'):
        # Standalone Single-Agent runs branch before the Swarm output setup.
        # Bind the readable output object here as well so the authenticated
        # /outputs route can serve the report it just generated.
        loop._write_single_output_owner_manifest(paths["base_dir"], file_id)
    steps_dir.mkdir(parents=True, exist_ok=True)
    slide = None
    trajectory: list[dict[str, Any]] = []
    if patient_context.get('preserve_stop_flag'):
        if loop.agent_stop_flags.get(room, False):
            logger.info('Patient roam worker cancelled before startup: %s', room)
            return None
    else:
        loop.agent_stop_flags[room] = False
    started_payload = {
        "file_id": file_id,
        "strategy": "single_agent_roam",
        "max_steps": step_limit,
    }
    _replace_roam_state(room, {
        "active": True,
        "status": "running",
        "file_id": str(file_id),
        "runtime_run_id": str(runtime_run_id or ""),
        "strategy": "single_agent_roam",
        "max_steps": step_limit,
        "current_step": 0,
        "current_action": None,
        "started": copy.deepcopy(started_payload),
        "timeline": [],
    })
    _emit(
        socketio, "agent.roam_started", started_payload, room,
        patient_event_sink=patient_event_sink,
    )
    _emit(socketio, "agent.progress", {
        "percentage": 2,
        "status": "single_agent_roam",
        "message": (
            "Opening the slide and building the overview…"
            if lang == "en" else
            "正在打开切片并生成整片概览…"
        ),
    }, room, patient_event_sink=patient_event_sink)
    try:
        opened_at = time.monotonic()
        logger.info("[Single Agent] 打开切片: %s", file_id)
        slide = openslide.OpenSlide(file_path)
        metadata = loop.safe_get_slide_metadata(slide)
        dimensions = tuple(metadata["dimensions"])
        logger.info(
            "[Single Agent] 切片已打开: %s 尺寸=%s 耗时=%.1fs",
            file_id, dimensions, time.monotonic() - opened_at,
        )
        overview_at = time.monotonic()
        overview = _make_overview(slide, file_id=file_id)
        overview.save(steps_dir / "overview.jpg", quality=88)
        logger.info(
            "[Single Agent] 整片概览已就绪: %s 图像=%s 耗时=%.1fs",
            file_id, overview.size, time.monotonic() - overview_at,
        )
        _emit(socketio, "agent.progress", {
            "percentage": 4,
            "status": "single_agent_roam",
            "message": (
                "Overview ready; choosing the first FOV…"
                if lang == "en" else
                "整片概览已就绪，正在选择第一个视野…"
            ),
        }, room, patient_event_sink=patient_event_sink)
        patient_note = ''
        if patient_context:
            patient_note = (
                f"\n\nPatient-level specimen context: part={patient_context.get('part_name') or 'unspecified'}, "
                f"slide {patient_context.get('wsi_index') or '?'} of {patient_context.get('total_wsi') or '?'}; "
                "analyze this slide independently and do not infer findings from other slides."
                if lang == 'en' else
                f"\n\n患者级取材上下文：部位={patient_context.get('part_name') or '未指定'}，"
                f"第 {patient_context.get('wsi_index') or '?'} / {patient_context.get('total_wsi') or '?'} 张；"
                "本次仅独立分析当前切片，不得从其他切片反推当前形态。"
            )
        trusted_specimen_note = str(specimen_context or '').strip()
        prompt = _navigation_prompt(
            lang,
            str(diagnostic_question or "").strip(),
        )
        if trusted_specimen_note:
            already_phrased = any(marker in trusted_specimen_note for marker in (
                'Trusted specimen context',
                '可信标本背景',
                'User-declared sampling site',
                '用户声明的取材部位',
                'sampling site has not been provided',
                '尚未提供取材部位',
                'does not know the sampling site',
                '不清楚取材部位',
            ))
            if already_phrased:
                prompt += "\n\n" + trusted_specimen_note
            elif lang == 'en':
                prompt += (
                    f"\n\nTrusted specimen context (server supplied):\n{trusted_specimen_note}\n"
                    "Use it only to establish specimen source; every histologic type, grade, "
                    "invasion, and differential conclusion must still be supported by the slide."
                )
            else:
                prompt += (
                    f"\n\n受信任的取材背景（由服务器提供）：\n{trusted_specimen_note}\n"
                    "该背景仅用于确定取材来源；组织学分型、分级、浸润及鉴别诊断仍必须由本切片形态证据支持。"
                )
        prompt += patient_note
        first_messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt + ("\n\nThe attached image is the whole-slide overview. Choose the first FOV." if lang == "en" else "\n\n附件是整片概览，请选择第一个观察视野。")},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_image_b64(overview, max_side=2000)}"}},
        ]}]
        logger.info("[Single Agent] 请求模型根据概览选择第一个视野: %s", file_id)
        nav_at = time.monotonic()
        action = _call_navigation(first_messages, metadata={"file_id": file_id, "step": 0})
        logger.info(
            "[Single Agent] 概览解读完成: %s 模型耗时=%.1fs",
            file_id, time.monotonic() - nav_at,
        )
        overview_item = {
            "step": 0,
            "label": "Overview",
            "observation": action["observation"],
            "why": action.get("why", ""),
            "rect": None,
            "zoom": None,
            "x": None,
            "y": None,
            "next": dict(action),
        }
        trajectory.append(overview_item)
        _append_roam_timeline(room, "step", overview_item)
        _emit(
            socketio, "agent.roam_step", overview_item, room,
            patient_event_sink=patient_event_sink,
        )
        _emit(socketio, "agent.progress", {
            "percentage": 5,
            "status": "single_agent_roam",
            "message": (
                "Single Agent overview complete"
                if lang == "en" else
                "整片概览已观察，开始游走"
            ),
        }, room, patient_event_sink=patient_event_sink)

        visited: list[dict[str, Any]] = []
        stopped_by = "agent"
        for step in range(1, step_limit + 1):
            if loop.agent_stop_flags.get(room):
                stopped_by = "user"
                break
            if action["action"] == "stop":
                stopped_by = "agent"
                break
            fov, rect = _render_fov(slide, metadata, action["x"], action["y"], action["zoom"])
            # Announce the navigation decision before asking the model to
            # interpret the destination.  The UI uses this event to animate
            # the viewer and to keep action and observation semantically
            # separate in the public chat transcript.
            action_payload = {
                "step": step,
                "action": "look",
                "zoom": action["zoom"],
                "x": action["x"],
                "y": action["y"],
                "rect": rect,
                "why": action.get("why", ""),
            }
            _append_roam_timeline(room, "action", action_payload)
            _emit(
                socketio, "agent.roam_action", action_payload, room,
                patient_event_sink=patient_event_sink,
            )
            image_name = f"step_{step:02d}_{action['zoom']}x.jpg"
            fov.save(steps_dir / image_name, quality=90)
            viewed = {"rect": rect, "zoom": action["zoom"]}
            context_overview = _overview_with_visited(overview, visited + [viewed], dimensions)
            history = _trajectory_text(trajectory, lang)
            instruction = prompt + "\n\n" + history
            instruction += "\n\n" + _current_scale_guidance(action["zoom"], lang)
            instruction += (
                f"\n\nImage 1 is the overview with visited FOVs; image 2 is current FOV #{step} at {action['zoom']}x. Observe this FOV and choose the next action."
                if lang == "en" else
                f"\n\n图1是标注已访视野的概览，图2是第 {step} 个当前视野（{action['zoom']}×）。请观察当前视野并选择下一动作。"
            )
            messages = [{"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_image_b64(context_overview, max_side=2000)}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_image_b64(fov)}"}},
            ]}]
            logger.info("[Single Agent] 请求模型解读视野 %s（%s×）", step, action["zoom"])
            nav_at = time.monotonic()
            next_action = _call_navigation(messages, metadata={"file_id": file_id, "step": step, "zoom": action["zoom"]})
            logger.info(
                "[Single Agent] 视野 %s 解读完成: 模型耗时=%.1fs",
                step, time.monotonic() - nav_at,
            )
            # A cooperative stop may arrive while the model request is in
            # flight.  Re-check immediately after the request returns so a
            # late observation is never projected or published as evidence.
            if loop.agent_stop_flags.get(room):
                stopped_by = "user"
                break
            item = {
                "step": step,
                "label": f"FOV {step} · {action['zoom']}×",
                "observation": next_action["observation"],
                "why": next_action.get("why", ""),
                "rect": rect,
                "zoom": action["zoom"],
                "x": action["x"],
                "y": action["y"],
                "image": image_name,
                "next": dict(next_action),
            }
            trajectory.append(item)
            visited.append(viewed)
            _append_roam_timeline(room, "step", item)
            _emit(
                socketio, "agent.roam_step", item, room,
                patient_event_sink=patient_event_sink,
            )
            _emit(socketio, "agent.progress", {
                "percentage": min(90, int(step / step_limit * 85) + 5),
                "status": "single_agent_roam",
                "message": (
                    f"Single Agent FOV {step}/{step_limit}"
                    if lang == "en" else
                    f"单 Agent 视野 {step}/{step_limit}"
                ),
            }, room, patient_event_sink=patient_event_sink)
            action = next_action
        else:
            stopped_by = "safety_cap"

        if loop.agent_stop_flags.get(room):
            stopped_by = "user"
        evidence = _trajectory_text(trajectory, lang)
        if stopped_by == "user":
            # A cancelled run keeps its audit trajectory, but it must not
            # publish a diagnostic answer or create files that look like a
            # completed pathology report.
            report = ""
        else:
            independent_fovs = _independent_fov_count(trajectory)
            synthesis = (
                "You are the same single pathology Agent. Write a concise evidence-linked final report based only on the collected observations. State uncertainty and sampling limits. Do not invent unobserved findings. The conclusion strength must not exceed the weakest necessary evidence. A high-power FOV is local cytologic evidence, never whole-slide subtype proof by itself. If fewer than two independent spatial locations were sampled, a specific subtype may only be described as favored or in the differential; do not use definite, diagnostic-of, or equivalent wording. Separate morphology from ancillary-test recommendations.\n\n"
                if lang == "en" else
                "你是同一个单一病理 Agent。请仅根据已采集观察生成简洁、可溯源的最终报告，明确不确定性和取样局限，禁止编造未观察到的发现。最终结论强度不得高于成立该结论所需证据中的最弱一环；高倍视野只能提供局部细胞学证据，不能单独确立全片亚型。若独立空间取样不足两个区域，特定组织学亚型只能写为‘形态学倾向’或鉴别方向，禁止使用‘确诊’‘确定为’‘完全符合’等确定性措辞。形态学证据与辅助检查建议必须分开。\n\n"
            )
            synthesis += (
                f"Independent spatial locations sampled: {independent_fovs}.\n\n"
                if lang == "en" else
                f"本次独立空间取样区域数：{independent_fovs}。\n\n"
            )
            if trusted_specimen_note:
                synthesis += (
                    f"Trusted specimen context (server supplied): {trusted_specimen_note}\n\n"
                    if lang == 'en' else
                    f"受信任的取材背景（由服务器提供）：{trusted_specimen_note}\n\n"
                )
            synthesis += evidence + "\n\n" + (str(diagnostic_question or "").strip())
            report, error = call_agent_model(
                [{"role": "user", "content": synthesis}],
                max_tokens=8192,
                max_retries=3,
                call_type="single_agent_roam_synthesis",
                metadata={"file_id": file_id, "steps": len(visited)},
            )
            if error or not report:
                logger.warning("Single Agent synthesis failed: %s", error)
                report = evidence + (f"\n\nSynthesis unavailable: {error}" if error else "")
            report = _calibrate_sparse_report(str(report), trajectory, lang)

        normalized_question = str(diagnostic_question or "").strip()
        (output_dir / "trajectory.json").write_text(
            json.dumps({
                "schema_version": 2,
                "strategy": "single_agent_roam",
                "protocol_version": "single_agent_roam_v2",
                "file_id": file_id,
                "filename": filename,
                "diagnostic_question": normalized_question,
                "diagnostic_question_sha256": hashlib.sha256(
                    normalized_question.encode("utf-8")
                ).hexdigest(),
                "specimen_context": trusted_specimen_note,
                "dimensions": dimensions,
                "stopped_by": stopped_by,
                "trajectory": trajectory,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if stopped_by != "user":
            (output_dir / "pathology_report.txt").write_text(str(report), encoding="utf-8")
            _write_report_html(output_dir, str(report), trajectory, filename)
            # Reuse the same public final-report event as the Swarm route.
            # report_ready remains a link card; this event is the one concise
            # answer to the user's question in the right-hand conversation.
            _emit(socketio, "agent.message", {
                "type": "assistant",
                "content": (
                    f"✅ Final Pathology Report:\n\n{report}"
                    if lang == "en" else
                    f"✅ 最终病理报告：\n\n{report}"
                ),
                "metadata": {
                    "message_type": "final_report",
                    "report": str(report),
                    "strategy": "single_agent_roam",
                    "file_id": str(file_id),
                },
            }, room)
            _emit(socketio, "agent.report_ready", {
                "html_path": paths["report_html"],
                "txt_path": paths["report_txt"],
                "strategy": "single_agent_roam",
            }, room)
        done_payload = {
            "status": "stopped" if stopped_by == "user" else "completed",
            "file_id": file_id,
            "strategy": "single_agent_roam",
            "steps": len(visited),
            "stopped_by": stopped_by,
            "report_path": (
                "" if stopped_by == "user"
                else str(output_dir / "pathology_report.txt")
            ),
        }
        _finish_roam_state(
            room,
            status=done_payload["status"],
            stopped_by=stopped_by,
        )
        _emit(
            socketio, "agent.roam_done", done_payload, room,
            patient_event_sink=patient_event_sink,
        )
        _emit(socketio, "agent.done", done_payload, room)
        return {
            "status": done_payload["status"],
            "strategy": "single_agent_roam",
            "trajectory": trajectory,
            "report": str(report),
            "wsi_summary": str(report),
            "pathology_findings": [] if stopped_by == "user" else [str(report)],
            "tree_nodes": [],
            "statistics": {
                "total_red": 0,
                "total_blue": 0,
                "total_green": 0,
                "red_frames": 0,
                "blue_frames": 0,
                "green_frames": 0,
                "roam_steps": len(visited),
                "independent_fovs": _independent_fov_count(trajectory),
                "strategy": "single_agent_roam",
            },
            "output_paths": paths,
        }
    except Exception as exc:
        logger.exception("Single Agent roam failed")
        _finish_roam_state(room, status="failed", stopped_by="error")
        _emit(
            socketio,
            "agent.roam_done",
            {
                "file_id": file_id,
                "status": "failed",
                "stopped_by": "error",
                "steps": len(trajectory),
            },
            room,
            patient_event_sink=patient_event_sink,
        )
        _emit(socketio, "agent.error", {
            "error": f"Single Agent roam failed: {exc}",
            "strategy": "single_agent_roam",
        }, room)
        raise
    finally:
        if slide is not None:
            try:
                slide.close()
            except Exception:
                logger.warning("Failed to close Single Agent slide", exc_info=True)
        loop.agent_stop_flags.pop(room, None)
