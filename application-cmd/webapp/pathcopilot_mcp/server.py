"""Owner-scoped PathCopilot MCP server.

The server deliberately exposes pathology analysis and Runtime control only.
It cannot execute arbitrary commands, fetch URLs, or read unrestricted files.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from pathcopilot_cli.kernel import ControlKernel
from pathcopilot_cli.launcher import LocalAnalysisLauncher


def _settings() -> tuple[Path, str, tuple[Path, ...]]:
    db = Path(
        os.environ.get(
            "PATHCOPILOT_RUNTIME_DB", "data/pathcopilot_runtime.sqlite3"
        )
    ).expanduser().resolve()
    owner = os.environ.get("PATHCOPILOT_MCP_OWNER_ID", "local-mcp").strip()
    if not owner:
        raise RuntimeError("PATHCOPILOT_MCP_OWNER_ID is required")
    raw_roots = os.environ.get("PATHCOPILOT_MCP_WSI_ROOTS", "")
    roots = tuple(
        Path(item.strip()).expanduser().resolve()
        for item in raw_roots.split(os.pathsep)
        if item.strip()
    )
    return db, owner, roots


def _allowed_wsi(value: str, roots: tuple[Path, ...]) -> Path:
    if not roots:
        raise ValueError(
            "local WSI submission is disabled; configure "
            "PATHCOPILOT_MCP_WSI_ROOTS"
        )
    candidate = Path(value).expanduser().resolve(strict=True)
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise ValueError("wsi_path is outside the configured WSI roots")
    return candidate


def _summary(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            key: value.get(key)
            for key in (
                "run_id",
                "root_task_id",
                "run_state",
                "task_id",
                "created",
                "accepted",
            )
            if key in value
        }
    if isinstance(value, list):
        return {"items": len(value)}
    return {"type": type(value).__name__}


def _cap(value: Any, maximum: int = 250_000) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) > maximum:
        return {
            "truncated": True,
            "message": "Result exceeded the MCP response limit",
            "preview": encoded[:maximum],
        }
    return value


def create_server() -> FastMCP:
    db_path, owner_id, roots = _settings()
    server = FastMCP(
        "PathCopilot",
        instructions=(
            "Analyze allowlisted local whole-slide images and inspect or "
            "control durable PathCopilot Agent runs. Hidden chain-of-thought "
            "is never returned; only public summaries and evidence are exposed."
        ),
    )

    def audited(
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[[ControlKernel], Any],
    ) -> Any:
        with ControlKernel(db_path, owner_id) as kernel:
            status = kernel.status(run_id)
            task_id = status.get("root_task_id")
            if not task_id:
                raise ValueError("Runtime Run has no root Agent")
            call = kernel.service.agents.agent_store.begin_tool_call(
                run_id=run_id,
                task_id=task_id,
                name=tool_name,
                arguments=arguments,
            )
            try:
                result = operation(kernel)
            except Exception as exc:
                kernel.service.agents.agent_store.finish_tool_call(
                    run_id=run_id,
                    tool_call_id=call.tool_call_id,
                    error={
                        "code": "MCP_TOOL_FAILED",
                        "message": str(exc)[:500],
                    },
                )
                raise
            kernel.service.agents.agent_store.finish_tool_call(
                run_id=run_id,
                tool_call_id=call.tool_call_id,
                output=_summary(result),
            )
            return _cap(result)

    @server.tool(name="pathcopilot_submit_analysis")
    def submit_analysis(
        wsi_path: str,
        question: str,
        max_steps: int = 12,
        lang: str = "zh",
    ) -> dict[str, Any]:
        """Submit one allowlisted local WSI to the managed pathology Agent."""

        source = _allowed_wsi(wsi_path, roots)
        result = LocalAnalysisLauncher(db_path, owner_id).submit(
            source,
            question=question,
            request_id=uuid.uuid4().hex,
            max_steps=max_steps,
            lang=lang,
        )
        run_id = str(result["run_id"])
        with ControlKernel(db_path, owner_id) as kernel:
            task_id = str(result["root_task_id"])
            call = kernel.service.agents.agent_store.begin_tool_call(
                run_id=run_id,
                task_id=task_id,
                name="pathcopilot_submit_analysis",
                arguments={
                    "wsi_name": source.name,
                    "question_length": len(question),
                    "max_steps": max_steps,
                    "lang": lang,
                },
            )
            kernel.service.agents.agent_store.finish_tool_call(
                run_id=run_id,
                tool_call_id=call.tool_call_id,
                output=_summary(result),
            )
        return result

    @server.tool(name="pathcopilot_run_status")
    def run_status(run_id: str) -> dict[str, Any]:
        """Return durable Run and Task counts for an owned analysis."""

        return audited(
            run_id,
            "pathcopilot_run_status",
            {},
            lambda kernel: kernel.status(run_id),
        )

    @server.tool(name="pathcopilot_agent_tree")
    def agent_tree(run_id: str) -> dict[str, Any]:
        """Return the parent-child Agent tree for an owned analysis."""

        return audited(
            run_id,
            "pathcopilot_agent_tree",
            {},
            lambda kernel: kernel.agents(run_id),
        )

    @server.tool(name="pathcopilot_events")
    def events(
        run_id: str, after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Replay durable Runtime events from a monotonic sequence cursor."""

        limit = min(max(int(limit), 1), 200)
        return audited(
            run_id,
            "pathcopilot_events",
            {"after_seq": after_seq, "limit": limit},
            lambda kernel: kernel.events(
                run_id, after_seq=after_seq, limit=limit
            ),
        )

    @server.tool(name="pathcopilot_messages")
    def messages(
        run_id: str, task_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Read public Agent messages and evidence summaries."""

        limit = min(max(int(limit), 1), 200)
        return audited(
            run_id,
            "pathcopilot_messages",
            {"task_id": task_id, "limit": limit},
            lambda kernel: kernel.messages(
                run_id, task_id=task_id, limit=limit
            ),
        )

    @server.tool(name="pathcopilot_followup")
    def followup(
        run_id: str, task_id: str, message: str
    ) -> dict[str, Any]:
        """Send a durable follow-up to an Agent or create a continuation."""

        return audited(
            run_id,
            "pathcopilot_followup",
            {"task_id": task_id, "message_length": len(message)},
            lambda kernel: kernel.followup(
                run_id,
                task_id=task_id,
                message=message,
                request_id=uuid.uuid4().hex,
            ),
        )

    @server.tool(name="pathcopilot_stop")
    def stop(
        run_id: str,
        task_id: str | None = None,
        reason: str = "MCP client requested stop",
    ) -> dict[str, Any]:
        """Request cooperative cancellation of a Run or child Agent."""

        return audited(
            run_id,
            "pathcopilot_stop",
            {"task_id": task_id, "reason": reason[:200]},
            lambda kernel: kernel.cancel(
                run_id, task_id=task_id, reason=reason
            ),
        )

    return server


def main() -> None:
    create_server().run(transport="stdio")
