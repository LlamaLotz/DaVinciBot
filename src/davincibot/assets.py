from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from davincibot.captions import parse_caption_file
from davincibot.media import MediaToolError, probe_media
from davincibot.models import AssetRef, AssetRole, CaptionCue, WorkspaceProfile
from davincibot.resources import limits_for

DEFAULT_EXTENSIONS: dict[AssetRole, set[str]] = {
    AssetRole.A_ROLL: {".mp4", ".mov", ".mxf", ".mkv", ".avi"},
    AssetRole.CAMERA: {".mp4", ".mov", ".mxf", ".mkv", ".avi"},
    AssetRole.VOICE: {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac"},
    AssetRole.MUSIC: {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac"},
    AssetRole.B_ROLL: {".mp4", ".mov", ".mxf", ".mkv", ".avi"},
    AssetRole.GRAPHIC: {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".svg"},
    AssetRole.CAPTION: {".srt", ".vtt"},
    AssetRole.TEMPLATE: {".drt", ".setting", ".comp", ".json"},
    AssetRole.OUTPUT: set(),
}


class ScanResult:
    def __init__(self, assets: list[AssetRef], captions: list[CaptionCue], warnings: list[str]):
        self.assets = assets
        self.captions = captions
        self.warnings = warnings


def group_key(path: Path, root: Path, by_subfolder: bool) -> str:
    relative = path.relative_to(root)
    if by_subfolder and len(relative.parts) > 1:
        return relative.parts[0].casefold()
    stem = path.stem.casefold()
    for token in ("_aroll", "_a-roll", "_cam1", "_cam2", "_voice", "_music", "_broll", "_captions"):
        stem = stem.replace(token, "")
    return stem.rstrip("_- ") or path.stem.casefold()


class AssetScanner:
    def scan(self, profile: WorkspaceProfile, probe: bool = True) -> ScanResult:
        assets: list[AssetRef] = []
        captions: list[CaptionCue] = []
        warnings: list[str] = []
        probe_candidates: list[AssetRef] = []
        for folder in profile.folders:
            if folder.role is AssetRole.OUTPUT:
                continue
            if not folder.path.exists():
                message = f"{folder.role.value} folder does not exist: {folder.path}"
                if folder.required:
                    warnings.append(f"REQUIRED: {message}")
                else:
                    warnings.append(message)
                continue
            files = folder.path.rglob("*") if folder.recursive else folder.path.glob("*")
            allowed = set(folder.extensions) or DEFAULT_EXTENSIONS[folder.role]
            for path in sorted(p for p in files if p.is_file() and p.suffix.lower() in allowed):
                stat = path.stat()
                asset = AssetRef(
                    path=path.resolve(),
                    role=folder.role,
                    group_key=group_key(path, folder.path, profile.group_by_subfolder),
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                )
                assets.append(asset)
                if folder.role is AssetRole.CAPTION:
                    try:
                        captions.extend(
                            cue.model_copy(update={"group_key": asset.group_key})
                            for cue in parse_caption_file(path)
                        )
                    except (OSError, UnicodeError, ValueError) as error:
                        warnings.append(f"Caption parse failed for {path.name}: {error}")
                elif folder.role in {
                    AssetRole.A_ROLL,
                    AssetRole.CAMERA,
                    AssetRole.VOICE,
                    AssetRole.MUSIC,
                    AssetRole.B_ROLL,
                }:
                    probe_candidates.append(asset)

        if probe and probe_candidates:
            limits = limits_for(profile.resource_mode)
            with ThreadPoolExecutor(max_workers=limits.max_parallel_probes) as pool:
                pending = {
                    pool.submit(probe_media, asset.path): asset for asset in probe_candidates
                }
                for future in as_completed(pending):
                    asset = pending[future]
                    try:
                        asset.media = future.result()
                    except MediaToolError as error:
                        warnings.append(f"Media probe failed for {asset.path.name}: {error}")
        for cue in captions:
            matches = [
                a
                for a in assets
                if a.group_key == cue.group_key
                and a.role in {AssetRole.A_ROLL, AssetRole.VOICE, AssetRole.CAMERA}
            ]
            if len(matches) == 1:
                cue.source_asset_id = matches[0].id
            elif not matches:
                warnings.append(f"Unmatched captions in group {cue.group_key}; assign a group.")
        return ScanResult(assets, captions, warnings)


def partition_batches(assets: list[AssetRef]) -> dict[str, list[AssetRef]]:
    groups: dict[str, list[AssetRef]] = {}
    for asset in assets:
        groups.setdefault(asset.group_key, []).append(asset)
    return groups
