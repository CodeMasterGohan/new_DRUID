"""Document endpoints: upload, list, fetch, delete, status (spec §5, §11, §12)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
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
from ingestion_admission import admit
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
    RegisterUploadRequest,
    RegisterUploadResponse,
    UploadResponse,
    VersionInfo,
    ArtifactReference,
)
from settings import DocumentApiSettings
from storage import ObjectStore, storage_key
from validation import (
    ALLOWED_MIME_TYPES,
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


class ValidatedUpload:
    def __init__(self, stream, size, checksum, header):
        self.stream, self.size, self.checksum, self.header = stream, size, checksum, header
    def __len__(self):
        return self.size


async def _read_bounded(file: UploadFile, limit_bytes: int) -> ValidatedUpload:
    """Hash bounded chunks, reusing FastAPI's seekable spool for parsing/storage."""
    digest = hashlib.sha256()
    total, header = 0, b""
    while True:
        chunk = await file.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        if not header:
            header = chunk[:16]
        total += len(chunk)
        if total > limit_bytes:
            raise UploadTooLargeError("upload exceeds configured byte limit")
        digest.update(chunk)
    await file.seek(0)
    return ValidatedUpload(file.file, total, digest.hexdigest(), header)


def _safe_display_name(filename: str | None) -> str:
    """Filename is display metadata only — strip paths, cap length."""
    name = os.path.basename(filename or "upload")
    name = name.strip() or "upload"
    return name[:255]


def hmac_compare(expected: str, presented: str) -> bool:
    """Constant-time string comparison for single-use ticket checks."""
    return hmac.compare_digest(
        expected.encode("utf-8"), presented.encode("utf-8")
    )


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


def _mark_job_enqueued(job, session: Session) -> None:
    """Record enqueue confirmation in job.payload after enqueue_rq succeeds."""
    session.refresh(job)
    existing_payload = getattr(job, "payload", None) or {}
    job.payload = {
        **existing_payload,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    session.commit()


def _mark_enqueue_failed(repo, job, session, exc):
    from sqlalchemy import update
    from services.catalog.models import Job, DocumentVersion
    changed = session.execute(update(Job).where(
        Job.id == job.id, Job.state == IngestionState.UPLOADED
    ).values(state=IngestionState.FAILED, error=f"enqueue failed: {exc}"))
    if changed.rowcount and job.document_version_id is not None:
        session.execute(update(DocumentVersion).where(
            DocumentVersion.id == job.document_version_id,
            DocumentVersion.status == IngestionState.UPLOADED
        ).values(status=IngestionState.FAILED))
    session.commit()
    session.refresh(job)


def _needs_reenqueue(job) -> bool:
    """True when a committed job never made it onto RQ (or failed enqueueing)."""
    payload = getattr(job, "payload", None) or {}
    if job.state in (IngestionState.FAILED, IngestionState.RETRY_PENDING, IngestionState.DELETING):
        return True
    if job.state == IngestionState.UPLOADED and not payload.get("enqueued_at"):
        return True
    return False


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
        mime = validate_mime(data.header, file.content_type)
        page_count = count_pages(data.stream, mime)
        if page_count > settings.max_pages_per_document:
            raise UploadValidationError(
                f"document has {page_count} pages, exceeding the "
                f"{settings.max_pages_per_document}-page limit"
            )
        checksum = data.checksum
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
        admit(session, doc.tenant_id, version.id, page_count, settings.quota_ingest_pages_per_day)
        key = _original_key(context, doc, version)
        store.put_file(key, data.stream, len(data), content_type=mime)
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
        stored_keys.clear()

        try:
            enqueue_rq(
                settings.ingest_queue, INGESTION_ENTRYPOINT, job, settings.redis_url
            )
            _mark_job_enqueued(job, session)
        except Exception as exc:
            # The job row exists but no worker will pick it up — park both
            # the job and version as FAILED so neither hangs in UPLOADED.
            _mark_enqueue_failed(repo, job, session, exc)
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
# POST /v1/documents/register — two-phase upload, step 1
# ---------------------------------------------------------------------------


_HEX_DIGITS = frozenset("0123456789abcdef")


def _validate_declaration(
    body: RegisterUploadRequest, max_bytes: int, max_upload_mb: int
) -> tuple[str, str, str]:
    """Validate the declared file metadata (no bytes available yet).

    Returns ``(display_name, mime_type, sha256_hex)``. Byte-level checks
    (magic-byte sniffing, page count) happen at redemption, when the bytes
    exist; the redeemed bytes must match this declaration exactly.
    """
    display_name = _safe_display_name(body.filename)
    mime = (body.mime_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME_TYPES:
        raise UploadValidationError(
            f"unsupported declared MIME type: {body.mime_type!r}"
        )
    if body.file_size_bytes < 1:
        raise UploadValidationError("file_size_bytes must be positive")
    if body.file_size_bytes > max_bytes:
        raise UploadTooLargeError(
            f"declared size exceeds the {max_upload_mb} MiB limit"
        )
    sha256_hex = body.content_sha256.lower()
    if len(sha256_hex) != 64 or any(c not in _HEX_DIGITS for c in sha256_hex):
        raise UploadValidationError(
            "content_sha256 must be 64 hexadecimal characters"
        )
    return display_name, mime, sha256_hex


#: Idempotency-key prefix for the job row created at registration. The
#: redemption endpoint finds the pending job through it.
_REGISTER_IDEMPOTENCY_PREFIX = "register-"


@router.post(
    "/v1/documents/register",
    response_model=RegisterUploadResponse,
    status_code=201,
)
def register_upload(
    body: RegisterUploadRequest,
    context: RequestContext = Depends(get_trusted_context),
    session: Session = Depends(get_session),
    settings: DocumentApiSettings = Depends(get_settings),
):
    """Two-phase upload, step 1: declare the file, reserve the records (§5).

    Validates the declaration (display name, MIME allowlist, size cap,
    SHA-256 shape), creates the catalog document record and the durable
    ingestion job (state ``UPLOADED``, **not** enqueued — no bytes exist
    yet), and issues a single-use upload ticket bound to this tenant,
    document, and declaration. The caller redeems the ticket with the
    actual bytes at ``POST /v1/documents/{id}/bytes`` before
    ``upload_ticket_ttl_seconds`` elapses.

    Quota enforcement lives at the gateway (§4); like the single-shot
    upload route, this endpoint performs no quota accounting itself.
    """
    repo = CatalogRepository(session, context)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    try:
        display_name, mime, sha256_hex = _validate_declaration(
            body, max_bytes, settings.max_upload_mb
        )
        doc = repo.create_document(
            display_name=display_name, content_checksum=sha256_hex
        )
        ticket = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.upload_ticket_ttl_seconds
        )
        job = repo.create_job(
            job_type="ingestion",
            document_id=doc.id,
            idempotency_key=f"{_REGISTER_IDEMPOTENCY_PREFIX}{doc.id.hex}",
            payload={
                "upload_ticket": ticket,
                "ticket_expires_at": expires_at.isoformat(),
                "declared_filename": display_name,
                "declared_mime_type": mime,
                "declared_size_bytes": body.file_size_bytes,
                "declared_sha256": sha256_hex,
            },
        )
        session.commit()
    except (UploadTooLargeError, UploadValidationError, InternalAuthError,
            TenantContextError, AuthorizationError, NotFoundError,
            ConflictError) as exc:
        session.rollback()
        log.warning(
            "register rejected: %s (%s) group=%s request_id=%s",
            type(exc).__name__,
            exc,
            context.group_id,
            context.request_id,
        )
        raise _to_http(exc)
    log.info(
        "register: document=%s job=%s group=%s request_id=%s",
        doc.id,
        job.id,
        context.group_id,
        context.request_id,
    )
    return RegisterUploadResponse(
        document_id=str(doc.id),
        job_id=str(job.id),
        upload_ticket=ticket,
        expires_in_seconds=settings.upload_ticket_ttl_seconds,
    )


# ---------------------------------------------------------------------------
# POST /v1/documents/{id}/bytes — two-phase upload, step 2 (redeem ticket)
# ---------------------------------------------------------------------------


@router.post("/v1/documents/{document_id}/bytes", response_model=UploadResponse)
async def upload_document_bytes(
    document_id: uuid.UUID,
    upload_ticket: str = Form(...),
    file: UploadFile = File(...),
    context: RequestContext = Depends(get_trusted_context),
    session: Session = Depends(get_session),
    settings: DocumentApiSettings = Depends(get_settings),
    store: ObjectStore = Depends(get_object_store),
):
    from services.catalog.work_lock import document_work_lock
    try:
        with document_work_lock(session, document_id):
            return await _upload_bytes_locked(document_id, upload_ticket, file, context,
                                              session, settings, store)
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


async def _upload_bytes_locked(document_id, upload_ticket, file, context, session, settings, store):
    """Two-phase upload, step 2: redeem the ticket with the file bytes (§5).

    The ticket is single-use, TTL-bound, and pinned to the tenant, the
    document, and the registered declaration: the uploaded bytes must match
    the declared size and SHA-256 exactly, and the sniffed MIME type must
    agree with the declared one. On success the version record is created,
    the bytes are stored under the tenant-scoped key, the ticket is marked
    consumed, and the registration job becomes a real ingestion job and is
    enqueued — mirroring the single-shot ``/upload`` route from there on.
    """
    repo = CatalogRepository(session, context)
    try:
        doc = repo.get_document_for_write(document_id)  # mutation authorization below
    except NotFoundError as exc:
        raise _to_http(exc)

    job = repo.get_job_by_idempotency_key(
        f"{_REGISTER_IDEMPOTENCY_PREFIX}{doc.id.hex}"
    )
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="no pending upload registration for this document",
        )
    from sqlalchemy import select
    from services.catalog.models import Job
    job = session.scalar(select(Job).where(Job.id == job.id).with_for_update()
                         .execution_options(populate_existing=True))
    payload = dict(job.payload or {})
    expected_ticket = payload.get("upload_ticket")
    if not isinstance(expected_ticket, str) or not hmac_compare(
        expected_ticket, upload_ticket
    ):
        raise HTTPException(status_code=403, detail="invalid upload ticket")

    already_consumed = bool(payload.get("ticket_consumed_at"))
    # Recovery path: if a prior redemption committed the version + job payload
    # but failed or crashed before enqueue_rq succeeded, allow the ticket
    # holder to retry redemption instead of stranding the version in FAILED/UPLOADED.
    recovering_unenqueued = (
        already_consumed
        and job.document_version_id is not None
        and _needs_reenqueue(job)
    )
    if already_consumed and not recovering_unenqueued:
        raise HTTPException(
            status_code=409, detail="upload ticket already used"
        )
    expires_at = payload.get("ticket_expires_at")
    try:
        expired = (
            expires_at is None
            or datetime.now(timezone.utc)
            >= datetime.fromisoformat(expires_at)
        )
    except ValueError:
        expired = True
    if expired:
        raise HTTPException(status_code=410, detail="upload ticket expired")

    declared_size = payload.get("declared_size_bytes")
    declared_sha256 = payload.get("declared_sha256")
    declared_mime = payload.get("declared_mime_type")
    if not isinstance(declared_size, int) or not declared_sha256:
        # Registration rows always carry the declaration; a missing one
        # means the job row was tampered with or predates this flow.
        raise HTTPException(
            status_code=500, detail="upload registration is malformed"
        )

    max_bytes = settings.max_upload_mb * 1024 * 1024
    stored_keys: list[str] = []
    log.info(
        "bytes upload start: document=%s job=%s group=%s request_id=%s",
        doc.id,
        job.id,
        context.group_id,
        context.request_id,
    )
    try:
        data = await _read_bounded(file, max_bytes)
        if not data:
            raise UploadValidationError("uploaded file is empty")
        if len(data) != declared_size:
            raise UploadValidationError(
                f"uploaded {len(data)} bytes, declaration said "
                f"{declared_size} bytes"
            )
        checksum = data.checksum
        if checksum != declared_sha256:
            raise UploadValidationError(
                "uploaded bytes do not match the declared SHA-256"
            )
        mime = validate_mime(data.header, declared_mime)
        page_count = count_pages(data.stream, mime)
        if page_count > settings.max_pages_per_document:
            raise UploadValidationError(
                f"document has {page_count} pages, exceeding the "
                f"{settings.max_pages_per_document}-page limit"
            )
        display_name = _safe_display_name(file.filename)

        if recovering_unenqueued and job.document_version_id is not None:
            version = repo.get_version(job.document_version_id)
            admit(session, doc.tenant_id, version.id, page_count, settings.quota_ingest_pages_per_day)
            key = _original_key(context, doc, version)
            store.put_file(key, data.stream, len(data), content_type=mime)
            repo.update_job_state(job.id, IngestionState.UPLOADED, error=None)
            repo.update_version_status(version.id, IngestionState.UPLOADED)
            session.commit()
        else:
            version = repo.create_version(
                doc.id,
                parser=UPLOAD_PARSER,
                parser_revision=UPLOAD_PARSER_REVISION,
                page_count=page_count,
            )
            admit(session, doc.tenant_id, version.id, page_count, settings.quota_ingest_pages_per_day)
            key = _original_key(context, doc, version)
            store.put_file(key, data.stream, len(data), content_type=mime)
            stored_keys.append(key)

            # Promote the registration job into the ingestion job: attach the
            # version, the worker payload, and mark the ticket consumed, then
            # commit BEFORE the job becomes visible to workers.
            job.document_version_id = version.id
            job.payload = {
                **payload,
                "queue": settings.ingest_queue,
                "pdf_key": key,
                "content_type": mime,
                "page_count": page_count,
                "document_id": str(doc.id),
                "document_version_id": str(version.id),
                "ticket_consumed_at": datetime.now(timezone.utc).isoformat(),
            }
            session.commit()
        stored_keys.clear()

        try:
            enqueue_rq(
                settings.ingest_queue, INGESTION_ENTRYPOINT, job, settings.redis_url
            )
            _mark_job_enqueued(job, session)
        except Exception as exc:
            _mark_enqueue_failed(repo, job, session, exc)
            log.error(
                "bytes upload enqueue failed: job=%s document=%s group=%s "
                "request_id=%s: %s",
                job.id,
                doc.id,
                context.group_id,
                context.request_id,
                exc,
            )
            raise HTTPException(
                status_code=500,
                detail=f"upload stored but job enqueue failed: {exc}",
            )

        log.info(
            "bytes upload finish: document=%s version=%s job=%s pages=%d "
            "group=%s request_id=%s",
            doc.id,
            version.id,
            job.id,
            page_count,
            context.group_id,
            context.request_id,
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
                    "bytes upload cleanup: delete_prefix(%r) failed: %s",
                    k,
                    cleanup_exc,
                )
        log.warning(
            "bytes upload rejected: %s (%s) document=%s group=%s request_id=%s",
            type(exc).__name__,
            exc,
            doc.id,
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
                    "bytes upload cleanup: delete_prefix(%r) failed: %s",
                    k,
                    cleanup_exc,
                )
        log.exception(
            "bytes upload failed: document=%s group=%s request_id=%s",
            doc.id,
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
    from services.catalog.work_lock import document_work_lock
    try:
        with document_work_lock(session, document_id):
            return _delete_locked(document_id, repo, session, settings)
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


def _delete_locked(document_id, repo, session, settings):
    """Authorize in the catalog, then enqueue complete deletion (§12).

    The worker (``run_deletion_job``) removes Milvus chunks, originals,
    derived artifacts, renders, caches, and catalog visibility. Retried
    DELETEs report the already-queued job instead of duplicating work.
    """
    try:
        doc = repo.get_document_for_write(document_id, include_deleted=True)
    except NotFoundError as exc:
        raise _to_http(exc)

    context = repo.context
    if (settings.druid_auth_mode == "keycloak"
            and "group_admin" not in context.roles
            and "admin" not in context.roles
            and doc.created_by != context.user_id):
        raise HTTPException(status_code=403, detail="not the document owner")
    log.info(
        "delete start: document=%s group=%s request_id=%s",
        document_id,
        context.group_id,
        context.request_id,
    )
    try:
        if repo.get_job_by_idempotency_key(f"delete-{doc.id.hex}") is not None:
            raise ConflictError("deletion already exists")
        job = create_deletion_job(repo, document_id=doc.id,
                                  queue_name=settings.delete_queue)
        session.commit()
    except ConflictError:
        # A deletion job row already exists for this document.
        existing = repo.get_job_by_idempotency_key(f"delete-{doc.id.hex}")
        session.commit()
        if existing is None:  # pragma: no cover - cannot happen post-conflict
            raise HTTPException(status_code=409,
                                detail="deletion already queued for this document")
        if _needs_reenqueue(existing):
            try:
                repo.update_job_state(existing.id, IngestionState.UPLOADED, error=None)
                session.commit()
                enqueue_rq(
                    settings.delete_queue,
                    DELETION_ENTRYPOINT,
                    existing,
                    settings.redis_url,
                )
                _mark_job_enqueued(existing, session)
            except Exception as exc:
                repo.update_job_state(
                    existing.id,
                    IngestionState.FAILED,
                    error=f"enqueue failed: {exc}",
                )
                session.commit()
                raise HTTPException(
                    status_code=500,
                    detail=f"deletion authorized but enqueue failed: {exc}",
                )
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
        _mark_job_enqueued(job, session)
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
