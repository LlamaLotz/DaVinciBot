from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from davincibot.models import AssetRole, TemplateManifest, TemplateSlot, WorkflowKind, WorkspaceMode

SUPPORTED_TRANSITIONS = {"cut", "cross_dissolve"}


def configured_template(template: TemplateManifest, overrides: dict) -> TemplateManifest:
    parameters = {**template.parameters, **overrides}
    numeric = {
        "target_duration": (0.1, 43200),
        "max_segment_seconds": (0.1, 3600),
        "clip_seconds": (0.1, 300),
        "punch_in_zoom": (1, 3),
        "font_size": (0.005, 0.2),
        "caption_y": (0.05, 0.95),
        "transition_seconds": (0.05, 2),
    }
    supported = set(numeric) | {"font", "transition"}
    for key, value in parameters.items():
        if key not in supported:
            raise ValueError(f"unsupported template parameter: {key}")
        if key in numeric:
            low, high = numeric[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not low <= value <= high
            ):
                raise ValueError(f"{key} must be between {low} and {high}")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be nonempty text")
    if parameters.get("transition", "cut") not in SUPPORTED_TRANSITIONS:
        raise ValueError("supported standard transitions are cut and cross_dissolve")
    return template.model_copy(deep=True, update={"parameters": parameters})


class TemplateValidation:
    def __init__(self, errors: list[str] | None = None, warnings: list[str] | None = None):
        self.errors = errors or []
        self.warnings = warnings or []

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_template(template: TemplateManifest) -> TemplateValidation:
    errors: list[str] = []
    warnings: list[str] = []
    if not template.name.strip() or not template.timeline_name.strip():
        errors.append("template name and Resolve timeline are required")
    if not template.workflows or not template.modes:
        errors.append("template requires at least one workflow and workspace mode")
    try:
        configured_template(template, {})
    except ValueError as error:
        errors.append(str(error))
    if template.plugins:
        errors.append("third-party plugins cannot yet be verified: " + ", ".join(template.plugins))
    ids = Counter(slot.id for slot in template.slots)
    errors.extend(f"duplicate slot id: {slot_id}" for slot_id, count in ids.items() if count > 1)
    for slot in template.slots:
        if not slot.track.startswith("DBOT_"):
            errors.append(f"slot {slot.id} must use a reserved DBOT_ track")
        declared_tracks = set(template.track_contract.get("video", [])) | set(
            template.track_contract.get("audio", [])
        )
        if declared_tracks and slot.track not in declared_tracks:
            errors.append(f"slot {slot.id} is missing from the registered track contract")
    unsupported = set(template.transitions) - SUPPORTED_TRANSITIONS
    if unsupported:
        warnings.append("not executable by this backend: " + ", ".join(sorted(unsupported)))
    for asset in template.fusion_assets:
        if not asset.exists():
            errors.append(f"Fusion asset does not exist: {asset}")
    if template.source_snapshot and not template.source_snapshot.exists():
        errors.append(f"template snapshot does not exist: {template.source_snapshot}")
    elif template.source_snapshot and template.snapshot_sha256:
        with template.source_snapshot.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != template.snapshot_sha256:
                errors.append("registered template snapshot changed; register a new version")
    if not template.slots:
        warnings.append("template has no marker-bound slots")
    return TemplateValidation(errors, warnings)


def slots_from_markers(markers: dict[Any, dict[str, Any]]) -> list[TemplateSlot]:
    slots: list[TemplateSlot] = []
    for marker in markers.values():
        raw = str(marker.get("customData", ""))
        if not raw.startswith("davincibot:"):
            continue
        try:
            payload = json.loads(raw.removeprefix("davincibot:"))
            slots.append(
                TemplateSlot(
                    id=payload["id"],
                    role=AssetRole(payload["role"]),
                    track=payload["track"],
                    required=payload.get("required", True),
                    marker_custom_data=raw,
                    max_items=payload.get("max_items"),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid DaVinciBot template marker: {raw}: {error}") from error
    return slots


def manifest_from_timeline(
    name: str,
    timeline_name: str,
    markers: dict[Any, dict[str, Any]],
    workflows: set[WorkflowKind],
    modes: set[WorkspaceMode],
    previous: TemplateManifest | None = None,
    snapshot: Path | None = None,
) -> TemplateManifest:
    return (
        TemplateManifest(
            id=previous.id if previous else None,  # type: ignore[arg-type]
            version=(previous.version + 1) if previous else 1,
            name=name,
            timeline_name=timeline_name,
            workflows=workflows,
            modes=modes,
            slots=slots_from_markers(markers),
            source_snapshot=snapshot,
        )
        if previous
        else TemplateManifest(
            name=name,
            timeline_name=timeline_name,
            workflows=workflows,
            modes=modes,
            slots=slots_from_markers(markers),
            source_snapshot=snapshot,
        )
    )
