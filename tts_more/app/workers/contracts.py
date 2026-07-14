from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from app.models import ScriptLine


class LoadRequest(BaseModel):
    profile: str
    parameters: dict[str, Any] = {}


class SynthesizeRequest(BaseModel):
    line: ScriptLine
    profile: str
    output_path: Path
    parameters: dict[str, Any] = {}
    delivery: Literal["path", "artifact"] = "path"
