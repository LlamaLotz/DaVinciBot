from __future__ import annotations

from davincibot.models import (
    AssetRef,
    AssetRole,
    CaptionCue,
    EditSegment,
    GraphicEvent,
    TemplateManifest,
    WorkflowKind,
    WorkspaceMode,
)


def speech_ranges(
    captions: list[CaptionCue],
    padding: float = 0.08,
    merge_gap: float = 0.35,
    max_length: float = 24,
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for cue in sorted(captions, key=lambda item: item.start_seconds):
        start = max(0, cue.start_seconds - padding)
        end = cue.end_seconds + padding
        if ranges and start - ranges[-1][1] <= merge_gap and end - ranges[-1][0] <= max_length:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    bounded = []
    for start, end in ranges:
        while end - start > max_length:
            bounded.append((start, start + max_length))
            start += max_length
        if end > start:
            bounded.append((start, end))
    return bounded


def retime_captions(captions, segments, assets):
    """Map source-time cues through primary edits, excluding B-roll overlays."""
    asset_map = {asset.id: asset for asset in assets}
    output = []
    for segment in segments:
        asset = asset_map[segment.asset_id]
        if segment.track == "DBOT_B_ROLL":
            continue
        for cue in captions:
            if cue.source_asset_id and cue.source_asset_id != asset.id:
                continue
            if cue.group_key and cue.group_key != asset.group_key:
                continue
            start = max(cue.start_seconds, segment.source_in)
            end = min(cue.end_seconds, segment.source_out)
            if end <= start:
                continue
            output.append(
                cue.model_copy(
                    update={
                        "start_seconds": segment.timeline_start
                        + (start - segment.source_in) / segment.speed,
                        "end_seconds": segment.timeline_start
                        + (end - segment.source_in) / segment.speed,
                    }
                )
            )
    return sorted(output, key=lambda cue: cue.start_seconds)


def _duration(asset: AssetRef) -> float:
    return asset.media.duration_seconds if asset.media else 0


def _transition(template: TemplateManifest, first: bool) -> str:
    if first:
        return "cut"
    return str(template.parameters.get("transition", "cut"))


def build_rule_segments(
    workflow: WorkflowKind,
    mode: WorkspaceMode,
    assets: list[AssetRef],
    captions: list[CaptionCue],
    template: TemplateManifest,
) -> tuple[list[EditSegment], list[GraphicEvent], list[str]]:
    videos = [
        asset
        for asset in assets
        if asset.role in {AssetRole.A_ROLL, AssetRole.CAMERA, AssetRole.B_ROLL}
        and _duration(asset) > 0
    ]
    primary = [a for a in videos if a.role in {AssetRole.A_ROLL, AssetRole.CAMERA}]
    broll = [a for a in videos if a.role is AssetRole.B_ROLL]
    warnings: list[str] = []
    graphics: list[GraphicEvent] = []
    if workflow is WorkflowKind.MONTAGE_PROMO:
        segments = _montage(
            videos,
            template,
            float(
                template.parameters.get(
                    "target_duration", 30 if mode is WorkspaceMode.SHORT_FORM else 120
                )
            ),
        )
    elif workflow is WorkflowKind.PODCAST_INTERVIEW:
        segments = _podcast(primary, captions, template)
    else:
        segments = _speech_edit(primary, captions, template, mode)
    if not segments:
        return [], [], ["No usable primary video segments were produced."]
    if broll and workflow in {
        WorkflowKind.TALKING_HEAD,
        WorkflowKind.EDUCATIONAL,
        WorkflowKind.NARRATED_YOUTUBE,
    }:
        end = max(
            segment.timeline_start + segment.source_out - segment.source_in for segment in segments
        )
        interval = 8.0 if mode is WorkspaceMode.SHORT_FORM else 25.0
        position = interval
        index = 0
        while position < end and index < len(broll) * 4:
            asset = broll[index % len(broll)]
            length = min(4.0, _duration(asset), end - position)
            segments.append(
                EditSegment(
                    asset_id=asset.id,
                    source_out=length,
                    timeline_start=position,
                    track="DBOT_B_ROLL",
                    transition=_transition(template, False),
                )
            )
            position += interval
            index += 1
    if workflow is WorkflowKind.EDUCATIONAL:
        mapped = retime_captions(captions, segments, assets)
        for cue in mapped[:: max(1, len(mapped) // 6 or 1)][:6]:
            graphics.append(
                GraphicEvent(
                    kind="callout",
                    start_seconds=cue.start_seconds,
                    duration_seconds=min(4, cue.end_seconds - cue.start_seconds + 2),
                    text=cue.text[:80],
                    template_name="DBOT Callout",
                )
            )
    if captions and max(cue.end_seconds for cue in captions) > max(
        _duration(asset) for asset in primary or videos
    ):
        warnings.append("Caption timing extends beyond the longest primary source.")
    return sorted(segments, key=lambda item: (item.timeline_start, item.track)), graphics, warnings


def _speech_edit(
    primary: list[AssetRef],
    captions: list[CaptionCue],
    template: TemplateManifest,
    mode: WorkspaceMode,
) -> list[EditSegment]:
    if not primary:
        return []
    output: list[EditSegment] = []
    position = 0.0
    max_length = float(
        template.parameters.get(
            "max_segment_seconds", 18 if mode is WorkspaceMode.SHORT_FORM else 45
        )
    )
    limit = float(
        template.parameters.get(
            "target_duration",
            60 if mode is WorkspaceMode.SHORT_FORM else sum(map(_duration, primary)),
        )
    )
    for asset in primary:
        matched = [
            cue
            for cue in captions
            if (not cue.source_asset_id or cue.source_asset_id == asset.id)
            and (not cue.group_key or cue.group_key == asset.group_key)
        ]
        ranges = speech_ranges(matched, max_length=max_length)
        if not ranges:
            ranges = speech_ranges(
                [CaptionCue(start_seconds=0, end_seconds=_duration(asset), text="source")],
                padding=0,
                max_length=max_length,
            )
        for start, end in ranges:
            end = min(end, _duration(asset), start + limit - position)
            if end <= start:
                continue
            output.append(
                EditSegment(
                    asset_id=asset.id,
                    source_in=start,
                    source_out=end,
                    timeline_start=position,
                    layout="punch_in" if len(output) % 3 == 2 else "full",
                    transition=_transition(template, not output),
                )
            )
            position += end - start
    return output


def _montage(
    videos: list[AssetRef], template: TemplateManifest, target_duration: float
) -> list[EditSegment]:
    if not videos:
        return []
    output: list[EditSegment] = []
    position = 0.0
    index = 0
    while position < target_duration and index < 10000:
        asset = videos[index % len(videos)]
        length = min(
            float(template.parameters.get("clip_seconds", 3)),
            _duration(asset),
            target_duration - position,
        )
        if length > 0:
            output.append(
                EditSegment(
                    asset_id=asset.id,
                    source_out=length,
                    timeline_start=position,
                    transition=_transition(template, index == 0),
                )
            )
            position += length
        index += 1
    return output


def _podcast(
    cameras: list[AssetRef], captions: list[CaptionCue], template: TemplateManifest
) -> list[EditSegment]:
    if not cameras:
        return []
    if not captions:
        return [
            EditSegment(asset_id=cameras[0].id, source_out=_duration(cameras[0]), timeline_start=0)
        ]
    output: list[EditSegment] = []
    speakers: dict[str, int] = {}
    position = 0.0
    for index, (start, end) in enumerate(speech_ranges(captions, max_length=20)):
        cue = next((item for item in captions if item.start_seconds >= start), captions[0])
        key = cue.speaker or "default"
        camera_index = speakers.setdefault(key, len(speakers) % len(cameras))
        camera = cameras[camera_index]
        end = min(end, _duration(camera))
        if end <= start:
            continue
        output.append(
            EditSegment(
                asset_id=camera.id,
                source_in=start,
                source_out=end,
                timeline_start=position,
                transition=_transition(template, index == 0),
            )
        )
        position += end - start
    return output
