"""Entry point copied into Resolve's Workspace Scripts directory."""

from __future__ import annotations

import os
from pathlib import Path

from davincibot.resolve.bridge import run_pending_in_resolve


def main() -> None:
    import DaVinciResolveScript as dvr_script

    resolve = dvr_script.scriptapp("Resolve")
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    result = run_pending_in_resolve(resolve, base / "DaVinciBot" / "bridge")
    print(result.get("message") or result)


if __name__ == "__main__":
    main()
