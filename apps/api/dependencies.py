"""FastAPI dependency accessors for shared application services."""

from __future__ import annotations

from fastapi import Request

from bootstrap import ApplicationServices
from core.jobs import JobRunner, JobService


def get_services(request: Request) -> ApplicationServices:
    return request.app.state.services


def get_job_service(request: Request) -> JobService:
    return get_services(request).job_service


def get_job_runner(request: Request) -> JobRunner:
    return get_services(request).job_runner


__all__ = ["get_job_runner", "get_job_service", "get_services"]
