from __future__ import annotations

import base64
import json
import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_chat_store_root() -> Path:
    configured = os.environ.get("PATHCOPILOT_CHAT_STORE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "PathCopilot" / "chat_threads"
    return Path.home() / ".pathcopilot" / "chat_threads"


def fallback_thread_title(text: str, language: str = "zh") -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"^[\s\-—:：，。！？!?]+|[\s\-—:：，。！？!?]+$", "", value)
    if not value:
        return "新对话" if language != "en" else "New conversation"
    limit = 22 if re.search(r"[\u3400-\u9fff]", value) else 64
    characters = list(value)
    return "".join(characters[:limit]) + ("…" if len(characters) > limit else "")


class ChatThreadStore:
    """Small local thread store modeled after Codex thread/list/read/name semantics."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or default_chat_store_root())
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _thread_dir(self, thread_id: str) -> Path:
        value = str(thread_id or "")
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("无效的对话 ID")
        return self.root / value

    def _thread_file(self, thread_id: str) -> Path:
        return self._thread_dir(thread_id) / "thread.json"

    def _read(self, thread_id: str) -> dict[str, Any]:
        path = self._thread_file(thread_id)
        if not path.is_file():
            raise FileNotFoundError(thread_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, thread: dict[str, Any]) -> None:
        directory = self._thread_dir(thread["id"])
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "thread.json"
        temporary = directory / "thread.json.tmp"
        temporary.write_text(
            json.dumps(thread, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    @staticmethod
    def _assert_owner(thread: dict[str, Any], owner_id: str) -> None:
        if str(thread.get("owner_id") or "") != str(owner_id or ""):
            raise PermissionError("无权访问该对话")

    @staticmethod
    def summary(thread: dict[str, Any]) -> dict[str, Any]:
        messages = thread.get("messages") or []
        return {
            "id": thread["id"],
            "title": thread.get("title") or "新对话",
            "created_at": thread.get("created_at"),
            "updated_at": thread.get("updated_at"),
            "message_count": len(messages),
            "manual_title": bool(thread.get("manual_title")),
            "auto_title": bool(thread.get("auto_title")),
        }

    def create(self, owner_id: str, title: str = "", language: str = "zh") -> dict[str, Any]:
        with self._lock:
            thread_id = uuid.uuid4().hex
            now = _utc_now()
            thread = {
                "version": 1,
                "id": thread_id,
                "owner_id": str(owner_id or "local"),
                "title": fallback_thread_title(title, language),
                "manual_title": False,
                "auto_title": bool(title),
                "language": "en" if language == "en" else "zh",
                "created_at": now,
                "updated_at": now,
                "active_attachment_ids": [],
                "messages": [],
            }
            self._write(thread)
            return deepcopy(thread)

    def list(self, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            threads: list[dict[str, Any]] = []
            for path in self.root.glob("*/thread.json"):
                try:
                    thread = json.loads(path.read_text(encoding="utf-8"))
                    self._assert_owner(thread, owner_id)
                    if thread.get("messages"):
                        threads.append(self.summary(thread))
                except (OSError, ValueError, PermissionError, json.JSONDecodeError):
                    continue
            threads.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return threads[: max(1, min(int(limit or 50), 200))]

    def get(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        with self._lock:
            thread = self._read(thread_id)
            self._assert_owner(thread, owner_id)
            return deepcopy(thread)

    def set_title(
        self,
        owner_id: str,
        thread_id: str,
        title: str,
        *,
        manual: bool = False,
        automatic: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            thread = self._read(thread_id)
            self._assert_owner(thread, owner_id)
            normalized = re.sub(r"\s+", " ", str(title or "")).strip()[:100]
            if not normalized:
                raise ValueError("标题不能为空")
            thread["title"] = normalized
            thread["manual_title"] = bool(manual)
            thread["auto_title"] = bool(automatic and not manual)
            thread["updated_at"] = _utc_now()
            self._write(thread)
            return deepcopy(thread)

    def set_active_attachments(
        self, owner_id: str, thread_id: str, attachment_ids: Iterable[str]
    ) -> dict[str, Any]:
        with self._lock:
            thread = self._read(thread_id)
            self._assert_owner(thread, owner_id)
            known = {
                str(attachment.get("id"))
                for message in thread.get("messages") or []
                for attachment in message.get("attachments") or []
            }
            requested = []
            for item in attachment_ids or []:
                value = str(item or "")
                if value in known and value not in requested:
                    requested.append(value)
            thread["active_attachment_ids"] = requested
            thread["updated_at"] = _utc_now()
            self._write(thread)
            return deepcopy(thread)

    def add_message(
        self,
        owner_id: str,
        thread_id: str,
        *,
        role: str,
        content: str,
        client_message_id: str = "",
        images: Iterable[dict[str, Any]] | None = None,
        attachment_ids: Iterable[str] | None = None,
        sources: Iterable[dict[str, Any]] | None = None,
        web_search: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized_role = str(role or "").lower()
        if normalized_role not in {"user", "assistant"}:
            raise ValueError("消息角色无效")
        normalized_content = str(content or "").strip()
        if not normalized_content:
            raise ValueError("消息内容不能为空")

        with self._lock:
            thread = self._read(thread_id)
            self._assert_owner(thread, owner_id)
            message_id = str(client_message_id or "")
            if message_id:
                for existing in thread.get("messages") or []:
                    if existing.get("client_message_id") == message_id:
                        return deepcopy(thread), deepcopy(existing)

            existing_attachments = {
                str(attachment.get("id")): attachment
                for item in thread.get("messages") or []
                for attachment in item.get("attachments") or []
            }
            stored_attachments: list[dict[str, Any]] = []
            for attachment_id in attachment_ids or []:
                value = str(attachment_id or "")
                attachment = existing_attachments.get(value)
                if attachment and attachment not in stored_attachments:
                    stored_attachments.append(deepcopy(attachment))

            attachment_dir = self._thread_dir(thread_id) / "attachments"
            for image in images or []:
                mime = str(image.get("mime") or "")
                extension = _MIME_EXTENSIONS.get(mime)
                if not extension:
                    continue
                requested_id = str(image.get("id") or "")
                attachment_id = requested_id if _SAFE_ID.fullmatch(requested_id) else uuid.uuid4().hex
                relative_path = f"attachments/{attachment_id}{extension}"
                destination = self._thread_dir(thread_id) / relative_path
                if not destination.exists():
                    attachment_dir.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(base64.b64decode(str(image.get("data") or "")))
                attachment = {
                    "id": attachment_id,
                    "name": str(image.get("name") or f"image{extension}")[:120],
                    "mime": mime,
                    "path": relative_path,
                }
                existing_attachments[attachment_id] = attachment
                if not any(item.get("id") == attachment_id for item in stored_attachments):
                    stored_attachments.append(attachment)

            now = _utc_now()
            message = {
                "id": uuid.uuid4().hex,
                "client_message_id": message_id or uuid.uuid4().hex,
                "role": normalized_role,
                "content": normalized_content,
                "created_at": now,
                "attachments": stored_attachments,
                "sources": [deepcopy(item) for item in (sources or []) if isinstance(item, dict)],
                "web_search": bool(web_search),
            }
            thread.setdefault("messages", []).append(message)
            if normalized_role == "user" and stored_attachments:
                thread["active_attachment_ids"] = [item["id"] for item in stored_attachments]
            if len(thread["messages"]) == 1 and not thread.get("manual_title"):
                thread["title"] = fallback_thread_title(
                    normalized_content, str(thread.get("language") or "zh")
                )
                thread["auto_title"] = True
            thread["updated_at"] = now
            self._write(thread)
            return deepcopy(thread), deepcopy(message)

    def attachment_path(self, owner_id: str, thread_id: str, attachment_id: str) -> tuple[Path, str]:
        with self._lock:
            thread = self._read(thread_id)
            self._assert_owner(thread, owner_id)
            for message in thread.get("messages") or []:
                for attachment in message.get("attachments") or []:
                    if str(attachment.get("id")) != str(attachment_id):
                        continue
                    path = (self._thread_dir(thread_id) / str(attachment.get("path") or "")).resolve()
                    if self._thread_dir(thread_id).resolve() not in path.parents or not path.is_file():
                        raise FileNotFoundError(attachment_id)
                    return path, str(attachment.get("mime") or "application/octet-stream")
            raise FileNotFoundError(attachment_id)
