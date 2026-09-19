from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from math import gcd

from davincibot.models import AssetRole, EditPlan, JobSpec, TemplateManifest, WorkspaceProfile
from davincibot.resolve.interchange import transition_errors
from davincibot.templates import validate_template


@dataclass
class PreflightReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.errors


def run_preflight(
    spec: JobSpec,
    plan: EditPlan,
    profile: WorkspaceProfile,
    template: TemplateManifest,
) -> PreflightReport:
    report = PreflightReport()
    from davincibot.planner.service import PlanningError, PlanningService

    try:
        PlanningService._validate_plan(plan, spec, [template])
    except PlanningError as error:
        report.errors.append(str(error))
    if spec.profile_id != profile.id or profile.mode not in template.modes:
        report.errors.append("job, workspace and template are incompatible")
    divisor = gcd(profile.timeline.width, profile.timeline.height)
    aspect = f"{profile.timeline.width // divisor}:{profile.timeline.height // divisor}"
    if aspect not in template.aspect_ratios:
        report.errors.append(f"template does not support workspace aspect ratio {aspect}")
    if template.frame_rate and abs(template.frame_rate - profile.timeline.frame_rate) > 0.01:
        report.errors.append(
            "template frame rate differs from the workspace; register a compatible template"
        )
    template_result = validate_template(template)
    report.errors.extend(template_result.errors)
    report.warnings.extend(template_result.warnings)
    present_roles = {asset.role for asset in spec.assets}
    for folder in profile.folders:
        if folder.required and folder.role not in present_roles:
            report.errors.append(f"required asset role has no files: {folder.role.value}")
    for slot in template.slots:
        if slot.required and slot.role not in present_roles:
            report.errors.append(f"required template slot is unassigned: {slot.id}")
    if profile.captions.required and not spec.captions:
        report.errors.append("this profile requires an SRT or VTT caption file")
    asset_ids = {asset.id for asset in spec.assets}
    voices = [
        a
        for a in spec.assets
        if a.id in plan.audio.dialogue_asset_ids and a.role is AssetRole.VOICE
    ]
    if len(voices) > 1 or len(plan.audio.music_asset_ids) > 1:
        report.errors.append("assign at most one voice-over and one music bed per job")
    for asset in spec.assets:
        try:
            stat = asset.path.stat()
            if not asset.path.is_file():
                raise OSError("not a regular file")
            if stat.st_size != asset.size_bytes or stat.st_mtime_ns != asset.modified_ns:
                report.errors.append(f"source changed since scan; rescan: {asset.path.name}")
        except OSError:
            report.errors.append(f"source missing or unreadable: {asset.path.name}")
    for folder in profile.folders:
        if folder.role is AssetRole.OUTPUT:
            parent = folder.path
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if shutil.disk_usage(parent).free < 512 * 1024 * 1024:
                report.errors.append("less than 512 MB free in output location")
    for cue in spec.captions:
        matches = [
            a
            for a in spec.assets
            if a.media
            and a.role in {AssetRole.A_ROLL, AssetRole.VOICE, AssetRole.CAMERA}
            and (not cue.source_asset_id or cue.source_asset_id == a.id)
            and (not cue.group_key or cue.group_key == a.group_key)
        ]
        if not matches:
            report.errors.append(
                "captions have no matching primary source; correct group assignments"
            )
        elif any(cue.end_seconds > a.media.duration_seconds + 0.05 for a in matches):
            report.errors.append("caption timing exceeds its source duration")
        if (
            not cue.source_asset_id
            and len(matches) > 1
            and plan.workflow.value != "podcast_interview"
        ):
            report.errors.append(
                "ambiguous caption source; assign source_asset_id in the job editor"
            )
    for segment in plan.segments:
        if segment.asset_id not in asset_ids:
            report.errors.append(f"segment references missing asset: {segment.asset_id}")
        if segment.transition not in {"cut", "cross_dissolve"}:
            report.errors.append("transition has no registered implementation")
    report.errors.extend(
        transition_errors(
            spec.model_dump(mode="json"),
            plan.model_dump(mode="json"),
            template.model_dump(mode="json"),
        )
    )
    if any(s.transition == "cross_dissolve" for s in plan.segments):
        report.warnings.append(
            "Dissolves use an editable nested XML base timeline; "
            "open the base timeline to edit individual clips."
        )
    if plan.workflow.value == "podcast_interview":
        cameras = [a for a in spec.assets if a.role is AssetRole.CAMERA]
        if len(cameras) > 1 and any(a.id not in spec.analysis.get("offsets", {}) for a in cameras):
            report.errors.append(
                "camera synchronization has not been analyzed; replan before building"
            )
        report.warnings.append(
            "podcast camera switching uses caption speaker labels; verify sync in Resolve"
        )
    if plan.workflow.value == "narrated_youtube":
        if AssetRole.VOICE not in present_roles:
            report.errors.append("narrated YouTube plans require a voice-over asset")
        else:
            report.warnings.append(
                "voice-over is mixed locally from the selected voice asset; "
                "verify timing in Resolve"
            )
    for event in plan.graphics:
        if not event.text and not event.asset_id:
            report.errors.append("graphic requires editable text or an assigned graphic asset")
    if not plan.segments:
        report.errors.append("edit plan contains no video segments")
    report.warnings.extend(plan.warnings)
    if plan.confidence < profile.batch_confidence_threshold:
        report.warnings.append(
            f"planner confidence {plan.confidence:.0%} is below the batch threshold "
            f"of {profile.batch_confidence_threshold:.0%}"
        )
    if AssetRole.MUSIC not in present_roles:
        report.warnings.append("no music asset is assigned")
    return report
