import copy
import math
import random
import shutil
import struct
import wave
from xml.etree.ElementTree import parse

import httpx
import pytest

from davincibot.cache import ManagedCache
from davincibot.captions import parse_caption_text
from davincibot.editing.analysis import onset_times, sync_offset
from davincibot.editing.audio import prepare_audio
from davincibot.editing.engine import build_rule_segments, retime_captions
from davincibot.media import probe_media
from davincibot.models import (
    AssetRef,
    AssetRole,
    CaptionCue,
    EditPlan,
    EditSegment,
    JobSpec,
    MediaInfo,
    ProviderKind,
    TemplateManifest,
    WorkflowKind,
    WorkspaceMode,
    WorkspaceProfile,
)
from davincibot.planner import providers
from davincibot.planner.service import PlanningService, offline_plan
from davincibot.preflight import run_preflight
from davincibot.resolve import runtime
from davincibot.resolve.bridge import FileResolveBridge
from davincibot.resolve.install import install_launcher
from davincibot.resolve.interchange import transition_errors, write_xml


@pytest.fixture
def job(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    stat = path.stat()
    asset = AssetRef(
        path=path,
        role=AssetRole.A_ROLL,
        group_key="lesson",
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        media=MediaInfo(duration_seconds=30, frame_rate=60),
    )
    template = TemplateManifest(
        name="Test",
        timeline_name="Template",
        workflows={WorkflowKind.TALKING_HEAD},
        modes={WorkspaceMode.SHORT_FORM},
        transitions=["cut", "cross_dissolve"],
    )
    profile = WorkspaceProfile(
        name="Short", mode=WorkspaceMode.SHORT_FORM, template_ids=[template.id]
    )
    spec = JobSpec(
        profile_id=profile.id,
        prompt="talking head",
        target_project="Project",
        assets=[asset],
        profile_snapshot=profile,
    )
    plan = offline_plan(spec, profile, [template])
    return spec, plan, profile, template


def test_captions_short_vtt_and_malformed():
    cues = parse_caption_text("WEBVTT\n\n00:01.000 --> 00:02.500\nHello\n")
    assert cues[0].start_seconds == 1
    with pytest.raises(ValueError):
        parse_caption_text("00:99.000 --> 00:02.000\nBad")


def test_caption_retiming_and_source_assignment(job):
    spec, _, _, _ = job
    aid = spec.assets[0].id
    cues = [
        CaptionCue(start_seconds=10, end_seconds=12, text="one", source_asset_id=aid),
        CaptionCue(start_seconds=20, end_seconds=22, text="two", source_asset_id=aid),
        CaptionCue(start_seconds=10, end_seconds=12, text="other", source_asset_id="other"),
    ]
    segments = [
        EditSegment(asset_id=aid, source_in=10, source_out=12, timeline_start=0),
        EditSegment(asset_id=aid, source_in=20, source_out=22, timeline_start=2),
    ]
    mapped = retime_captions(cues, segments, spec.assets)
    assert [(c.start_seconds, c.end_seconds, c.text) for c in mapped] == [
        (0, 2, "one"),
        (2, 4, "two"),
    ]


def test_preflight_detects_changed_missing_sources_and_retiming(job):
    spec, plan, profile, template = job
    assert run_preflight(spec, plan, profile, template).ready
    spec.assets[0].path.write_bytes(b"modified")
    assert not run_preflight(spec, plan, profile, template).ready
    spec.assets[0].path.unlink()
    assert not run_preflight(spec, plan, profile, template).ready
    plan.segments[0].speed = 2
    with pytest.raises(ValueError if False else RuntimeError, match="retiming"):
        PlanningService._validate_plan(plan, spec, [template])


@pytest.mark.parametrize(
    "kind",
    [
        ProviderKind.OPENAI,
        ProviderKind.OPENAI_COMPATIBLE,
        ProviderKind.ANTHROPIC,
        ProviderKind.GEMINI,
    ],
)
@pytest.mark.parametrize("scenario", ["valid", "malformed", "refused", "limited", "oversized"])
def test_provider_contracts(monkeypatch, kind, scenario):
    text = '{"ok": true}' if scenario == "valid" else "broken"
    if scenario == "oversized":
        text = "x" * 2_000_001
    if scenario == "refused":
        text = None
    if kind in {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE}:
        payload = {"choices": [{"message": {"content": text}}]}
    elif kind is ProviderKind.ANTHROPIC:
        payload = {"content": [{"type": "text", "text": text}]}
    else:
        payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    monkeypatch.setattr(
        providers,
        "_post",
        lambda *a, **k: httpx.Response(429 if scenario == "limited" else 200, json=payload),
    )
    provider = providers.make_provider(kind, "test-not-a-key", "model")
    if scenario == "valid":
        assert provider.complete_json("system", "user", {"type": "object"}) == {"ok": True}
    else:
        with pytest.raises(providers.ProviderError):
            provider.complete_json("system", "user", {"type": "object"})


def test_strict_schema_does_not_mutate_local_schema():
    schema = EditPlan.model_json_schema()
    original = copy.deepcopy(schema)
    strict = providers.strict_schema(schema)
    assert schema == original
    assert set(strict["required"]) == set(strict["properties"])
    for item in strict["$defs"].values():
        if item.get("type") == "object":
            assert set(item["required"]) == set(item["properties"])
            assert item["additionalProperties"] is False


def test_onsets_and_waveform_offset():
    values = [0.0] * 500
    for index in (50, 150, 250, 350):
        values[index] = 1
    assert onset_times(values) == [0.5, 1.5, 2.5, 3.5]
    rng = random.Random(9)
    reference = [rng.random() for _ in range(900)]
    delayed = [0] * 37 + reference
    offset, confidence = sync_offset(reference, delayed, max_seconds=2)
    assert offset == 0.37
    assert confidence > 0.99
    assert sync_offset([0] * 900, [0] * 900, max_seconds=1)[1] == 0


def test_narrated_uses_voice_duration_with_broll(job):
    spec, _, profile, template = job
    spec.assets[0].role = AssetRole.B_ROLL
    voice = spec.assets[0].model_copy(
        update={"id": "voice", "role": AssetRole.VOICE, "media": MediaInfo(duration_seconds=11)}
    )
    segments, _, _ = build_rule_segments(
        WorkflowKind.NARRATED_YOUTUBE, profile.mode, [*spec.assets, voice], [], template
    )
    assert max(s.timeline_start + s.source_out - s.source_in for s in segments) == 11


def test_transition_xml_and_handle_validation(tmp_path, job):
    spec, plan, _, template = job
    aid = spec.assets[0].id
    plan.segments = [
        EditSegment(asset_id=aid, source_in=1, source_out=5, timeline_start=0),
        EditSegment(
            asset_id=aid, source_in=6, source_out=10, timeline_start=4, transition="cross_dissolve"
        ),
    ]
    args = [v.model_dump(mode="json") for v in (spec, plan, template)]
    path = tmp_path / "timeline.xml"
    write_xml(path, *args)
    xml = parse(path)
    assert xml.findtext(".//transitionitem/effect/name") == "Cross Dissolve"
    assert xml.findtext(".//transitionitem/start") == "112"
    assert xml.findtext(".//clipitem/in") == "60"
    assert xml.findtext(".//clipitem/end") == "-1"
    args[1]["segments"][1]["source_in"] = 0
    assert transition_errors(*args)


def test_file_queue_cancel_and_failed_job_does_not_block_next(tmp_path, job, monkeypatch):
    spec, plan, _, template = job
    bridge = FileResolveBridge(tmp_path / "bridge")
    bridge.build(spec, plan, template)
    bridge.cancel(spec.id)
    assert runtime.run_pending(None, bridge.root)["state"] == "idle"
    with pytest.raises(runtime.ResolveBuildError):
        bridge.build(spec, plan, template)
    for i in range(2):
        next_spec = spec.model_copy(update={"id": f"00000000-0000-0000-0000-{i:012d}"})
        next_plan = plan.model_copy(update={"job_id": next_spec.id})
        bridge.build(next_spec, next_plan, template)
    monkeypatch.setattr(runtime, "build", lambda *a: (_ for _ in ()).throw(ValueError("failure")))
    assert runtime.run_pending(None, bridge.root)["state"] == "failed"
    monkeypatch.setattr(runtime, "build", lambda *a: "Project_v002")
    assert runtime.run_pending(None, bridge.root)["state"] == "ready"
    assert not list(bridge.pending.glob("*.json"))


def test_launcher_has_durable_stdlib_runtime(tmp_path):
    launcher = install_launcher(tmp_path / "Build.py")
    assert "site-packages" not in launcher.read_text()
    assert "_MEI" not in launcher.read_text()
    assert launcher.with_name("davincibot_runtime.py").exists()
    assert launcher.with_name("davincibot_interchange.py").exists()


def test_cache_keeps_unknown_files_and_durable_pins(tmp_path):
    cache = ManagedCache(tmp_path / "cache")
    outside = tmp_path / "source.wav"
    outside.write_bytes(b"original")
    path = cache.path_for("a" * 64, ".wav")
    path.parent.mkdir()
    path.write_bytes(b"generated")
    unknown = cache.root / "personal.txt"
    unknown.write_text("never delete")
    cache.pin("job", [path])
    assert ManagedCache(cache.root).cleanup(0) == []
    cache.unpin("job")
    assert cache.cleanup(0) == [path]
    assert unknown.exists() and outside.read_bytes() == b"original"
    with pytest.raises(ValueError):
        cache.path_for("a" * 64, "/../../source.wav")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires local FFmpeg")
def test_actual_audio_output_matches_edit_and_reuses_cache(tmp_path, job):
    spec, plan, _, _ = job
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(
            b"".join(
                struct.pack("<h", int(10000 * math.sin(i * 2 * math.pi * 440 / 8000)))
                for i in range(24000)
            )
        )
    asset = spec.assets[0]
    asset.path = path
    asset.size_bytes, asset.modified_ns = path.stat().st_size, path.stat().st_mtime_ns
    asset.media = MediaInfo(duration_seconds=3, channels=1, sample_rate=8000)
    plan.segments = [
        EditSegment(asset_id=asset.id, source_in=0, source_out=0.5, timeline_start=0),
        EditSegment(asset_id=asset.id, source_in=1.5, source_out=2, timeline_start=0.5),
    ]
    output = prepare_audio(spec, plan, tmp_path / "cache")
    assert abs(probe_media(output).duration_seconds - 1) < 0.01
    stamp = output.stat().st_mtime_ns
    assert prepare_audio(spec, plan, tmp_path / "cache") == output
    assert output.stat().st_mtime_ns == stamp
