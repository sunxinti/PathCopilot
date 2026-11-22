"""Launch the mature pathology Agent through managed Runtime ownership."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_WSI = {
    ".svs", ".ndpi", ".tiff", ".tif", ".vms",
    ".vmu", ".scn", ".mrxs", ".bif",
}


class LocalAnalysisLauncher:
    """Register a local WSI without copying or mutating the source file."""

    def __init__(self, db_path: str | Path, owner_id: str):
        self.db_path = Path(db_path).expanduser().resolve()
        self.owner_id = str(owner_id).strip()
        if not self.owner_id:
            raise ValueError("owner_id is required")

    def submit(
        self,
        wsi_path: str | Path,
        *,
        question: str,
        request_id: str | None = None,
        max_steps: int = 12,
        strategy: str = "default",
        mode: str = "ai",
        lang: str = "zh",
    ) -> dict[str, Any]:
        source = Path(wsi_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_WSI:
            raise ValueError("wsi_path is not a supported whole-slide image")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question is required")
        if len(question.strip()) > 4000:
            raise ValueError("question exceeds 4000 characters")
        if type(max_steps) is not int or not 1 <= max_steps <= 200:
            raise ValueError("max_steps must be an integer from 1 to 200")

        os.environ["PATHCOPILOT_RUNTIME_MODE"] = "managed"
        os.environ["PATHCOPILOT_RUNTIME_DB"] = str(self.db_path)
        os.environ["PATHCOPILOT_LOCAL_MODE"] = "1"

        # Import only for submit. Status/inspection commands never import the
        # Flask application or start a pathology worker.
        import app as webapp

        webapp._ensure_runtime_started()
        file_id = f"local-{uuid.uuid5(uuid.NAMESPACE_URL, str(source))}"
        session_id = f"cli-{uuid.uuid4().hex}"
        request_id = (request_id or uuid.uuid4().hex).strip()[:200]
        thumbnail_path = self._register_wsi(webapp, source, file_id)
        legacy_kwargs = {
            "socketio": webapp.socketio,
            "room": session_id,
            "file_id": file_id,
            "max_steps": max_steps,
            "strategy": str(strategy)[:80],
            "mode": str(mode)[:40],
            "lang": str(lang)[:16],
            "diagnostic_question": question.strip(),
        }
        submission = webapp.runtime_service.submit(
            owner_id=self.owner_id,
            session_id=session_id,
            file_id=file_id,
            request_id=request_id,
            legacy_kwargs=legacy_kwargs,
            activate=False,
        )
        webapp.runtime_service.activate(
            submission.run_id, owner_id=self.owner_id
        )
        return {
            **submission.to_dict(),
            "session_id": session_id,
            "file_id": file_id,
            "wsi_path": str(source),
            "thumbnail_path": str(thumbnail_path),
        }

    @staticmethod
    def _register_wsi(webapp: Any, source: Path, file_id: str) -> Path:
        slide = webapp.openslide.OpenSlide(str(source))
        try:
            dimensions = slide.dimensions
            level_count = slide.level_count
            level_dimensions = slide.level_dimensions
            properties = dict(slide.properties)
            thumbnail = slide.get_thumbnail((300, 300))
            if thumbnail.mode != "RGB":
                thumbnail = thumbnail.convert("RGB")
            thumbnail_dir = Path(webapp.THUMBNAILS_FOLDER).resolve()
            thumbnail_dir.mkdir(parents=True, exist_ok=True)
            thumbnail_path = thumbnail_dir / f"{file_id}_thumb.jpg"
            thumbnail.save(thumbnail_path, "JPEG", quality=85)
        finally:
            slide.close()
        webapp.uploaded_files[file_id] = {
            "file_id": file_id,
            "filename": source.name,
            "file_path": str(source),
            "file_size": source.stat().st_size,
            "upload_time": datetime.now(timezone.utc).isoformat(),
            "dimensions": dimensions,
            "level_count": level_count,
            "level_dimensions": level_dimensions,
            "properties": properties,
            "thumbnail_path": str(thumbnail_path),
            "dzi_generated": False,
            "user_id": "",
            "batch_id": f"cli-{uuid.uuid4().hex}",
            "source": "local-cli",
        }
        return thumbnail_path
