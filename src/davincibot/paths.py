from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    database: Path
    bridge: Path
    cache: Path
    logs: Path

    @classmethod
    def default(cls) -> AppPaths:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        root = (base / "DaVinciBot").resolve()
        return cls(
            root=root,
            database=root / "davincibot.sqlite3",
            bridge=root / "bridge",
            cache=root / "cache",
            logs=root / "logs",
        )

    def ensure(self) -> None:
        for path in (self.root, self.bridge, self.cache, self.logs):
            path.mkdir(parents=True, exist_ok=True)
