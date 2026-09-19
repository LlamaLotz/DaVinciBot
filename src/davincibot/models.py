from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class WorkspaceMode(StrEnum):
    SHORT_FORM = "short_form"
    LONG_FORM = "long_form"


class WorkflowKind(StrEnum):
    TALKING_HEAD = "talking_head"
    MONTAGE_PROMO = "montage_promo"
    EDUCATIONAL = "educational"
    PODCAST_INTERVIEW = "podcast_interview"
    NARRATED_YOUTUBE = "narrated_youtube"


class AssetRole(StrEnum):
    A_ROLL = "a_roll"
    CAMERA = "camera"
    VOICE = "voice"
    MUSIC = "music"
    B_ROLL = "b_roll"
    GRAPHIC = "graphic"
    CAPTION = "caption"
    TEMPLATE = "template"
    OUTPUT = "output"


class ResourceMode(StrEnum):
    ECO = "eco"
    BALANCED = "balanced"
    TURBO = "turbo"


class ProviderKind(StrEnum):
    OFFLINE = "offline"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OPENAI_COMPATIBLE = "openai_compatible"


class JobState(StrEnum):
    DRAFT = "draft"
    PREFLIGHT = "preflight"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    AWAITING_RESOLVE = "awaiting_resolve"
    BUILDING = "building"
    READY = "ready"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FolderRule(StrictModel):
    role: AssetRole
    path: Path
    recursive: bool = True
    required: bool = False
    extensions: list[str] = Field(default_factory=list)

    @field_validator("extensions")
    @classmethod
    def normalize_extensions(cls, values: list[str]) -> list[str]:
        return sorted({f".{item.lower().lstrip('.')}" for item in values})


class AudioPolicy(StrictModel):
    target_lufs: float = Field(default=-14, ge=-70, le=-5)
    true_peak_db: float = Field(default=-1, ge=-9, le=0)
    music_duck_db: float = Field(default=-12, ge=-60, le=0)
    attack_ms: int = Field(default=120, ge=1, le=2000)
    release_ms: int = Field(default=500, ge=1, le=9000)
    fade_ms: int = Field(default=250, ge=0, le=10000)
    preserve_reference_tracks: bool = True


class CaptionPolicy(StrictModel):
    enabled: bool = True
    required: bool = False
    max_chars_per_line: int = Field(default=42, ge=1, le=60)
    max_lines: int = Field(default=2, ge=1, le=3)
    template_name: str = "DBOT Caption"


class TimelineDefaults(StrictModel):
    width: int = Field(default=1920, gt=0, le=8192)
    height: int = Field(default=1080, gt=0, le=8192)
    frame_rate: float = Field(default=30.0, gt=0)
    start_timecode: str = "01:00:00:00"


class WorkspaceProfile(StrictModel):
    schema_version: int = 1
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    mode: WorkspaceMode
    folders: list[FolderRule] = Field(default_factory=list)
    template_ids: list[str] = Field(default_factory=list)
    resource_mode: ResourceMode = ResourceMode.BALANCED
    audio: AudioPolicy = Field(default_factory=AudioPolicy)
    captions: CaptionPolicy = Field(default_factory=CaptionPolicy)
    timeline: TimelineDefaults = Field(default_factory=TimelineDefaults)
    batch_confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    group_by_subfolder: bool = True
    remove_silence: bool = True
    align_music_beats: bool = True
    synchronize_cameras: bool = True

    @model_validator(mode="after")
    def unique_folder_roles(self) -> WorkspaceProfile:
        roles = [folder.role for folder in self.folders]
        if len(roles) != len(set(roles)):
            raise ValueError("a workspace profile may define each folder role only once")
        return self


class TemplateSlot(StrictModel):
    id: str
    role: AssetRole
    track: str
    required: bool = True
    marker_custom_data: str | None = None
    max_items: int | None = Field(default=None, gt=0)


class TemplateManifest(StrictModel):
    schema_version: int = 1
    id: str = Field(default_factory=lambda: str(uuid4()))
    version: int = Field(default=1, ge=1)
    name: str
    timeline_name: str
    workflows: set[WorkflowKind]
    modes: set[WorkspaceMode]
    aspect_ratios: set[str] = Field(default_factory=lambda: {"16:9"})
    slots: list[TemplateSlot] = Field(default_factory=list)
    track_contract: dict[str, list[str]] = Field(default_factory=dict)
    fonts: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    transitions: list[str] = Field(default_factory=lambda: ["cut"])
    fusion_assets: list[Path] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    source_snapshot: Path | None = None
    snapshot_sha256: str | None = None
    frame_rate: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def unique_slots(self) -> TemplateManifest:
        slot_ids = [slot.id for slot in self.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("template slot ids must be unique")
        return self


class MediaInfo(StrictModel):
    duration_seconds: float = Field(default=0, ge=0)
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    codec: str | None = None


class AssetRef(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    path: Path
    role: AssetRole
    group_key: str
    size_bytes: int = Field(ge=0)
    modified_ns: int = Field(ge=0)
    media: MediaInfo | None = None


class CaptionCue(StrictModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str
    speaker: str | None = None
    source_asset_id: str | None = None
    group_key: str | None = None

    @model_validator(mode="after")
    def valid_range(self) -> CaptionCue:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("caption end must be after start")
        return self


class JobSpec(StrictModel):
    schema_version: int = 1
    id: str = Field(default_factory=lambda: str(uuid4()))
    profile_id: str
    prompt: str = Field(min_length=1)
    provider: ProviderKind = ProviderKind.OFFLINE
    provider_model: str = "offline-rules"
    provider_base_url: str | None = None
    target_project: str
    assets: list[AssetRef] = Field(default_factory=list)
    captions: list[CaptionCue] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)
    resource_mode: ResourceMode = ResourceMode.BALANCED
    batch: bool = False
    profile_snapshot: WorkspaceProfile | None = None
    template_snapshot: TemplateManifest | None = None
    prepared_audio: Path | None = None
    analysis: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class EditSegment(StrictModel):
    asset_id: str
    source_in: float = Field(default=0, ge=0)
    source_out: float = Field(gt=0)
    timeline_start: float = Field(ge=0)
    track: str = "DBOT_A_ROLL"
    layout: str = "full"
    transition: str = "cut"
    speed: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def valid_range(self) -> EditSegment:
        if self.source_out <= self.source_in:
            raise ValueError("segment source_out must be after source_in")
        return self


class GraphicEvent(StrictModel):
    kind: str
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    text: str | None = None
    asset_id: str | None = None
    template_name: str | None = None


class AudioInstruction(StrictModel):
    dialogue_asset_ids: list[str] = Field(default_factory=list)
    music_asset_ids: list[str] = Field(default_factory=list)
    target_lufs: float = Field(default=-14, ge=-70, le=-5)
    true_peak_db: float = Field(default=-1, ge=-9, le=0)
    duck_db: float = Field(default=-12, ge=-60, le=0)


class EditPlan(StrictModel):
    schema_version: int = 1
    job_id: str
    template_id: str
    template_version: int = Field(ge=1)
    workflow: WorkflowKind
    timeline_name: str
    segments: list[EditSegment] = Field(default_factory=list)
    graphics: list[GraphicEvent] = Field(default_factory=list)
    captions: list[CaptionCue] = Field(default_factory=list)
    audio: AudioInstruction = Field(default_factory=AudioInstruction)
    confidence: float = Field(default=1.0, ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)
    planner_notes: str = ""


class JobRecord(StrictModel):
    id: str
    state: JobState
    spec: JobSpec
    plan: EditPlan | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
