from __future__ import annotations

import os
from dataclasses import dataclass

from davincibot.models import ResourceMode


@dataclass(frozen=True)
class ResourceLimits:
    workers: int
    ffmpeg_threads: int
    max_parallel_probes: int
    process_priority: str


def limits_for(mode: ResourceMode, logical_processors: int | None = None) -> ResourceLimits:
    count = max(1, logical_processors or os.cpu_count() or 1)
    if mode is ResourceMode.ECO:
        workers = max(1, count // 4)
        return ResourceLimits(workers, workers, max(1, workers), "below_normal")
    if mode is ResourceMode.TURBO:
        workers = max(1, count - 2) if count > 2 else 1
        return ResourceLimits(workers, workers, min(8, workers), "normal")
    workers = max(1, min(6, count // 2))
    return ResourceLimits(workers, workers, min(4, workers), "below_normal")
