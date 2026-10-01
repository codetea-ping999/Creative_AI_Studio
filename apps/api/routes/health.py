from fastapi import APIRouter

from core.version import get_release_tag, get_version

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/version")
def version_info() -> dict[str, str]:
    """Report the running release version.

    Gate 5 of the release checklist requires tag/artifact/version consistency
    to be verifiable. This endpoint exposes the repository-root ``VERSION``
    file, which is also what the artifact builder names the tarball after, so a
    running instance can be checked against the tag it was cut from.
    """

    return {"version": get_version(), "release_tag": get_release_tag()}
