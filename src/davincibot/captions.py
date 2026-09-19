from __future__ import annotations

import re
from pathlib import Path

from davincibot.models import CaptionCue

_TIMING = re.compile(
    r"(?P<start>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
    r"(?P<end>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})"
)
_SPEAKER = re.compile(r"^(?:<v\s+([^>]+)>|\[([^]]+)\]:\s*)(.*)$", re.DOTALL)


def parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        parts.insert(0, "0")
    hours, minutes, seconds = parts
    if not 0 <= int(minutes) < 60 or not 0 <= float(seconds) < 60:
        raise ValueError("invalid caption timestamp")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_caption_text(text: str) -> list[CaptionCue]:
    lines = text.replace("\ufeff", "").replace("\r\n", "\n").split("\n")
    cues: list[CaptionCue] = []
    index = 0
    while index < len(lines):
        match = _TIMING.search(lines[index])
        if not match:
            if "-->" in lines[index]:
                raise ValueError(f"invalid caption timing at line {index + 1}")
            index += 1
            continue
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip():
            content.append(lines[index].strip())
            index += 1
        body = " ".join(content).strip()
        speaker = None
        speaker_match = _SPEAKER.match(body)
        if speaker_match:
            speaker = speaker_match.group(1) or speaker_match.group(2)
            body = speaker_match.group(3).strip()
        body = re.sub(r"</?[^>]+>", "", body).strip()
        if body:
            cues.append(
                CaptionCue(
                    start_seconds=parse_timestamp(match.group("start")),
                    end_seconds=parse_timestamp(match.group("end")),
                    text=body,
                    speaker=speaker,
                )
            )
        index += 1
    return sorted(cues, key=lambda cue: cue.start_seconds)


def parse_caption_file(path: Path) -> list[CaptionCue]:
    if path.suffix.lower() not in {".srt", ".vtt"}:
        raise ValueError(f"unsupported caption format: {path.suffix}")
    return parse_caption_text(path.read_text(encoding="utf-8-sig"))
