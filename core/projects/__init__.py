"""Project management for grouping related generations and reusable assets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any
import uuid


@dataclass(slots=True)
class Project:
    """A named collection of related generations and reusable assets."""

    id: str
    name: str
    description: str = ""
    status: str = "active"
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    pinned_asset_ids: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    job_ids: list[str] | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()
        if self.job_ids is None:
            self.job_ids = []
        if self.tags is None:
            self.tags = []
        if self.metadata is None:
            self.metadata = {}
        if self.pinned_asset_ids is None:
            self.pinned_asset_ids = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "pinned_asset_ids": list(self.pinned_asset_ids),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "job_ids": list(self.job_ids),
        }


class ProjectRepository:
    """Persist and manage projects on disk."""

    def __init__(self, project_dir: str | Path = "data/projects") -> None:
        self.project_dir = Path(project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        description: str = "",
        *,
        status: str = "active",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Project:
        project = Project(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            status=status,
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            pinned_asset_ids=[],
            created_at=datetime.now(),
            updated_at=datetime.now(),
            job_ids=[],
        )
        self._save_project(project)
        return project

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        pinned_asset_ids: list[str] | None = None,
    ) -> Project | None:
        project = self.get(project_id)
        if project is None:
            return None

        changed = False
        if name is not None and name != project.name:
            project.name = name
            changed = True
        if description is not None and description != project.description:
            project.description = description
            changed = True
        if status is not None and status != project.status:
            project.status = status
            changed = True
        if tags is not None and list(tags) != project.tags:
            project.tags = list(tags)
            changed = True
        if metadata is not None and dict(metadata) != project.metadata:
            project.metadata = dict(metadata)
            changed = True
        if pinned_asset_ids is not None and list(pinned_asset_ids) != project.pinned_asset_ids:
            project.pinned_asset_ids = list(pinned_asset_ids)
            changed = True

        if changed:
            project.updated_at = datetime.now()
            self._save_project(project)
        return project

    def get(self, project_id: str) -> Project | None:
        project_file = self.project_dir / f"{project_id}.json"
        if not project_file.exists():
            return None
        return self._try_load_project(project_file)

    def list_all(
        self,
        *,
        query_text: str | None = None,
        status: str | None = None,
        tag: str | None = None,
    ) -> list[Project]:
        normalized_query = query_text.strip().lower() if query_text else None
        normalized_tag = tag.strip().lower() if tag else None
        projects: list[Project] = []
        for project_file in sorted(self.project_dir.glob("*.json")):
            project = self._try_load_project(project_file)
            if project is None:
                continue
            if status and project.status != status:
                continue
            if normalized_tag and normalized_tag not in {entry.lower() for entry in project.tags}:
                continue
            if normalized_query:
                haystack = " ".join(
                    [
                        project.name,
                        project.description,
                        " ".join(project.tags),
                        json.dumps(project.metadata, ensure_ascii=True, sort_keys=True),
                    ]
                ).lower()
                if normalized_query not in haystack:
                    continue
            projects.append(project)

        projects.sort(key=lambda entry: entry.updated_at, reverse=True)
        return projects

    def add_job(self, project_id: str, job_id: str) -> Project | None:
        project = self.get(project_id)
        if project is None:
            return None

        if job_id not in project.job_ids:
            project.job_ids.append(job_id)
            project.updated_at = datetime.now()
            self._save_project(project)

        return project

    def remove_job(self, project_id: str, job_id: str) -> Project | None:
        project = self.get(project_id)
        if project is None:
            return None

        if job_id not in project.job_ids:
            return None

        project.job_ids.remove(job_id)
        project.updated_at = datetime.now()
        self._save_project(project)

        return project

    def delete(self, project_id: str) -> bool:
        project_file = self.project_dir / f"{project_id}.json"
        if project_file.exists():
            project_file.unlink()
            return True
        return False

    def _load_project(self, project_file: Path) -> Project:
        data = json.loads(project_file.read_text(encoding="utf-8"))
        return Project(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            status=data.get("status", "active"),
            tags=list(data.get("tags", [])),
            metadata=dict(data.get("metadata", {})),
            pinned_asset_ids=list(data.get("pinned_asset_ids", [])),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            job_ids=list(data.get("job_ids", [])),
        )

    def _try_load_project(self, project_file: Path) -> Project | None:
        try:
            return self._load_project(project_file)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save_project(self, project: Project) -> None:
        project_file = self.project_dir / f"{project.id}.json"
        project_file.write_text(
            json.dumps(project.to_dict(), ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )


__all__ = ["Project", "ProjectRepository"]
