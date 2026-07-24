"""Validation for user-supplied export destinations.

The export endpoints are reachable on the local, unauthenticated API. Without
validation, ``destination_dir`` / ``destination_name`` are passed straight to
``mkdir`` + ``shutil.copy2``, which lets a caller write asset bytes anywhere on
disk (absolute paths, or ``..`` traversal). To contain this, exports are
constrained to the ``exports`` directory under the configured output root, and
destination file names must be bare file names.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status

from bootstrap import ApplicationServices


def exports_root(services: ApplicationServices) -> Path:
    """Return the only directory tree that exports may be written into."""

    return (services.output_dir.parent / "exports").resolve()


def resolve_export_dir(
    services: ApplicationServices,
    requested_dir: str | None,
    *,
    default_subpath: str | Path,
) -> Path:
    """Resolve an export directory, constrained to the exports root.

    A missing ``requested_dir`` falls back to ``exports_root / default_subpath``.
    A supplied value (absolute or relative) must resolve to a location inside
    the exports root; anything else raises ``400``.
    """

    root = exports_root(services)
    if not requested_dir:
        return (root / Path(default_subpath)).resolve()

    candidate = Path(requested_dir)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"destination_dir must stay within {root}",
        )
    return resolved


def sanitize_export_name(name: str | None) -> str | None:
    """Reject destination names that are not bare file names."""

    if name is None:
        return None
    if name in {"", ".", ".."} or name != Path(name).name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="destination_name must be a bare file name",
        )
    return name


__all__ = ["exports_root", "resolve_export_dir", "sanitize_export_name"]
