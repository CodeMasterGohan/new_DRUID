"""Admin endpoints: cross-tenant operator views (spec §16 web_ui).

All routes require the ``admin`` role in the caller's JWT claims (enforced
here, in addition to the web_ui's own gate). Cross-tenant reads are allowed
for admins only; the optional ``tenant`` filter is an explicit query param —
never derived from headers, which the gateway strips.

Reachable via the gateway's ``/v1/{path:path}`` proxy like every other
document_api route; identity arrives as HMAC-signed ``X-Druid-*`` headers.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from services.catalog.models import IngestionState
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext

from deps import (
    get_redis_client,
    get_repository,
    get_settings,
    get_trusted_context,
)
from schemas import (
    AdminDocumentListResponse,
    AdminDocumentSummary,
    AdminJobListResponse,
    AdminJobSummary,
    AdminQuotaListResponse,
    AdminTenantQuota,
)
from settings import DocumentApiSettings

router = APIRouter()

log = logging.getLogger(__name__)

#: Role required for every route in this module (spec §16).
ADMIN_ROLE = "admin"

#: Max characters of a job's error surfaced in list views; the full text
#: stays in the catalog for the detail path.
_ERROR_EXCERPT_CHARS = 500


def _require_admin(context: RequestContext) -> None:
    """Deny non-admin callers with 403 (no detail leakage)."""
    if not context.has_role(ADMIN_ROLE):
        log.warning(
            "admin denied → 403: user=%s group=%s roles=%s request_id=%s",
            context.user_id,
            context.group_id,
            ",".join(sorted(context.roles)),
            context.request_id,
        )
        raise HTTPException(status_code=403, detail="admin role required")


def _parse_status(value: str | None) -> IngestionState | None:
    """Validate the status filter against the ingestion state machine (§12)."""
    if value is None:
        return None
    try:
        return IngestionState(value)
    except ValueError:
        valid = ", ".join(s.value for s in IngestionState)
        raise HTTPException(
            status_code=400,
            detail=f"invalid status {value!r}; expected one of: {valid}",
        )


def _quota_keys(tenant: str, today: dt.date) -> tuple[str, str]:
    """Redis keys for the gateway's quota counters.

    MUST stay in sync with ``services/gateway/quotas.py``:
      ingestion: ``druid:quota:ingest:{tenant}:{YYYY-MM-DD}`` (integer counter)
      query:     ``druid:quota:query:{tenant}`` (1s sliding-window sorted set)

    ``tenant`` is the ``group_id`` claim (tenant *name*), exactly as the
    gateway keys it — not the catalog UUID.
    """
    return (
        f"druid:quota:ingest:{tenant}:{today.isoformat()}",
        f"druid:quota:query:{tenant}",
    )


# ---------------------------------------------------------------------------
# GET /v1/admin/documents
# ---------------------------------------------------------------------------


@router.get("/v1/admin/documents", response_model=AdminDocumentListResponse)
def admin_list_documents(
    tenant: str | None = Query(
        default=None, description="Tenant name (group_id) filter; omit for all tenants"
    ),
    status: str | None = Query(
        default=None, description="Filter by active-version ingestion state"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: RequestContext = Depends(get_trusted_context),
    repo: CatalogRepository = Depends(get_repository),
):
    """Cross-tenant document list for operators."""
    _require_admin(context)
    state = _parse_status(status)
    rows = repo.list_all_documents(
        tenant_name=tenant, status=state, limit=limit, offset=offset
    )
    log.info(
        "admin documents: user=%s rows=%d tenant_filter=%s status_filter=%s "
        "request_id=%s",
        context.user_id,
        len(rows),
        tenant,
        status,
        context.request_id,
    )
    return AdminDocumentListResponse(
        documents=[
            AdminDocumentSummary(
                document_id=str(doc.id),
                display_name=doc.display_name,
                group_id=tenant_name,
                status=ver_status.value if ver_status is not None else None,
                updated_at=doc.updated_at,
            )
            for doc, tenant_name, ver_status in rows
        ],
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /v1/admin/jobs
# ---------------------------------------------------------------------------


@router.get("/v1/admin/jobs", response_model=AdminJobListResponse)
def admin_list_jobs(
    tenant: str | None = Query(
        default=None, description="Tenant name (group_id) filter; omit for all tenants"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: RequestContext = Depends(get_trusted_context),
    repo: CatalogRepository = Depends(get_repository),
):
    """Cross-tenant ingestion/deletion job list for operators.

    Reuses the durable job rows (§12); ``progress`` is not reported because
    the jobs table carries no progress column — ``state`` + ``attempt_count``
    are the progress signal.
    """
    _require_admin(context)
    rows = repo.list_all_jobs(tenant_name=tenant, limit=limit, offset=offset)
    log.info(
        "admin jobs: user=%s rows=%d tenant_filter=%s request_id=%s",
        context.user_id,
        len(rows),
        tenant,
        context.request_id,
    )
    summaries = []
    for job, tenant_name in rows:
        duration = None
        if job.created_at is not None and job.updated_at is not None:
            duration = (job.updated_at - job.created_at).total_seconds()
        error = job.error
        if error is not None and len(error) > _ERROR_EXCERPT_CHARS:
            error = error[:_ERROR_EXCERPT_CHARS] + "…"
        summaries.append(
            AdminJobSummary(
                job_id=str(job.id),
                group_id=tenant_name,
                job_type=job.job_type,
                state=job.state.value,
                attempt_count=job.attempt_count,
                error=error,
                document_id=str(job.document_id) if job.document_id else None,
                created_at=job.created_at,
                updated_at=job.updated_at,
                duration_seconds=duration,
            )
        )
    return AdminJobListResponse(jobs=summaries, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# GET /v1/admin/quotas
# ---------------------------------------------------------------------------


@router.get("/v1/admin/quotas", response_model=AdminQuotaListResponse)
def admin_quotas(
    context: RequestContext = Depends(get_trusted_context),
    repo: CatalogRepository = Depends(get_repository),
    settings: DocumentApiSettings = Depends(get_settings),
    redis_client=Depends(get_redis_client),
):
    """Per-tenant quota usage vs configured defaults.

    Usage is read from the same Redis counters the gateway enforces
    (see :func:`_quota_keys`); limits come from this service's settings,
    which mirror the gateway's ``QUOTA_*`` env vars. When the counter
    backend is unreachable, usage is reported as ``null`` rather than a
    misleading zero.
    """
    _require_admin(context)
    today = dt.date.today()
    tenants: list[AdminTenantQuota] = []
    for name in repo.list_tenant_names():
        ingest_key, query_key = _quota_keys(name, today)
        ingest_used: int | None = None
        query_qps: int | None = None
        try:
            raw = redis_client.get(ingest_key)
            ingest_used = int(raw) if raw is not None else 0
            query_qps = int(redis_client.zcard(query_key) or 0)
        except Exception as exc:  # Redis down: report unknown, not zero
            log.warning(
                "admin quotas: redis read failed for tenant %s request_id=%s: %s",
                name,
                context.request_id,
                exc,
            )
        tenants.append(
            AdminTenantQuota(
                group_id=name,
                ingest_pages_used=ingest_used,
                ingest_pages_limit=settings.quota_ingest_pages_per_day,
                query_qps=query_qps,
                query_qps_limit=settings.quota_query_qps,
            )
        )
    log.info(
        "admin quotas: user=%s tenants=%d request_id=%s",
        context.user_id,
        len(tenants),
        context.request_id,
    )
    return AdminQuotaListResponse(tenants=tenants)


__all__ = ["ADMIN_ROLE", "router"]
