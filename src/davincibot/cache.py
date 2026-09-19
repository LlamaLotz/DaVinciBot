from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


class ManagedCache:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, source: Path, settings: dict[str, Any]) -> str:
        stat = source.stat()
        payload = json.dumps(settings, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256()
        digest.update(str(source.resolve()).encode())
        digest.update(str(stat.st_size).encode())
        digest.update(str(stat.st_mtime_ns).encode())
        digest.update(payload.encode())
        return digest.hexdigest()

    def path_for(self, key: str, suffix: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", key) or not re.fullmatch(
            r"\.[a-zA-Z0-9]{1,12}", suffix
        ):
            raise ValueError("invalid cache key")
        return self.root / key[:2] / f"{key}{suffix}"

    def usage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def cleanup(self, max_bytes: int, pinned: set[Path] | None = None) -> list[Path]:
        if max_bytes < 0:
            raise ValueError("cache size limit cannot be negative")
        pinned_resolved = {path.resolve() for path in (pinned or set())}
        for paths in self.pins().values():
            pinned_resolved.update((self.root / path).resolve() for path in paths)
        files = sorted(
            (path for path in self.root.rglob("*") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
        )
        current = sum(path.stat().st_size for path in files)
        removed: list[Path] = []
        for path in files:
            resolved = path.resolve()
            if current <= max_bytes:
                break
            if resolved in pinned_resolved or self.root not in resolved.parents:
                continue
            relative = resolved.relative_to(self.root)
            # Delete only named derivatives, never arbitrary files under a user-chosen root.
            managed = bool(re.fullmatch(r"[0-9a-f]{64}\.[a-zA-Z0-9]+", relative.name))
            managed |= (
                len(relative.parts) == 3
                and relative.parts[0] == "audio"
                and bool(re.fullmatch(r"[0-9a-f]{64}", relative.parts[1]))
            )
            if not managed or path.is_symlink():
                continue
            size = path.stat().st_size
            path.unlink()
            current -= size
            removed.append(path)
        return removed

    def pins(self):
        path = self.root / "pins.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def pin(self, job_id: str, paths: list[Path]):
        pins = self.pins()
        relative = []
        for path in paths:
            resolved = path.resolve()
            if self.root not in resolved.parents:
                raise ValueError("cannot pin a path outside the derivative cache")
            relative.append(str(resolved.relative_to(self.root)))
        pins[job_id] = relative
        temporary = self.root / "pins.tmp"
        temporary.write_text(json.dumps(pins), encoding="utf-8")
        temporary.replace(self.root / "pins.json")

    def unpin(self, job_id: str):
        pins = self.pins()
        pins.pop(job_id, None)
        temporary = self.root / "pins.tmp"
        temporary.write_text(json.dumps(pins), encoding="utf-8")
        temporary.replace(self.root / "pins.json")
