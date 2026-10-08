"""Version activation logic (spec §12).

"Do not expose partially indexed versions. Index a new version, verify
expected artifacts and chunk counts, then activate it in the catalog."

Activation is a single transactional check-and-swap:

1. The version must exist and belong to the context's tenant (fail closed).
2. The version must be in ``INDEXING`` — i.e. fully indexed, never partial.
3. Its actual chunk count must fall inside the plausible range recorded for
   the active (parser, parser_revision) in ``parser_revision_stats``.
   Ranges, not exact counts: counts legitimately vary between revisions.
4. No page may be sitting in the review quarantine for this version —
   unreviewed pages must not become searchable (§5, §12).
5. The previous active version (if any) is deactivated; the new version
   becomes ``READY`` + ``is_active`` and the document's
   ``active_version_id`` pointer moves. Retrieval's hard filter
   (:meth:`CatalogRepository.searchable_versions_query`) picks it up from
   there — no index-state dependence.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.common.request_context import RequestContext

from .errors import ActivationError
from .models import (
    Document,
    DocumentVersion,
    IngestionState,
    ParserRevisionStats,
    ReviewQuarantineItem,
)


def get_expected_chunk_range(
    session: Session,
    *,
    parser: str,
    parser_revision: str,
    page_count: int,
) -> tuple[int, int]:
    """Return the plausible ``(min, max)`` chunk count for a version.

    Derived from ``parser_revision_stats`` as
    ``[min_chunks_per_page, max_chunks_per_page] * page_count``.
    Raises :class:`ActivationError` when no baseline is recorded — activating
    without a baseline would be guessing, so it fails closed.
    """
    if page_count is None or page_count < 1:
        raise ActivationError("cannot validate chunk counts without a page count")
    stats = (
        session.execute(
            select(ParserRevisionStats).where(
                ParserRevisionStats.parser == parser,
                ParserRevisionStats.parser_revision == parser_revision,
            )
        )
        .scalars()
        .one_or_none()
    )
    if stats is None:
        raise ActivationError(
            f"no chunk-count baseline recorded for parser "
            f"{parser!r} revision {parser_revision!r}; record one from the "
            f"M1 eval corpus before activating versions"
        )
    lo = max(1, math.floor(stats.min_chunks_per_page * page_count))
    hi = max(lo, math.ceil(stats.max_chunks_per_page * page_count))
    return lo, hi


def pending_review_pages(session: Session, version_id: uuid.UUID) -> int:
    """Count unreviewed quarantine pages for a version."""
    return (
        session.execute(
            select(func.count())
            .select_from(ReviewQuarantineItem)
            .where(
                ReviewQuarantineItem.document_version_id == version_id,
                ReviewQuarantineItem.status == "pending",
            )
        ).scalar()
        or 0
    )


def activate_version(
    session: Session, context: RequestContext, version_id: uuid.UUID
) -> DocumentVersion:
    """Validate and activate a document version.

    The caller owns the transaction: this function flushes but does not
    commit, so a surrounding unit of work can roll everything back on
    failure. Raises :class:`ActivationError` on any failed check — the
    version is left untouched.
    """
    from .repository import CatalogRepository

    repo = CatalogRepository(session, context)  # validates the context, fail closed
    version = repo.get_version(version_id)  # tenant-scoped; NotFoundError if foreign

    # Quarantine is checked first so a version parked for review reports the
    # actionable reason (unreviewed pages) rather than a generic status
    # mismatch — unreviewed pages must never become searchable.
    pending = pending_review_pages(session, version.id)
    if pending:
        raise ActivationError(
            f"version {version_id} has {pending} unreviewed quarantine page(s); "
            f"unreviewed pages must not become searchable"
        )

    if version.status != IngestionState.INDEXING:
        raise ActivationError(
            f"version {version_id} is {version.status.value}, not INDEXING; "
            f"only fully indexed versions may be activated"
        )
    if version.chunk_count_actual is None:
        raise ActivationError(
            f"version {version_id} has no recorded chunk count; refusing to activate"
        )

    lo, hi = get_expected_chunk_range(
        session,
        parser=version.parser,
        parser_revision=version.parser_revision,
        page_count=version.page_count,
    )
    actual = version.chunk_count_actual
    if not (lo <= actual <= hi):
        raise ActivationError(
            f"version {version_id} chunk count {actual} outside plausible range "
            f"[{lo}, {hi}] for parser {version.parser!r} revision "
            f"{version.parser_revision!r}; investigate before activating"
        )

    document = (
        session.execute(select(Document).where(Document.id == version.document_id))
        .scalars()
        .one()
    )
    if document.is_deleted:
        raise ActivationError(f"document {document.id} is deleted; cannot activate")

    # Check-and-swap: deactivate the previous active version first so there is
    # never more than one active version per document.
    previous = (
        session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    for old in previous:
        if old.id != version.id:
            old.is_active = False

    version.is_active = True
    version.status = IngestionState.READY
    version.activated_at = dt.datetime.now(dt.timezone.utc)
    document.active_version_id = version.id
    session.flush()
    return version
