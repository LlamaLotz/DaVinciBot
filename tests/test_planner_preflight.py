from pathlib import Path

import pytest

from davincibot.models import (
    AssetRef,
    AssetRole,
    FolderRule,
    JobSpec,
    MediaInfo,
    ProviderKind,
    TemplateManifest,
    TemplateSlot,
    WorkflowKind,
    WorkspaceMode,
    WorkspaceProfile,
)
from davincibot.planner.providers import OpenAIChatProvider, PlannerProvider, ProviderError
from davincibot.planner.service import PlanningError, PlanningService, offline_plan
from davincibot.preflight import run_preflight


def fixtures(tmp_path: Path):
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video source")
    stat = source.stat()
    asset = AssetRef(
        path=source,
        role=AssetRole.A_ROLL,
        group_key="lesson",
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        media=MediaInfo(duration_seconds=90),
    )
    template = TemplateManifest(
        id="education",
        name="Education",
        timeline_name="DBOT Education",
        workflows={WorkflowKind.EDUCATIONAL},
        modes={WorkspaceMode.SHORT_FORM},
        slots=[TemplateSlot(id="main", role=AssetRole.A_ROLL, track="DBOT_A_ROLL")],
    )
    profile = WorkspaceProfile(
        id="short",
        name="Short",
        mode=WorkspaceMode.SHORT_FORM,
        folders=[FolderRule(role=AssetRole.A_ROLL, path=tmp_path, required=True)],
        template_ids=[template.id],
    )
    spec = JobSpec(
        profile_id=profile.id,
        prompt="Make an educational tutorial",
        target_project="Course",
        assets=[asset],
    )
    return asset, template, profile, spec


def test_offline_plan_is_deterministic_and_preflight_ready(tmp_path: Path) -> None:
    _, template, profile, spec = fixtures(tmp_path)
    plan = offline_plan(spec, profile, [template])
    assert plan.template_id == template.id
    assert plan.workflow is WorkflowKind.EDUCATIONAL
    assert plan.segments[0].source_out <= 90
    assert run_preflight(spec, plan, profile, template).ready


def test_provider_null_content_is_reported_as_provider_error(monkeypatch) -> None:
    class Response:
        is_error = False

        def json(self):
            return {"choices": [{"message": {"content": None}}]}

    monkeypatch.setattr(
        "davincibot.planner.providers.httpx.post", lambda *args, **kwargs: Response()
    )
    with pytest.raises(ProviderError, match="did not contain JSON content"):
        OpenAIChatProvider("secret", "model").complete_json("system", "user", {})


class BadProvider(PlannerProvider):
    calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        return {"invalid": True}


def test_cloud_planner_repairs_once_then_fails(tmp_path: Path) -> None:
    _, template, profile, spec = fixtures(tmp_path)
    spec.provider = ProviderKind.OPENAI
    service = PlanningService(lambda _provider: "secret")
    provider = BadProvider()
    with pytest.raises(PlanningError, match="schema repair"):
        service.plan(spec, profile, [template], lambda *_args: provider)
    assert provider.calls == 2
