from pathlib import Path

import pytest
from pydantic import ValidationError

from davincibot.captions import parse_caption_text, parse_timestamp
from davincibot.editing.engine import build_rule_segments, speech_ranges
from davincibot.models import (
    AssetRef,
    AssetRole,
    MediaInfo,
    ResourceMode,
    TemplateManifest,
    TemplateSlot,
    WorkflowKind,
    WorkspaceMode,
    WorkspaceProfile,
)
from davincibot.resources import limits_for


def test_caption_parser_handles_srt_vtt_and_speakers() -> None:
    cues = parse_caption_text(
        """WEBVTT

00:00:01.000 --> 00:00:02.500
<v Alex>Hello world</v>

2
00:00:03,000 --> 00:00:04,000
[Sam]: Next line
"""
    )
    assert parse_timestamp("01:02:03,500") == 3723.5
    assert [cue.speaker for cue in cues] == ["Alex", "Sam"]
    assert cues[0].text == "Hello world"


def test_caption_rejects_reverse_range() -> None:
    with pytest.raises(ValidationError):
        from davincibot.models import CaptionCue

        CaptionCue(start_seconds=2, end_seconds=1, text="bad")


def test_resource_limits_reserve_capacity() -> None:
    assert limits_for(ResourceMode.ECO, 12).workers == 3
    assert limits_for(ResourceMode.BALANCED, 12).workers == 6
    assert limits_for(ResourceMode.TURBO, 12).workers == 10


def test_profile_rejects_duplicate_folder_roles(tmp_path: Path) -> None:
    from davincibot.models import FolderRule

    with pytest.raises(ValidationError):
        WorkspaceProfile(
            name="bad",
            mode=WorkspaceMode.SHORT_FORM,
            folders=[
                FolderRule(role=AssetRole.A_ROLL, path=tmp_path),
                FolderRule(role=AssetRole.A_ROLL, path=tmp_path / "two"),
            ],
        )


def test_speech_ranges_merge_short_gaps() -> None:
    cues = parse_caption_text(
        """1
00:00:00,100 --> 00:00:01,000
One

2
00:00:01,200 --> 00:00:02,000
Two
"""
    )
    assert len(speech_ranges(cues)) == 1


def test_talking_head_rules_add_broll_and_callouts(tmp_path: Path) -> None:
    primary = AssetRef(
        path=tmp_path / "main.mp4",
        role=AssetRole.A_ROLL,
        group_key="main",
        size_bytes=1,
        modified_ns=1,
        media=MediaInfo(duration_seconds=60, width=1920, height=1080),
    )
    broll = AssetRef(
        path=tmp_path / "broll.mp4",
        role=AssetRole.B_ROLL,
        group_key="main",
        size_bytes=1,
        modified_ns=1,
        media=MediaInfo(duration_seconds=10),
    )
    captions = parse_caption_text(
        """1
00:00:00,000 --> 00:00:12,000
Lesson one

2
00:00:14,000 --> 00:00:30,000
Lesson two
"""
    )
    template = TemplateManifest(
        name="Education",
        timeline_name="Template",
        workflows={WorkflowKind.EDUCATIONAL},
        modes={WorkspaceMode.SHORT_FORM},
        slots=[TemplateSlot(id="primary", role=AssetRole.A_ROLL, track="DBOT_A_ROLL")],
    )
    segments, graphics, warnings = build_rule_segments(
        WorkflowKind.EDUCATIONAL,
        WorkspaceMode.SHORT_FORM,
        [primary, broll],
        captions,
        template,
    )
    assert any(segment.track == "DBOT_B_ROLL" for segment in segments)
    assert graphics
    assert not warnings
