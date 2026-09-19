from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

RESOLVE_EXE = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe")
RESOLVE_API = Path(r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting")


def system_diagnostics() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "resolve_installed": RESOLVE_EXE.exists(),
        "resolve_version": _file_version(RESOLVE_EXE),
        "resolve_scripting_api": RESOLVE_API.exists(),
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "logical_processors": __import__("os").cpu_count(),
    }


def _file_version(path: Path) -> str | None:
    if not path.exists() or platform.system() != "Windows":
        return None
    try:
        import win32api  # type: ignore[import-not-found]

        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (ImportError, OSError, KeyError):
        return None
