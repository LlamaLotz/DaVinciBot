from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime

from davincibot.cache import ManagedCache
from davincibot.database import Database
from davincibot.editing.audio import prepare_audio
from davincibot.models import EditPlan, JobRecord, JobSpec, JobState, TemplateManifest
from davincibot.paths import AppPaths
from davincibot.preflight import run_preflight
from davincibot.resolve.bridge import FileResolveBridge, ResolveBridge
from davincibot.templates import configured_template

ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.DRAFT: {
        JobState.PREFLIGHT,
        JobState.CANCELLED,
        JobState.FAILED,
        JobState.NEEDS_ATTENTION,
    },
    JobState.PREFLIGHT: {JobState.QUEUED, JobState.NEEDS_ATTENTION, JobState.CANCELLED},
    JobState.NEEDS_ATTENTION: {JobState.PREFLIGHT, JobState.CANCELLED},
    JobState.QUEUED: {JobState.ANALYZING, JobState.CANCELLED},
    JobState.ANALYZING: {
        JobState.BUILDING,
        JobState.AWAITING_RESOLVE,
        JobState.FAILED,
        JobState.CANCELLED,
    },
    JobState.AWAITING_RESOLVE: {
        JobState.READY,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.NEEDS_ATTENTION,
    },
    JobState.BUILDING: {JobState.READY, JobState.FAILED},
    JobState.READY: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
}


class InvalidJobTransition(RuntimeError):
    pass


class JobService:
    """Persists jobs and serializes all Resolve mutations."""

    def __init__(self, database: Database, paths: AppPaths | None = None):
        self.database = database
        self._resolve_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="resolve")
        self._lock = threading.RLock()
        self.paths = paths or AppPaths.default()
        self._file_bridges = {}
        interrupted = database.list_jobs([JobState.QUEUED, JobState.ANALYZING, JobState.BUILDING])
        if interrupted:
            recovery_bridge = FileResolveBridge(self.paths.bridge)
            for record in interrupted:
                record.state = JobState.NEEDS_ATTENTION
                record.error = (
                    "Interrupted job. Inspect Resolve before regenerating; "
                    "not automatically replayed."
                )
                database.save_job(record)
                pending = recovery_bridge.pending / f"{record.id}.json"
                if pending.exists():
                    pending.replace(recovery_bridge.root / "archive" / pending.name)
                status = recovery_bridge.status / f"{record.id}.json"
                if not status.exists():
                    from davincibot.resolve import runtime

                    runtime.atomic_json(
                        status,
                        {
                            "state": "needs_attention",
                            "error": "Desktop interrupted before Resolve could safely finish",
                        },
                    )

    def create(self, spec: JobSpec) -> JobRecord:
        return self.database.create_job(spec)

    def attach_plan(self, job_id: str, plan: EditPlan) -> JobRecord:
        record = self._required(job_id)
        if record.state not in {JobState.DRAFT, JobState.PREFLIGHT, JobState.NEEDS_ATTENTION}:
            raise InvalidJobTransition("cannot replace an approved plan")
        if plan.job_id != job_id:
            raise ValueError("plan belongs to a different job")
        record.plan = plan
        record.state = JobState.PREFLIGHT
        record.updated_at = datetime.now(UTC)
        self.database.save_job(record)
        return record

    def transition(self, job_id: str, state: JobState, error: str | None = None) -> JobRecord:
        with self._lock:
            record = self._required(job_id)
            if state not in ALLOWED_TRANSITIONS[record.state]:
                raise InvalidJobTransition(f"cannot move {record.state.value} to {state.value}")
            record.state = state
            record.error = error
            record.updated_at = datetime.now(UTC)
            self.database.save_job(record)
            return record

    def approve_and_build(
        self,
        job_id: str,
        bridge: ResolveBridge,
        template: TemplateManifest,
        callback: Callable[[JobRecord], None] | None = None,
    ) -> Future[JobRecord]:
        with self._lock:
            record = self._required(job_id)
            profile = record.spec.profile_snapshot or self.database.get_profile(
                record.spec.profile_id
            )
            if not record.plan or not profile:
                raise ValueError("job requires a plan and workspace profile")
            template = configured_template(template, record.spec.overrides.get("parameters", {}))
            report = run_preflight(record.spec, record.plan, profile, template)
            if not report.ready:
                raise ValueError("Preflight failed: " + "; ".join(report.errors))
            record.spec.profile_snapshot = profile.model_copy(deep=True)
            record.spec.template_snapshot = template.model_copy(deep=True)
            self.database.save_job(record)
            record = self.transition(job_id, JobState.QUEUED)
            if isinstance(bridge, FileResolveBridge):
                self._file_bridges[job_id] = bridge

        def work() -> JobRecord:
            with self._lock:
                if self._required(record.id).state is JobState.CANCELLED:
                    return self._required(record.id)
                current = self.transition(record.id, JobState.ANALYZING)
            try:
                if not current.plan:
                    raise RuntimeError("job has no approved edit plan")
                current.spec.prepared_audio = prepare_audio(
                    current.spec,
                    current.plan,
                    self.paths.cache,
                    lambda: self._required(current.id).state is JobState.CANCELLED,
                )
                if current.spec.prepared_audio:
                    ManagedCache(self.paths.cache).pin(current.id, [current.spec.prepared_audio])
                with self._lock:
                    if self._required(current.id).state is JobState.CANCELLED:
                        return self._required(current.id)
                    self.database.save_job(current)
                    if isinstance(bridge, FileResolveBridge):
                        bridge.build(current.spec, current.plan, template)
                        finished = self.transition(current.id, JobState.AWAITING_RESOLVE)
                    else:
                        self.transition(current.id, JobState.BUILDING)
                if not isinstance(bridge, FileResolveBridge):
                    bridge.build(current.spec, current.plan, template)
                    finished = self.transition(current.id, JobState.READY)
            except Exception as error:
                latest = self._required(current.id)
                if latest.state in {JobState.ANALYZING, JobState.BUILDING}:
                    finished = self.transition(current.id, JobState.FAILED, str(error))
                else:
                    finished = latest
            if callback:
                callback(finished)
            return finished

        return self._resolve_executor.submit(work)

    def sync_file_bridge(self, bridge: FileResolveBridge) -> list[JobRecord]:
        updated: list[JobRecord] = []
        for record in self.database.list_jobs([JobState.AWAITING_RESOLVE]):
            status = bridge.read_status(record.id)
            if not status:
                continue
            if status.get("state") == "ready":
                updated.append(self.transition(record.id, JobState.READY))
            elif status.get("state") == "failed":
                updated.append(
                    self.transition(
                        record.id,
                        JobState.FAILED,
                        status.get("error", "Resolve failed"),
                    )
                )
            elif status.get("state") == "cancelled":
                updated.append(self.transition(record.id, JobState.CANCELLED))
            elif status.get("state") == "needs_attention":
                updated.append(
                    self.transition(record.id, JobState.NEEDS_ATTENTION, status.get("error"))
                )
        return updated

    def cancel(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self._required(job_id)
            if record.state in {JobState.BUILDING, JobState.READY, JobState.FAILED}:
                raise InvalidJobTransition("a started Resolve mutation cannot be cancelled")
            if record.state is JobState.AWAITING_RESOLVE:
                bridge = self._file_bridges.get(job_id) or FileResolveBridge(self.paths.bridge)
                bridge.cancel(job_id)
            return self.transition(job_id, JobState.CANCELLED)

    def shutdown(self):
        self._resolve_executor.shutdown(wait=True, cancel_futures=False)

    def _required(self, job_id: str) -> JobRecord:
        record = self.database.get_job(job_id)
        if not record:
            raise KeyError(f"unknown job: {job_id}")
        return record
