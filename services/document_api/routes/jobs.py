"""Job-scoped endpoints: tenant-scoped job status and retry (spec §11, §12)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from services.catalog.errors import NotFoundError
from services.catalog.models import IngestionState
from services.catalog.repository import CatalogRepository

from deps import get_repository, get_session, get_settings
from job_queue import DELETION_ENTRYPOINT, INGESTION_ENTRYPOINT
from schemas import JobStatusResponse
from settings import DocumentApiSettings

router = APIRouter()

log = logging.getLogger(__name__)

#: Error text is an excerpt; the full text stays in the catalog row.
_ERROR_EXCERPT_CHARS = 500


def _error_excerpt(error: str | None) -> str | None:
    if error is not None and len(error) > _ERROR_EXCERPT_CHARS:
        return error[:_ERROR_EXCERPT_CHARS] + "…"
    return error


def _job_to_response(job) -> JobStatusResponse:
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
        if job.document_id is not None:
            repo.get_document(job.document_id, include_deleted=job.job_type == "deletion")
    except NotFoundError:
        log.info(
            "job status → 404: job=%s group=%s",
            job_id,
            repo.context.group_id,
        )
        raise
    return _job_to_response(job)


@router.post("/v1/jobs/{job_id}/retry", response_model=JobStatusResponse)
def retry_job(
    job_id: uuid.UUID,
    repo: CatalogRepository = Depends(get_repository),
    session: Session = Depends(get_session),
    settings: DocumentApiSettings = Depends(get_settings),
):
    """Re-enqueue a failed, retry-pending, or un-enqueued job within the tenant."""
    try:
        job = repo.get_job(job_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc

    from services.catalog.work_lock import document_work_lock
    from services.catalog.errors import ConflictError
    try:
        with document_work_lock(session, job.document_id):
            job = repo.get_job(job_id)
            doc = repo.get_document_for_write(job.document_id, include_deleted=job.job_type == "deletion")
            if (repo.context.user_id != doc.created_by
                    and not set(repo.context.roles).intersection({"admin", "group_admin"})):
                # Legacy group-wide mode retains its historical mutation policy.
                if settings.druid_auth_mode == "keycloak":
                    raise HTTPException(403, "not the document owner")
            return _retry_locked(job, repo, session, settings)
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


def _retry_locked(job, repo, session, settings):
    import routes.documents as documents_routes
    payload = dict(job.payload or {})
    unenqueued_upload = (
        job.state == IngestionState.UPLOADED and not payload.get("enqueued_at")
    )
    if job.state not in (IngestionState.FAILED, IngestionState.RETRY_PENDING, IngestionState.DELETING) and not unenqueued_upload:
        raise HTTPException(
            status_code=409,
            detail=f"job in state {job.state.value} does not need retry",
        )
    if job.job_type == "ingestion":
        if not payload.get("pdf_key") or job.document_version_id is None:
            raise HTTPException(
                status_code=409,
                detail="upload registration has no stored bytes to enqueue",
            )
        queue_name = payload.get("queue") or settings.ingest_queue
        entrypoint = INGESTION_ENTRYPOINT
    elif job.job_type == "deletion":
        queue_name = payload.get("queue") or settings.delete_queue
        entrypoint = DELETION_ENTRYPOINT
    else:
        raise HTTPException(
            status_code=400, detail=f"unsupported job_type {job.job_type!r}"
        )

    repo.update_job_state(job.id, IngestionState.UPLOADED, error=None)
    if job.document_version_id is not None:
        repo.update_version_status(job.document_version_id, IngestionState.UPLOADED)
    job.payload = {k: v for k, v in payload.items() if k != "enqueued_at"}
    session.commit()  # visible before dispatch; the document lock excludes workers
    try:
        documents_routes.enqueue_rq(
            queue_name, entrypoint, job, settings.redis_url
        )
    except Exception as exc:
        documents_routes._mark_enqueue_failed(repo, job, session, exc)
        raise HTTPException(
            status_code=500, detail=f"job retry enqueue failed: {exc}"
        ) from exc

    documents_routes._mark_job_enqueued(job, session)
    return _job_to_response(job)


__all__ = ["router"]
