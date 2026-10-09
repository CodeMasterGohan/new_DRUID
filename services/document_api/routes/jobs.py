"""Job-scoped endpoints: tenant-scoped job status (spec §11, §12)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends

from services.catalog.errors import NotFoundError
from services.catalog.repository import CatalogRepository

from deps import get_repository
from schemas import JobStatusResponse

router = APIRouter()

log = logging.getLogger(__name__)

#: Error text is an excerpt; the full text stays in the catalog row.
_ERROR_EXCERPT_CHARS = 500


def _error_excerpt(error: str | None) -> str | None:
    if error is not None and len(error) > _ERROR_EXCERPT_CHARS:
        return error[:_ERROR_EXCERPT_CHARS] + "…"
    return error


@router.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(
    job_id: uuid.UUID,
    repo: CatalogRepository = Depends(get_repository),
):
    """Job status by job id, resolved within the caller's tenant (§11).

    The tenant predicate is enforced by the repository: a job owned by
    another tenant reads as not found (404), never as forbidden. Covers
    ingestion, deletion, and registration jobs alike — anything the
    caller's tenant created.
    """
    try:
        job = repo.get_job(job_id)
    except NotFoundError:
        log.info(
            "job status → 404: job=%s group=%s",
            job_id,
            repo.context.group_id,
        )
        raise
    return JobStatusResponse(
        job_id=str(job.id),
        job_type=job.job_type,
        state=job.state.value,
        document_id=str(job.document_id) if job.document_id else None,
        document_version_id=str(job.document_version_id)
        if job.document_version_id
        else None,
        attempt_count=job.attempt_count,
        error=_error_excerpt(job.error),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


__all__ = ["router"]
