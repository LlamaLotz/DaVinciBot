from __future__ import annotations

import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from uuid import UUID

from davincibot.models import EditPlan, JobSpec, TemplateManifest
from davincibot.paths import AppPaths
from davincibot.resolve import runtime
from davincibot.resolve.runtime import ResolveBuildError, next_timeline_name  # noqa: F401


class ResolveBridge(ABC):
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def build(self, spec: JobSpec, plan: EditPlan, template: TemplateManifest) -> str:
        raise NotImplementedError


def connect_resolve() -> Any:
    api_root = Path(
        os.environ.get(
            "RESOLVE_SCRIPT_API",
            r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
        )
    )
    modules = str(api_root / "Modules")
    if modules not in sys.path:
        sys.path.insert(0, modules)
    try:
        import DaVinciResolveScript

        resolve = DaVinciResolveScript.scriptapp("Resolve")
    except ImportError as error:
        raise ResolveBuildError("Resolve scripting module is unavailable") from error
    if not resolve:
        raise ResolveBuildError("Resolve is not running or external scripting is unavailable")
    return resolve


def build_with_resolve(resolve, spec, plan, template):
    return runtime.build(
        resolve,
        spec.model_dump(mode="json"),
        plan.model_dump(mode="json"),
        template.model_dump(mode="json"),
        AppPaths.default().bridge / "support",
    )


class DirectResolveBridge(ResolveBridge):
    def available(self):
        try:
            return bool(connect_resolve())
        except ResolveBuildError:
            return False

    def build(self, spec, plan, template):
        with runtime.bridge_lock(AppPaths.default().bridge):
            return build_with_resolve(connect_resolve(), spec, plan, template)

    def current_timeline_data(self):
        resolve = connect_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project else None
        if not timeline:
            raise ResolveBuildError("no Resolve timeline is currently open")
        return {
            "name": timeline.GetName(),
            "markers": timeline.GetMarkers() or {},
            "tracks": {
                kind: [
                    timeline.GetTrackName(kind, i)
                    for i in range(1, int(timeline.GetTrackCount(kind)) + 1)
                ]
                for kind in ("video", "audio", "subtitle")
            },
            "frame_rate": timeline.GetSetting("timelineFrameRate"),
        }

    def export_snapshot(self, path: Path, expected_name: str):
        resolve = connect_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project else None
        if not timeline or timeline.GetName() != expected_name:
            raise ResolveBuildError(
                "open the selected template timeline before saving its snapshot"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ResolveBuildError("template snapshot already exists; register a new version")
        if not timeline.Export(str(path), resolve.EXPORT_DRT):
            raise ResolveBuildError("Resolve could not export the template snapshot")


class FileResolveBridge(ResolveBridge):
    def __init__(self, bridge_root: Path):
        self.root = bridge_root.resolve()
        self.pending = self.root / "pending"
        self.status = self.root / "status"
        for directory in (self.pending, self.status, self.root / "archive"):
            directory.mkdir(parents=True, exist_ok=True)

    def available(self):
        return True

    def build(self, spec, plan, template):
        UUID(spec.id)
        with runtime.bridge_lock(self.root):
            if self.read_status(spec.id) or (self.pending / f"{spec.id}.json").exists():
                raise ResolveBuildError("job is already submitted; regenerate with a new job ID")
            runtime.atomic_json(
                self.pending / f"{spec.id}.json",
                {
                    "schema_version": 1,
                    "spec": spec.model_dump(mode="json"),
                    "plan": plan.model_dump(mode="json"),
                    "template": template.model_dump(mode="json"),
                },
            )
        return spec.id

    def cancel(self, job_id):
        UUID(job_id)
        with runtime.bridge_lock(self.root):
            status = self.read_status(job_id)
            if status and status.get("state") not in {"cancelled"}:
                raise ResolveBuildError("Resolve already claimed this job; inspect its result")
            runtime.atomic_json(self.status / f"{job_id}.json", {"state": "cancelled"})
            pending = self.pending / f"{job_id}.json"
            if pending.exists():
                pending.replace(self.root / "archive" / pending.name)

    def read_status(self, job_id):
        UUID(job_id)
        path = self.status / f"{job_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def run_pending_in_resolve(resolve, bridge_root):
    return runtime.run_pending(resolve, bridge_root)
