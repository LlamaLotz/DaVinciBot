from __future__ import annotations

import os
import shutil
from pathlib import Path

LAUNCHER = '''"""DaVinciBot Resolve Free bridge launcher."""
import os
import runpy
from pathlib import Path
import DaVinciResolveScript
runtime = runpy.run_path(str(Path(__file__).with_name("davincibot_runtime.py")))
base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
result = runtime["run_pending"](
    DaVinciResolveScript.scriptapp("Resolve"), base / "DaVinciBot" / "bridge"
)
print(result)
'''


def launcher_destination() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return (
        appdata
        / "Blackmagic Design"
        / "DaVinci Resolve"
        / "Support"
        / "Fusion"
        / "Scripts"
        / "Utility"
        / "DaVinciBot"
        / "Build Pending Job.py"
    )


def install_launcher(destination: Path | None = None) -> Path:
    from davincibot.resolve import runtime

    target = (destination or launcher_destination()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path(runtime.__file__).with_name("runtime.py")
    shutil.copyfile(source, target.with_name("davincibot_runtime.py"))
    target.write_text(LAUNCHER, encoding="utf-8")
    return target
