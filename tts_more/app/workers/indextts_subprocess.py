from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName


class IndexTTSSubprocessAdapter:
    """IndexTTS adapter used by the IndexTTS worker process.

    Two modes:
    - Resident (default, env TTS_MORE_INDEXTTS_RESIDENT != "0"): the IndexTTS2
      model is loaded once in-process on first /load and held for low-latency
      synthesis. /unload frees it; the next /load rebuilds it.
    - Subprocess (env TTS_MORE_INDEXTTS_RESIDENT == "0"): each synthesis shells
      out to indextts_line_launcher.py, reloading the model per line. Higher
      latency, but no resident GPU memory between calls. Kept as a fallback.
    """

    engine = EngineName.INDEX_TTS

    def __init__(self, repo_dir: Path, python_exe: str = "python") -> None:
        self.repo_dir = repo_dir.resolve(strict=False)
        self.python_exe = python_exe
        self.loaded_profile: str | None = None
        self._resident_tts: Any = None
        # Resident is the default; set TTS_MORE_INDEXTTS_RESIDENT=0 to force the
        # per-line subprocess fallback.
        self.resident_mode = os.environ.get("TTS_MORE_INDEXTTS_RESIDENT", "1") != "0"

    def health(self) -> dict[str, Any]:
        cli = self.repo_dir / "indextts" / "cli_v2.py"
        return {
            "engine": self.engine.value,
            "ready": cli.exists(),
            "cli": str(cli),
            "mode": "resident" if self.resident_mode else "subprocess",
            "model_loaded": self._resident_tts is not None,
        }

    def load(self, profile: str) -> None:
        self.loaded_profile = profile
        if self.resident_mode:
            self._ensure_resident()

    def _ensure_resident(self) -> Any:
        if self._resident_tts is not None:
            return self._resident_tts
        repo_str = str(self.repo_dir)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)
        try:
            from indextts.infer_v2 import IndexTTS2  # type: ignore
        except Exception as exc:  # pragma: no cover - requires torch/GPU env
            raise RuntimeError(f"failed to import IndexTTS2: {exc}") from exc
        model_dir = Path(os.environ.get("TTS_MORE_INDEXTTS_MODEL_DIR", self.repo_dir / "checkpoints"))
        self._resident_tts = IndexTTS2(
            model_dir=str(model_dir),
            cfg_path=str(model_dir / "config.yaml"),
            use_fp16=os.environ.get("TTS_MORE_INDEXTTS_FP16", "0") == "1",
            use_deepspeed=os.environ.get("TTS_MORE_INDEXTTS_DEEPSPEED", "0") == "1",
            use_cuda_kernel=os.environ.get("TTS_MORE_INDEXTTS_CUDA_KERNEL", "0") == "1",
        )
        return self._resident_tts

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        voice = request.parameters.get("voice")
        if not voice:
            raise RuntimeError("IndexTTS voice reference path is required")
        output_path = request.output_path.resolve(strict=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.resident_mode:
            return self._synthesize_resident(request, voice, output_path)
        return self._synthesize_subprocess(request, voice, output_path)

    def _synthesize_resident(self, request: SynthesisRequest, voice: str, output_path: Path) -> SynthesisResult:
        tts = self._ensure_resident()
        params = request.parameters
        emo_vector = params.get("emotion_vector")
        if emo_vector is not None:
            emo_vector = tts.normalize_emo_vec(list(emo_vector), apply_bias=True)
        emotion_mode = str(params.get("emotion_mode", "emotion_text" if request.line.note else "same_as_voice"))
        emo_audio = params.get("emotion_audio") if emotion_mode == "emotion_audio" else None
        use_emo_text = emotion_mode == "emotion_text"
        emo_text = str(params.get("emotion_text") or request.line.note or "") if use_emo_text else ""
        generation_kwargs = {
            "do_sample": bool(params.get("do_sample", False)),
            "top_p": params.get("top_p"),
            "top_k": params.get("top_k"),
            "temperature": params.get("temperature"),
            "length_penalty": params.get("length_penalty"),
            "num_beams": params.get("num_beams"),
            "repetition_penalty": params.get("repetition_penalty"),
            "max_mel_tokens": params.get("max_mel_tokens"),
        }
        generation_kwargs = {k: v for k, v in generation_kwargs.items() if v is not None}
        tts.infer(
            spk_audio_prompt=voice,
            text=request.line.text,
            output_path=str(output_path),
            emo_audio_prompt=emo_audio,
            emo_alpha=params.get("emotion_weight"),
            emo_vector=emo_vector,
            use_emo_text=use_emo_text,
            emo_text=emo_text,
            use_random=bool(params.get("emotion_random", False)),
            verbose=False,
            max_text_tokens_per_segment=params.get("max_text_tokens_per_segment", 120),
            **generation_kwargs,
        )
        return SynthesisResult(audio_path=output_path, metadata={"service": "indextts-worker", "mode": "resident"})

    def _synthesize_subprocess(self, request: SynthesisRequest, voice: str, output_path: Path) -> SynthesisResult:
        project_root = Path(__file__).resolve().parents[3]
        launcher = project_root / "backend" / "app" / "workers" / "indextts_line_launcher.py"
        command = [
            self.python_exe,
            str(launcher),
            "--text",
            request.line.text,
            "--voice",
            str(voice),
            "--output",
            str(output_path),
            "--repo-dir",
            str(self.repo_dir),
        ]
        command += self._parameter_args(request)
        completed = subprocess.run(
            command,
            cwd=self.repo_dir,
            text=True,
            capture_output=True,
            check=False,
            timeout=float(request.parameters.get("timeout_seconds", 900)),
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip())
        return SynthesisResult(audio_path=output_path, metadata={"stdout": completed.stdout.strip(), "mode": "subprocess"})

    def unload(self) -> None:
        self.loaded_profile = None
        self._resident_tts = None  # release resident GPU memory

    def _parameter_args(self, request: SynthesisRequest) -> list[str]:
        params = request.parameters
        args: list[str] = []
        model_dir = params.get("model_dir")
        if model_dir:
            args += ["--model-dir", str(model_dir)]
        emotion_mode = str(params.get("emotion_mode", "emotion_text" if request.line.note else "same_as_voice"))
        if emotion_mode == "emotion_audio" and params.get("emotion_audio"):
            args += ["--emotion-audio", str(params["emotion_audio"])]
        elif emotion_mode == "emotion_vector" and params.get("emotion_vector") is not None:
            args += ["--emotion-vector", ",".join(str(item) for item in params.get("emotion_vector", []))]
        elif emotion_mode == "emotion_text":
            emotion_text = str(params.get("emotion_text") or request.line.note or "")
            if emotion_text:
                args += ["--emotion-text", emotion_text]
        if params.get("emotion_weight") is not None:
            args += ["--emotion-weight", str(params["emotion_weight"])]
        if params.get("emotion_random"):
            args.append("--emotion-random")
        bool_map = {
            "do_sample": "--do-sample",
        }
        for key, flag in bool_map.items():
            if params.get(key) is not None:
                args.append(flag if params.get(key) else f"--no-{flag[2:]}")
        numeric_flags = {
            "top_p": "--top-p",
            "top_k": "--top-k",
            "temperature": "--temperature",
            "length_penalty": "--length-penalty",
            "num_beams": "--num-beams",
            "repetition_penalty": "--repetition-penalty",
            "max_mel_tokens": "--max-mel-tokens",
            "max_text_tokens_per_segment": "--max-text-tokens-per-segment",
        }
        for key, flag in numeric_flags.items():
            if params.get(key) is not None:
                args += [flag, str(params[key])]
        return args
