from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import ValidationError

from davincibot.editing.engine import build_rule_segments, retime_captions
from davincibot.models import (
    AssetRef,
    AssetRole,
    AudioInstruction,
    EditPlan,
    JobSpec,
    ProviderKind,
    TemplateManifest,
    WorkflowKind,
    WorkspaceProfile,
)
from davincibot.planner.providers import PlannerProvider, ProviderError


class PlanningError(RuntimeError):
    pass


SYSTEM_PROMPT = """You are the planning component of DaVinciBot.
Choose exactly one supplied template and return a deterministic edit plan matching the schema.
Never emit file paths, shell commands, code, Resolve calls, or assets not listed by ID.
Use only supported workflow, transition, layout, and template capabilities.
The local engine, not you, performs edits. Put uncertainty in warnings and confidence.
"""


def infer_workflow(prompt: str, profile: WorkspaceProfile) -> WorkflowKind:
    value = prompt.casefold()
    checks = (
        (WorkflowKind.PODCAST_INTERVIEW, ("podcast", "interview", "multi-camera", "multicam")),
        (WorkflowKind.EDUCATIONAL, ("educational", "lesson", "tutorial", "course")),
        (WorkflowKind.MONTAGE_PROMO, ("montage", "promo", "trailer", "music video")),
        (WorkflowKind.NARRATED_YOUTUBE, ("narrated", "voice-over", "voiceover", "youtube")),
        (WorkflowKind.TALKING_HEAD, ("talking head", "reel", "short", "tiktok")),
    )
    for workflow, terms in checks:
        if any(term in value for term in terms):
            return workflow
    return (
        WorkflowKind.TALKING_HEAD
        if profile.mode.value == "short_form"
        else WorkflowKind.NARRATED_YOUTUBE
    )


def choose_template(
    workflow: WorkflowKind, profile: WorkspaceProfile, templates: list[TemplateManifest]
) -> TemplateManifest:
    candidates = [
        template
        for template in templates
        if (not profile.template_ids or template.id in profile.template_ids)
        and profile.mode in template.modes
        and workflow in template.workflows
    ]
    if not candidates:
        raise PlanningError(f"no {workflow.value} template is registered for {profile.mode.value}")
    return sorted(candidates, key=lambda item: (item.version, item.name), reverse=True)[0]


def _editable_assets(assets: list[AssetRef]) -> list[AssetRef]:
    roles = {AssetRole.A_ROLL, AssetRole.CAMERA, AssetRole.B_ROLL}
    return [asset for asset in assets if asset.role in roles and asset.media]


def offline_plan(
    spec: JobSpec, profile: WorkspaceProfile, templates: list[TemplateManifest]
) -> EditPlan:
    workflow = infer_workflow(spec.prompt, profile)
    selected_id = spec.overrides.get("template_id")
    if selected_id:
        templates = [
            t
            for t in templates
            if t.id == selected_id
            and t.version == spec.overrides.get("template_version", t.version)
        ]
        if templates and workflow not in templates[0].workflows:
            workflow = sorted(templates[0].workflows)[0]
    template = choose_template(workflow, profile, templates)
    from davincibot.templates import configured_template

    template = configured_template(template, spec.overrides.get("parameters", {}))
    videos = _editable_assets(spec.assets)
    if not videos:
        raise PlanningError("no probed A-roll, camera, or B-roll video is available")
    segments, graphics, rule_warnings = build_rule_segments(
        workflow, profile.mode, spec.assets, spec.captions, template, spec.analysis
    )
    if not segments:
        raise PlanningError("the selected video files have no usable duration")
    dialogue = [
        a.id for a in spec.assets if a.role in {AssetRole.A_ROLL, AssetRole.VOICE, AssetRole.CAMERA}
    ]
    music = [a.id for a in spec.assets if a.role is AssetRole.MUSIC]
    captions = retime_captions(spec.captions, segments, spec.assets)
    if workflow is WorkflowKind.NARRATED_YOUTUBE:
        captions = spec.captions
    elif workflow is WorkflowKind.PODCAST_INTERVIEW:
        captions = []
        for segment in segments:
            offset = spec.analysis.get("offsets", {}).get(segment.asset_id, 0)
            for cue in spec.captions:
                begin, end = (
                    max(cue.start_seconds, segment.source_in - offset),
                    min(cue.end_seconds, segment.source_out - offset),
                )
                if end > begin:
                    captions.append(
                        cue.model_copy(
                            update={
                                "start_seconds": segment.timeline_start
                                + begin
                                - (segment.source_in - offset),
                                "end_seconds": segment.timeline_start
                                + end
                                - (segment.source_in - offset),
                            }
                        )
                    )
    return EditPlan(
        job_id=spec.id,
        template_id=template.id,
        template_version=template.version,
        workflow=workflow,
        timeline_name=spec.target_project,
        segments=segments,
        graphics=graphics,
        captions=captions if profile.captions.enabled else [],
        audio=AudioInstruction(
            dialogue_asset_ids=dialogue,
            music_asset_ids=music[:1],
            target_lufs=profile.audio.target_lufs,
            true_peak_db=profile.audio.true_peak_db,
            duck_db=profile.audio.music_duck_db,
        ),
        confidence=0.75,
        warnings=[
            "Offline planner used; review template and segment choices.",
            *rule_warnings,
            *spec.analysis.get("warnings", []),
        ],
        planner_notes="Deterministic local fallback plan",
    )


class PlanningService:
    def __init__(self, secret_lookup: Callable[[ProviderKind], str | None]):
        self.secret_lookup = secret_lookup

    def plan(
        self,
        spec: JobSpec,
        profile: WorkspaceProfile,
        templates: list[TemplateManifest],
        provider_factory: Callable[[ProviderKind, str, str, str | None], PlannerProvider],
    ) -> EditPlan:
        from davincibot.editing.analysis import analyze_job

        spec.analysis = analyze_job(spec, profile)
        if spec.provider is ProviderKind.OFFLINE:
            return offline_plan(spec, profile, templates)
        key = self.secret_lookup(spec.provider)
        if not key:
            raise PlanningError(f"no API key is stored for {spec.provider.value}")
        compatible = [
            template
            for template in templates
            if profile.mode in template.modes
            and (not profile.template_ids or template.id in profile.template_ids)
            and (
                not spec.overrides.get("template_id")
                or (
                    template.id == spec.overrides["template_id"]
                    and template.version == spec.overrides.get("template_version", template.version)
                )
            )
        ]
        if not compatible:
            raise PlanningError("no compatible templates are registered")
        provider = provider_factory(spec.provider, key, spec.provider_model, spec.provider_base_url)
        payload = self._request_payload(spec, profile, compatible)
        last_error: Exception | None = None
        for attempt in range(2):
            request = payload
            if attempt and last_error:
                request += "\nPrevious response failed validation. Return a complete valid plan."
            try:
                raw = provider.complete_json(SYSTEM_PROMPT, request, EditPlan.model_json_schema())
                plan = EditPlan.model_validate(raw)
                self._validate_plan(plan, spec, compatible)
                if not profile.captions.enabled:
                    plan.captions = []
                return plan
            except (ProviderError, ValidationError, PlanningError) as error:
                last_error = error
        raise PlanningError(f"planner failed after schema repair: {last_error}")

    @staticmethod
    def _request_payload(
        spec: JobSpec, profile: WorkspaceProfile, templates: list[TemplateManifest]
    ) -> str:
        transcript = "\n".join(
            f"{cue.start_seconds:.3f}-{cue.end_seconds:.3f}"
            f" {f'[{cue.speaker}] ' if cue.speaker else ''}{cue.text}"
            for cue in spec.captions
        )
        assets = [
            {
                "id": asset.id,
                "filename": asset.path.name,
                "role": asset.role.value,
                "duration_seconds": asset.media.duration_seconds if asset.media else None,
            }
            for asset in spec.assets
        ]
        template_data = [
            template.model_dump(mode="json", exclude={"source_snapshot", "fusion_assets"})
            for template in templates
        ]
        if len(transcript) > 200_000:
            raise PlanningError("transcript exceeds 200,000 characters; split the job")
        return json.dumps(
            {
                "job_id": spec.id,
                "prompt": spec.prompt,
                "profile_mode": profile.mode.value,
                "timeline_name": spec.target_project,
                "assets": assets,
                "transcript": transcript,
                "templates": template_data,
                "overrides": spec.overrides,
                "local_analysis": spec.analysis,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _validate_plan(plan: EditPlan, spec: JobSpec, templates: list[TemplateManifest]) -> None:
        if plan.job_id != spec.id:
            raise PlanningError("planner changed the job id")
        selected = next(
            (
                item
                for item in templates
                if item.id == plan.template_id and item.version == plan.template_version
            ),
            None,
        )
        if selected is None:
            raise PlanningError("planner selected an unavailable template or version")
        if plan.workflow not in selected.workflows:
            raise PlanningError("template does not support the selected workflow")
        if plan.timeline_name != spec.target_project:
            raise PlanningError("planner changed the target timeline name")
        if not plan.segments:
            raise PlanningError("planner returned no segments")
        asset_map = {asset.id: asset for asset in spec.assets}
        for asset_id in plan.audio.dialogue_asset_ids + plan.audio.music_asset_ids:
            if asset_id not in asset_map:
                raise PlanningError("audio references an unknown asset")
        for event in plan.graphics:
            if event.asset_id and event.asset_id not in asset_map:
                raise PlanningError("graphic references an unknown asset")
        for segment in plan.segments:
            asset = asset_map.get(segment.asset_id)
            if asset is None:
                raise PlanningError("planner referenced an unknown asset")
            if asset.media:
                if segment.source_out > asset.media.duration_seconds + 0.05:
                    raise PlanningError(f"segment exceeds the duration of {asset.path.name}")
                if segment.source_in >= asset.media.duration_seconds:
                    raise PlanningError(f"segment starts beyond the duration of {asset.path.name}")
            if segment.transition not in selected.transitions:
                raise PlanningError(f"template does not support {segment.transition}")
            if segment.layout not in {"full", "punch_in"}:
                raise PlanningError("only full and punch_in layouts are supported")
            if segment.speed != 1:
                raise PlanningError("speed changes require a retiming backend; use normal speed")
            if not asset.media or asset.media.duration_seconds <= 0:
                raise PlanningError("segment requires successfully probed media")
            allowed_tracks = {"DBOT_A_ROLL", "DBOT_B_ROLL"} | {s.track for s in selected.slots}
            if segment.track not in allowed_tracks:
                raise PlanningError("plan uses a track outside the template contract")
