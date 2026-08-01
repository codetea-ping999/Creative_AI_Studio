"""Persistence for batch records."""

from __future__ import annotations

import json
from pathlib import Path

from core.storage.json_files import utc_now, write_json_atomic

from .schemas import BatchRecord


class BatchRepository:
    """Persist and manage batch records on disk."""

    def __init__(self, batch_dir: str | Path = "data/batches") -> None:
        self.batch_dir = Path(batch_dir)
        self.batch_dir.mkdir(parents=True, exist_ok=True)

    def create(self, record: BatchRecord) -> BatchRecord:
        self._save(record)
        return record

    def get(self, batch_id: str) -> BatchRecord | None:
        batch_file = self.batch_dir / f"{batch_id}.json"
        if not batch_file.exists():
            return None
        return self._try_load(batch_file)

    def save(self, record: BatchRecord) -> BatchRecord:
        updated = record.model_copy(update={"updated_at": utc_now()})
        self._save(updated)
        return updated

    def list_all(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[BatchRecord]:
        records: list[BatchRecord] = []
        for batch_file in sorted(self.batch_dir.glob("*.json")):
            record = self._try_load(batch_file)
            if record is None:
                continue
            if project_id and record.spec.project_id != project_id:
                continue
            records.append(record)

        records.sort(key=lambda entry: entry.updated_at, reverse=True)
        if limit is not None:
            return records[:limit]
        return records

    def find_by_job_id(self, job_id: str) -> BatchRecord | None:
        """Return the batch owning a job, or None.

        Scans the directory because a batch is small and there are few of them;
        an index would add a second thing to keep consistent for no measured win.
        """

        for record in self.list_all():
            if any(item.job_id == job_id for item in record.items):
                return record
        return None

    def delete(self, batch_id: str) -> bool:
        batch_file = self.batch_dir / f"{batch_id}.json"
        if batch_file.exists():
            batch_file.unlink()
            return True
        return False

    def _save(self, record: BatchRecord) -> None:
        write_json_atomic(
            self.batch_dir / f"{record.id}.json",
            record.model_dump(mode="json"),
        )

    def _try_load(self, batch_file: Path) -> BatchRecord | None:
        try:
            return BatchRecord.model_validate_json(
                batch_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return None


__all__ = ["BatchRepository"]
