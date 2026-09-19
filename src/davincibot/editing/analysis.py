"""Bounded local audio analysis. Audio never leaves the machine."""

from __future__ import annotations

import math
import shutil
import statistics
import wave
from array import array
from pathlib import Path
from tempfile import TemporaryDirectory

from davincibot.media import MediaToolError, _run, detect_silence
from davincibot.models import AssetRole


def envelope(path: Path, mode, seconds=120):
    """100 Hz RMS envelope from at most two minutes, using under 2 MB of samples."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaToolError("FFmpeg is required for audio synchronization")
    with TemporaryDirectory(prefix="davincibot-analysis-") as temporary:
        target = Path(temporary) / "mono.wav"
        result = _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-i",
                str(path),
                "-t",
                str(seconds),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            mode=mode,
        )
        if result.returncode:
            raise MediaToolError("could not decode audio for timing analysis")
        values = []
        with wave.open(str(target), "rb") as audio:
            while raw := audio.readframes(80):
                block = array("h", raw)
                values.append(math.sqrt(sum(x * x for x in block) / len(block)) / 32768)
        return values


def onset_times(values, rate=100, minimum_gap=0.25):
    if len(values) < 3:
        return []
    rises = [max(0, values[i] - values[i - 1]) for i in range(1, len(values))]
    threshold = statistics.mean(rises) + 1.5 * statistics.pstdev(rises)
    output = []
    for i in range(1, len(rises) - 1):
        moment = (i + 1) / rate
        if (
            rises[i] > max(threshold, 0.001)
            and rises[i] >= rises[i - 1]
            and rises[i] > rises[i + 1]
            and (not output or moment - output[-1] >= minimum_gap)
        ):
            output.append(moment)
    return output


def sync_offset(reference, camera, rate=100, max_seconds=20):
    """Return source_time - reference_time and normalized correlation confidence."""

    def correlation(lag, stride):
        start, end = max(0, -lag), min(len(reference), len(camera) - lag)
        if end - start < rate * 2:
            return -1.0
        if stride > 1:
            xs = [sum(reference[i:i + stride]) / stride for i in range(start, end - stride + 1, stride)]
            ys = [sum(camera[i + lag:i + lag + stride]) / stride for i in range(start, end - stride + 1, stride)]
        else:
            xs = reference[start:end]
            ys = camera[start + lag:end + lag]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        if vx * vy < 1e-12:
            return -1.0
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(vx * vy)

    coarse = max(
        range(-max_seconds * rate, max_seconds * rate + 1, 10), key=lambda lag: correlation(lag, 10)
    )
    refined = [(correlation(lag, 1), lag) for lag in range(coarse - 10, coarse + 11)]
    score, lag = max(refined)
    return lag / rate, max(0, score)


def analyze_job(spec, profile):
    """Run before preflight so the user sees the exact analyzed edit they approve."""
    data = {"speech": {}, "beats": [], "offsets": {}, "warnings": []}
    primary = [a for a in spec.assets if a.role in {AssetRole.A_ROLL, AssetRole.CAMERA}]
    if profile.remove_silence and not spec.captions:
        for asset in primary:
            if asset.media and asset.media.channels:
                silent = detect_silence(asset.path, mode=spec.resource_mode)
                start = 0.0
                speech = []
                for begin, end in silent:
                    if begin > start:
                        speech.append((start, begin))
                    start = max(start, end)
                if start < asset.media.duration_seconds:
                    speech.append((start, asset.media.duration_seconds))
                data["speech"][asset.id] = speech
    music = next(
        (a for a in spec.assets if a.role is AssetRole.MUSIC and a.media and a.media.channels), None
    )
    if music and profile.align_music_beats:
        data["beats"] = onset_times(envelope(music.path, spec.resource_mode))
        if not data["beats"]:
            data["warnings"].append("No distinct music onsets detected; regular montage cuts used.")
    cameras = [a for a in primary if a.role is AssetRole.CAMERA]
    if len(cameras) > 1:
        manual = spec.overrides.get("sync_offsets", {})
        data["offsets"][cameras[0].id] = 0.0
        reference = None
        for camera in cameras[1:]:
            if camera.id in manual:
                offset = float(manual[camera.id])
                if not math.isfinite(offset):
                    raise MediaToolError("camera offset must be finite")
                data["offsets"][camera.id] = offset
            elif profile.synchronize_cameras and all(
                a.media and a.media.channels for a in [cameras[0], camera]
            ):
                if reference is None:
                    reference = envelope(cameras[0].path, spec.resource_mode)
                offset, confidence = sync_offset(
                    reference, envelope(camera.path, spec.resource_mode)
                )
                if confidence < 0.65:
                    raise MediaToolError(
                    "Camera sync confidence is low. "
                    "Set sync_offsets explicitly in the job editor."
                    )
                data["offsets"][camera.id] = offset
            else:
                raise MediaToolError(
                "Camera sync needs scratch audio or explicit sync_offsets "
                "for each additional camera."
                )
    return data
