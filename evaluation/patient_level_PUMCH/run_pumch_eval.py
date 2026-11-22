#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless Patient Level evaluator for the private PUMCH cohort.

Patients are processed in manifest order.  Slides belonging to the same
patient are analyzed by the existing Auto red-blue-green Agent using either
the default parallel WSI scheduler or ``--wsi-schedule sequential``.  The
patient-level report is synthesized only after every slide has completed.
No Web server is started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parent
COMMON = ROOT / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

MCP_CONFIG = ROOT / "mcp.outbound.json"
SUPPORTED_WSI_SUFFIXES = {
    ".svs", ".tif", ".tiff", ".ndpi", ".mrxs", ".scn", ".bif", ".vms", ".vmu"
}


def _preflight_outbound_mcp() -> Dict[str, List[str]]:
    """Verify both allowlisted FastMCP servers before starting a patient run.

    This performs only MCP initialize/list_tools handshakes; it does not make
    a network evidence query. Failing early prevents a nominally MCP-enabled
    PUMCH run from silently executing without the declared native tools.
    """

    from pathcopilot_outbound_mcp import list_mcp_tools

    expected = {
        "pubmed": "search_pubmed",
        "web_search": "search_pathology_web",
    }
    inventory: Dict[str, List[str]] = {}
    for server_name, required_tool in expected.items():
        names = [item["name"] for item in list_mcp_tools(server_name)]
        if required_tool not in names:
            raise RuntimeError(
                f"MCP server {server_name!r} did not expose {required_tool!r}"
            )
        inventory[server_name] = names
    return inventory


class EventSink:
    """Thread-safe Socket.IO-compatible event sink for the CLI runtime."""

    def __init__(self, event_log: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._event_log = event_log
        if event_log is not None:
            event_log.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, payload: Any = None, to: str | None = None, **_: Any) -> None:
        item = {
            "time": time.time(),
            "event": event,
            "room": to,
            "payload": payload,
        }
        with self._lock:
            self._events.append(item)
            if self._event_log is not None:
                with self._event_log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

        if event == "agent.error":
            logging.error("%s", (payload or {}).get("error", payload))
        elif event in {"agent.patient_progress", "agent.wsi_started", "agent.wsi_completed"}:
            message = payload.get("message") if isinstance(payload, dict) else None
            if message:
                logging.info("%s", message)

    @staticmethod
    def sleep(seconds: float = 0) -> None:
        time.sleep(max(0.0, float(seconds or 0)))

    @property
    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)


def _stable_file_id(path: Path) -> str:
    stat = path.stat()
    material = f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _read_manifest(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON manifest: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Manifest must be an object with schema_version=1")
    patients = payload.get("patients")
    if not isinstance(patients, list) or not patients:
        raise ValueError("Manifest must contain a non-empty patients list")

    seen_patient_ids = set()
    normalized: List[Dict[str, Any]] = []
    manifest_dir = path.parent.resolve()
    for patient_index, patient in enumerate(patients, start=1):
        if not isinstance(patient, dict):
            raise ValueError(f"patients[{patient_index}] must be an object")
        patient_id = str(patient.get("patient_id", "")).strip()
        if not patient_id:
            raise ValueError(f"patients[{patient_index}].patient_id is required")
        if patient_id in seen_patient_ids:
            raise ValueError(f"Duplicate patient_id: {patient_id}")
        if any(ch in patient_id for ch in '<>:"/\\|?*') or patient_id in {".", ".."}:
            raise ValueError(f"patient_id is not filesystem-safe: {patient_id}")
        seen_patient_ids.add(patient_id)

        patient_identifier = str(patient.get("patient_identifier") or patient_id).strip()
        slides = patient.get("slides")
        if not isinstance(slides, list) or not slides:
            raise ValueError(f"Patient {patient_id} must contain at least one slide")

        normalized_slides = []
        seen_paths = set()
        for slide_index, slide in enumerate(slides, start=1):
            if not isinstance(slide, dict):
                raise ValueError(f"Patient {patient_id} slide {slide_index} must be an object")
            raw_path = str(slide.get("path", "")).strip()
            part = str(slide.get("part", "")).strip()
            if not raw_path or not part:
                raise ValueError(f"Patient {patient_id} slide {slide_index} requires path and part")
            slide_path = Path(raw_path).expanduser()
            if not slide_path.is_absolute():
                slide_path = manifest_dir / slide_path
            slide_path = slide_path.resolve()
            if not slide_path.is_file():
                raise FileNotFoundError(f"WSI file does not exist: {slide_path}")
            if slide_path.suffix.lower() not in SUPPORTED_WSI_SUFFIXES:
                raise ValueError(f"Unsupported WSI extension: {slide_path}")
            path_key = os.path.normcase(str(slide_path))
            if path_key in seen_paths:
                raise ValueError(f"Duplicate slide path for patient {patient_id}: {slide_path}")
            seen_paths.add(path_key)
            normalized_slides.append({"path": slide_path, "part": part})

        normalized.append({
            "patient_id": patient_id,
            "patient_identifier": patient_identifier,
            "slides": normalized_slides,
        })
    return normalized


def _selected_patients(patients: Iterable[Dict[str, Any]], selected: List[str]) -> List[Dict[str, Any]]:
    patients = list(patients)
    if not selected:
        return patients
    wanted = set(selected)
    available = {item["patient_id"] for item in patients}
    missing = sorted(wanted - available)
    if missing:
        raise ValueError(f"Unknown patient_id(s): {', '.join(missing)}")
    return [item for item in patients if item["patient_id"] in wanted]


def _run_patient(
    patient: Dict[str, Any],
    *,
    lang: str,
    cache: str,
    events: bool,
    runtime: Any,
    wsi_schedule: str,
    api_max_concurrency: int,
) -> Dict[str, Any]:
    from file_registry import register_file
    from agent.agent_loop import run_patient_level_agent

    patient_id = patient["patient_id"]
    file_ids: List[str] = []
    file_infos: List[Dict[str, Any]] = []
    for index, slide in enumerate(patient["slides"]):
        path = slide["path"]
        file_id = _stable_file_id(path)
        register_file(file_id, path)
        file_ids.append(file_id)
        file_infos.append({
            "index": index,
            "file_id": file_id,
            "filename": path.name,
            "part": slide["part"],
        })

    event_path = ROOT / "outputs" / f"patient_{patient_id}" / "auto" / "events.jsonl" if events else None
    sink = EventSink(event_path)
    runner_kwargs = {
        "patient_id": patient_id,
        "file_infos": file_infos,
        "folder_name": patient["patient_identifier"],
        "lang": lang,
        "cache_action": "rerun_all" if cache == "rerun" else "use_selected",
        "skip_file_ids": [] if cache == "rerun" else file_ids,
        "wsi_schedule": wsi_schedule,
        "api_max_concurrency": api_max_concurrency,
    }
    result, runtime_receipt = runtime.run(
        run_patient_level_agent,
        socketio=sink,
        room=f"cli-{patient_id}",
        patient_id=patient_id,
        file_infos=file_infos,
        runner_kwargs=runner_kwargs,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Patient Level runner returned no result for {patient_id}")
    result = dict(result)
    result["agent_runtime"] = runtime_receipt
    return result


def _write_summary(summary: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PathCopilot PUMCH Patient Level Auto evaluation")
    parser.add_argument("--manifest", required=True, type=Path, help="Manifest JSON (schema_version=1)")
    parser.add_argument("--patient", action="append", default=[], help="Run only this patient_id (repeatable)")
    parser.add_argument("--lang", choices=("zh", "en"), default="zh")
    parser.add_argument("--cache", choices=("rerun", "reuse"), default="rerun")
    parser.add_argument("--events", action="store_true", help="Write a local events.jsonl audit log")
    parser.add_argument("--summary", type=Path, default=ROOT / "outputs" / "run_summary.json")
    parser.add_argument(
        "--runtime-mode",
        choices=("off", "shadow"),
        default="shadow",
        help="Agent Runtime mode (default: shadow)",
    )
    parser.add_argument(
        "--runtime-db",
        type=Path,
        default=None,
        help="Agent Runtime SQLite path (default: outputs/agent_runtime.sqlite3)",
    )
    parser.add_argument(
        "--outbound-mcp",
        action="store_true",
        help=(
            "Enable PubMed/Web Search MCP. Outbound MCP is off by default, "
            "matching the reported PUMCH evaluation."
        ),
    )
    parser.add_argument(
        "--no-outbound-mcp",
        action="store_true",
        help="Keep outbound MCP off (the default; kept for compatibility).",
    )
    parser.add_argument(
        "--wsi-schedule",
        choices=("sequential", "parallel"),
        default="parallel",
        help=(
            "WSI scheduler. parallel (default) starts every slide together, "
            "matching the manuscript protocol. sequential finishes one slide "
            "before starting the next; use this for ordinary or strictly "
            "rate-limited API services. Intra-slide red/blue/green work is "
            "unchanged in both modes."
        ),
    )
    parser.add_argument(
        "--api-max-concurrency",
        type=int,
        default=3,
        help=(
            "Maximum in-flight OpenRouter/model HTTP requests for this "
            "process (default: 3). Independent of --wsi-schedule; 0 or "
            "unlimited is not allowed."
        ),
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.api_max_concurrency < 1:
        parser.error("--api-max-concurrency must be an integer >= 1")

    # Configure outbound MCP before importing any Agent module. Outbound MCP is
    # off by default, as in the reported evaluation; --outbound-mcp enables it.
    outbound_mcp_enabled = args.outbound_mcp and not args.no_outbound_mcp
    os.environ["PATHCOPILOT_OUTBOUND_MCP"] = "1" if outbound_mcp_enabled else "0"
    os.environ["PATHCOPILOT_OUTBOUND_MCP_CONFIG"] = str(MCP_CONFIG.resolve())
    os.environ["PATHCOPILOT_AGENT_DIR"] = str((ROOT / "agent").resolve())

    if not os.environ.get("OPENROUTER_API_KEYS", "").strip():
        parser.error("OPENROUTER_API_KEYS must be set")

    mcp_inventory: Dict[str, List[str]] = {}
    if outbound_mcp_enabled:
        try:
            mcp_inventory = _preflight_outbound_mcp()
        except Exception as exc:
            parser.error(f"Outbound MCP preflight failed: {exc}")

    manifest_path = args.manifest.expanduser().resolve()
    try:
        patients = _selected_patients(_read_manifest(manifest_path), args.patient)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    os.environ["PATHCOPILOT_WSI_SCHEDULE"] = args.wsi_schedule
    os.environ["PATHCOPILOT_API_MAX_CONCURRENCY"] = str(args.api_max_concurrency)

    os.chdir(ROOT)
    from agent.gpt_client import configure_api_concurrency, freeze_api_concurrency
    from patient_runtime import PatientLevelAgentRuntime

    configure_api_concurrency(args.api_max_concurrency)
    freeze_api_concurrency()

    runtime_db = (
        args.runtime_db.expanduser().resolve()
        if args.runtime_db is not None
        else (ROOT / "outputs" / "agent_runtime.sqlite3").resolve()
    )
    runtime = PatientLevelAgentRuntime(runtime_db, mode=args.runtime_mode)
    runtime.start()

    summary: Dict[str, Any] = {
        "schema_version": 1,
        "manifest": str(manifest_path),
        "mode": "ai",
        "agent_runtime": {
            "mode": args.runtime_mode,
            "database": str(runtime_db),
        },
        "outbound_mcp": {
            "enabled": outbound_mcp_enabled,
            "config": str(MCP_CONFIG.resolve()),
            "servers": ["pubmed", "web_search"],
            "tools": mcp_inventory,
        },
        "wsi_schedule": args.wsi_schedule,
        "api_max_concurrency": args.api_max_concurrency,
        "patients": [],
    }
    failed = False
    try:
        for patient in patients:
            logging.info("Starting patient %s (%d slides)", patient["patient_id"], len(patient["slides"]))
            try:
                result = _run_patient(
                    patient,
                    lang=args.lang,
                    cache=args.cache,
                    events=args.events,
                    runtime=runtime,
                    wsi_schedule=args.wsi_schedule,
                    api_max_concurrency=args.api_max_concurrency,
                )
                summary["patients"].append(result)
                failed = failed or result.get("status") != "completed"
            except Exception as exc:
                logging.exception("Patient %s failed", patient["patient_id"])
                summary["patients"].append({
                    "patient_id": patient["patient_id"],
                    "status": "failed",
                    "error": str(exc),
                })
                failed = True
            _write_summary(summary, args.summary.expanduser().resolve())
    except KeyboardInterrupt:
        try:
            from agent.gpt_client import request_api_interrupt
            request_api_interrupt()
        except Exception:
            logging.debug("API interrupt signal was unavailable", exc_info=True)
        _write_summary(summary, args.summary.expanduser().resolve())
        return 130
    finally:
        runtime.stop()

    summary_path = args.summary.expanduser().resolve()
    _write_summary(summary, summary_path)
    print(summary_path)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
