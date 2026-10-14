"""Review queue endpoints (§5 review queue, §11 tools)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from services.catalog.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext

from deps import (
    get_object_store,
    get_repository,
    get_session,
    get_settings,
    get_trusted_context,
)
from schemas import (
    ResolveReviewRequest,
    ResolveReviewResponse,
    ReviewItemDetail,
    ReviewItemListResponse,
    ReviewItemSummary,
)
from settings import DocumentApiSettings
from storage import ObjectStore

router = APIRouter()

log = logging.getLogger(__name__)

#: Roles permitted to resolve quarantine items (§11 transport security).
REVIEWER_ROLE = "reviewer"
ADMIN_ROLE = "admin"


def _to_summary(item) -> ReviewItemSummary:
    return ReviewItemSummary(
        item_id=str(item.id),
        document_id=str(item.document_id),
        document_version_id=str(item.document_version_id),
        page_index=item.page_index,
        status=item.status,
        created_at=item.created_at,
    )


@router.get("/v1/review/items", response_model=ReviewItemListResponse)
def list_review_items(
    status: str = Query(default="pending", description="pending | accepted | re-routed | rejected"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: CatalogRepository = Depends(get_repository),
):
    """List quarantine items for the tenant (§5 review surface)."""
    if status not in ("pending", "accepted", "re-routed", "rejected"):
        raise HTTPException(status_code=400, detail=f"invalid status: {status!r}")
    items = repo.list_quarantine(status=status, limit=limit, offset=offset)
    return ReviewItemListResponse(
        items=[_to_summary(i) for i in items], limit=limit, offset=offset
    )


@router.get("/v1/review/items/{item_id}", response_model=ReviewItemDetail)
def get_review_item(
    item_id: uuid.UUID,
    repo: CatalogRepository = Depends(get_repository),
    store: ObjectStore = Depends(get_object_store),
    settings: DocumentApiSettings = Depends(get_settings),
):
    """Return a quarantine item: page render, both parse attempts, routing diagnostics (§5)."""
    try:
        item = repo.get_quarantine_item(item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    render_url = store.presigned_get_url(
        item.page_render_ref, expires_seconds=settings.rustfs_presign_expiry_seconds
    )
    return ReviewItemDetail(
        item_id=str(item.id),
        document_id=str(item.document_id),
        document_version_id=str(item.document_version_id),
        page_index=item.page_index,
        status=item.status,
        page_render_key=item.page_render_ref,
        page_render_url=render_url,
        parse_attempts=item.parse_attempts or {},
        routing_diagnostics=item.routing_diagnostics or {},
        reviewer_decision=item.reviewer_decision,
        reviewed_by=item.reviewed_by,
        reviewed_at=item.reviewed_at,
        created_at=item.created_at,
    )


@router.post("/v1/review/items/{item_id}/resolve", response_model=ResolveReviewResponse)
def resolve_review_item(
    item_id: uuid.UUID,
    body: ResolveReviewRequest,
    repo: CatalogRepository = Depends(get_repository),
    session: Session = Depends(get_session),
    context: RequestContext = Depends(get_trusted_context),
):
    """Accept / re-route / reject a quarantined page. Reviewer or admin role required."""
    if not (context.has_role(REVIEWER_ROLE) or context.has_role(ADMIN_ROLE)):
        log.warning(
            "review resolve denied → 403: item=%s user=%s group=%s request_id=%s",
            item_id,
            context.user_id,
            context.group_id,
            context.request_id,
        )
        raise HTTPException(
            status_code=403, detail="resolving review items requires the reviewer or admin role"
        )
    try:
        item = repo.resolve_quarantine_item(
            item_id,
            decision=body.decision,
            reviewed_by=context.user_id,
            detail=body.detail,
        )
        session.commit()
    except NotFoundError:
        session.rollback()
        log.info(
            "review resolve → 404: item=%s user=%s request_id=%s",
            item_id,
            context.user_id,
            context.request_id,
        )
        raise HTTPException(status_code=404, detail="not found")
    except ConflictError as exc:
        session.rollback()
        log.warning(
            "review resolve → 409: item=%s user=%s request_id=%s: %s",
            item_id,
            context.user_id,
            context.request_id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc))
    except AuthorizationError as exc:
        session.rollback()
        log.warning(
            "review resolve → 403: item=%s user=%s request_id=%s: %s",
            item_id,
            context.user_id,
            context.request_id,
            exc,
        )
        raise HTTPException(status_code=403, detail=str(exc))
    log.info(
        "review resolved: item=%s decision=%s status=%s reviewed_by=%s "
        "group=%s request_id=%s",
        item.id,
        body.decision,
        item.status,
        context.user_id,
        context.group_id,
        context.request_id,
    )
    return ResolveReviewResponse(
        item_id=str(item.id), status=item.status, reviewed_by=context.user_id
    )


__all__ = ["REVIEWER_ROLE", "router"]
