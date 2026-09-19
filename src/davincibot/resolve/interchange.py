"""Standard-library FCP7 XML compiler; also installed with the Free launcher."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, SubElement


def value(parent, name, text):
    node = SubElement(parent, name)
    node.text = str(text)
    return node


def rate(parent, fps):
    node = SubElement(parent, "rate")
    value(node, "timebase", round(fps))
    value(node, "ntsc", "TRUE" if abs(fps - round(fps) * 1000 / 1001) < 0.005 else "FALSE")


def transition_errors(spec, plan, template):
    assets = {a["id"]: a for a in spec["assets"]}
    half = float(template.get("parameters", {}).get("transition_seconds", 0.5)) / 2
    tracks = defaultdict(list)
    for segment in plan["segments"]:
        tracks[segment["track"]].append(segment)
    errors = []
    for segments in tracks.values():
        segments.sort(key=lambda s: s["timeline_start"])
        for i, segment in enumerate(segments):
            if segment["transition"] != "cross_dissolve":
                continue
            if not i:
                errors.append("cross dissolve needs a preceding clip on the same track")
                continue
            previous = segments[i - 1]
            end = previous["timeline_start"] + previous["source_out"] - previous["source_in"]
            media = assets[previous["asset_id"]].get("media") or {}
            if abs(end - segment["timeline_start"]) > 0.02:
                errors.append("cross dissolve requires adjacent clips")
            if (
                segment["source_in"] < half
                or media.get("duration_seconds", 0) - previous["source_out"] < half
            ):
                errors.append("cross dissolve needs unused source handles on both clips")
            if (
                min(
                    segment["source_out"] - segment["source_in"],
                    previous["source_out"] - previous["source_in"],
                )
                < 2 * half
            ):
                errors.append("clip is shorter than its transition")
    return errors


def write_xml(path: Path, spec: dict, plan: dict, template: dict):
    errors = transition_errors(spec, plan, template)
    if errors:
        raise ValueError("; ".join(errors))
    defaults = (spec.get("profile_snapshot") or {}).get("timeline", {})
    fps = float(defaults.get("frame_rate", 30))
    half_frames = max(
        1, round(float(template.get("parameters", {}).get("transition_seconds", 0.5)) * fps / 2)
    )
    half = half_frames / fps
    assets = {a["id"]: a for a in spec["assets"]}
    root = Element("xmeml", version="5")
    sequence = SubElement(root, "sequence", id="davincibot-base")
    value(sequence, "name", plan["timeline_name"] + " base edit")
    rate(sequence, fps)
    duration = max(s["timeline_start"] + s["source_out"] - s["source_in"] for s in plan["segments"])
    value(sequence, "duration", round(duration * fps))
    timecode = SubElement(sequence, "timecode")
    rate(timecode, fps)
    value(timecode, "frame", 0)
    value(timecode, "displayformat", "NDF")
    media = SubElement(sequence, "media")
    video = SubElement(media, "video")
    sample = SubElement(SubElement(video, "format"), "samplecharacteristics")
    rate(sample, fps)
    value(sample, "width", defaults.get("width", 1920))
    value(sample, "height", defaults.get("height", 1080))
    value(sample, "pixelaspectratio", "square")
    value(sample, "fielddominance", "none")
    tracks = defaultdict(list)
    for segment in plan["segments"]:
        tracks[segment["track"]].append(segment)
    for name, segments in sorted(tracks.items()):
        track = SubElement(video, "track")
        value(track, "name", name)
        segments.sort(key=lambda s: s["timeline_start"])
        for i, segment in enumerate(segments):
            incoming = segment["transition"] == "cross_dissolve"
            outgoing = i + 1 < len(segments) and segments[i + 1]["transition"] == "cross_dissolve"
            start = round(segment["timeline_start"] * fps)
            if incoming:
                transition = SubElement(track, "transitionitem")
                rate(transition, fps)
                value(transition, "start", start - half_frames)
                value(transition, "end", start + half_frames)
                value(transition, "alignment", "center")
                effect = SubElement(transition, "effect")
                for key, text in [
                    ("name", "Cross Dissolve"),
                    ("effectid", "Cross Dissolve"),
                    ("effecttype", "transition"),
                    ("mediatype", "video"),
                ]:
                    value(effect, key, text)
            asset = assets[segment["asset_id"]]
            source_rate = (asset.get("media") or {}).get("frame_rate") or fps
            length = round(asset["media"]["duration_seconds"] * source_rate)
            clip = SubElement(track, "clipitem", id=f"clip-{len(list(root.iter('clipitem')))}")
            value(clip, "name", Path(asset["path"]).name)
            rate(clip, source_rate)
            value(clip, "duration", length)
            value(clip, "start", -1 if incoming else start)
            value(
                clip,
                "end",
                -1
                if outgoing
                else round(
                    (segment["timeline_start"] + segment["source_out"] - segment["source_in"]) * fps
                ),
            )
            value(
                clip, "in", round((segment["source_in"] - (half if incoming else 0)) * source_rate)
            )
            value(
                clip,
                "out",
                round((segment["source_out"] + (half if outgoing else 0)) * source_rate),
            )
            file = SubElement(clip, "file", id=f"file-{len(list(root.iter('file')))}")
            value(file, "name", Path(asset["path"]).name)
            value(file, "pathurl", Path(asset["path"]).resolve().as_uri())
            rate(file, source_rate)
            value(file, "duration", length)
            source = SubElement(
                SubElement(SubElement(file, "media"), "video"), "samplecharacteristics"
            )
            rate(source, source_rate)
            value(source, "width", asset["media"].get("width") or defaults.get("width", 1920))
            value(source, "height", asset["media"].get("height") or defaults.get("height", 1080))
    ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
