"""Argument parser for the local PathCopilot Agent Runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .kernel import ControlKernel


def default_db_path() -> Path:
    configured = os.environ.get("PATHCOPILOT_RUNTIME_DB")
    return Path(configured or "data/pathcopilot_runtime.sqlite3").resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pathcopilot")
    parser.add_argument("--db", default=str(default_db_path()))
    parser.add_argument(
        "--owner", default=os.environ.get("PATHCOPILOT_OWNER_ID", "local-cli")
    )
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="submit one local WSI analysis")
    run.add_argument("wsi_path")
    run.add_argument("question")
    run.add_argument("--request-id")
    run.add_argument("--max-steps", type=int, default=12)
    run.add_argument("--strategy", default="default")
    run.add_argument("--mode", default="ai")
    run.add_argument("--lang", default="zh")
    run.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="seconds to wait; 0 waits until terminal",
    )

    for name in ("status", "agents", "messages", "events", "wait"):
        item = commands.add_parser(name)
        item.add_argument("run_id")
        if name == "messages":
            item.add_argument("--task-id")
            item.add_argument("--limit", type=int, default=500)
        elif name == "events":
            item.add_argument("--after-seq", type=int, default=0)
            item.add_argument("--limit", type=int, default=500)
        elif name == "wait":
            item.add_argument("--timeout", type=float, default=0.0)

    followup = commands.add_parser("followup")
    followup.add_argument("run_id")
    followup.add_argument("task_id")
    followup.add_argument("message")
    followup.add_argument("--request-id", default=None)

    cancel = commands.add_parser("cancel")
    cancel.add_argument("run_id")
    cancel.add_argument("--task-id")
    cancel.add_argument("--reason", default="user requested stop")
    return parser


def execute(args: argparse.Namespace) -> Any:
    if args.command == "run":
        from .launcher import LocalAnalysisLauncher

        result = LocalAnalysisLauncher(args.db, args.owner).submit(
            args.wsi_path,
            question=args.question,
            request_id=args.request_id,
            max_steps=args.max_steps,
            strategy=args.strategy,
            mode=args.mode,
            lang=args.lang,
        )
        if result.get("run_id"):
            with ControlKernel(args.db, args.owner) as kernel:
                try:
                    result["final"] = kernel.wait(
                        result["run_id"], timeout_seconds=args.timeout
                    )
                except KeyboardInterrupt:
                    kernel.cancel(
                        result["run_id"],
                        reason="CLI interrupted by user",
                    )
                    raise
        return result

    with ControlKernel(args.db, args.owner) as kernel:
        if args.command == "status":
            return kernel.status(args.run_id)
        if args.command == "agents":
            return kernel.agents(args.run_id)
        if args.command == "messages":
            return kernel.messages(
                args.run_id, task_id=args.task_id, limit=args.limit
            )
        if args.command == "events":
            return kernel.events(
                args.run_id, after_seq=args.after_seq, limit=args.limit
            )
        if args.command == "followup":
            return kernel.followup(
                args.run_id,
                task_id=args.task_id,
                message=args.message,
                request_id=args.request_id or uuid.uuid4().hex,
            )
        if args.command == "cancel":
            return kernel.cancel(
                args.run_id, task_id=args.task_id, reason=args.reason
            )
        if args.command == "wait":
            return kernel.wait(args.run_id, timeout_seconds=args.timeout)
    raise RuntimeError("unknown command")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
