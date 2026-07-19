from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from app.adapters.base import SynthesisRequest
from app.workers.artifacts import ArtifactStore, artifact_output, artifact_response, register_artifact_routes
from app.workers.contracts import LoadRequest, SynthesizeRequest
from app.workers.indextts_subprocess import IndexTTSSubprocessAdapter
from app.workers.runtime import release_cuda_memory, worker_runtime_status

REPO_DIR = Path(os.environ.get("TTS_MORE_INDEXTTS_REPO", "repo/index-tts"))
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _resolve_python_exe() -> str:
    """Resolve the per-service Python interpreter, with a cross-platform guard.

    TTS_MORE_INDEXTTS_PYTHON / TTS_MORE_PYTHON_EXE may point at a venv path
    authored for a different OS (e.g. .venv\\Scripts\\python.exe from a Windows
    .env.example copied onto macOS). If the configured path does not exist,
    fall back to sys.executable so the worker still runs instead of failing to
    spawn a missing interpreter.
    """
    candidate = os.environ.get("TTS_MORE_INDEXTTS_PYTHON") or os.environ.get("TTS_MORE_PYTHON_EXE")
    if candidate and Path(candidate).exists():
        return candidate
    return sys.executable


PYTHON_EXE = _resolve_python_exe()

app = FastAPI(title="TTS More IndexTTS Worker", version="0.1.0")
adapter = IndexTTSSubprocessAdapter(REPO_DIR, python_exe=PYTHON_EXE)
loaded_profile: str | None = None


def _artifact_store() -> ArtifactStore:
    configured_root = os.environ.get("TTS_MORE_ARTIFACT_ROOT")
    artifact_root = Path(configured_root).expanduser() if configured_root else (
        PROJECT_ROOT / "data" / "runtime" / "worker-artifacts" / "indextts"
    )
    if not artifact_root.is_absolute():
        artifact_root = PROJECT_ROOT / artifact_root
    return ArtifactStore(artifact_root)


register_artifact_routes(app, _artifact_store)


@app.get("/health")
def health() -> dict[str, Any]:
    adapter_health = adapter.health()
    return {
        **adapter_health,
        "ready": adapter_health.get("ready", False),
        "worker": "indextts-standard",
        "tts_more_commit": os.environ.get("TTS_MORE_APP_COMMIT", ""),
    }


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "capabilities": [
            "tts",
            "reference_audio_voice",
            "emotion_text",
            "reference-audio",
            "emotion-text",
            "artifact-transfer",
        ]
    }


@app.post("/load")
def load(request: LoadRequest) -> dict[str, str]:
    global loaded_profile
    adapter.load(request.profile)
    loaded_profile = request.profile
    return {"status": "loaded", "profile": request.profile}


@app.post("/synthesize")
def synthesize(request: SynthesizeRequest) -> dict[str, Any]:
    store = _artifact_store()
    output_path, artifact_id = artifact_output(store, request.delivery, Path(request.output_path), ".wav")
    result = adapter.synthesize(
        SynthesisRequest(
            line=request.line,
            profile=request.profile,
            output_path=output_path,
            parameters=request.parameters,
        )
    )
    return {
        "audio_path": str(result.audio_path),
        "metadata": result.metadata,
        **artifact_response(store, artifact_id),
    }


@app.post("/unload")
def unload() -> dict[str, str]:
    global loaded_profile
    adapter.unload()
    loaded_profile = None
    release_cuda_memory()
    return {"status": "unloaded"}


@app.get("/status")
def status() -> dict[str, Any]:
    return {
        **worker_runtime_status(loaded=loaded_profile is not None, model=loaded_profile),
        "ready": loaded_profile is not None,
        "repo_found": REPO_DIR.exists(),
        "mode": "resident" if adapter.resident_mode else "subprocess",
    }
