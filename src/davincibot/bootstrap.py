from __future__ import annotations

from pathlib import Path

from davincibot.database import Database
from davincibot.models import (
    AssetRole,
    FolderRule,
    TemplateManifest,
    TemplateSlot,
    WorkflowKind,
    WorkspaceMode,
    WorkspaceProfile,
)

TEMPLATE_IDS = {
    WorkflowKind.TALKING_HEAD: "builtin-talking-head",
    WorkflowKind.MONTAGE_PROMO: "builtin-montage-promo",
    WorkflowKind.EDUCATIONAL: "builtin-educational",
    WorkflowKind.PODCAST_INTERVIEW: "builtin-podcast-interview",
    WorkflowKind.NARRATED_YOUTUBE: "builtin-narrated-youtube",
}


def _folders(root: Path) -> list[FolderRule]:
    return [
        FolderRule(role=AssetRole.A_ROLL, path=root / "A-Roll"),
        FolderRule(role=AssetRole.CAMERA, path=root / "Cameras"),
        FolderRule(role=AssetRole.VOICE, path=root / "Voice"),
        FolderRule(role=AssetRole.MUSIC, path=root / "Music"),
        FolderRule(role=AssetRole.B_ROLL, path=root / "B-Roll"),
        FolderRule(role=AssetRole.GRAPHIC, path=root / "Graphics"),
        FolderRule(role=AssetRole.CAPTION, path=root / "Captions"),
        FolderRule(role=AssetRole.TEMPLATE, path=root / "Templates"),
        FolderRule(role=AssetRole.OUTPUT, path=root / "Output"),
    ]


def seed_defaults(database: Database) -> None:
    definitions = [
        (WorkflowKind.TALKING_HEAD, "Talking Head", {WorkspaceMode.SHORT_FORM}),
        (WorkflowKind.MONTAGE_PROMO, "Montage Promo", {WorkspaceMode.SHORT_FORM}),
        (
            WorkflowKind.EDUCATIONAL,
            "Educational",
            {WorkspaceMode.SHORT_FORM, WorkspaceMode.LONG_FORM},
        ),
        (WorkflowKind.PODCAST_INTERVIEW, "Podcast Interview", {WorkspaceMode.LONG_FORM}),
        (WorkflowKind.NARRATED_YOUTUBE, "Narrated YouTube", {WorkspaceMode.LONG_FORM}),
    ]
    if not database.list_templates():
        for workflow, label, modes in definitions:
            database.save_template(
                TemplateManifest(
                    id=TEMPLATE_IDS[workflow],
                    name=f"Starter {label}",
                    timeline_name=f"DBOT Template - {label}",
                    workflows={workflow},
                    modes=modes,
                    aspect_ratios=({"9:16", "16:9"}),
                    slots=[
                        TemplateSlot(
                            id="primary",
                            role=AssetRole.A_ROLL,
                            track="DBOT_A_ROLL",
                            required=workflow
                            in {WorkflowKind.TALKING_HEAD, WorkflowKind.EDUCATIONAL},
                        ),
                        TemplateSlot(
                            id="broll",
                            role=AssetRole.B_ROLL,
                            track="DBOT_B_ROLL",
                            required=False,
                        ),
                    ],
                )
            )
    if database.list_profiles():
        return
    videos = Path.home() / "Videos" / "DaVinciBot"
    short_ids = [
        TEMPLATE_IDS[WorkflowKind.TALKING_HEAD],
        TEMPLATE_IDS[WorkflowKind.MONTAGE_PROMO],
        TEMPLATE_IDS[WorkflowKind.EDUCATIONAL],
    ]
    long_ids = [
        TEMPLATE_IDS[WorkflowKind.PODCAST_INTERVIEW],
        TEMPLATE_IDS[WorkflowKind.NARRATED_YOUTUBE],
        TEMPLATE_IDS[WorkflowKind.EDUCATIONAL],
    ]
    database.save_profile(
        WorkspaceProfile(
            id="default-short-form",
            name="Short Form",
            mode=WorkspaceMode.SHORT_FORM,
            folders=_folders(videos / "Short Form"),
            template_ids=short_ids,
            timeline={"width": 1080, "height": 1920, "frame_rate": 30},
        )
    )
    database.save_profile(
        WorkspaceProfile(
            id="default-long-form",
            name="Long Form",
            mode=WorkspaceMode.LONG_FORM,
            folders=_folders(videos / "Long Form"),
            template_ids=long_ids,
            audio={"target_lufs": -16},
        )
    )
