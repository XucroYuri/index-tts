"""Non-invasive embedded worker for CosyVoice.

Mirrors the GPT-SoVITS / IndexTTS worker pattern: a standalone FastAPI app
that runs inside the CosyVoice repo's Python environment, imports the upstream
inference class directly, and exposes the standard worker contract. It does
NOT modify any upstream file.

Start it (from the project root, using the CosyVoice venv):

    TTS_MORE_COSYVOICE_REPO=repo/CosyVoice \
    .venv/bin/python -m uvicorn app.workers.cosyvoice_worker:app \
        --app-dir backend --host 127.0.0.1 --port 9882

CosyVoice modes (set via ``parameters.mode`` on /synthesize):
  - zero_shot    : clone from a reference audio (needs ref_audio_path + prompt_text)
  - cross_lingual: clone across languages (needs ref_audio_path)

The locked default is the original ``CosyVoice-300M`` model. It has
``cosyvoice.yaml`` and no SFT speaker metadata, so SFT and instruction modes
are deliberately rejected instead of being advertised as available.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from app.workers.artifacts import ArtifactStore, artifact_output, artifact_response, register_artifact_routes
from app.workers.contracts import LoadRequest, SynthesizeRequest
from app.workers.runtime import release_cuda_memory, worker_runtime_status

REPO_DIR = Path(os.environ.get("TTS_MORE_COSYVOICE_REPO", "repo/CosyVoice")).resolve(strict=False)
MODEL_DIR = os.environ.get("TTS_MORE_COSYVOICE_MODEL_DIR", "pretrained_models/CosyVoice-300M")

_pipeline: Any = None
_loaded_mode: str | None = None

# Map the orchestrator's mode names to CosyVoice inference methods.
# The orchestrator historically used Chinese mode labels (Gradio legacy); the
# worker accepts both the English and Chinese forms.
_MODE_MAP = {
    "zero_shot": "zero_shot",
    "cross_lingual": "cross_lingual",
    "3s极速复刻": "zero_shot",
    "跨语种复刻": "cross_lingual",
}


def _bootstrap_repo() -> None:
    if not REPO_DIR.exists():
        return
    for path in (REPO_DIR, REPO_DIR / "third_party" / "Matcha-TTS"):
        path_str = str(path)
        if path.exists() and path_str not in sys.path:
            sys.path.insert(0, path_str)
    try:
        os.chdir(REPO_DIR)
    except OSError:
        pass


def _ensure_pipeline() -> Any:
    """Construct the resident CosyVoice pipeline on first use (lazy load)."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    if not REPO_DIR.exists():
        raise RuntimeError(f"CosyVoice repo not found at {REPO_DIR}")
    _bootstrap_repo()
    try:
        from cosyvoice.cli.cosyvoice import CosyVoice  # type: ignore
    except Exception as exc:  # pragma: no cover - requires torch/GPU env
        raise RuntimeError(f"failed to import CosyVoice pipeline: {exc}") from exc
    model_path = REPO_DIR / MODEL_DIR if not Path(MODEL_DIR).is_absolute() else Path(MODEL_DIR)
    _pipeline = CosyVoice(model_dir=str(model_path))
    return _pipeline


app = FastAPI(title="TTS More CosyVoice Worker", version="0.1.0")


def _artifact_store() -> ArtifactStore:
    return ArtifactStore(REPO_DIR / "uploaded_ref")


register_artifact_routes(app, _artifact_store)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ready": _pipeline is not None or REPO_DIR.exists(),
        "worker": "cosyvoice-standard",
        "tts_more_commit": os.environ.get("TTS_MORE_APP_COMMIT", ""),
        "repo_found": REPO_DIR.exists(),
        "pipeline_loaded": _pipeline is not None,
    }


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "capabilities": [
            "tts",
            "zero-shot-voice",
            "zero_shot_voice",
            "cross-lingual-voice",
            "cross_lingual_voice",
            "artifact-transfer",
        ]
    }


@app.post("/load")
def load(request: LoadRequest) -> dict[str, Any]:
    """Load/prepare the pipeline. CosyVoice has no per-role weight switch like
    GPT-SoVITS; /load simply ensures the pipeline is resident. The mode is
    chosen per-synthesis from parameters.mode."""
    global _loaded_mode
    raw_mode = str(request.parameters.get("mode", "zero_shot")) if request.parameters else "zero_shot"
    mode = _resolve_mode(raw_mode)
    _ensure_pipeline()
    _loaded_mode = mode
    return {"status": "loaded", "profile": request.profile, "mode": _loaded_mode}


@app.post("/synthesize")
def synthesize(request: SynthesizeRequest) -> dict[str, Any]:
    params = request.parameters or {}
    raw_mode = str(params.get("mode", "zero_shot"))
    mode = _resolve_mode(raw_mode)
    pipeline = _ensure_pipeline()
    text = request.line.text
    store = _artifact_store()
    output_path, artifact_id = artifact_output(store, request.delivery, Path(request.output_path), ".wav")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunks = _run_cosyvoice(pipeline, mode, text, params)
    _write_chunks(chunks, output_path)
    return {
        "audio_path": str(output_path),
        "metadata": {"service": "cosyvoice-worker", "mode": mode},
        **artifact_response(store, artifact_id),
    }


@app.post("/unload")
def unload() -> dict[str, Any]:
    global _pipeline, _loaded_mode
    _pipeline = None
    _loaded_mode = None
    release_cuda_memory()
    return {"status": "unloaded"}


@app.get("/status")
def status() -> dict[str, Any]:
    return {
        **worker_runtime_status(loaded=_pipeline is not None, model=_loaded_mode or MODEL_DIR),
        "ready": _pipeline is not None,
        "mode": _loaded_mode,
        "repo_found": REPO_DIR.exists(),
    }


# ---------------------------------------------------------------------------
# inference helpers
# ---------------------------------------------------------------------------


def _run_cosyvoice(pipeline: Any, mode: str, text: str, params: dict[str, Any]) -> list[bytes]:
    """Dispatch to the correct CosyVoice inference method by mode.

    TODO(GPU-env): confirm the exact method names + return shape for the
    deployed build. Upstream CosyVoice returns a generator of dicts with a
    'tts_speech' numpy array; this helper collects and encodes to wav bytes.
    """
    speed = float(params.get("speed", 1.0))
    if mode == "cross_lingual":
        ref_audio = _reference_audio_path(params)
        gen = pipeline.inference_cross_lingual(text, _load_audio(ref_audio), stream=False, speed=speed)
    elif mode == "zero_shot":
        ref_audio = _reference_audio_path(params)
        prompt_text = str(params.get("prompt_text") or "")
        gen = pipeline.inference_zero_shot(text, prompt_text, _load_audio(ref_audio), stream=False, speed=speed)
    else:
        raise RuntimeError(f"unsupported CosyVoice-300M mode reached inference: {mode}")
    sample_rate = int(getattr(pipeline, "sample_rate", 22050) or 22050)
    return [_chunk_to_wav(chunk, sample_rate=sample_rate) for chunk in gen]


def _resolve_mode(raw_mode: str) -> str:
    mode = _MODE_MAP.get(raw_mode)
    if mode is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CosyVoice-300M does not support mode '{raw_mode}'; "
                "supported modes: zero_shot, cross_lingual"
            ),
        )
    return mode


def _reference_audio_path(params: dict[str, Any]) -> str:
    return str(
        params.get("prompt_audio_path")
        or params.get("voice_reference_audio")
        or params.get("ref_audio_path")
        or ""
    )


def _load_audio(path: str) -> str:
    """Let the upstream CosyVoice frontend load and resample reference audio."""
    return path


def _chunk_to_wav(chunk: Any, sample_rate: int | None = None) -> bytes:
    import io
    import numpy as np
    from scipy.io import wavfile  # type: ignore

    data = np.asarray(chunk.get("tts_speech", chunk), dtype=np.float32).reshape(-1)
    buf = io.BytesIO()
    resolved_rate = int(chunk.get("sample_rate", sample_rate or 22050)) if isinstance(chunk, dict) else int(sample_rate or 22050)
    wavfile.write(buf, resolved_rate, data)
    return buf.getvalue()


def _write_chunks(chunks: list[bytes], output_path: Path) -> None:
    """Merge WAV chunks and rebuild RIFF sizes without changing sample encoding."""
    if not chunks:
        output_path.write_bytes(b"")
        return
    if len(chunks) == 1:
        output_path.write_bytes(chunks[0])
        return
    import struct

    parsed = [_wav_format_and_data(chunk) for chunk in chunks]
    fmt = parsed[0][0]
    if any(candidate_fmt != fmt for candidate_fmt, _data in parsed[1:]):
        raise RuntimeError("CosyVoice returned incompatible WAV chunks")
    audio = b"".join(data for _candidate_fmt, data in parsed)
    fmt_padding = b"\x00" if len(fmt) % 2 else b""
    data_padding = b"\x00" if len(audio) % 2 else b""
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + fmt_padding
        + b"data"
        + struct.pack("<I", len(audio))
        + audio
        + data_padding
    )
    output_path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def _wav_format_and_data(payload: bytes) -> tuple[bytes, bytes]:
    import struct

    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise RuntimeError("CosyVoice returned an invalid WAV chunk")
    fmt: bytes | None = None
    data: bytes | None = None
    offset = 12
    while offset + 8 <= len(payload):
        chunk_id = payload[offset:offset + 4]
        chunk_size = struct.unpack_from("<I", payload, offset + 4)[0]
        start = offset + 8
        end = start + chunk_size
        if end > len(payload):
            raise RuntimeError("CosyVoice returned a truncated WAV chunk")
        if chunk_id == b"fmt ":
            fmt = payload[start:end]
        elif chunk_id == b"data":
            data = payload[start:end]
        offset = end + (chunk_size % 2)
    if fmt is None or data is None:
        raise RuntimeError("CosyVoice WAV chunk is missing fmt or data")
    return fmt, data
