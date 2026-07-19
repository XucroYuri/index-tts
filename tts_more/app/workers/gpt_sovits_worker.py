"""Non-invasive embedded worker for GPT-SoVITS.

This is a standalone FastAPI app that runs INSIDE the GPT-SoVITS repo's Python
environment, imports the upstream inference pipeline directly, and exposes a
clean REST contract. It does NOT modify any file in the upstream GPT-SoVITS
repo and does NOT depend on the fork's Gradio UI changes — it works against
the official upstream build.

Start it (from the project root, using the GPT-SoVITS venv):

    TTS_MORE_GPTSOVITS_REPO=repo/GPT-SoVITS \
    .venv/bin/python -m uvicorn app.workers.gpt_sovits_worker:app \
        --app-dir backend --host 127.0.0.1 --port 9880

Exposed endpoints:
  Standard worker contract (consumed by HttpTTSServiceClient):
    GET  /health
    GET  /capabilities
    POST /load          {profile, parameters}  — switch GPT/SoVITS weights
    POST /synthesize    {line, profile, output_path, parameters}
    POST /unload        — release the resident pipeline (frees GPU memory)

  Model/reference discovery (replaces Gradio scraping + fork api_v2 patches):
    GET  /models                       — list roles + weights + sample counts
    GET  /models/{name}/samples        — training audio + reference text
    GET  /status                       — current weights/version/device
    POST /upload_ref                   — upload reference audio (cross-machine)

The pipeline is constructed once at startup and held resident for low latency;
``/unload`` drops it and the next ``/load`` rebuilds it.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

# The standard worker request schemas.
from app.workers.artifacts import ArtifactStore, artifact_output, artifact_response, register_artifact_routes
from app.workers.contracts import LoadRequest, SynthesizeRequest
from app.workers.runtime import release_cuda_memory, worker_runtime_status

# --- repo bootstrap: put the upstream GPT-SoVITS repo on the path and chdir ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO_DIR = Path(os.environ.get("TTS_MORE_GPTSOVITS_REPO", "repo/GPT-SoVITS")).resolve(strict=False)
CONFIG_YAML = os.environ.get("TTS_MORE_GPTSOVITS_CONFIG", "GPT_SoVITS/configs/tts_infer.yaml")

# The worker imports the upstream pipeline lazily so that simply importing this
# module (e.g. for OpenAPI generation on the orchestrator side) does not require
# torch/CUDA. _pipeline / _config are populated by _ensure_pipeline().
_pipeline: Any = None
_config: Any = None
_loaded_profile: str | None = None
_weight_roots: list[Path] = []
_dll_directories: list[Any] = []
_dll_directory_paths: set[str] = set()


def _bootstrap_repo() -> None:
    """Make the configured upstream checkout importable on any host path."""
    if not REPO_DIR.exists():
        return  # will surface as a health error; lets the app still import
    package_dir = REPO_DIR / "GPT_SoVITS"
    for directory in (REPO_DIR, package_dir):
        directory_str = str(directory)
        if directory_str not in sys.path:
            sys.path.insert(0, directory_str)

    # Upstream modules mix package imports (``GPT_SoVITS.*``) with top-level
    # imports (``AR``, ``TTS_infer_pack``, ``config``).  Both directories must
    # therefore be present; deriving them from REPO_DIR avoids machine-specific
    # PYTHONPATH configuration.
    configured_package_root = os.environ.get("TTS_MORE_PACKAGE_ROOT")
    packaged_ffmpeg_bin = (
        Path(configured_package_root).resolve(strict=False) / "ffmpeg-shared" / "bin"
        if configured_package_root
        else None
    )
    ffmpeg_bin = (
        packaged_ffmpeg_bin
        if packaged_ffmpeg_bin is not None and packaged_ffmpeg_bin.is_dir()
        else REPO_DIR / "ffmpeg-shared" / "bin"
    )
    if ffmpeg_bin.is_dir():
        ffmpeg_str = str(ffmpeg_bin)
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if ffmpeg_str not in path_entries:
            os.environ["PATH"] = os.pathsep.join([ffmpeg_str, *path_entries])
        add_dll_directory = getattr(os, "add_dll_directory", None)
        normalized = os.path.normcase(os.path.normpath(ffmpeg_str))
        if add_dll_directory is not None and normalized not in _dll_directory_paths:
            _dll_directories.append(add_dll_directory(ffmpeg_str))
            _dll_directory_paths.add(normalized)
    try:
        os.chdir(REPO_DIR)
    except OSError:
        pass


def _ensure_pipeline() -> Any:
    """Construct the resident TTS pipeline on first use (lazy load)."""
    global _pipeline, _config
    if _pipeline is not None:
        return _pipeline
    if not REPO_DIR.exists():
        raise RuntimeError(f"GPT-SoVITS repo not found at {REPO_DIR}")
    _bootstrap_repo()
    try:
        from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # type: ignore
    except Exception as exc:  # pragma: no cover - requires torch/GPU env
        raise RuntimeError(f"failed to import GPT-SoVITS pipeline: {exc}") from exc
    _relax_reference_duration_limit(TTS)
    _config = TTS_Config(CONFIG_YAML)
    _pipeline = TTS(_config)
    return _pipeline


def _relax_reference_duration_limit(tts_cls: Any) -> None:
    """Remove the upstream 3–10s reference-audio hard limit.

    GPT-SoVITS's ``_set_prompt_semantic`` raises ``OSError`` when the reference
    audio is outside 3–10s (48000–160000 samples at 16kHz). TTS More does not
    treat this as a hard constraint — longer/shorter references are legitimate
    and the upstream limit would block valid inputs. We replace the method with
    a copy that skips only the length check, preserving all the semantic-
    extraction logic (librosa load, hubert feature, codes, prompt_semantic).

    This is a process-local monkey-patch: it touches NO upstream file, so it
    works against the official upstream build and the fork alike. Set
    TTS_MORE_ENFORCE_REF_DURATION=1 to keep the original hard limit.
    """
    if os.environ.get("TTS_MORE_ENFORCE_REF_DURATION", "0") == "1":
        return  # operator opted into the original upstream behavior

    import types  # noqa: required for the bound method replacement

    try:
        import librosa  # type: ignore  # noqa: provided by the GPT-SoVITS env
        import torch  # type: ignore  # noqa: provided by the GPT-SoVITS env
        import numpy as np  # type: ignore  # noqa
    except Exception:  # pragma: no cover - requires torch env
        return  # cannot patch without the deps; upstream limit stays

    def _set_prompt_semantic_nolimit(self: Any, ref_wav_path: str) -> None:
        zero_wav = np.zeros(
            int(self.configs.sampling_rate * 0.3),
            dtype=np.float16 if self.configs.is_half else np.float32,
        )
        with torch.no_grad():
            wav16k, sr = librosa.load(ref_wav_path, sr=16000)
            # Upstream raises OSError here if wav16k.shape[0] is outside
            # [48000, 160000] (3–10s). TTS More allows any duration; very
            # short clips may still produce poor results, but that is a
            # quality tradeoff the caller chooses, not a hard error.
            wav16k = torch.from_numpy(wav16k)
            zero_wav_torch = torch.from_numpy(zero_wav)
            wav16k = wav16k.to(self.configs.device)
            zero_wav_torch = zero_wav_torch.to(self.configs.device)
            if self.configs.is_half:
                wav16k = wav16k.half()
                zero_wav_torch = zero_wav_torch.half()
            wav16k = torch.cat([wav16k, zero_wav_torch])
            hubert_feature = self.cnhuhbert_model.model(wav16k.unsqueeze(0))["last_hidden_state"].transpose(1, 2)
            codes = self.vits_model.extract_latent(hubert_feature)
            prompt_semantic = codes[0, 0].to(self.configs.device)
            self.prompt_cache["prompt_semantic"] = prompt_semantic

    tts_cls._set_prompt_semantic = _set_prompt_semantic_nolimit


def _resolve_weight_roots() -> list[Path]:
    """Return the weight directories the upstream config declares, without
    requiring the resident pipeline (filesystem-only)."""
    if not REPO_DIR.exists():
        return []
    _bootstrap_repo()
    try:
        import config as gs_config  # type: ignore  # GPT-SoVITS repo config.py
    except Exception:
        return []
    roots: list[Path] = []
    for attr in ("GPT_weight_root", "SoVITS_weight_root"):
        for name in getattr(gs_config, attr, []) or []:
            roots.append(REPO_DIR / name)
    return roots


app = FastAPI(title="TTS More GPT-SoVITS Worker", version="0.1.0")


def _artifact_store() -> ArtifactStore:
    configured_limit = os.environ.get("GPT_SOVITS_MAX_UPLOAD_BYTES")
    max_upload_bytes = int(configured_limit) if configured_limit else None
    configured_root = os.environ.get("TTS_MORE_ARTIFACT_ROOT")
    artifact_root = Path(configured_root).expanduser() if configured_root else (
        PROJECT_ROOT / "data" / "runtime" / "worker-artifacts" / "gpt-sovits"
    )
    if not artifact_root.is_absolute():
        artifact_root = PROJECT_ROOT / artifact_root
    return ArtifactStore(artifact_root, max_upload_bytes=max_upload_bytes)


register_artifact_routes(app, _artifact_store)


# ---------------------------------------------------------------------------
# Standard worker contract
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    ready = _pipeline is not None or REPO_DIR.exists()
    return {
        "ready": bool(ready),
        "worker": "gpt-sovits-standard",
        "tts_more_commit": os.environ.get("TTS_MORE_APP_COMMIT", ""),
        "repo_found": REPO_DIR.exists(),
        "pipeline_loaded": _pipeline is not None,
    }


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "capabilities": [
            "tts",
            "trained-weights-voice",
            "reference-audio-voice",
            "gpt-weights",
            "sovits-weights",
            "artifact-transfer",
        ]
    }


@app.post("/load")
def load(request: LoadRequest) -> dict[str, Any]:
    """Switch GPT/SoVITS weights. ``parameters`` may carry:
    gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, prompt_lang.
    """
    global _loaded_profile
    pipeline = _ensure_pipeline()
    params = request.parameters or {}
    gpt = params.get("gpt_weights_path")
    sovits = params.get("sovits_weights_path")
    if gpt:
        pipeline.init_t2s_weights(gpt)
    if sovits:
        pipeline.init_vits_weights(sovits)
    ref = params.get("ref_audio_path")
    if ref:
        pipeline.set_ref_audio(ref)
    _loaded_profile = request.profile
    return {"status": "loaded", "profile": request.profile}


@app.post("/synthesize")
def synthesize(request: SynthesizeRequest) -> dict[str, Any]:
    params = request.parameters or {}
    media_type = str(params.get("media_type", "wav")).strip().casefold()
    if media_type != "wav":
        raise HTTPException(status_code=400, detail="GPT-SoVITS worker supports WAV output only")
    if request.delivery == "path" and request.output_path.suffix.casefold() != ".wav":
        raise HTTPException(status_code=400, detail="GPT-SoVITS path delivery requires a .wav output path")
    pipeline = _ensure_pipeline()
    inputs: dict[str, Any] = {
        "text": request.line.text,
        "text_lang": params.get("text_lang", "zh"),
        "ref_audio_path": params.get("ref_audio_path", ""),
        "prompt_text": params.get("prompt_text", ""),
        "prompt_lang": params.get("prompt_lang", "zh"),
        "text_split_method": params.get("text_split_method", "cut1"),
        "speed_factor": params.get("speed_factor", 1.0),
        "media_type": media_type,
        "streaming_mode": False,
        "return_fragment": True,
    }
    for opt in ("top_k", "top_p", "temperature", "batch_size", "batch_threshold",
                "split_bucket", "fragment_interval", "seed", "parallel_infer",
                "repetition_penalty", "sample_steps", "super_sampling"):
        if opt in params:
            inputs[opt] = params[opt]
    store = _artifact_store()
    output_path, artifact_id = artifact_output(
        store,
        request.delivery,
        Path(request.output_path),
        str(inputs["media_type"]),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sampling_rate, audio = _normalize_tts_run_output(pipeline.run(inputs))
    _write_audio(audio, sampling_rate, output_path, inputs["media_type"])
    return {
        "audio_path": str(output_path),
        "metadata": {"sampling_rate": int(sampling_rate), "service": "gpt-sovits-worker"},
        **artifact_response(store, artifact_id),
    }


@app.post("/unload")
def unload() -> dict[str, Any]:
    """Release the resident pipeline to free GPU memory. Next /load rebuilds it."""
    global _pipeline, _config, _loaded_profile
    _pipeline = None
    _config = None
    _loaded_profile = None
    release_cuda_memory()
    return {"status": "unloaded"}


# ---------------------------------------------------------------------------
# Model/reference discovery (non-invasive; works against upstream official)
# ---------------------------------------------------------------------------


@app.get("/models")
def models() -> dict[str, Any]:
    """List training roles discovered from the weight directories the upstream
    config declares. Roles are matched by the shared logs-name prefix (epoch/step
    suffixes stripped), so GPT and SoVITS weights for the same role pair up
    without depending on any fork-specific dropdown api_name."""
    from app.workers.discovery import (
        GPT_WEIGHT_SUFFIXES,
        SOVITS_WEIGHT_SUFFIXES,
        extract_logs_name_from_weight,
        scan_weight_files,
        weight_epoch_step_score,
    )

    # Discovery is filesystem-only — it must NOT require the resident pipeline
    # (so it works even before any /load and on a machine without GPU).
    weight_roots = _resolve_weight_roots()
    gpt_roots = [r for r in weight_roots if "gpt" in r.name.lower()]
    sovits_roots = [r for r in weight_roots if "sovits" in r.name.lower()]
    if not gpt_roots and not sovits_roots:
        gpt_roots = weight_roots
        sovits_roots = weight_roots
    gpt_files = scan_weight_files(gpt_roots, GPT_WEIGHT_SUFFIXES)
    sovits_files = scan_weight_files(sovits_roots, SOVITS_WEIGHT_SUFFIXES)

    roles: dict[str, dict[str, Any]] = {}
    for path in gpt_files:
        name = extract_logs_name_from_weight(path.stem)
        roles.setdefault(name, {"name": name, "gpt_weights": [], "sovits_weights": []})
        roles[name]["gpt_weights"].append(str(path))
    for path in sovits_files:
        name = extract_logs_name_from_weight(path.stem)
        roles.setdefault(name, {"name": name, "gpt_weights": [], "sovits_weights": []})
        roles[name]["sovits_weights"].append(str(path))

    # Rank weights newest-first and attach sample counts from logs/.
    out = []
    for role in roles.values():
        role["gpt_weights"].sort(key=lambda p: weight_epoch_step_score(Path(p).stem), reverse=True)
        role["sovits_weights"].sort(key=lambda p: weight_epoch_step_score(Path(p).stem), reverse=True)
        samples = _count_training_samples(role["name"])
        role["sample_count"] = samples["count"]
        role["has_training_data"] = samples["count"] > 0
        out.append(role)
    out.sort(key=lambda r: r["name"])
    return {"models": out}


@app.get("/models/{model_name}/samples")
def model_samples(model_name: str) -> dict[str, Any]:
    """List training-audio samples + reference text for a role."""
    from app.workers.discovery import scan_training_samples

    logs_dir = REPO_DIR / "GPT_SoVITS" / "logs" / model_name
    if not logs_dir.exists():
        logs_dir = REPO_DIR / "logs" / model_name
    if not logs_dir.exists():
        return {"samples": []}
    return {"samples": scan_training_samples(logs_dir)}


@app.get("/status")
def status() -> dict[str, Any]:
    """Current loaded weights / version / device."""
    cfg = _config
    return {
        **worker_runtime_status(
            loaded=_pipeline is not None,
            model=_loaded_profile or getattr(cfg, "version", None),
            device_hint=str(getattr(cfg, "device", "") or "") or None,
        ),
        "ready": _pipeline is not None,
        "version": getattr(cfg, "version", None),
        "t2s_weights_path": getattr(cfg, "t2s_weights_path", None),
        "vits_weights_path": getattr(cfg, "vits_weights_path", None),
        "languages": list(getattr(cfg, "languages", []) or []),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _count_training_samples(role_name: str) -> dict[str, int]:
    from app.workers.discovery import read_name2text_records

    for logs_root in (REPO_DIR / "GPT_SoVITS" / "logs", REPO_DIR / "logs"):
        logs_dir = logs_root / role_name
        if logs_dir.exists():
            return {"count": len(read_name2text_records(logs_dir))}
    return {"count": 0}


def _normalize_tts_run_output(result: Any) -> tuple[int, Any]:
    """Accept GPT-SoVITS tuple output or the upstream generator form."""
    if isinstance(result, tuple) and len(result) == 2:
        return int(result[0]), result[1]

    try:
        iterator = iter(result)
    except TypeError as exc:
        raise RuntimeError("GPT-SoVITS TTS.run returned an unsupported result") from exc

    chunks: list[tuple[int, Any]] = []
    for chunk in iterator:
        if isinstance(chunk, tuple) and len(chunk) >= 2:
            chunks.append((int(chunk[0]), chunk[1]))
            continue
        raise RuntimeError("GPT-SoVITS TTS.run yielded an unsupported audio chunk")
    if not chunks:
        raise RuntimeError("GPT-SoVITS TTS.run yielded no audio")
    if len(chunks) == 1:
        return chunks[0]

    sampling_rate = chunks[0][0]
    try:
        import numpy as np

        return sampling_rate, np.concatenate([np.asarray(audio) for _, audio in chunks])
    except Exception:
        merged: list[Any] = []
        for _, audio in chunks:
            merged.extend(list(audio))
        return sampling_rate, merged


def _write_audio(audio: Any, sampling_rate: int, output_path: Path, media_type: str) -> None:
    """Write the np.ndarray audio returned by TTS.run() to disk as wav."""
    import numpy as np  # local import; torch env provides numpy
    from scipy.io import wavfile  # type: ignore

    data = np.asarray(audio, dtype=np.float32)
    wavfile.write(str(output_path), int(sampling_rate), data)
