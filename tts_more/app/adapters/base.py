from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models import ScriptLine


@dataclass(frozen=True)
class SynthesisRequest:
    line: ScriptLine
    profile: str
    output_path: Path
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisResult:
    audio_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)

