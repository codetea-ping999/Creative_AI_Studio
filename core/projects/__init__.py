"""Project management for grouping related generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import uuid


@dataclass(slots=True)
class Project:
    """A named collection of related generations."""

    id: str
    name: str
    description: str = ""
    created_at: datetime = None
    updated_at: datetime = None
    job_ids: list[str] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()
        if self.job_ids is None:
            self.job_ids = []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "job_ids": self.job_ids,
        }


class ProjectRepository:
    """Persist and manage projects on disk."""

    def __init__(self, project_dir: str | Path = "data/projects") -> None:
        self.project_dir = Path(project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)

    def create(self, name: str, description: str = "") -> Project:
        """Create a new project."""
        project = Project(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
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
    ) -> Project | None:
        """Update project metadata."""
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

        if changed:
            project.updated_at = datetime.now()
            self._save_project(project)
        return project

    def get(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        project_file = self.project_dir / f"{project_id}.json"
        if not project_file.exists():
            return None

        data = json.loads(project_file.read_text(encoding="utf-8"))
        return Project(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            job_ids=data.get("job_ids", []),
        )

    def list_all(self) -> list[Project]:
        """List all projects."""
        projects = []
        for project_file in sorted(self.project_dir.glob("*.json")):
            data = json.loads(project_file.read_text(encoding="utf-8"))
            project = Project(
                id=data["id"],
                name=data["name"],
                description=data.get("description", ""),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                job_ids=data.get("job_ids", []),
            )
            projects.append(project)

        # Sort by updated_at, newest first
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def add_job(self, project_id: str, job_id: str) -> Project | None:
        """Add a job to a project."""
        project = self.get(project_id)
        if project is None:
            return None

        if job_id not in project.job_ids:
            project.job_ids.append(job_id)
            project.updated_at = datetime.now()
            self._save_project(project)

        return project

    def remove_job(self, project_id: str, job_id: str) -> Project | None:
        """Remove a job from a project."""
        project = self.get(project_id)
        if project is None:
            return None

        if job_id in project.job_ids:
            project.job_ids.remove(job_id)
            project.updated_at = datetime.now()
            self._save_project(project)

        return project

    def delete(self, project_id: str) -> bool:
        """Delete a project."""
        project_file = self.project_dir / f"{project_id}.json"
        if project_file.exists():
            project_file.unlink()
            return True
        return False

    def _save_project(self, project: Project) -> None:
        """Persist project to disk."""
        project_file = self.project_dir / f"{project.id}.json"
        project_file.write_text(
            json.dumps(project.to_dict(), ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )


__all__ = ["Project", "ProjectRepository"]
