"""Shared model/asset discovery helpers for TTS workers.

These functions let a worker expose a uniform "catalog" of training roles,
their weights, and reference-audio samples — without modifying the upstream
TTS repo. They are used by the GPT-SoVITS worker (and reusable for any
service whose training output follows the GPT-SoVITS convention:
``logs/<exp>/5-wav32k/*.wav`` + ``logs/<exp>/2-name2text.txt``).

The design is deliberately filesystem-only and dependency-light so it works
against any upstream GPT-SoVITS build (official or fork): it discovers roles
by scanning the weight directories the upstream config already declares, and
recovers the training experiment ("logs") name by stripping epoch/step
suffixes from the weight filename — a convention shared by all GPT-SoVITS
versions, not a fork-only feature.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = [
    "GPT_WEIGHT_SUFFIXES",
    "SOVITS_WEIGHT_SUFFIXES",
    "extract_logs_name_from_weight",
    "weight_epoch_step_score",
    "scan_weight_files",
    "read_name2text_records",
    "scan_training_samples",
]

GPT_WEIGHT_SUFFIXES = (".ckpt", ".pt")
SOVITS_WEIGHT_SUFFIXES = (".pth", ".ckpt")

_CLEANUP_PATTERNS = [
    r"(?:[-_])e\d+(?:[-_])s\d+$",
    r"(?:[-_])e\d+$",
    r"(?:[-_])s\d+$",
    r"(?:[-_])epoch=\d+(?:[-_])step=\d+$",
]


def extract_logs_name_from_weight(raw: str) -> str:
    """Recover the training experiment ("logs") name from a weight filename.

    Strips leading digits and trailing epoch/step markers such as
    ``-e50``, ``_e24_s360``, ``-epoch=30-step=1000``. This convention is shared
    by all GPT-SoVITS training versions, so it works against upstream official
    builds — not only the fork.
    """
    text = re.sub(r"^\d+", "", raw).strip()
    changed = True
    while changed:
        changed = False
        for pattern in _CLEANUP_PATTERNS:
            next_text = re.sub(pattern, "", text, flags=re.IGNORECASE)
            if next_text != text:
                text = next_text
                changed = True
    return text.strip("-_ ") or raw


def weight_epoch_step_score(stem: str) -> tuple[int, int]:
    """Return (epoch, step) for ranking weights of the same role; higher is newer."""
    epoch = max([int(m) for m in re.findall(r"(?:^|[-_])e(\d+)", stem, flags=re.IGNORECASE)] or [0])
    step = max([int(m) for m in re.findall(r"(?:^|[-_])s(\d+)", stem, flags=re.IGNORECASE)] or [0])
    return (epoch, step)


def scan_weight_files(roots: list[Path], suffixes: tuple[str, ...]) -> list[Path]:
    """Recursively gather weight files under the given roots by suffix."""
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in suffixes:
                key = str(path.resolve())
                if key not in seen:
                    seen.add(key)
                    out.append(path)
    return out


def read_name2text_records(logs_dir: Path) -> list[dict[str, str]]:
    """Parse GPT-SoVITS ``logs/<exp>/2-name2text.txt`` into sample records.

    Each line: ``wav_name\\tphones\\tword2ph\\tnorm_text`` (lang optional in a
    4th+/5th field). Returns a list of {wav_name, text, lang}.
    """
    name2text = logs_dir / "2-name2text.txt"
    if not name2text.exists():
        return []
    records: list[dict[str, str]] = []
    for line in name2text.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        wav_name = parts[0].strip()
        norm_text = parts[3].strip()
        lang = parts[4].strip() if len(parts) > 4 else ""
        if wav_name and norm_text:
            records.append({"wav_name": wav_name, "text": norm_text, "lang": lang})
    return records


def scan_training_samples(logs_dir: Path) -> list[dict[str, str]]:
    """List training-audio samples under ``logs/<exp>/5-wav32k/`` joined with
    their reference text from ``2-name2text.txt``."""
    wav_dir = logs_dir / "5-wav32k"
    if not wav_dir.is_dir():
        return []
    text_by_wav = {rec["wav_name"]: rec for rec in read_name2text_records(logs_dir)}
    samples: list[dict[str, str]] = []
    for wav in sorted(wav_dir.glob("*.wav")):
        rec = text_by_wav.get(wav.name) or text_by_wav.get(wav.stem) or {}
        samples.append(
            {
                "audio_name": wav.name,
                "audio_path": str(wav),
                "text": rec.get("text", ""),
                "lang": rec.get("lang", ""),
            }
        )
    return samples
