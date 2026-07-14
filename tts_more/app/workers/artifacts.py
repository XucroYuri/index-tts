from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse


AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".webm"}
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 100 * 1024 * 1024
DEFAULT_TTL_SECONDS = 24 * 60 * 60
ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ArtifactStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_upload_bytes: int | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.root = Path(root).resolve(strict=False)
        self.max_upload_bytes = max_upload_bytes
        self.max_output_bytes = max_output_bytes
        self.ttl_seconds = ttl_seconds

    def allocate_output(self, suffix: str = ".wav") -> tuple[str, Path]:
        self.cleanup()
        suffix = suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
        if suffix not in AUDIO_SUFFIXES:
            suffix = ".wav"
        artifact_id = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        return artifact_id, self.root / f"{artifact_id}{suffix}"

    async def save_upload(self, file: UploadFile) -> dict[str, Any]:
        raw_name = (file.filename or "").replace("\\", "/")
        suffix = Path(raw_name).suffix.lower()
        if suffix not in AUDIO_SUFFIXES:
            raise HTTPException(status_code=400, detail="unsupported audio file")
        limit = self.max_upload_bytes
        if limit is None:
            limit = int(os.environ.get("TTS_MORE_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))
        content = await file.read(limit + 1)
        if not content:
            raise HTTPException(status_code=400, detail="audio file is empty")
        if len(content) > limit:
            raise HTTPException(status_code=413, detail="audio file exceeds upload limit")
        artifact_id, path = self.allocate_output(suffix)
        self._atomic_write(path, content)
        return {"artifact_id": artifact_id, "path": str(path)}

    def describe(self, artifact_id: str) -> dict[str, Any]:
        path = self.resolve(artifact_id)
        if path is None:
            raise FileNotFoundError(artifact_id)
        size = path.stat().st_size
        if size > self.max_output_bytes:
            raise ValueError("output exceeds artifact limit")
        return {
            "artifact_id": artifact_id,
            "download_url": f"/artifacts/{artifact_id}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": size,
        }

    def resolve(self, artifact_id: str) -> Path | None:
        if not ARTIFACT_ID_RE.fullmatch(artifact_id):
            return None
        matches = list(self.root.glob(f"{artifact_id}.*")) if self.root.is_dir() else []
        if len(matches) != 1 or not matches[0].is_file():
            return None
        path = matches[0].resolve(strict=False)
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            return None
        if modified_at < time.time() - self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        return path

    def delete(self, artifact_id: str) -> bool:
        path = self.resolve(artifact_id)
        if path is None:
            return False
        path.unlink(missing_ok=True)
        return True

    def cleanup(self, *, now: float | None = None) -> int:
        if not self.root.is_dir():
            return 0
        cutoff = (time.time() if now is None else now) - self.ttl_seconds
        removed = 0
        for path in self.root.iterdir():
            try:
                expired = path.is_file() and path.stat().st_mtime < cutoff
            except OSError:
                continue
            if expired:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def register_artifact_routes(app: FastAPI, store_provider: Callable[[], ArtifactStore]) -> None:
    @app.middleware("http")
    async def cleanup_expired_artifacts(request, call_next):
        store_provider().cleanup()
        return await call_next(request)

    @app.post("/upload_ref")
    async def upload_reference(file: UploadFile = File(...)) -> dict[str, Any]:
        return await store_provider().save_upload(file)

    @app.get("/artifacts/{artifact_id}")
    def download_artifact(artifact_id: str):
        store = store_provider()
        path = store.resolve(artifact_id)
        if path is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        if path.stat().st_size > store.max_output_bytes:
            raise HTTPException(status_code=413, detail="artifact exceeds download limit")
        return FileResponse(path, media_type=_media_type(path.suffix), filename=path.name)

    @app.delete("/artifacts/{artifact_id}")
    def delete_artifact(artifact_id: str) -> dict[str, bool]:
        return {"deleted": store_provider().delete(artifact_id)}


def artifact_output(store: ArtifactStore, delivery: str, requested_path: Path, suffix: str = ".wav") -> tuple[Path, str | None]:
    if delivery != "artifact":
        if os.environ.get("TTS_MORE_WORKER_ALLOW_PATH_DELIVERY", "0") != "1":
            raise HTTPException(status_code=400, detail="path delivery is disabled for this worker")
        return requested_path, None
    artifact_id, path = store.allocate_output(suffix)
    return path, artifact_id


def artifact_response(store: ArtifactStore, artifact_id: str | None) -> dict[str, Any]:
    return store.describe(artifact_id) if artifact_id else {}


def _media_type(suffix: str) -> str:
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
        ".opus": "audio/opus",
        ".webm": "audio/webm",
    }.get(suffix.lower(), "application/octet-stream")
