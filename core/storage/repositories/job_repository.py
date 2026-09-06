"""SQLite repository for persisted job records."""

from __future__ import annotations

import builtins
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from core.jobs.schemas import JobRecord
from core.jobs.statuses import JOB_STATUSES, TERMINAL_JOB_STATUSES, is_valid_transition
from core.schemas import GenerationRequest, GenerationResult, GenerationStatus

_UNSET = object()

# Shared column list for every full-row SELECT (get/list/list_tolerant/
# list_terminal_pending_completion) so adding a column only means editing
# this one place plus `_row_to_record()`.
_SELECT_COLUMNS = (
    "id, project_id, media_type, status, request_json, result_json, "
    "progress, error_message, completion_state, completion_error, "
    "created_at, updated_at"
)


class JobRecordDecodeError(Exception):
    """A job row could not be deterministically reconstructed into a `JobRecord`.

    Raised by `_row_to_record()` when the row's own stored bytes -- not the
    read operation itself -- are the problem, at any stage of
    reconstruction: malformed JSON (`json.JSONDecodeError`) or a schema the
    current `GenerationRequest`/`GenerationResult` models no longer accept
    (`pydantic.ValidationError`) in the request/result payload; JSON nested
    deep enough to overflow the decoder's C stack (`RecursionError`); a
    malformed `created_at`/`updated_at` timestamp, whether a bad ISO string
    (`datetime.fromisoformat` raising `ValueError`) or a non-`str` value --
    SQLite tolerates a BLOB in a TEXT-affinity column, so a raw byte string
    there raises `TypeError` instead; or the row as a whole failing
    `JobRecord`'s own validation (an invalid persisted `status`/`media_type`
    literal, a `progress` outside `[0.0, 1.0]`, etc. -- also
    `pydantic.ValidationError`). Every one of these is deterministic: the
    same persisted bytes fail identically on every future read of this row,
    unlike a `sqlite3.Error` (locked, busy, disk I/O), which says nothing
    about the row's content. A caller (see `core.jobs.runner.JobRunner`)
    uses this type, specifically, to tell "retrying can never help" apart
    from "the database itself had a transient problem."
    """


class JobRepository:
    """Persist and retrieve job records from SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def data_directory(self) -> Path:
        """Return the directory containing this repository's SQLite data."""

        return self._db_path.resolve().parent

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
                    completion_state,
                    completion_error,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    job.completion_state,
                    job.completion_error,
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
                f"""
                SELECT {_SELECT_COLUMNS}
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
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM jobs
                ORDER BY created_at DESC
                """
            ).fetchall()

        return [self._row_to_record(row) for row in rows]

    def list_tolerant(
        self,
    ) -> tuple[
        "builtins.list[JobRecord]", "builtins.list[tuple[str, JobRecordDecodeError]]"
    ]:
        """Row-by-row read of every job, tolerating a per-row decode failure.

        Annotated via ``builtins.list`` (not the bare ``list[...]``
        generic): this class already defines a method named ``list()``,
        which shadows the builtin type of the same name for any annotation
        appearing later in this class body under
        ``from __future__ import annotations``.

        Unlike `list()`, a single poison row (see `JobRecordDecodeError`)
        never aborts the whole read -- it is reported alongside its `id`
        instead, so a caller (startup recovery, Story replay candidate
        selection) can still see and process every other row. A `sqlite3`-
        level failure fetching the rows in the first place still propagates
        normally: that is a transient, whole-scan problem, not a per-row
        content one, and must not be silently treated as "scan complete."
        Only for recovery/best-effort scanning; ordinary reads should keep
        using `get()`/`list()`.
        """

        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM jobs
                ORDER BY created_at DESC
                """
            ).fetchall()

        records: list[JobRecord] = []
        failures: list[tuple[str, JobRecordDecodeError]] = []
        for row in rows:
            try:
                records.append(self._row_to_record(row))
            except JobRecordDecodeError as exc:
                failures.append((row["id"], exc))
        return records, failures

    def list_terminal_pending_completion(self) -> "builtins.list[JobRecord]":
        """Terminal jobs whose completion convergence has not committed yet.

        A narrow, indexed-by-column filter (`completion_state = 'pending'`
        AND a terminal `status`) for the runtime/retry convergence loop, so
        it does not need to decode the whole table on every pass. A poison
        row here is silently skipped (not reported) -- it is
        `list_tolerant()`'s job, via startup recovery, to quarantine those;
        this method is read-only best-effort convergence input.
        """

        placeholders = ", ".join("?" for _ in TERMINAL_JOB_STATUSES)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM jobs
                WHERE completion_state = 'pending'
                  AND status IN ({placeholders})
                ORDER BY created_at ASC
                """,
                tuple(TERMINAL_JOB_STATUSES),
            ).fetchall()

        records: list[JobRecord] = []
        for row in rows:
            try:
                records.append(self._row_to_record(row))
            except JobRecordDecodeError:
                continue
        return records

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
        return self._update(
            job_id,
            status=status,
            progress=progress,
            project_id=project_id,
            result=result,
            error_message=error_message,
        )

    def update_if_status(
        self,
        job_id: str,
        expected_statuses: tuple[str, ...],
        *,
        status: str | None = None,
        progress: float | None = None,
        project_id: str | None | object = _UNSET,
        result: GenerationResult | None | object = _UNSET,
        error_message: str | None | object = _UNSET,
    ) -> JobRecord | None:
        """Apply an update only while the persisted status is still expected."""

        if not expected_statuses:
            return None
        return self._update(
            job_id,
            status=status,
            progress=progress,
            project_id=project_id,
            result=result,
            error_message=error_message,
            expected_statuses=expected_statuses,
        )

    def transition_if_status(
        self,
        job_id: str,
        expected_statuses: tuple[str, ...],
        *,
        status: GenerationStatus,
        progress: float | None = None,
        error_message: str | None | object = _UNSET,
    ) -> bool:
        """Atomically transition a job and return only whether the CAS won.

        Unlike ``update_if_status()``, this primitive deliberately does not
        reread the job after its UPDATE commits.  Callers that establish
        execution ownership need that boolean before any fallible follow-up
        read can occur.

        ``error_message`` (optional, `_UNSET` by default so ordinary claim/
        transition callers leave the column untouched) lets a caller
        quarantining an unreadable row -- see
        `core.jobs.runner.JobRunner._quarantine_poison_job` -- persist the
        failure reason in the same atomic UPDATE, without ever needing to
        read the row back first. Recording *why* a job failed only via a
        transient event publish is not enough: it must survive in the
        database exactly like `JobService.mark_failed()`'s own
        ``error_message`` does.
        """

        if not expected_statuses or status not in JOB_STATUSES:
            return False

        transition_sources = tuple(
            current
            for current in JOB_STATUSES
            if is_valid_transition(current, status)
        )
        assignments = ["status = ?"]
        parameters: list[Any] = [status]
        if progress is not None:
            assignments.append("progress = ?")
            parameters.append(progress)
        if error_message is not _UNSET:
            assignments.append("error_message = ?")
            parameters.append(error_message)
        assignments.append("updated_at = ?")
        parameters.append(self._normalize_timestamp(None))
        parameters.append(job_id)
        expected_placeholders = ", ".join("?" for _ in expected_statuses)
        transition_placeholders = ", ".join("?" for _ in transition_sources)
        parameters.extend(expected_statuses)
        parameters.extend(transition_sources)

        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET {', '.join(assignments)}
                WHERE id = ?
                  AND status IN ({expected_placeholders})
                  AND status IN ({transition_placeholders})
                """,
                parameters,
            )
        return cursor.rowcount > 0

    def get_raw_status(self, job_id: str) -> str | None:
        """Return `job_id`'s persisted `status` column, or `None` if missing.

        A narrow, single-column read that never touches `request_json`/
        `result_json`/timestamps and never goes through `_row_to_record()`,
        so it cannot itself raise `JobRecordDecodeError` -- it is exactly
        what a caller needs to tell a *structurally invalid* raw status
        (outside `JOB_STATUSES` entirely) apart from a valid one that
        simply is not the status a CAS expected. Not a general escape
        hatch: it reads one column for one job id and nothing else.
        """

        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return None if row is None else row["status"]

    def quarantine_structurally_invalid_status(
        self,
        job_id: str,
        *,
        error_message: str,
    ) -> bool:
        """Terminalize a row whose persisted `status` is outside `JOB_STATUSES`.

        A structurally invalid raw status (never written by this
        codebase -- external corruption, a truncated write, a downgraded
        schema) can never equal any expected status in an ordinary CAS, so
        `transition_if_status()` always reports it as a miss; treating that
        miss as "already resolved by something else" would discard the
        queue entry while leaving the row stuck in that invalid status
        forever -- a lost job (Codex exact-HEAD review). This is a
        deliberately narrow escape hatch, not a general "rewrite any
        status" helper:

        - Scoped to exactly one `job_id`.
        - The UPDATE's own WHERE clause re-checks, transactionally, that the
          *current* raw status is NOT one of `JOB_STATUSES` -- so this can
          never match, and can never overwrite, a row already in any valid
          status (including a valid terminal one). It is not a bypass of
          the execution-claim or lifecycle-transition contract; it only
          ever fires for a status that contract was never defined for.
        - Always sets `status="failed"`, `progress=1.0`, and
          `error_message` together in the one atomic UPDATE -- there is no
          reread step for a caller to lose the reason on, matching
          `transition_if_status()`'s own no-reread design.

        Returns whether this call's UPDATE actually matched a row -- a
        `False` here means the row was no longer in that invalid status by
        the time this ran (someone else already resolved or repaired it),
        not that this call failed.
        """

        placeholders = ", ".join("?" for _ in JOB_STATUSES)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET status = ?, progress = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                  AND status NOT IN ({placeholders})
                """,
                (
                    "failed",
                    1.0,
                    error_message,
                    self._normalize_timestamp(None),
                    job_id,
                    *JOB_STATUSES,
                ),
            )
        return cursor.rowcount > 0

    def mark_completion_done(self, job_id: str) -> bool:
        """Record that `job_id`'s post-terminal convergence fully applied.

        Only ever flips `completion_state`/`completion_error`; never
        touches `status`/`result`/`error_message` -- convergence succeeding
        or failing must never look like the generation itself changed
        outcome. Scoped to a terminal `status` so this can never mark an
        active job "done" by mistake.
        """

        placeholders = ", ".join("?" for _ in TERMINAL_JOB_STATUSES)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET completion_state = 'done', completion_error = NULL,
                    updated_at = ?
                WHERE id = ?
                  AND status IN ({placeholders})
                """,
                (self._normalize_timestamp(None), job_id, *TERMINAL_JOB_STATUSES),
            )
        return cursor.rowcount > 0

    def mark_completion_pending_with_error(self, job_id: str, error_message: str) -> bool:
        """Record a retryable/ambiguous completion failure, staying `pending`.

        `completion_state` is left exactly as it already is ('pending' by
        construction for anything that has not converged yet) -- this only
        records *why* the last attempt did not reach 'done', so a later
        retry (or an operator) can see it via `JobRepository.get()` without
        needing to have been listening for an event at the time. Never
        touches `status`/`result`/`error_message`: a completion failure
        must never look like the generation itself failed.
        """

        placeholders = ", ".join("?" for _ in TERMINAL_JOB_STATUSES)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET completion_error = ?, updated_at = ?
                WHERE id = ?
                  AND status IN ({placeholders})
                  AND completion_state = 'pending'
                """,
                (error_message, self._normalize_timestamp(None), job_id, *TERMINAL_JOB_STATUSES),
            )
        return cursor.rowcount > 0

    def _update(
        self,
        job_id: str,
        *,
        status: str | None,
        progress: float | None,
        project_id: str | None | object,
        result: GenerationResult | None | object,
        error_message: str | None | object,
        expected_statuses: tuple[str, ...] | None = None,
    ) -> JobRecord | None:
        assignments: list[str] = []
        parameters: list[Any] = []
        transition_sources: tuple[str, ...] | None = None

        if status is not None:
            if status not in JOB_STATUSES:
                return None
            # A caller's stale read must never become execution authority.
            # Keep the lifecycle edge in this UPDATE's WHERE clause so every
            # public and legacy update path shares one SQLite-enforced contract.
            transition_sources = tuple(
                current
                for current in JOB_STATUSES
                if is_valid_transition(current, status)
            )
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
        where_clause = "id = ?"
        if expected_statuses is not None:
            placeholders = ", ".join("?" for _ in expected_statuses)
            where_clause += f" AND status IN ({placeholders})"
            parameters.extend(expected_statuses)
        if transition_sources is not None:
            placeholders = ", ".join("?" for _ in transition_sources)
            where_clause += f" AND status IN ({placeholders})"
            parameters.extend(transition_sources)

        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE {where_clause}",
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
            # PR3: completion convergence tracking, separate from the
            # generation-level `status` (see `JobRecord.completion_state`).
            # `ALTER TABLE ... ADD COLUMN ... DEFAULT 'pending'` backfills
            # every pre-existing row with 'pending' too -- the safe default
            # for a legacy row (see the schema docstring for why).
            self._ensure_column(connection, "completion_state", "TEXT NOT NULL DEFAULT 'pending'")
            self._ensure_column(connection, "completion_error", "TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        # The API request threads and the in-process job runner thread both
        # open short-lived connections to this database. WAL plus a busy
        # timeout lets concurrent readers/writers coexist instead of failing
        # with "database is locked".
        connection.execute("PRAGMA journal_mode=WAL;")
        connection.execute("PRAGMA busy_timeout=5000;")
        connection.execute("PRAGMA synchronous=NORMAL;")
        return connection

    @contextmanager
    def _connection(self):
        with closing(self._connect()) as connection, connection:
            yield connection

    def _row_to_record(self, row: sqlite3.Row) -> JobRecord:
        # The entire reconstruction -- payload JSON decode, request/result
        # model validation, timestamp parsing, and the final JobRecord
        # construction (itself a Pydantic validation of status/media_type/
        # progress/id together) -- runs on data already fetched into `row`.
        # None of it performs I/O or touches SQLite again, so wrapping the
        # whole method body in one try/except cannot ever catch a
        # sqlite3.Error: only a deterministic, content-caused failure can
        # originate here (Codex exact-HEAD review: a malformed
        # created_at/updated_at or a JobRecord-level validation failure
        # -- e.g. an invalid persisted status/progress -- was previously
        # unwrapped, past the payload-only boundary below, and would have
        # been misclassified as transient and requeued forever).
        try:
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
                completion_state=row["completion_state"],
                completion_error=row["completion_error"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        except (ValueError, RecursionError, TypeError) as exc:
            # ValueError covers json.JSONDecodeError, every
            # pydantic.ValidationError (request/result payload *and* the
            # final JobRecord construction), and datetime.fromisoformat's
            # ValueError on a malformed-but-string timestamp; RecursionError
            # covers JSON nested deep enough to overflow the decoder's C
            # stack; TypeError covers datetime.fromisoformat's own reaction
            # to a non-str value -- SQLite's type affinity is advisory, so a
            # BLOB can end up in created_at/updated_at despite the column's
            # TEXT affinity (Codex exact-HEAD review: this was the one
            # content-caused exception type this boundary missed). All of
            # these can only be caused by this row's own persisted values --
            # never by the read operation itself -- so they are normalized
            # into one boundary type a caller can classify as "never retry"
            # without having to know which underlying library, or which
            # field, raised it. Deliberately scoped to just these three
            # types: a `sqlite3.Error` (or any other exception) must keep
            # propagating unchanged, since it says nothing about this row's
            # content -- and this scoping is safe precisely because nothing
            # in this method's body performs I/O, so it can never coincide
            # with a database-level TypeError from somewhere else.
            raise JobRecordDecodeError(
                f"Job {row['id']!r}'s persisted row could not be "
                f"reconstructed: {exc}"
            ) from exc

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
        status: GenerationStatus,
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
