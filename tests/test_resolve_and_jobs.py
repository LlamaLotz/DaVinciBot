from pathlib import Path

import pytest

from davincibot.database import Database
from davincibot.jobs import InvalidJobTransition, JobService
from davincibot.models import (
    AssetRef,
    AssetRole,
    CaptionCue,
    EditPlan,
    EditSegment,
    JobSpec,
    JobState,
    MediaInfo,
    TemplateManifest,
    TemplateSlot,
    WorkflowKind,
    WorkspaceMode,
    WorkspaceProfile,
)
from davincibot.resolve import runtime
from davincibot.resolve.bridge import next_timeline_name


class Timeline:
    def __init__(self, name: str):
        self.name = name

    def GetName(self):
        return self.name


class Project:
    def __init__(self, names: list[str]):
        self.timelines = [Timeline(name) for name in names]

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, index):
        return self.timelines[index - 1]


def test_versioned_timeline_name_never_overwrites() -> None:
    project = Project(["Video_v001", "Video_v002", "Template"])
    assert next_timeline_name(project, "Video") == "Video_v003"


class FakeTool:
    def __init__(self):
        self.inputs = {}

    def SetInput(self, name, value):
        self.inputs[name] = value
        return True

    def ConnectInput(self, name, value):
        return True


class FakeFusion:
    def AddTool(self, name):
        return FakeTool()


class FakeClip:
    def __init__(self):
        self.properties = {}

    def SetProperty(self, name, value):
        self.properties[name] = value
        return True

    def AddFusionComp(self):
        return FakeFusion()


class FakeTimeline:
    def __init__(self):
        self.tracks = {"video": [], "audio": []}
        self.items = {"video": {}, "audio": {}}
        self.appended = []
        self.enabled = {}
        self.markers = []

    def GetSetting(self, name):
        return "30" if name == "timelineFrameRate" else ""

    def GetStartFrame(self):
        return 0

    def GetTrackCount(self, kind):
        return len(self.tracks[kind])

    def GetTrackName(self, kind, index):
        return self.tracks[kind][index - 1]

    def AddTrack(self, kind, *_args):
        self.tracks[kind].append("")
        return True

    def SetTrackName(self, kind, index, name):
        self.tracks[kind][index - 1] = name
        self.items[kind][index] = []
        return True

    def GetItemListInTrack(self, kind, index):
        return self.items[kind].get(index, [])

    def DeleteClips(self, items, _ripple):
        return True

    def SetTrackEnable(self, kind, index, enabled):
        self.enabled[(kind, index)] = enabled
        return True

    def AddMarker(self, *args):
        self.markers.append(args)
        return True


class FakeMediaPool:
    def __init__(self, timeline):
        self.timeline = timeline
        self.imported = {}
        self.clips = []

    def ImportMedia(self, paths):
        item = object()
        self.imported[paths[0]] = item
        return [item]

    def AppendToTimeline(self, infos):
        self.timeline.appended.extend(infos)
        clip = FakeClip()
        self.clips.append(clip)
        return [clip]


class FakeProject:
    def __init__(self, timeline):
        self.pool = FakeMediaPool(timeline)

    def GetMediaPool(self):
        return self.pool


def test_runtime_uses_source_frame_rate_and_applies_edit_properties(tmp_path: Path) -> None:
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"source")
    stat = source.stat()
    asset = AssetRef(
        path=source,
        role=AssetRole.A_ROLL,
        group_key="camera",
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        media=MediaInfo(duration_seconds=10, frame_rate=60, channels=2),
    )
    template = TemplateManifest(
        id="template",
        name="Template",
        timeline_name="Template",
        workflows={WorkflowKind.TALKING_HEAD},
        modes={WorkspaceMode.SHORT_FORM},
        slots=[TemplateSlot(id="main", role=AssetRole.A_ROLL, track="DBOT_A_ROLL")],
        parameters={"font": "Arial", "punch_in_zoom": 1.2},
    )
    spec = JobSpec(
        id="job-runtime",
        profile_id="profile",
        prompt="edit",
        target_project="Project",
        assets=[asset],
        profile_snapshot=WorkspaceProfile(
            id="profile", name="Short", mode=WorkspaceMode.SHORT_FORM
        ),
    )
    plan = EditPlan(
        job_id=spec.id,
        template_id=template.id,
        template_version=1,
        workflow=WorkflowKind.TALKING_HEAD,
        timeline_name="Project",
        segments=[
            EditSegment(
                asset_id=asset.id,
                source_in=1,
                source_out=2,
                timeline_start=0,
                layout="punch_in",
                speed=1,
            )
        ],
        captions=[CaptionCue(start_seconds=0, end_seconds=1, text="A caption")],
    )
    timeline = FakeTimeline()
    timeline_project = FakeProject(timeline)
    runtime.populate(
        timeline_project,
        timeline,
        spec.model_dump(mode="json"),
        plan.model_dump(mode="json"),
        template.model_dump(mode="json"),
        tmp_path / "support",
    )
    video = next(item for item in timeline.appended if item["mediaType"] == 1)
    assert video["startFrame"] == 60
    assert video["endFrame"] == 119
    assert any(clip.properties.get("ZoomX") == 1.2 for clip in timeline_project.pool.clips)


def test_job_state_machine_blocks_invalid_transitions(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    service = JobService(database)
    record = service.create(JobSpec(profile_id="p", prompt="edit", target_project="Demo"))
    service.transition(record.id, JobState.PREFLIGHT)
    with pytest.raises(InvalidJobTransition):
        service.transition(record.id, JobState.READY)
    database.close()
