"""Render the approved primary edit to a reusable, normalized master WAV."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from davincibot.media import MediaToolError, _run
from davincibot.models import AssetRole
from davincibot.resources import limits_for


def prepare_audio(spec, plan, cache_root: Path, cancelled=lambda: False):
    assets = {a.id: a for a in spec.assets}
    primary = sorted(
        (s for s in plan.segments if s.track != "DBOT_B_ROLL"), key=lambda s: s.timeline_start
    )
    if not primary:
        return None
    sound = [
        s
        for s in primary
        if assets[s.asset_id].media
        and assets[s.asset_id].media.channels
        and assets[s.asset_id].role in {AssetRole.A_ROLL, AssetRole.CAMERA}
    ]
    music = [assets[key] for key in plan.audio.music_asset_ids]
    voice = [
        assets[key]
        for key in plan.audio.dialogue_asset_ids
        if assets[key].role is AssetRole.VOICE
    ]
    if not sound and not music and not voice:
        return None
    settings = {
        "version": 1,
        "segments": [s.model_dump() for s in primary],
        "assets": [a.model_dump(mode="json") for a in spec.assets],
        "audio": plan.audio.model_dump(),
        "policy": spec.profile_snapshot.audio.model_dump() if spec.profile_snapshot else {},
    }
    # Asset UUIDs change on scans: canonicalize references to stable fingerprints.
    for segment in settings["segments"]:
        segment["asset_id"] = str(assets[segment["asset_id"]].path)
    for asset in settings["assets"]:
        asset.pop("id", None)
    settings["audio"]["dialogue_asset_ids"] = [
        str(assets[k].path) for k in plan.audio.dialogue_asset_ids
    ]
    settings["audio"]["music_asset_ids"] = [str(assets[k].path) for k in plan.audio.music_asset_ids]
    key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    directory = cache_root.resolve() / "audio" / key
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "master.wav"
    if output.exists() and output.stat().st_size > 44:
        return output
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaToolError("FFmpeg is required to prepare the master audio")
    threads = str(limits_for(spec.resource_mode).ffmpeg_threads)
    base = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
    ]
    duration = max(s.timeline_start + s.source_out - s.source_in for s in plan.segments)
    if shutil.disk_usage(directory).free < duration * 48000 * 8 + 512 * 1024 * 1024:
        raise MediaToolError("insufficient disk space for audio derivatives")

    def run(args):
        if cancelled():
            raise MediaToolError("cancelled before Resolve mutation")
        result = _run(base + args, timeout=3600, mode=spec.resource_mode)
        if result.returncode:
            raise MediaToolError("audio preparation failed; verify source audio is decodable")

    pieces = []
    position = 0.0

    def piece(length, segment=None):
        if length <= 0.00001:
            return
        path = directory / f"piece-{len(pieces):06d}.wav"
        if segment is None or segment not in sound:
            inputs = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        else:
            inputs = [
                "-threads",
                "1",
                "-ss",
                str(segment.source_in),
                "-i",
                str(assets[segment.asset_id].path),
            ]
        run(
            inputs
            + [
                "-t",
                str(length),
                "-map",
                "0:a:0",
                "-vn",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                "-threads",
                threads,
                str(path),
            ]
        )
        pieces.append(path)

    for segment in primary:
        if segment.timeline_start < position - 0.001:
            raise MediaToolError("overlapping primary audio is not supported; adjust the plan")
        piece(segment.timeline_start - position)
        piece(segment.source_out - segment.source_in, segment)
        position = segment.timeline_start + segment.source_out - segment.source_in
    piece(duration - position)
    listing = directory / "concat.txt"
    listing.write_text("\n".join(f"file '{path.name}'" for path in pieces), encoding="utf-8")
    dialogue = directory / "dialogue.wav"
    run(["-f", "concat", "-safe", "1", "-i", str(listing), "-c:a", "copy", str(dialogue)])
    if voice:
        voice_mix = directory / "dialogue-with-voice.wav"
        run(
            [
                "-threads",
                "1",
                "-i",
                str(dialogue),
                "-i",
                str(voice[0].path),
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[voice_mix]",
                "-map",
                "[voice_mix]",
                "-t",
                str(duration),
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(voice_mix),
            ]
        )
        dialogue = voice_mix
    inputs = ["-threads", "1", "-i", str(dialogue)]
    if music:
        inputs += ["-stream_loop", "-1", "-threads", "1", "-i", str(music[0].path)]
        graph = (
            f"[0:a]asplit=2[dry][side];[1:a]volume={plan.audio.duck_db}dB[bed];"
            "[bed][side]sidechaincompress=threshold=0.02:ratio=6:attack=120:release=500[duck];"
            "[dry][duck]amix=inputs=2:duration=first:normalize=0[mix];"
        )
        label = "[mix]"
    else:
        graph, label = "", "[0:a]"
    fade = min(
        (spec.profile_snapshot.audio.fade_ms / 1000 if spec.profile_snapshot else 0.25),
        duration / 2,
    )
    graph += (
        f"{label}loudnorm=I={plan.audio.target_lufs}:TP={plan.audio.true_peak_db}:LRA=11,"
        f"afade=t=in:d={fade},afade=t=out:st={duration - fade}:d={fade}[out]"
    )
    temporary = directory / "master.pending.wav"
    run(
        inputs
        + [
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-t",
            str(duration),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            str(temporary),
        ]
    )
    temporary.replace(output)
    return output
