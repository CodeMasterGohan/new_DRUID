"""Document endpoints: upload, list, fetch, delete, status (spec §5, §11, §12)."""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from services.catalog.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    TenantContextError,
)
from services.catalog.models import IngestionState
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext

from deps import (
    get_object_store,
    get_repository,
    get_session,
    get_settings,
    get_trusted_context,
)
from internal_auth import InternalAuthError
from job_queue import (
    DELETION_ENTRYPOINT,
    INGESTION_ENTRYPOINT,
    create_deletion_job,
    create_ingestion_job,
    enqueue_rq,
)
from schemas import (
    DeleteResponse,
    DocumentDetail,
    DocumentListResponse,
    DocumentSummary,
    JobStatusResponse,
    UploadResponse,
    VersionInfo,
    ArtifactReference,
)
from settings import DocumentApiSettings
from storage import ObjectStore, storage_key
from validation import (
    UploadTooLargeError,
    UploadValidationError,
    count_pages,
    validate_mime,
)

router = APIRouter()

log = logging.getLogger(__name__)

_READ_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming reads

#: Parser label for versions created at upload time. The per-page router
#: decides native vs visual per page during PARSING; the version as a whole
#: is therefore "mixed".
UPLOAD_PARSER = "mixed"
UPLOAD_PARSER_REVISION = "router-v1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _read_bounded(file: UploadFile, limit_bytes: int) -> bytes:
    """Stream the upload, aborting past ``limit_bytes`` (→ HTTP 413)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise UploadTooLargeError(
                f"upload exceeds the {limit_bytes // (1024 * 1024)} MiB limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_display_name(filename: str | None) -> str:
    """Filename is display metadata only — strip paths, cap length."""
    name = os.path.basename(filename or "upload")
    name = name.strip() or "upload"
    return name[:255]


def _doc_to_summary(doc) -> DocumentSummary:
    return DocumentSummary(
        document_id=str(doc.id),
        display_name=doc.display_name,
        checksum_sha256=doc.content_checksum,
        active_version_id=str(doc.active_version_id)
        if doc.active_version_id
        else None,
        is_deleted=doc.is_deleted,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _version_to_info(version) -> VersionInfo:
    return VersionInfo(
        version_id=str(version.id),
        version_number=version.version_number,
        status=version.status.value,
        parser=version.parser,
        parser_revision=version.parser_revision,
        page_count=version.page_count,
        chunk_count_actual=version.chunk_count_actual,
        is_active=version.is_active,
        created_at=version.created_at,
    )


def _artifact_refs(
    store: ObjectStore,
    keys: dict[str, str],
    expires_seconds: int,
) -> dict[str, ArtifactReference]:
    refs: dict[str, ArtifactReference] = {}
    for name, key in keys.items():
        url = store.presigned_get_url(key, expires_seconds=expires_seconds)
        refs[name] = ArtifactReference(key=key, download_url=url)
    return refs


def _original_key(context: RequestContext, doc, version) -> str:
    return storage_key(context.group_id, str(doc.id), str(version.id), "original")


# ---------------------------------------------------------------------------
# POST /v1/documents/upload
# ---------------------------------------------------------------------------


@router.post("/v1/documents/upload", response_model=UploadResponse, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    context: RequestContext = Depends(get_trusted_context),
    session: Session = Depends(get_session),
    settings: DocumentApiSettings = Depends(get_settings),
    store: ObjectStore = Depends(get_object_store),
):
    """Upload workflow (§5): validate → store → catalog → durable job → enqueue.

    Returns ``{document_id, job_id, status}``. The filename is display
    metadata only and never becomes a storage path.
    """
    repo = CatalogRepository(session, context)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    stored_keys: list[str] = []
    started = time.monotonic()
    log.info(
        "upload start: filename=%r group=%s request_id=%s",
        _safe_display_name(file.filename),
        context.group_id,
        context.request_id,
    )

    try:
        data = await _read_bounded(file, max_bytes)
        if not data:
            raise UploadValidationError("uploaded file is empty")
        mime = validate_mime(data, file.content_type)
        page_count = count_pages(data, mime)
        if page_count > settings.max_pages_per_document:
            raise UploadValidationError(
                f"document has {page_count} pages, exceeding the "
                f"{settings.max_pages_per_document}-page limit"
            )
        checksum = hashlib.sha256(data).hexdigest()
        display_name = _safe_display_name(file.filename)

        doc = repo.create_document(
            display_name=display_name, content_checksum=checksum
        )
        version = repo.create_version(
            doc.id,
            parser=UPLOAD_PARSER,
            parser_revision=UPLOAD_PARSER_REVISION,
            page_count=page_count,
        )
        key = _original_key(context, doc, version)
        store.put_bytes(key, data, content_type=mime)
        stored_keys.append(key)

        job = create_ingestion_job(
            repo,
            document_id=doc.id,
            document_version_id=version.id,
            pdf_key=key,
            content_type=mime,
            page_count=page_count,
            queue_name=settings.ingest_queue,
        )
        # Commit BEFORE the job becomes visible to workers: a worker that
        # races the commit must never see a missing job row.
        session.commit()

        try:
            enqueue_rq(
                settings.ingest_queue, INGESTION_ENTRYPOINT, job, settings.redis_url
            )
        except Exception as exc:
            # The job row exists but no worker will pick it up — park it as
            # FAILED so it shows up in status instead of hanging in UPLOADED.
            repo.update_job_state(
                job.id, IngestionState.FAILED, error=f"enqueue failed: {exc}"
            )
            session.commit()
            log.error(
                "upload enqueue failed: job=%s document=%s group=%s request_id=%s: %s",
                job.id,
                doc.id,
                context.group_id,
                context.request_id,
                exc,
            )
            raise HTTPException(
                status_code=500, detail=f"upload stored but job enqueue failed: {exc}"
            )

        elapsed_ms = (time.monotonic() - started) * 1000.0
        log.info(
            "upload finish: document=%s version=%s job=%s pages=%d group=%s "
            "request_id=%s in %.1fms",
            doc.id,
            version.id,
            job.id,
            page_count,
            context.group_id,
            context.request_id,
            elapsed_ms,
        )
        return UploadResponse(
            document_id=str(doc.id),
            job_id=str(job.id),
            status=job.state.value,
            page_count=page_count,
            checksum_sha256=checksum,
            display_name=display_name,
        )
    except (UploadTooLargeError, UploadValidationError, InternalAuthError,
            TenantContextError, AuthorizationError, NotFoundError,
            ConflictError) as exc:
        session.rollback()
        for k in stored_keys:  # best-effort cleanup of orphaned bytes
            try:
                store.delete_prefix(k)
            except Exception as cleanup_exc:
                log.debug(
                    "upload cleanup: delete_prefix(%r) failed: %s", k, cleanup_exc
                )
        log.warning(
            "upload rejected: %s (%s) group=%s request_id=%s",
            type(exc).__name__,
            exc,
            context.group_id,
            context.request_id,
        )
        raise _to_http(exc)
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:  # pragma: no cover - defensive
        session.rollback()
        for k in stored_keys:
            try:
                store.delete_prefix(k)
            except Exception as cleanup_exc:
                log.debug(
                    "upload cleanup: delete_prefix(%r) failed: %s", k, cleanup_exc
                )
        log.exception(
            "upload failed: group=%s request_id=%s",
            context.group_id,
            context.request_id,
        )
        raise HTTPException(status_code=500, detail=f"upload failed: {exc}")


# ---------------------------------------------------------------------------
# GET /v1/documents
# ---------------------------------------------------------------------------


@router.get("/v1/documents", response_model=DocumentListResponse)
def list_documents(
    review_required: bool | None = Query(
        default=None, description="Filter to documents with pending review items"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: CatalogRepository = Depends(get_repository),
):
    """List the tenant's documents (§11 ``list_documents``)."""
    docs = repo.list_documents(
        review_required=review_required, limit=limit, offset=offset
    )
    return DocumentListResponse(
        documents=[_doc_to_summary(d) for d in docs], limit=limit, offset=offset
    )


# ---------------------------------------------------------------------------
# GET /v1/documents/{id}
# ---------------------------------------------------------------------------


@router.get("/v1/documents/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: uuid.UUID,
    repo: CatalogRepository = Depends(get_repository),
    context: RequestContext = Depends(get_trusted_context),
    store: ObjectStore = Depends(get_object_store),
    settings: DocumentApiSettings = Depends(get_settings),
):
    """Authorized document metadata + artifact references (§11)."""
    try:
        doc = repo.get_document(document_id)
    except NotFoundError as exc:
        raise _to_http(exc)
    versions = repo.list_versions(document_id)
    artifacts: dict[str, ArtifactReference] = {}
    # Artifact references for the active version, else the latest version.
    target = next((v for v in versions if v.is_active), versions[0] if versions else None)
    if target is not None:
        key = _original_key(context, doc, target)
        artifacts = _artifact_refs(
            store, {"original": key}, settings.rustfs_presign_expiry_seconds
        )
    detail = _doc_to_summary(doc).model_dump()
    return DocumentDetail(
        **detail,
        versions=[_version_to_info(v) for v in versions],
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# DELETE /v1/documents/{id}
# ---------------------------------------------------------------------------


@router.delete(
    "/v1/documents/{document_id}", response_model=DeleteResponse, status_code=202
)
def delete_document(
    document_id: uuid.UUID,
    repo: CatalogRepository = Depends(get_repository),
    session: Session = Depends(get_session),
    settings: DocumentApiSettings = Depends(get_settings),
):
    """Authorize in the catalog, then enqueue complete deletion (§12).

    The worker (``run_deletion_job``) removes Milvus chunks, originals,
    derived artifacts, renders, caches, and catalog visibility. Retried
    DELETEs report the already-queued job instead of duplicating work.
    """
    try:
        doc = repo.get_document(document_id)  # 404 unless owned by tenant
    except NotFoundError as exc:
        raise _to_http(exc)

    context = repo.context
    log.info(
        "delete start: document=%s group=%s request_id=%s",
        document_id,
        context.group_id,
        context.request_id,
    )
    try:
        job = create_deletion_job(repo, document_id=doc.id,
                                  queue_name=settings.delete_queue)
        session.commit()
    except ConflictError:
        # A deletion is already queued for this document — report it.
        existing = repo.get_job_by_idempotency_key(f"delete-{doc.id.hex}")
        session.commit()
        if existing is None:  # pragma: no cover - cannot happen post-conflict
            raise HTTPException(status_code=409,
                                detail="deletion already queued for this document")
        log.info(
            "delete deduped: document=%s existing_job=%s group=%s request_id=%s",
            doc.id,
            existing.id,
            context.group_id,
            context.request_id,
        )
        return DeleteResponse(
            document_id=str(doc.id),
            job_id=str(existing.id),
            status=existing.state.value,
        )

    try:
        enqueue_rq(settings.delete_queue, DELETION_ENTRYPOINT, job,
                   settings.redis_url)
    except Exception as exc:
        repo.update_job_state(job.id, IngestionState.FAILED,
                              error=f"enqueue failed: {exc}")
        session.commit()
        log.error(
            "delete enqueue failed: job=%s document=%s group=%s request_id=%s: %s",
            job.id,
            doc.id,
            context.group_id,
            context.request_id,
            exc,
        )
        raise HTTPException(status_code=500,
                            detail=f"deletion authorized but enqueue failed: {exc}")
    log.info(
        "delete finish: document=%s job=%s group=%s request_id=%s",
        doc.id,
        job.id,
        context.group_id,
        context.request_id,
    )
    return DeleteResponse(
        document_id=str(doc.id), job_id=str(job.id), status=job.state.value
    )


# ---------------------------------------------------------------------------
# GET /v1/documents/{id}/status
# ---------------------------------------------------------------------------


@router.get("/v1/documents/{document_id}/status", response_model=JobStatusResponse)
def get_ingestion_status(
    document_id: uuid.UUID,
    repo: CatalogRepository = Depends(get_repository),
):
    """Job status for the document's active version ingestion job (§11)."""
    try:
        doc = repo.get_document(document_id)
    except NotFoundError as exc:
        raise _to_http(exc)
    versions = repo.list_versions(document_id)
    target = next((v for v in versions if v.is_active), versions[0] if versions else None)
    if target is None:
        raise HTTPException(status_code=404, detail="document has no versions")
    jobs = repo.list_version_jobs(target.id, job_type="ingestion", limit=1)
    if not jobs:
        raise HTTPException(
            status_code=404, detail="no ingestion job found for this version"
        )
    job = jobs[0]
    return JobStatusResponse(
        job_id=str(job.id),
        job_type=job.job_type,
        state=job.state.value,
        document_id=str(job.document_id) if job.document_id else None,
        document_version_id=str(job.document_version_id)
        if job.document_version_id
        else None,
        attempt_count=job.attempt_count,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def _to_http(exc: Exception) -> HTTPException:
    """Map domain errors to HTTP status codes (fail closed, no leakage)."""
    if isinstance(exc, UploadTooLargeError):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, UploadValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (InternalAuthError, TenantContextError)):
        return HTTPException(status_code=401, detail="unauthorized")
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail="forbidden")
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail="not found")
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="internal error")


__all__ = ["router"]
