"""Durable job creation + RQ enqueueing for the document API (spec §12).

The RQ workers live in ``services.ingestion.pipeline`` (``run_ingestion_job``
/ ``run_deletion_job``). Jobs are enqueued by dotted-path string so this
service's image does not need the ingestion dependency tree (pdftext, torch,
olmOCR toolkit). The entry-point constants below are the contract with the
pipeline module — ``tests/test_entrypoints.py`` asserts they resolve.

Job creation (DB row, via :class:`CatalogRepository`) is separated from the
RQ enqueue so callers can commit the database transaction *before* the job
becomes visible to workers. Enqueueing first risks a worker racing the
commit and failing on a missing job row.

Payload contract (read by the pipeline worker):
* ingestion: ``{"queue", "pdf_key", "content_type", "page_count",
  "document_id", "document_version_id"}`` — ``pdf_key`` is the tenant-scoped
  MinIO key of the original file.
* deletion: ``{"queue", "document_id"}``.
"""

from __future__ import annotations

import logging
import uuid

from services.catalog.models import Job
from services.catalog.repository import CatalogRepository

log = logging.getLogger(__name__)

#: RQ entry point for ingestion workers (services.ingestion.pipeline).
INGESTION_ENTRYPOINT = "services.ingestion.pipeline.run_ingestion_job"
#: RQ entry point for deletion workers (services.ingestion.pipeline).
DELETION_ENTRYPOINT = "services.ingestion.pipeline.run_deletion_job"


def enqueue_rq(queue_name: str, entrypoint: str, job: Job, redis_url: str) -> None:
    """Put an existing job row on the RQ ``queue_name``.

    Call only after the job row is committed — workers look it up by id.
    """
    try:
        from redis import Redis
        from rq import Queue
    except ImportError as exc:
        raise RuntimeError("rq + redis are required to enqueue jobs") from exc
    redis = Redis.from_url(redis_url)
    queue = Queue(queue_name, connection=redis)
    queue.enqueue(entrypoint, str(job.id), job_id=f"{queue_name}-{job.id}")
    log.info(
        "enqueued job: job=%s type=%s queue=%s entrypoint=%s",
        job.id,
        job.job_type,
        queue_name,
        entrypoint,
    )


def create_ingestion_job(
    repo: CatalogRepository,
    *,
    document_id: uuid.UUID,
    document_version_id: uuid.UUID,
    pdf_key: str,
    content_type: str,
    page_count: int,
    queue_name: str = "ingest",
) -> Job:
    """Create the durable ingestion job row (not yet enqueued).

    The job persists the trusted ownership context (``repo.create_job``);
    workers revalidate it before acting (§4). The idempotency key is derived
    from the version id so a retried request for the same version never
    creates a duplicate job.
    """
    return repo.create_job(
        job_type="ingestion",
        document_id=document_id,
        document_version_id=document_version_id,
        idempotency_key=f"ingest-{document_version_id.hex}",
        payload={
            "queue": queue_name,
            "pdf_key": pdf_key,
            "content_type": content_type,
            "page_count": page_count,
            "document_id": str(document_id),
            "document_version_id": str(document_version_id),
        },
    )


def create_deletion_job(
    repo: CatalogRepository,
    *,
    document_id: uuid.UUID,
    queue_name: str = "delete",
) -> Job:
    """Create the durable deletion job row (not yet enqueued).

    The idempotency key is per document: a second DELETE while the first is
    still queued raises ``ConflictError`` from ``create_job`` and the caller
    should report the existing job instead of creating another.
    """
    return repo.create_job(
        job_type="deletion",
        document_id=document_id,
        idempotency_key=f"delete-{document_id.hex}",
        payload={"queue": queue_name, "document_id": str(document_id)},
    )


__all__ = [
    "DELETION_ENTRYPOINT",
    "INGESTION_ENTRYPOINT",
    "create_deletion_job",
    "create_ingestion_job",
    "enqueue_rq",
]
