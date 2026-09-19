"""Dependency-free Resolve runner, also copied beside the Workspace launcher.

Never import the desktop UI, Pydantic, or third-party packages from this module.
"""

from __future__ import annotations

import json
import os
import re
import struct
import zlib
from contextlib import contextmanager
from pathlib import Path


class ResolveBuildError(RuntimeError):
    pass


def checked(result, action):
    if not result:
        raise ResolveBuildError(action)
    return result


def next_timeline_name(project, requested):
    names = {
        project.GetTimelineByIndex(i).GetName()
        for i in range(1, int(project.GetTimelineCount()) + 1)
    }
    base = re.sub(r"_v\d+$", "", requested)
    version = 1
    while f"{base}_v{version:03d}" in names:
        version += 1
    return f"{base}_v{version:03d}"


def track(timeline, kind, name):
    for i in range(1, int(timeline.GetTrackCount(kind)) + 1):
        if timeline.GetTrackName(kind, i) == name:
            return i
    checked(
        timeline.AddTrack(kind, "stereo") if kind == "audio" else timeline.AddTrack(kind),
        f"could not add {name}",
    )
    index = int(timeline.GetTrackCount(kind))
    checked(timeline.SetTrackName(kind, index, name), f"could not name {name}")
    return index


def transparent_png(path, width=1920, height=1080):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
    compressor = zlib.compressobj()
    compressed = bytearray()
    for _ in range(height):
        compressed.extend(compressor.compress(b"\x00" * (width * 4 + 1)))
    compressed.extend(compressor.flush())
    data += chunk(b"IDAT", bytes(compressed))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


def populate(project, timeline, spec, plan, template, support_root, base_timeline=None):
    support_root = Path(support_root)
    support_root.mkdir(parents=True, exist_ok=True)
    pool = project.GetMediaPool()
    assets = {a["id"]: a for a in spec["assets"]}
    params = {**template.get("parameters", {}), **spec.get("overrides", {}).get("parameters", {})}
    fps = float(timeline.GetSetting("timelineFrameRate") or 30)
    origin = int(timeline.GetStartFrame())
    imported = {}

    def media(path):
        key = str(Path(path).resolve()).casefold()
        if key not in imported:
            items = checked(pool.ImportMedia([str(Path(path).resolve())]), "media import failed")
            if len(items) != 1:
                raise ResolveBuildError("ambiguous media import; image sequences are unsupported")
            imported[key] = items[0]
        return imported[key]

    roles = {s["role"]: s["track"] for s in template.get("slots", [])}

    def segment_track(segment):
        asset = assets[segment["asset_id"]]
        # A B-roll asset can be a primary montage clip; explicit layer wins.
        if segment["track"] == "DBOT_B_ROLL":
            return roles.get("b_roll", segment["track"])
        if asset["role"] == "b_roll":
            return segment["track"]
        return roles.get(asset["role"], segment["track"])

    video_names = {segment_track(s) for s in plan["segments"]}
    if plan.get("captions"):
        video_names.add("DBOT_CAPTIONS")
    if plan.get("graphics"):
        video_names.add("DBOT_GRAPHICS")
    # Only explicit generated tracks are replaced; other template tracks survive.
    for name in sorted(video_names):
        index = track(timeline, "video", name)
        existing = timeline.GetItemListInTrack("video", index) or []
        if existing:
            checked(
                timeline.DeleteClips(existing, False), f"could not clear placeholder track {name}"
            )

    def append(path, start, end, position, name, source_fps, kind="video"):
        first = round(start * source_fps)
        last = max(first, round(end * source_fps) - 1)
        info = {
            "mediaPoolItem": media(path),
            "startFrame": first,
            "endFrame": last,
            "trackIndex": track(timeline, kind, name),
            "recordFrame": origin + round(position * fps),
            "mediaType": 1 if kind == "video" else 2,
        }
        result = checked(pool.AppendToTimeline([info]), "clip append failed")
        if len(result) != 1:
            raise ResolveBuildError("Resolve returned the wrong number of appended clips")
        get_duration = getattr(result[0], "GetDuration", None)
        if callable(get_duration):
            actual = get_duration()
            expected = round((end - start) * fps)
            if actual is None or abs(float(actual) - expected) > 1:
                raise ResolveBuildError("Resolve clip duration differs from the approved plan")
        return result[0]

    reference_index = None
    if spec.get("prepared_audio"):
        reference_index = track(timeline, "audio", "DBOT_REFERENCE_AUDIO")
        for name in ("DBOT_MASTER_AUDIO", "DBOT_REFERENCE_AUDIO"):
            index = track(timeline, "audio", name)
            existing = timeline.GetItemListInTrack("audio", index) or []
            if existing:
                checked(timeline.DeleteClips(existing, False), "could not clear generated audio")
    if base_timeline:
        duration = max(
            s["timeline_start"] + s["source_out"] - s["source_in"] for s in plan["segments"]
        )
        checked(
            pool.AppendToTimeline(
                [
                    {
                        "mediaPoolItem": base_timeline.GetMediaPoolItem(),
                        "startFrame": 0,
                        "endFrame": max(0, round(duration * fps) - 1),
                        "recordFrame": origin,
                        "trackIndex": track(timeline, "video", "DBOT_A_ROLL"),
                        "mediaType": 1,
                    }
                ]
            ),
            "could not append editable base timeline",
        )
    for segment in plan["segments"]:
        asset = assets[segment["asset_id"]]
        rate = (asset.get("media") or {}).get("frame_rate") or fps
        item = (
            None
            if base_timeline
            else append(
                asset["path"],
                segment["source_in"],
                segment["source_out"],
                segment["timeline_start"],
                segment_track(segment),
                rate,
            )
        )
        speed = float(segment.get("speed", 1))
        if abs(speed - 1) > 0.0001:
            raise ResolveBuildError("retiming is not supported by this backend")
        if item and segment.get("layout") == "punch_in":
            zoom = float(params.get("punch_in_zoom", 1.15))
            checked(item.SetProperty("ZoomX", zoom), "punch-in ZoomX failed")
            checked(item.SetProperty("ZoomY", zoom), "punch-in ZoomY failed")
        if (asset.get("media") or {}).get("channels") and asset["role"] in {"a_roll", "camera"}:
            audio_track = "DBOT_REFERENCE_AUDIO" if reference_index else "DBOT_DIALOGUE"
            append(
                asset["path"],
                segment["source_in"],
                segment["source_out"],
                segment["timeline_start"],
                audio_track,
                rate,
                "audio",
            )
    duration = max(
        s["timeline_start"] + (s["source_out"] - s["source_in"]) / s.get("speed", 1)
        for s in plan["segments"]
    )
    if spec.get("prepared_audio"):
        append(spec["prepared_audio"], 0, duration, 0, "DBOT_MASTER_AUDIO", fps, "audio")
        checked(
            timeline.SetTrackEnable("audio", reference_index, False),
            "could not mute reference audio",
        )
        for role in ("voice", "music"):
            for index, asset in enumerate(a for a in spec["assets"] if a["role"] == role):
                name = f"DBOT_REFERENCE_{role.upper()}_{index + 1}"
                length = (asset.get("media") or {}).get("duration_seconds", 0)
                if length > 0:
                    append(asset["path"], 0, min(length, duration), 0, name, fps, "audio")
                    checked(
                        timeline.SetTrackEnable("audio", track(timeline, "audio", name), False),
                        "could not disable original audio reference",
                    )

    def title(text, start, length, name, caption=False):
        if caption:
            policy = (spec.get("profile_snapshot") or {}).get("captions", {})
            width = int(policy.get("max_chars_per_line", 42))
            lines = []
            words = text.split()
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                lines.append(current)
            maximum = int(policy.get("max_lines", 2))
            if len(lines) > maximum:
                groups = [lines[i : i + maximum] for i in range(0, len(lines), maximum)]
                step = length / len(groups)
                for index, group in enumerate(groups):
                    title("\n".join(group), start + index * step, step, name, True)
                return
            text = "\n".join(lines)
        defaults = (spec.get("profile_snapshot") or {}).get("timeline", {})
        width, height = int(defaults.get("width", 1920)), int(defaults.get("height", 1080))
        background = support_root / f"transparent-{width}x{height}.png"
        if not background.exists():
            transparent_png(background, width, height)
        item = append(background, 0, length, start, name, fps)
        registered = {Path(path).stem: path for path in template.get("fusion_assets", [])}
        caption_name = ((spec.get("profile_snapshot") or {}).get("captions") or {}).get(
            "template_name", "DBOT Caption"
        )
        comp_path = registered.get(caption_name if caption else "DBOT Callout")
        comp = checked(
            item.ImportFusionComp(comp_path) if comp_path else item.AddFusionComp(),
            "could not add editable Fusion composition",
        )
        if comp_path:
            tools = comp.GetToolList(False, "TextPlus") or {}
            if len(tools) != 1:
                raise ResolveBuildError(
                    "registered title composition must contain exactly one TextPlus"
                )
            checked(
                next(iter(tools.values())).SetInput("StyledText", text),
                "could not update registered title",
            )
            return
        text_tool = checked(comp.AddTool("TextPlus"), "TextPlus is unavailable")
        output = checked(comp.AddTool("MediaOut"), "Fusion MediaOut is unavailable")
        checked(text_tool.SetInput("StyledText", text), "could not set title text")
        checked(text_tool.SetInput("Font", params.get("font", "Arial")), "could not set title font")
        checked(
            text_tool.SetInput("Size", float(params.get("font_size", 0.045))),
            "could not set title size",
        )
        if caption:
            checked(
                text_tool.SetInput("Center", {1: 0.5, 2: float(params.get("caption_y", 0.12))}),
                "could not place caption",
            )
        checked(output.ConnectInput("Input", text_tool), "could not connect title output")

    for cue in plan.get("captions", []):
        title(
            cue["text"],
            cue["start_seconds"],
            cue["end_seconds"] - cue["start_seconds"],
            "DBOT_CAPTIONS",
            True,
        )
    for event in plan.get("graphics", []):
        if event.get("asset_id"):
            asset = assets[event["asset_id"]]
            append(
                asset["path"],
                0,
                event["duration_seconds"],
                event["start_seconds"],
                "DBOT_GRAPHICS",
                fps,
            )
        elif event.get("text"):
            title(event["text"], event["start_seconds"], event["duration_seconds"], "DBOT_GRAPHICS")


def build(resolve, spec, plan, template, support_root):
    support_root = Path(support_root)
    support_root.mkdir(parents=True, exist_ok=True)
    if plan["job_id"] != spec["id"] or not plan["segments"]:
        raise ResolveBuildError("invalid job package")
    if (plan["template_id"], plan["template_version"]) != (template["id"], template["version"]):
        raise ResolveBuildError("template version mismatch")
    for segment in plan["segments"]:
        if segment.get("transition", "cut") not in {"cut", "cross_dissolve"}:
            raise ResolveBuildError(
                "non-cut transitions require a registered Resolve transition asset"
            )
        if float(segment.get("speed", 1)) != 1:
            raise ResolveBuildError("retiming is not supported by this backend")
        if not segment["track"].startswith("DBOT_"):
            raise ResolveBuildError("invalid generated track")
    for asset in spec["assets"]:
        stat = Path(asset["path"]).stat()
        if stat.st_size != asset["size_bytes"] or stat.st_mtime_ns != asset["modified_ns"]:
            raise ResolveBuildError("source changed after approval; rescan before building")
    if template.get("source_snapshot") and template.get("snapshot_sha256"):
        import hashlib
        with Path(template["source_snapshot"]).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != template["snapshot_sha256"]:
                raise ResolveBuildError("registered template snapshot changed after approval")
    manager = resolve.GetProjectManager()
    project = manager.GetCurrentProject()
    if not project or project.GetName() != spec["target_project"]:
        project = manager.LoadProject(spec["target_project"])
    checked(project, "target Resolve project is unavailable")
    timelines = [
        project.GetTimelineByIndex(i) for i in range(1, int(project.GetTimelineCount()) + 1)
    ]
    marker_id = "davincibot-job:" + spec["id"]
    # Crash recovery: replaying a completed package opens the existing result.
    for previous in timelines:
        if any(m.get("customData") == marker_id for m in (previous.GetMarkers() or {}).values()):
            checked(project.SetCurrentTimeline(previous), "could not open completed job")
            checked(resolve.OpenPage("edit"), "could not open Edit page")
            return previous.GetName()
    source = next((t for t in timelines if t.GetName() == template["timeline_name"]), None)
    name = next_timeline_name(project, plan["timeline_name"])
    pool = project.GetMediaPool()
    original = project.GetCurrentTimeline()
    if template.get("source_snapshot"):
        timeline = checked(
            pool.ImportTimelineFromFile(template["source_snapshot"]),
            "could not import registered template snapshot",
        )
    else:
        checked(source, "template timeline not found; register a real Resolve timeline first")
        timeline = checked(source.DuplicateTimeline(name), "could not duplicate template")
    created = [timeline]
    try:
        if timeline.GetName() != name:
            checked(timeline.SetName(name), "could not name generated template copy")
        checked(project.SetCurrentTimeline(timeline), "could not select generated timeline")
        profile = spec.get("profile_snapshot") or {}
        defaults = profile.get("timeline", {})
        fps = float(timeline.GetSetting("timelineFrameRate") or 30)
        if defaults and abs(fps - float(defaults["frame_rate"])) > 0.01:
            raise ResolveBuildError("template and workspace frame rates differ")
        for setting, key in (
            ("timelineResolutionWidth", "width"),
            ("timelineResolutionHeight", "height"),
        ):
            if defaults:
                checked(
                    timeline.SetSetting(setting, str(defaults[key])), f"could not set {setting}"
                )
        if defaults:
            checked(
                timeline.SetStartTimecode(defaults.get("start_timecode", "01:00:00:00")),
                "could not set timeline start timecode",
            )
        base_timeline = None
        if any(s.get("transition") == "cross_dissolve" for s in plan["segments"]):
            try:
                from davincibot.resolve.interchange import write_xml
            except ImportError:
                import runpy

                write_xml = runpy.run_path(
                    str(Path(__file__).with_name("davincibot_interchange.py"))
                )["write_xml"]
            xml_path = support_root / (spec["id"] + ".xml")
            write_xml(xml_path, spec, plan, template)
            base_timeline = checked(
                pool.ImportTimelineFromFile(
                    str(xml_path), {"timelineName": name + "_Base", "importSourceClips": True}
                ),
                "XML base import failed",
            )
            created.append(base_timeline)
            by_track = {}
            for segment in plan["segments"]:
                by_track.setdefault(segment["track"], []).append(segment)
            for index, (track_name, segments) in enumerate(sorted(by_track.items()), 1):
                checked(
                    base_timeline.SetTrackName("video", index, track_name),
                    "base track naming failed",
                )
                clips = list(base_timeline.GetItemListInTrack("video", index) or [])
                if len(clips) != len(segments):
                    raise ResolveBuildError("XML import clip count mismatch")
                for clip, segment in zip(
                    clips, sorted(segments, key=lambda s: s["timeline_start"]), strict=True
                ):
                    if segment.get("layout") == "punch_in":
                        for key in ("ZoomX", "ZoomY"):
                            checked(
                                clip.SetProperty(
                                    key,
                                    float(
                                        template.get("parameters", {}).get("punch_in_zoom", 1.15)
                                    ),
                                ),
                                "base punch-in failed",
                            )
            checked(project.SetCurrentTimeline(timeline), "could not restore review timeline")
        populate(project, timeline, spec, plan, template, support_root, base_timeline)
        marker_frame = 0
        existing_markers = timeline.GetMarkers() or {}
        while marker_frame in existing_markers:
            marker_frame += 1
        checked(
            timeline.AddMarker(
                marker_frame, "Green", "DaVinciBot", "Ready for review", 1, marker_id
            ),
            "could not write job completion marker",
        )
        checked(manager.SaveProject(), "project save failed")
        checked(resolve.OpenPage("edit"), "could not open Edit page")
        return timeline.GetName()
    except Exception as error:
        if original:
            project.SetCurrentTimeline(original)
        removed = pool.DeleteTimelines(created)
        saved = manager.SaveProject()
        if not removed or not saved:
            raise ResolveBuildError(
                f"build failed; rollback incomplete for {name}; inspect Resolve before retrying"
            ) from error
        raise ResolveBuildError(str(error)) from error


@contextmanager
def bridge_lock(root):
    """OS-held lock: serializes launcher, desktop builds and cancellation; crash-safe."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "mutation.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ResolveBuildError("Resolve mutation is already in progress") from error
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path, payload):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def run_pending(resolve, root):
    root = Path(root)
    (root / "status").mkdir(parents=True, exist_ok=True)
    (root / "archive").mkdir(parents=True, exist_ok=True)
    with bridge_lock(root):
        pending = sorted((root / "pending").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        if not pending:
            return {"state": "idle", "message": "No pending DaVinciBot jobs"}
        path = pending[0]
        status_path = root / "status" / path.name
        if status_path.exists():
            previous = json.loads(status_path.read_text(encoding="utf-8"))
            if previous.get("state") in {"ready", "failed", "cancelled", "needs_attention"}:
                path.replace(root / "archive" / path.name)
                return previous
            # A killed process may have mutated Resolve. Never blindly replay it.
            if previous.get("state") == "building":
                interrupted = {
                    "state": "needs_attention",
                    "error": "Interrupted build; inspect Resolve before recovery",
                }
                atomic_json(status_path, interrupted)
                path.replace(root / "archive" / path.name)
                return interrupted
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["spec"]["id"] != path.stem:
                raise ValueError("job package identity mismatch")
            atomic_json(status_path, {"state": "building"})
            name = build(
                resolve, payload["spec"], payload["plan"], payload["template"], root / "support"
            )
            status = {"state": "ready", "timeline_name": name}
        except Exception as error:
            status = {"state": "failed", "error": str(error)}
        atomic_json(status_path, status)
        path.replace(root / "archive" / path.name)
        return status
