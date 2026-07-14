from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class EngineName(str, Enum):
    GPT_SOVITS = "gpt-sovits"
    INDEX_TTS = "indextts"
    COSYVOICE = "cosyvoice"
    VIBEVOICE = "vibevoice"
    COMMERCIAL = "commercial"


class ProviderType(str, Enum):
    GPT_SOVITS = "gpt-sovits"
    INDEX_TTS = "indextts"
    COSYVOICE = "cosyvoice"
    VIBEVOICE = "vibevoice"
    OPENAI = "openai"
    GEMINI = "gemini"
    XAI = "xai"
    VOLCENGINE = "volcengine"
    GENERIC_HTTP = "generic-http"


SourceProfile = Literal["local_repo", "local_endpoint", "lan_endpoint", "cloud_endpoint", "api_placeholder"]
CatalogProvider = Literal["gpt-sovits", "indextts", "cosyvoice"]
SetupState = Literal[
    "not_configured",
    "repo_missing",
    "repo_found",
    "env_missing",
    "endpoint_unreachable",
    "partial",
    "ready",
]


PROVIDER_ENGINE_DEFAULTS: dict[ProviderType, EngineName] = {
    ProviderType.GPT_SOVITS: EngineName.GPT_SOVITS,
    ProviderType.INDEX_TTS: EngineName.INDEX_TTS,
    ProviderType.COSYVOICE: EngineName.COSYVOICE,
    ProviderType.VIBEVOICE: EngineName.VIBEVOICE,
    ProviderType.OPENAI: EngineName.COMMERCIAL,
    ProviderType.GEMINI: EngineName.COMMERCIAL,
    ProviderType.XAI: EngineName.COMMERCIAL,
    ProviderType.VOLCENGINE: EngineName.COMMERCIAL,
    ProviderType.GENERIC_HTTP: EngineName.COMMERCIAL,
}


class ProjectCharacterMode(str, Enum):
    REFERENCE = "reference"
    SNAPSHOT = "snapshot"


class VoiceBinding(BaseModel):
    binding_id: str
    provider_type: ProviderType
    service_id: str | None = None
    fallback_services: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class VoiceProfile(BaseModel):
    id: str
    name: str
    engine: EngineName
    service_id: str | None = None
    fallback_services: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    bindings: list[VoiceBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_legacy_binding(self) -> "VoiceProfile":
        if self.bindings:
            return self
        provider_type = ProviderType(self.engine.value) if self.engine.value in ProviderType._value2member_map_ else ProviderType.GENERIC_HTTP
        self.bindings = [
            VoiceBinding(
                binding_id=self.id,
                provider_type=provider_type,
                service_id=self.service_id,
                fallback_services=self.fallback_services,
                capabilities=[],
                config=self.config,
            )
        ]
        return self


class TTSServiceEndpoint(BaseModel):
    service_id: str
    service_kind: Literal["tts", "llm-parser"] = "tts"
    display_name: str = ""
    engine: EngineName | None = None
    provider_type: ProviderType | None = None
    api_contract: str = "tts-more-v1"
    base_url: str
    mode: Literal["local", "external"] = "local"
    network_scope: Literal["localhost", "lan", "public", "commercial"] = "localhost"
    managed: bool = True
    enabled: bool = True
    poll_interval_seconds: int = Field(default=5, ge=1, le=300)
    repo_path: str | None = None
    start_command: list[str] = Field(default_factory=list)
    start_cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    health_url: str | None = None
    resource_group: str = "local-gpu-0"
    capacity: int = Field(default=1, ge=1)
    priority: int = 100
    capabilities: list[str] = Field(default_factory=lambda: ["tts"])
    auth_header_env: str | None = None
    auth_profile: dict[str, str] = Field(default_factory=dict)
    default_params: dict[str, Any] = Field(default_factory=dict)
    cost_policy: dict[str, Any] = Field(default_factory=dict)
    source_profile: SourceProfile | None = None
    catalog_provider: CatalogProvider | None = None
    setup_state: SetupState | None = None

    @model_validator(mode="after")
    def populate_compat_fields(self) -> "TTSServiceEndpoint":
        if not self.display_name:
            self.display_name = self.service_id
        if self.mode == "external" and self.network_scope == "localhost" and self.source_profile != "local_endpoint":
            self.network_scope = "commercial" if self.cost_policy or "paid_provider" in self.capabilities else "lan"
        if self.mode == "external":
            self.managed = False
        if self.provider_type is None:
            if self.engine and self.engine.value in ProviderType._value2member_map_:
                self.provider_type = ProviderType(self.engine.value)
            else:
                self.provider_type = ProviderType.GENERIC_HTTP
        if self.engine is None:
            self.engine = PROVIDER_ENGINE_DEFAULTS[self.provider_type]
        if self.source_profile is None:
            if "paid_provider" in self.capabilities or self.network_scope == "commercial":
                self.source_profile = "api_placeholder"
            elif self.repo_path:
                self.source_profile = "local_repo"
            elif self.network_scope == "localhost":
                self.source_profile = "local_endpoint"
            elif self.network_scope == "lan":
                self.source_profile = "lan_endpoint"
            else:
                self.source_profile = "cloud_endpoint"
        if self.catalog_provider is None and self.provider_type in {
            ProviderType.GPT_SOVITS,
            ProviderType.INDEX_TTS,
            ProviderType.COSYVOICE,
        }:
            self.catalog_provider = self.provider_type.value  # type: ignore[assignment]
        # Only assign a provider-specific default contract when none was
        # explicitly set. tts-more-v1 is a legitimate worker contract (not a
        # "unset" sentinel), so an explicitly-configured tts-more-v1 endpoint
        # for an open-source provider must be preserved — it routes to the
        # non-invasive worker rather than the Gradio fallback.
        if not self.api_contract:
            contract_by_provider = {
                ProviderType.GPT_SOVITS: "gradio-gpt-sovits-webui",
                ProviderType.INDEX_TTS: "gradio-indextts2-webui",
                ProviderType.COSYVOICE: "gradio-cosyvoice-webui",
                ProviderType.VIBEVOICE: "tts-more-v1",
                ProviderType.OPENAI: "openai-speech-v1",
                ProviderType.GEMINI: "gemini-tts-v1",
                ProviderType.XAI: "xai-tts-v1",
                ProviderType.VOLCENGINE: "volcengine-tts-v1",
                ProviderType.GENERIC_HTTP: "tts-more-v1",
            }
            self.api_contract = contract_by_provider[self.provider_type]
        return self


class GPTSoVITSBindingConfig(BaseModel):
    gpt_weights_path: str = ""
    sovits_weights_path: str = ""
    ref_audio_path: str = ""
    aux_ref_audio_paths: list[str] = Field(default_factory=list)
    prompt_text: str = ""
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    text_split_method: str = "cut5"
    top_k: int = 15
    top_p: float = 1.0
    temperature: float = 1.0
    batch_size: int = 1
    batch_threshold: float = 0.75
    split_bucket: bool = True
    speed_factor: float = 1.0
    fragment_interval: float = 0.3
    seed: int = -1
    parallel_infer: bool = True
    repetition_penalty: float = 1.35
    sample_steps: int = 32
    super_sampling: bool = False
    media_type: Literal["wav", "raw", "ogg", "aac"] = "wav"


class IndexTTSBindingConfig(BaseModel):
    voice: str = ""
    emotion_mode: Literal["same_as_voice", "emotion_audio", "emotion_vector", "emotion_text"] = "same_as_voice"
    emotion_audio: str = ""
    emotion_text: str = ""
    emotion_vector: list[float] = Field(default_factory=lambda: [0.0] * 8)
    emotion_weight: float = 0.65
    emotion_random: bool = False
    do_sample: bool = True
    top_p: float = 0.8
    top_k: int = 30
    temperature: float = 0.8
    length_penalty: float = 0.0
    num_beams: int = 3
    repetition_penalty: float = 10.0
    max_mel_tokens: int = 1500
    max_text_tokens_per_segment: int = 120


class ReferenceAudioSample(BaseModel):
    path: str
    text: str = ""
    text_source: Literal["sidecar", "manual", "none"] = "none"
    duration_seconds: float | None = None


class ReferenceAudioGroup(BaseModel):
    id: str
    name: str
    paths: list[str] = Field(default_factory=list)
    copied_paths: list[str] = Field(default_factory=list)
    samples: list[ReferenceAudioSample] = Field(default_factory=list)


class Character(BaseModel):
    id: str
    name: str
    avatar_path: str | None = None
    aliases: list[str] = Field(default_factory=list)
    nicknames: list[str] = Field(default_factory=list)
    match_names: list[str] = Field(default_factory=list)
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    library_status: Literal["draft", "partial", "confirmed", "archived"] = "confirmed"
    source_assets: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reference_audio_groups: list[ReferenceAudioGroup] = Field(default_factory=list)
    profiles: list[VoiceProfile] = Field(default_factory=list)
    default_engine: EngineName | None = None
    default_profile: str | None = None
    fallback_profiles: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_default_profile(self) -> "Character":
        if self.default_profile is None:
            return self
        profile_ids = {profile.id for profile in self.profiles}
        if self.default_profile not in profile_ids:
            raise ValueError(f"default profile {self.default_profile!r} is not defined")
        return self


class ProjectCharacter(BaseModel):
    project_character_id: str
    name: str
    library_character_id: str | None = None
    mode: ProjectCharacterMode = ProjectCharacterMode.REFERENCE
    character_snapshot: Character | None = None
    project_binding: VoiceBinding | None = None
    match_confidence: float | None = None
    match_status: Literal["matched", "unmatched", "ambiguous", "manual"] | None = None


class ScriptLine(BaseModel):
    id: str
    line_uid: str = ""
    character_id: str
    text: str
    note: str = ""
    language: str | None = None
    engine_override: EngineName | None = None
    profile_override: str | None = None
    binding_override: str | None = None
    service_override: str | None = None
    temporary_binding: VoiceBinding | None = None

    @model_validator(mode="after")
    def populate_line_uid(self) -> "ScriptLine":
        if not self.line_uid:
            self.line_uid = self.id
        return self


class ScriptRevision(BaseModel):
    revision_id: str
    source_markdown: str
    parent_revision_id: str | None = None
    summary: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ParseRevision(BaseModel):
    revision_id: str
    script_revision_id: str
    parent_parse_revision_id: str | None = None
    provider: str = "legacy"
    warnings: list[str] = Field(default_factory=list)
    project_characters: list[ProjectCharacter] = Field(default_factory=list)
    lines: list[ScriptLine] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScriptProject(BaseModel):
    title: str
    default_language: str = "zh"
    active_script_revision_id: str | None = None
    active_parse_revision_id: str | None = None
    script_revisions: list[ScriptRevision] = Field(default_factory=list)
    parse_revisions: list[ParseRevision] = Field(default_factory=list)
    project_characters: list[ProjectCharacter] = Field(default_factory=list)
    lines: list[ScriptLine] = Field(default_factory=list)

    @model_validator(mode="after")
    def materialize_revision_tree_and_validate_lines(self) -> "ScriptProject":
        if not self.script_revisions:
            self.script_revisions = [
                ScriptRevision(
                    revision_id="script-r001",
                    source_markdown=_project_lines_to_markdown(self.project_characters, self.lines),
                    summary="Initial imported script",
                )
            ]
        if self.active_script_revision_id is None:
            self.active_script_revision_id = self.script_revisions[-1].revision_id

        if not self.parse_revisions:
            parse_revision_id = "parse-r001"
            initial_lines = [_line_with_revision_uid(line, parse_revision_id) for line in self.lines]
            self.parse_revisions = [
                ParseRevision(
                    revision_id=parse_revision_id,
                    script_revision_id=self.active_script_revision_id,
                    project_characters=self.project_characters,
                    lines=initial_lines,
                )
            ]
        if self.active_parse_revision_id is None:
            self.active_parse_revision_id = self.parse_revisions[-1].revision_id

        active_parse = next((item for item in self.parse_revisions if item.revision_id == self.active_parse_revision_id), None)
        if active_parse is not None:
            self.lines = [_line_with_revision_uid(line, active_parse.revision_id) for line in active_parse.lines]
            if active_parse.project_characters and not self.project_characters:
                self.project_characters = active_parse.project_characters

        seen: set[str] = set()
        for line in self.lines:
            if line.id in seen:
                raise ValueError(f"duplicate line id: {line.id}")
            seen.add(line.id)
        return self


class GenerationTask(BaseModel):
    line: ScriptLine
    engine: EngineName
    profile: str
    service_id: str | None = None
    fallback_service_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    binding_id: str | None = None
    provider_type: ProviderType | None = None
    required_capabilities: list[str] = Field(default_factory=list)


class TTSIntent(BaseModel):
    text: str
    character_id: str
    language: str | None = None
    note: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    bindings: list[VoiceBinding] = Field(default_factory=list)
    service_id: str | None = None
    fallback_service_ids: list[str] = Field(default_factory=list)
    output_format: str = "wav"


GenerationStatus = Literal["queued", "loading", "running", "finalizing", "completed", "failed", "cancelled"]


class GenerationVersion(BaseModel):
    version_id: str
    line_uid: str | None = None
    script_revision_id: str | None = None
    parse_revision_id: str | None = None
    engine: EngineName
    profile: str
    service_id: str | None = None
    resource_group: str | None = None
    provider_type: ProviderType | None = None
    binding_id: str | None = None
    binding_snapshot: dict[str, Any] | None = None
    requested_load_signature: str | None = None
    verified_load_signature: str | None = None
    status: GenerationStatus
    audio_path: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    log_summary: str = ""
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GenerationQueueItem(BaseModel):
    task_id: str
    line_id: str
    line_uid: str | None = None
    status: GenerationStatus = "queued"
    progress: float = 0.0
    queue_position: int | None = None
    cluster_key: str = ""
    cluster_size: int | None = None
    cluster_position: int | None = None
    load_signature: str | None = None
    service_id: str | None = None
    resource_group: str | None = None
    error: str | None = None
    version_id: str | None = None


class GenerationJob(BaseModel):
    job_id: str
    project_id: str
    status: GenerationStatus = "queued"
    progress: float = 0.0
    items: list[GenerationQueueItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None


class LineGenerationHistory(BaseModel):
    line_id: str
    versions: list[GenerationVersion] = Field(default_factory=list)


class GenerationManifest(BaseModel):
    project_id: str
    lines: dict[str, LineGenerationHistory] = Field(default_factory=dict)

    def line_key(self, line_id: str, line_uid: str | None = None) -> str:
        return line_uid or line_id

    def history_for_line(self, line_id: str, line_uid: str | None = None) -> LineGenerationHistory | None:
        if line_uid:
            return self.lines.get(line_uid)
        return self.lines.get(line_id)

    def append_version(self, line_id: str, version: GenerationVersion) -> None:
        key = self.line_key(line_id, version.line_uid)
        history = self.lines.setdefault(key, LineGenerationHistory(line_id=key))
        version = version.model_copy(update={"version_id": _next_generation_version_id(history.versions, version.version_id)})
        history.versions.append(version)


def _line_with_revision_uid(line: ScriptLine, parse_revision_id: str) -> ScriptLine:
    if line.line_uid and line.line_uid.startswith(f"{parse_revision_id}:"):
        return line
    return line.model_copy(update={"line_uid": f"{parse_revision_id}:{line.id}"})


def _project_lines_to_markdown(project_characters: list[ProjectCharacter], lines: list[ScriptLine]) -> str:
    names = {character.project_character_id: character.name for character in project_characters}
    output: list[str] = []
    for line in lines:
        name = names.get(line.character_id, line.character_id)
        note = f"（{line.note.strip()}）" if line.note.strip() else ""
        output.append(f"{name}{note}: {line.text}")
    return "\n".join(output)


def _next_generation_version_id(existing_versions: list[GenerationVersion], requested: str) -> str:
    max_number = 0
    existing_ids: set[str] = set()
    for version in existing_versions:
        existing_ids.add(version.version_id)
        match = re.fullmatch(r"v(\d+)", version.version_id)
        if match:
            max_number = max(max_number, int(match.group(1)))
    requested_match = re.fullmatch(r"v(\d+)", requested)
    if requested_match and requested not in existing_ids and int(requested_match.group(1)) > max_number:
        return requested
    return f"v{max_number + 1:03d}"
