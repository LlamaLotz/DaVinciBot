from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from davincibot.models import MediaInfo, ResourceMode
from davincibot.resources import limits_for


class MediaToolError(RuntimeError):
    pass


def _run(
    command: list[str], timeout: float = 120, mode: ResourceMode = ResourceMode.BALANCED
) -> subprocess.CompletedProcess[str]:
    limits = limits_for(mode)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | (
                    getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                    if limits.process_priority == "below_normal"
                    else 0
                )
            ),
        )
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
                kernel.SetProcessAffinityMask.restype = wintypes.BOOL
                if not kernel.SetProcessAffinityMask(
                    int(process._handle), (1 << limits.workers) - 1
                ):
                    raise OSError("could not apply media worker CPU limit")
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaToolError(str(error)) from error


def _rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    numerator, denominator = value.split("/", 1) if "/" in value else (value, "1")
    return float(numerator) / float(denominator) if float(denominator) else None


def probe_media(path: Path) -> MediaInfo:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise MediaToolError("ffprobe was not found on PATH")
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode:
        raise MediaToolError(result.stderr.strip() or f"ffprobe failed for {path.name}")
    payload = json.loads(result.stdout)
    video = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in payload.get("streams", []) if s.get("codec_type") == "audio"), {})
    return MediaInfo(
        duration_seconds=float(payload.get("format", {}).get("duration") or 0),
        width=video.get("width"),
        height=video.get("height"),
        frame_rate=_rate(video.get("r_frame_rate")),
        sample_rate=int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        channels=audio.get("channels"),
        codec=video.get("codec_name") or audio.get("codec_name"),
    )


def detect_silence(
    path: Path,
    threshold_db: float = -35.0,
    minimum_seconds: float = 0.45,
    mode: ResourceMode = ResourceMode.BALANCED,
) -> list[tuple[float, float]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaToolError("ffmpeg was not found on PATH")
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={threshold_db}dB:d={minimum_seconds}",
            "-f",
            "null",
            os.devnull,
        ],
        timeout=3600,
        mode=mode,
    )
    if result.returncode:
        raise MediaToolError("silence analysis failed")
    starts: list[float] = []
    ranges: list[tuple[float, float]] = []
    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            starts.append(float(line.rsplit("silence_start:", 1)[1].strip()))
        elif "silence_end:" in line and starts:
            end_text = line.rsplit("silence_end:", 1)[1].split("|", 1)[0].strip()
            ranges.append((starts.pop(0), float(end_text)))
    if starts:
        ranges.append((starts[0], probe_media(path).duration_seconds))
    return ranges


def audio_mix_command(
    dialogue: Path,
    music: Path | None,
    output: Path,
    target_lufs: float,
    true_peak_db: float,
    duck_db: float,
    threads: int,
) -> list[str]:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    if music is None:
        return [
            ffmpeg,
            "-y",
            "-threads",
            str(threads),
            "-i",
            str(dialogue),
            "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak_db}:LRA=11",
            str(output),
        ]
    duck_ratio = max(2.0, min(20.0, math.pow(10, abs(duck_db) / 20)))
    graph = (
        f"[1:a][0:a]sidechaincompress=threshold=0.02:ratio={duck_ratio:.2f}:"
        "attack=120:release=500[ducked];"
        f"[0:a][ducked]amix=inputs=2:normalize=0,loudnorm=I={target_lufs}:"
        f"TP={true_peak_db}:LRA=11[out]"
    )
    return [
        ffmpeg,
        "-y",
        "-threads",
        str(threads),
        "-i",
        str(dialogue),
        "-i",
        str(music),
        "-filter_complex",
        graph,
        "-map",
        "[out]",
        str(output),
    ]


def render_audio_mix(command: list[str]) -> None:
    result = _run(command, timeout=3600)
    if result.returncode:
        raise MediaToolError(result.stderr[-2000:])
