from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one IndexTTS2 line with full TTS More parameters.")
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--model-dir")
    parser.add_argument("--text", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--emotion-audio")
    parser.add_argument("--emotion-text")
    parser.add_argument("--emotion-vector")
    parser.add_argument("--emotion-weight", type=float, default=0.65)
    parser.add_argument("--emotion-random", action="store_true")
    parser.add_argument("--do-sample", dest="do_sample", action="store_true", default=True)
    parser.add_argument("--no-do-sample", dest="do_sample", action="store_false")
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--length-penalty", type=float, default=0.0)
    parser.add_argument("--num-beams", type=int, default=3)
    parser.add_argument("--repetition-penalty", type=float, default=10.0)
    parser.add_argument("--max-mel-tokens", type=int, default=1500)
    parser.add_argument("--max-text-tokens-per-segment", type=int, default=120)
    parser.add_argument("--fp16", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--deepspeed", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--cuda-kernel", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve(strict=False)
    model_dir = Path(args.model_dir or os.environ.get("TTS_MORE_INDEXTTS_MODEL_DIR", repo_dir / "checkpoints")).resolve(strict=False)
    output_path = Path(args.output).resolve(strict=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(repo_dir))

    from indextts.infer_v2 import IndexTTS2  # type: ignore

    tts = IndexTTS2(
        model_dir=str(model_dir),
        cfg_path=str(model_dir / "config.yaml"),
        use_fp16=bool(args.fp16) if args.fp16 is not None else False,
        use_deepspeed=bool(args.deepspeed) if args.deepspeed is not None else False,
        use_cuda_kernel=bool(args.cuda_kernel) if args.cuda_kernel is not None else False,
    )
    emo_vector = _emotion_vector(args.emotion_vector)
    if emo_vector is not None:
        emo_vector = tts.normalize_emo_vec(emo_vector, apply_bias=True)
    generation_kwargs = {
        "do_sample": bool(args.do_sample),
        "top_p": args.top_p,
        "top_k": args.top_k if args.top_k > 0 else None,
        "temperature": args.temperature,
        "length_penalty": args.length_penalty,
        "num_beams": args.num_beams,
        "repetition_penalty": args.repetition_penalty,
        "max_mel_tokens": args.max_mel_tokens,
    }
    tts.infer(
        spk_audio_prompt=args.voice,
        text=args.text,
        output_path=str(output_path),
        emo_audio_prompt=args.emotion_audio,
        emo_alpha=args.emotion_weight,
        emo_vector=emo_vector,
        use_emo_text=bool(args.emotion_text),
        emo_text=args.emotion_text,
        use_random=args.emotion_random,
        verbose=args.verbose,
        max_text_tokens_per_segment=args.max_text_tokens_per_segment,
        **generation_kwargs,
    )
    print(f"Generated: {output_path}")
    return 0


def _emotion_vector(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if len(values) < 8:
        values.extend([0.0] * (8 - len(values)))
    return values[:8]


if __name__ == "__main__":
    raise SystemExit(main())
