from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from davincibot.models import (
    EditPlan,
    JobRecord,
    JobSpec,
    JobState,
    TemplateManifest,
    WorkspaceProfile,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS templates (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    spec TEXT NOT NULL,
    plan TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs(state, updated_at);
"""


class Database:
    """Small repository with serialized writes and versioned Pydantic payloads."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_profile(self, profile: WorkspaceProfile) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO profiles(id, mode, name, payload, updated_at)
                   VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, name=excluded.name,
                   payload=excluded.payload, updated_at=CURRENT_TIMESTAMP""",
                (profile.id, profile.mode.value, profile.name, profile.model_dump_json()),
            )

    def list_profiles(self) -> list[WorkspaceProfile]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM profiles ORDER BY mode, name"
            ).fetchall()
        return [WorkspaceProfile.model_validate_json(row["payload"]) for row in rows]

    def get_profile(self, profile_id: str) -> WorkspaceProfile | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
        return WorkspaceProfile.model_validate_json(row["payload"]) if row else None

    def save_template(self, template: TemplateManifest) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO templates(id, version, name, payload)
                   VALUES(?, ?, ?, ?)""",
                (template.id, template.version, template.name, template.model_dump_json()),
            )

    def list_templates(self, latest_only: bool = True) -> list[TemplateManifest]:
        query = "SELECT payload FROM templates ORDER BY name, version DESC"
        if latest_only:
            query = """SELECT t.payload FROM templates t
                       JOIN (SELECT id, MAX(version) version FROM templates GROUP BY id) latest
                       ON t.id=latest.id AND t.version=latest.version ORDER BY t.name"""
        with self._lock:
            rows = self._connection.execute(query).fetchall()
        return [TemplateManifest.model_validate_json(row["payload"]) for row in rows]

    def get_template(self, template_id: str, version: int | None = None) -> TemplateManifest | None:
        if version is None:
            sql = "SELECT payload FROM templates WHERE id=? ORDER BY version DESC LIMIT 1"
            values: tuple[object, ...] = (template_id,)
        else:
            sql = "SELECT payload FROM templates WHERE id=? AND version=?"
            values = (template_id, version)
        with self._lock:
            row = self._connection.execute(sql, values).fetchone()
        return TemplateManifest.model_validate_json(row["payload"]) if row else None

    def create_job(self, spec: JobSpec) -> JobRecord:
        record = JobRecord(id=spec.id, state=JobState.DRAFT, spec=spec)
        self.save_job(record)
        return record

    def save_job(self, record: JobRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO jobs(id, state, spec, plan, error, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET state=excluded.state, spec=excluded.spec,
                   plan=excluded.plan, error=excluded.error, updated_at=excluded.updated_at""",
                (
                    record.id,
                    record.state.value,
                    record.spec.model_dump_json(),
                    record.plan.model_dump_json() if record.plan else None,
                    record.error,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._record(row) if row else None

    def list_jobs(self, states: Iterable[JobState] | None = None) -> list[JobRecord]:
        params: tuple[str, ...] = ()
        sql = "SELECT * FROM jobs"
        if states:
            params = tuple(state.value for state in states)
            sql += f" WHERE state IN ({','.join('?' for _ in params)})"
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._record(row) for row in rows]

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value)
            )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            state=JobState(row["state"]),
            spec=JobSpec.model_validate_json(row["spec"]),
            plan=EditPlan.model_validate_json(row["plan"]) if row["plan"] else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
