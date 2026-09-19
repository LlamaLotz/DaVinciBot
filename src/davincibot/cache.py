from __future__ import annotations

import hashlib
import json
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
        if not key.isalnum() or len(key) != 64:
            raise ValueError("invalid cache key")
        return self.root / key[:2] / f"{key}{suffix}"

    def usage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def cleanup(self, max_bytes: int, pinned: set[Path] | None = None) -> list[Path]:
        pinned_resolved = {path.resolve() for path in (pinned or set())}
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
            size = path.stat().st_size
            path.unlink()
            current -= size
            removed.append(path)
        return removed
