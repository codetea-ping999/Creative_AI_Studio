"""Single source of truth for the Creative AI Studio release version.

The version lives in the repository-root ``VERSION`` file so that the Python
API, the release artifact builder, the artifact smoke test, and the Web UI
package metadata all agree without any of them re-declaring the number.

``tests/test_version_consistency.py`` is the gate that keeps the copies in
``apps/web/package.json`` (and the desktop bundle config, when present) aligned
with this file.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

#: ``MAJOR.MINOR.PATCH`` with an optional pre-release suffix (``1.0.0-rc.1``).
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def _read_version_file(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable checkout/artifact
        raise RuntimeError(f"cannot read the VERSION file at {path}") from exc

    version = raw.strip()
    if not _VERSION_PATTERN.match(version):
        raise RuntimeError(
            f"{path} must contain a single MAJOR.MINOR.PATCH version, got {version!r}"
        )
    return version


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the release version, e.g. ``"1.0.0"``.

    The value is read once per process; the file cannot change under a running
    server, and the release artifact ships the same file as the checkout.
    """

    return _read_version_file(VERSION_FILE)


def get_release_tag() -> str:
    """Return the Git tag form of the release version, e.g. ``"v1.0.0"``."""

    return f"v{get_version()}"


def get_base_version() -> str:
    """Return the version without its pre-release suffix.

    ``1.0.0-rc.1`` and ``1.0.0`` share the base version ``1.0.0``. The release
    *identity* — the tag, the artifact name, ``GET /version`` — carries the
    suffix, because those must name the exact thing that was cut. Everything
    that describes the release *contents* tracks the base version instead: the
    changelog section and the package metadata are the same for a candidate and
    for the release it is a candidate for. That is what lets a release
    candidate be cut by editing ``VERSION`` alone, however many candidates
    there are.
    """

    return get_version().split("-", 1)[0]


__all__ = [
    "VERSION_FILE",
    "get_base_version",
    "get_release_tag",
    "get_version",
]
