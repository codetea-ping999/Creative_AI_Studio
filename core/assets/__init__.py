"""Persistent asset registry for generated outputs and export/reuse lineage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from core.jobs import JobRecord


def _now() -> datetime:
    return datetime.now()


def _stable_asset_id(job_id: str, output_path: str) -> str:
    digest = hashlib.sha1(f"{job_id}:{output_path}".encode("utf-8")).hexdigest()[:24]
    return f"asset_{digest}"


@dataclass(slots=True)
class Asset:
    """A persistent representation of a reusable generated asset."""

    id: str
    job_id: str
    project_id: str | None
    media_type: str
    kind: str
    title: str
    prompt: str
    model_id: str
    path: str
    preview_path: str | None = None
    parent_asset_id: str | None = None
    lineage: list[str] | None = None
    export_paths: list[str] | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = _now()
        if self.updated_at is None:
            self.updated_at = _now()
        if self.lineage is None:
            self.lineage = []
        if self.export_paths is None:
            self.export_paths = []
        if self.tags is None:
            self.tags = []
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "project_id": self.project_id,
            "media_type": self.media_type,
            "kind": self.kind,
            "title": self.title,
            "prompt": self.prompt,
            "model_id": self.model_id,
            "path": self.path,
            "preview_path": self.preview_path,
            "parent_asset_id": self.parent_asset_id,
            "lineage": list(self.lineage),
            "export_paths": list(self.export_paths),
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class AssetRepository:
    """Persist and manage reusable generated assets on disk."""

    def __init__(self, asset_dir: str | Path = "data/assets") -> None:
        self.asset_dir = Path(asset_dir)
        self.asset_dir.mkdir(parents=True, exist_ok=True)

    def sync_job(self, job: JobRecord) -> list[Asset]:
        """Create or update asset records for a succeeded job."""

        if job.status != "succeeded" or job.result is None:
            return []

        assets: list[Asset] = []
        outputs = [output for output in job.result.outputs if output]
        if not outputs:
            return assets

        preview_path = next((preview for preview in job.result.previews if preview), None)
        for output_path in outputs:
            asset_id = _stable_asset_id(job.id, output_path)
            existing = self.get(asset_id)
            lineage = list(existing.lineage) if existing is not None else []
            export_paths = list(existing.export_paths) if existing is not None else []
            tags = list(existing.tags) if existing is not None else []
            metadata = dict(existing.metadata) if existing is not None else {}
            metadata.update(dict(job.result.metadata))

            request_params = (
                dict(job.request.params)
                if isinstance(job.request.params, dict)
                else {}
            )
            parent_asset_id = request_params.get("source_asset_id")
            if isinstance(parent_asset_id, str) and parent_asset_id:
                if parent_asset_id not in lineage:
                    lineage.append(parent_asset_id)
            else:
                parent_asset_id = existing.parent_asset_id if existing is not None else None

            metadata["reuse_count"] = int(metadata.get("reuse_count", 0))
            metadata["export_count"] = len(export_paths)

            asset = Asset(
                id=asset_id,
                job_id=job.id,
                project_id=job.project_id,
                media_type=job.media_type,
                kind="output",
                title=_summarize_prompt(job.request.prompt, fallback=f"{job.media_type} asset"),
                prompt=job.request.prompt,
                model_id=job.request.model_id,
                path=str(output_path),
                preview_path=preview_path or str(output_path),
                parent_asset_id=parent_asset_id,
                lineage=lineage,
                export_paths=export_paths,
                tags=tags,
                metadata=metadata,
                created_at=existing.created_at if existing is not None else job.created_at,
                updated_at=existing.updated_at if existing is not None else _now(),
            )
            if existing is not None and self._asset_state(existing) == self._asset_state(asset):
                assets.append(existing)
                continue

            asset.updated_at = _now()
            self._save_asset(asset)
            assets.append(asset)
        return assets

    def sync_jobs(self, jobs: list[JobRecord]) -> list[Asset]:
        synced: list[Asset] = []
        for job in jobs:
            synced.extend(self.sync_job(job))
        return synced

    def create_or_update(self, asset: Asset) -> Asset:
        self._save_asset(asset)
        return asset

    def get(self, asset_id: str) -> Asset | None:
        asset_path = self.asset_dir / f"{asset_id}.json"
        if not asset_path.exists():
            return None
        return self._try_load_asset(asset_path)

    def get_by_job(self, job_id: str) -> list[Asset]:
        return [
            asset
            for asset in self.list_all()
            if asset.job_id == job_id
        ]

    def get_primary_by_job(self, job_id: str) -> Asset | None:
        assets = self.get_by_job(job_id)
        return assets[0] if assets else None

    def list_all(
        self,
        *,
        media_type: str | None = None,
        project_id: str | None = None,
        query_text: str | None = None,
        limit: int | None = None,
    ) -> list[Asset]:
        normalized_query = query_text.strip().lower() if query_text else None
        assets: list[Asset] = []
        for asset_path in sorted(self.asset_dir.glob("*.json")):
            asset = self._try_load_asset(asset_path)
            if asset is None:
                continue
            if media_type and asset.media_type != media_type:
                continue
            if project_id and asset.project_id != project_id:
                continue
            if normalized_query:
                haystack = " ".join(
                    [
                        asset.title,
                        asset.prompt,
                        asset.model_id,
                        asset.path,
                        asset.project_id or "",
                        json.dumps(asset.metadata, ensure_ascii=True, sort_keys=True),
                    ]
                ).lower()
                if normalized_query not in haystack:
                    continue
            assets.append(asset)

        assets.sort(key=lambda item: item.updated_at or item.created_at or _now(), reverse=True)
        if limit is not None:
            return assets[:limit]
        return assets

    def bind_project(self, asset_id: str, project_id: str | None) -> Asset | None:
        asset = self.get(asset_id)
        if asset is None:
            return None
        if asset.project_id == project_id:
            return asset
        asset.project_id = project_id
        asset.updated_at = _now()
        self._save_asset(asset)
        return asset

    def bind_job_assets(self, job_id: str, project_id: str | None) -> list[Asset]:
        updated: list[Asset] = []
        for asset in self.get_by_job(job_id):
            if asset.project_id == project_id:
                updated.append(asset)
                continue
            asset.project_id = project_id
            asset.updated_at = _now()
            self._save_asset(asset)
            updated.append(asset)
        return updated

    def mark_reused(
        self,
        asset_id: str,
        *,
        action: str,
        derived_job_id: str | None = None,
    ) -> Asset | None:
        asset = self.get(asset_id)
        if asset is None:
            return None
        derived_job_ids = asset.metadata.get("derived_job_ids")
        if not isinstance(derived_job_ids, list):
            derived_job_ids = []
        if derived_job_id is not None and derived_job_id not in derived_job_ids:
            derived_job_ids.append(derived_job_id)
        asset.metadata["reuse_count"] = int(asset.metadata.get("reuse_count", 0)) + 1
        asset.metadata["last_reuse_action"] = action
        asset.metadata["derived_job_ids"] = derived_job_ids
        asset.updated_at = _now()
        self._save_asset(asset)
        return asset

    def record_export(self, asset_id: str, export_path: str) -> Asset | None:
        asset = self.get(asset_id)
        if asset is None:
            return None
        changed = False
        if export_path not in asset.export_paths:
            asset.export_paths.append(export_path)
            changed = True
        export_count = len(asset.export_paths)
        if asset.metadata.get("export_count") != export_count:
            asset.metadata["export_count"] = export_count
            changed = True
        if asset.metadata.get("last_export_path") != export_path:
            asset.metadata["last_export_path"] = export_path
            changed = True
        if not changed:
            return asset
        asset.updated_at = _now()
        self._save_asset(asset)
        return asset

    def export_asset(
        self,
        asset_id: str,
        *,
        export_root: str | Path,
        destination_name: str | None = None,
        include_metadata: bool = True,
    ) -> dict[str, str]:
        asset = self.get(asset_id)
        if asset is None:
            raise LookupError(f"Unknown asset: {asset_id}")

        source_path = Path(asset.path)
        if not source_path.exists():
            raise FileNotFoundError(f"Asset file does not exist: {source_path}")

        export_root_path = Path(export_root)
        export_root_path.mkdir(parents=True, exist_ok=True)
        export_name = destination_name or source_path.name
        export_path = export_root_path / export_name
        shutil.copy2(source_path, export_path)

        metadata_path = ""
        if include_metadata:
            metadata_path = str(export_root_path / f"{export_path.stem}.metadata.json")
            Path(metadata_path).write_text(
                json.dumps(asset.to_dict(), ensure_ascii=True, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        self.record_export(asset_id, str(export_path))
        return {
            "asset_id": asset.id,
            "export_path": str(export_path),
            "metadata_path": metadata_path,
        }

    def export_project_bundle(
        self,
        *,
        project_id: str,
        export_root: str | Path,
        assets: list[Asset],
        project_manifest: dict[str, Any],
    ) -> dict[str, str]:
        bundle_root = Path(export_root)
        bundle_root.mkdir(parents=True, exist_ok=True)
        manifest_path = bundle_root / "project.manifest.json"
        manifest_assets: list[dict[str, Any]] = []

        for asset in assets:
            exported = self.export_asset(
                asset.id,
                export_root=bundle_root / asset.media_type,
                include_metadata=False,
            )
            manifest_assets.append(
                {
                    "asset_id": asset.id,
                    "job_id": asset.job_id,
                    "media_type": asset.media_type,
                    "export_path": exported["export_path"],
                }
            )

        payload = {
            "project": project_manifest,
            "assets": manifest_assets,
        }
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "project_id": project_id,
            "bundle_root": str(bundle_root),
            "manifest_path": str(manifest_path),
        }

    def _load_asset(self, asset_path: Path) -> Asset:
        data = json.loads(asset_path.read_text(encoding="utf-8"))
        return Asset(
            id=data["id"],
            job_id=data["job_id"],
            project_id=data.get("project_id"),
            media_type=data["media_type"],
            kind=data.get("kind", "output"),
            title=data.get("title", ""),
            prompt=data.get("prompt", ""),
            model_id=data.get("model_id", ""),
            path=data["path"],
            preview_path=data.get("preview_path"),
            parent_asset_id=data.get("parent_asset_id"),
            lineage=list(data.get("lineage", [])),
            export_paths=list(data.get("export_paths", [])),
            tags=list(data.get("tags", [])),
            metadata=dict(data.get("metadata", {})),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    def _try_load_asset(self, asset_path: Path) -> Asset | None:
        try:
            return self._load_asset(asset_path)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save_asset(self, asset: Asset) -> None:
        asset_path = self.asset_dir / f"{asset.id}.json"
        asset_path.write_text(
            json.dumps(asset.to_dict(), ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _asset_state(self, asset: Asset) -> dict[str, Any]:
        return {
            "id": asset.id,
            "job_id": asset.job_id,
            "project_id": asset.project_id,
            "media_type": asset.media_type,
            "kind": asset.kind,
            "title": asset.title,
            "prompt": asset.prompt,
            "model_id": asset.model_id,
            "path": asset.path,
            "preview_path": asset.preview_path,
            "parent_asset_id": asset.parent_asset_id,
            "lineage": list(asset.lineage),
            "export_paths": list(asset.export_paths),
            "tags": list(asset.tags),
            "metadata": dict(asset.metadata),
            "created_at": asset.created_at,
        }


def _summarize_prompt(prompt: str, *, fallback: str) -> str:
    compact = " ".join(prompt.strip().split())
    if not compact:
        return fallback
    if len(compact) <= 72:
        return compact
    return f"{compact[:69]}..."


__all__ = ["Asset", "AssetRepository"]
