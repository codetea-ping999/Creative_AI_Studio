"""SQLite repository for persisted job records."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from core.jobs.schemas import JobRecord
from core.schemas import GenerationRequest, GenerationResult

_UNSET = object()


class JobRepository:
    """Persist and retrieve job records from SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(self, job: JobRecord) -> JobRecord:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id,
                    project_id,
                    media_type,
                    status,
                    request_json,
                    result_json,
                    progress,
                    error_message,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.project_id,
                    job.media_type,
                    job.status,
                    self._serialize_payload(job.request),
                    self._serialize_payload(job.result),
                    job.progress,
                    job.error_message,
                    self._normalize_timestamp(job.created_at),
                    self._normalize_timestamp(job.updated_at),
                ),
            )

        created_job = self.get(job.id)
        if created_job is None:
            raise RuntimeError(f"Job {job.id!r} was not persisted.")
        return created_job

    def get(self, job_id: str) -> JobRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    project_id,
                    media_type,
                    status,
                    request_json,
                    result_json,
                    progress,
                    error_message,
                    created_at,
                    updated_at
                FROM jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()

        if row is None:
            return None
        return self._row_to_record(row)

    def list(self) -> list[JobRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    project_id,
                    media_type,
                    status,
                    request_json,
                    result_json,
                    progress,
                    error_message,
                    created_at,
                    updated_at
                FROM jobs
                ORDER BY created_at DESC
                """
            ).fetchall()

        return [self._row_to_record(row) for row in rows]

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: float | None = None,
        project_id: str | None | object = _UNSET,
        result: GenerationResult | None | object = _UNSET,
        error_message: str | None | object = _UNSET,
    ) -> JobRecord | None:
        assignments: list[str] = []
        parameters: list[Any] = []

        if status is not None:
            assignments.append("status = ?")
            parameters.append(status)

        if progress is not None:
            assignments.append("progress = ?")
            parameters.append(progress)

        if project_id is not _UNSET:
            assignments.append("project_id = ?")
            parameters.append(project_id)

        if result is not _UNSET:
            assignments.append("result_json = ?")
            parameters.append(self._serialize_payload(result))

        if error_message is not _UNSET:
            assignments.append("error_message = ?")
            parameters.append(error_message)

        if not assignments:
            return self.get(job_id)

        assignments.append("updated_at = ?")
        parameters.append(self._normalize_timestamp(None))
        parameters.append(job_id)

        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )

        if cursor.rowcount == 0:
            return None
        return self.get(job_id)

    def update_status(
        self,
        job_id: str,
        status: str,
        *,
        progress: float | None = None,
    ) -> JobRecord | None:
        return self.update(job_id, status=status, progress=progress)

    def update_result(
        self,
        job_id: str,
        result: GenerationResult,
    ) -> JobRecord | None:
        return self.update(job_id, result=result)

    def update_project(
        self,
        job_id: str,
        project_id: str | None,
    ) -> JobRecord | None:
        return self.update(job_id, project_id=project_id)

    def update_error(
        self,
        job_id: str,
        error_message: str | None,
    ) -> JobRecord | None:
        return self.update(job_id, error_message=error_message)

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    progress REAL NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(connection, "project_id", "TEXT")
            self._ensure_column(connection, "media_type", "TEXT NOT NULL DEFAULT 'image'")
            self._ensure_column(connection, "error_message", "TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        with closing(self._connect()) as connection, connection:
            yield connection

    def _row_to_record(self, row: sqlite3.Row) -> JobRecord:
        request_payload = self._deserialize_payload(row["request_json"]) or {}
        result_payload = self._deserialize_payload(row["result_json"])
        request = GenerationRequest.model_validate(request_payload)
        result = (
            GenerationResult.model_validate(result_payload)
            if result_payload is not None
            else None
        )
        return JobRecord(
            id=row["id"],
            project_id=row["project_id"],
            media_type=row["media_type"],
            status=row["status"],
            request=request,
            result=result,
            progress=row["progress"],
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        column_name: str,
        definition: str,
    ) -> None:
        columns = connection.execute("PRAGMA table_info(jobs)").fetchall()
        if any(column["name"] == column_name for column in columns):
            return
        connection.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {definition}")

    def _serialize_payload(self, payload: Any) -> str | None:
        if payload is None:
            return None
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)

    def _deserialize_payload(self, payload: str | None) -> Any:
        if payload is None:
            return None
        return json.loads(payload)

    def _normalize_timestamp(self, value: datetime | str | None) -> str:
        if value is None:
            return datetime.now(timezone.utc).isoformat()
        if isinstance(value, str):
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()

    def create_job(
        self,
        *,
        job_id: str,
        request: GenerationRequest,
        status: str,
        progress: float = 0.0,
        result: GenerationResult | None = None,
        created_at: datetime | str | None = None,
        updated_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        created_job = self.create(
            JobRecord(
                id=job_id,
                project_id=None,
                media_type=request.media_type,
                status=status,
                request=request,
                result=result,
                progress=progress,
                error_message=None,
                created_at=self._coerce_datetime(created_at),
                updated_at=self._coerce_datetime(updated_at or created_at),
            )
        )
        return created_job.model_dump(mode="json")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        return None if job is None else job.model_dump(mode="json")

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: float | None = None,
        result: GenerationResult | None | object = _UNSET,
    ) -> dict[str, Any] | None:
        job = self.update(job_id, status=status, progress=progress, result=result)
        return None if job is None else job.model_dump(mode="json")

    def update_job_status(self, job_id: str, status: str) -> dict[str, Any] | None:
        job = self.update_status(job_id, status)
        return None if job is None else job.model_dump(mode="json")

    def update_job_progress(self, job_id: str, progress: float) -> dict[str, Any] | None:
        job = self.update(job_id, progress=progress)
        return None if job is None else job.model_dump(mode="json")

    def update_job_result(
        self,
        job_id: str,
        result: GenerationResult,
    ) -> dict[str, Any] | None:
        job = self.update_result(job_id, result)
        return None if job is None else job.model_dump(mode="json")

    def _coerce_datetime(self, value: datetime | str | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
