"""Ingestion job pipeline: §12 state machine driving catalog + RQ queue.

Stage flow for one ingestion job::

    UPLOADED → QUEUED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY
                                                  ↘ REVIEW_REQUIRED (quarantine)
    failures → RETRY_PENDING (attempts remain) | FAILED (exhausted)

The trusted ownership context is built from the job's *persisted* trusted
fields and revalidated before acting (§4: background jobs persist and
revalidate trusted ownership context). It never comes from the payload.

Seams (owned by sibling agents, honest stubs here):
* ``IndexSink`` — the Milvus agent provides the real sink;
  :class:`LoggingIndexSink` records what *would* be indexed.
* ``ObjectStore`` — RustFS in production; :class:`MemoryObjectStore` for tests.
* Embedding backend — :class:`MockEmbeddingBackend` default here; the real
  EmbeddingGemma 2 backend wires in at deploy time.

Chunk IDs are content-derived (stable), so retries never create duplicates
(§12 idempotency).
"""

from __future__ import annotations

import abc
import logging
import os
import threading
from typing import Callable
import uuid
from dataclasses import dataclass, field

from services.catalog.activation import activate_version
from services.catalog.errors import ActivationError
from services.catalog.models import Base, Document, IngestionState, Job, Tenant
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext

from .chunker import Chunk, StructuralChunker
from .config import IngestionConfig
from .embeddings import (
    EmbeddingBackend,
    MockEmbeddingBackend,
    build_document_text,
    make_page_image_record,
)
from .native_worker import NativePage, extract_pages_native, extract_tables_native
from .olmocr_client import OlmOcrClient, OlmOcrConfig
from .router import PageRoute, RouterDecision, route_page, signals_to_dict
from .tables import TableRegion
from .tree import Node, build_tree_from_native, build_tree_from_olmocr, tree_to_markdown

log = logging.getLogger("druid.ingestion")

PARSER_REVISION_NATIVE = "pdftext-0.4.2+router-v1"
PARSER_REVISION_OLMOCR = "olmocr-2-7b-1025-fp8+router-v1"

_SESSION_MAKER_LOCK = threading.Lock()
_SESSION_MAKERS: dict[str, object] = {}
_REDIS_CLIENTS: dict[str, object] = {}


class _ReviewRequired(Exception):
    """Control-flow: pages parked in the review queue (§5)."""


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


class IndexSink(abc.ABC):
    """Where indexed chunk records go. Milvus agent owns the real one."""

    @abc.abstractmethod
    def index_chunks(self, records: list[dict]) -> int:
        """Insert chunk records; return count indexed."""

    @abc.abstractmethod
    def delete_document(self, document_id: str, group_id: str) -> int:
        """Delete all chunks for a document; return count deleted."""


class LoggingIndexSink(IndexSink):
    """Honest stub: records what would be indexed, indexes nothing."""

    def __init__(self) -> None:
        self.indexed: list[dict] = []
        self.deleted: list[tuple[str, str]] = []

    def index_chunks(self, records: list[dict]) -> int:
        self.indexed.extend(records)
        log.info("index_sink(stub): staged %d chunk records", len(records))
        return len(records)

    def delete_document(self, document_id: str, group_id: str) -> int:
        self.deleted.append((document_id, group_id))
        log.info("index_sink(stub): staged deletion of %s", document_id)
        return 0


class ObjectStore(abc.ABC):
    """Tenant-scoped object storage boundary (RustFS in production)."""

    @abc.abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> None: ...

    @abc.abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def delete_prefix(self, prefix: str) -> int: ...


class MemoryObjectStore(ObjectStore):
    """In-memory store for tests/dev."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> None:
        self._data[key] = data

    def get_bytes(self, key: str) -> bytes:
        return self._data[key]

    def delete_prefix(self, prefix: str) -> int:
        doomed = [k for k in self._data if k.startswith(prefix)]
        for k in doomed:
            del self._data[k]
        return len(doomed)


class RustFSObjectStore(ObjectStore):
    """Production store (lazy minio import; bucket must exist)."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str, *, secure: bool = False) -> None:
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError("minio package is required for RustFSObjectStore") from exc
        self._client = Minio(endpoint, access_key=access_key,
                             secret_key=secret_key, secure=secure)
        self._bucket = bucket

    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> None:
        import io

        self._client.put_object(self._bucket, key, io.BytesIO(data), len(data),
                               content_type=content_type or "application/octet-stream")

    def get_bytes(self, key: str) -> bytes:
        resp = self._client.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            try:
                resp.close()
                resp.release_conn()
            except Exception:  # noqa: BLE001
                pass

    def delete_prefix(self, prefix: str) -> int:
        count = 0
        for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
            self._client.remove_object(self._bucket, obj.object_name)
            count += 1
        return count


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _get_redis_client(redis_url: str):
    from redis import Redis

    with _SESSION_MAKER_LOCK:
        client = _REDIS_CLIENTS.get(redis_url)
        if client is None:
            client = Redis.from_url(redis_url)
            _REDIS_CLIENTS[redis_url] = client
        return client


def enqueue_ingestion_job(
    repo: CatalogRepository,
    *,
    document_id: uuid.UUID,
    document_version_id: uuid.UUID,
    idempotency_key: str,
    queue_name: str = "ingest",
    payload_extra: dict | None = None,
) -> Job:
    """Create the durable job record and enqueue it on the RQ ``ingest`` queue.

    The document API (sibling agent) calls this after upload validation.
    ``payload_extra`` is merged into the job payload (e.g. ``pdf_key``) so the
    worker has everything it needs; the queue name is always recorded.
    """
    payload: dict = {"queue": queue_name}
    if payload_extra:
        payload.update(payload_extra)
    job = repo.create_job(
        job_type="ingestion",
        document_id=document_id,
        document_version_id=document_version_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    try:
        import rq  # noqa: F401
        from redis import Redis  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("rq + redis are required to enqueue jobs") from exc

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis = _get_redis_client(redis_url)
    q = rq.Queue(queue_name, connection=redis)
    q.enqueue(
        "services.ingestion.pipeline.run_ingestion_job",
        str(job.id),
        job_id=f"ingest-{job.id}",
    )
    return job


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------


@dataclass
class PipelineDeps:
    """Injectable dependencies (production wiring vs tests)."""

    config: IngestionConfig = field(default_factory=IngestionConfig)
    embedding_backend: EmbeddingBackend = field(default_factory=MockEmbeddingBackend)
    index_sink: IndexSink = field(default_factory=LoggingIndexSink)
    object_store: ObjectStore = field(default_factory=MemoryObjectStore)
    olmocr_config: OlmOcrConfig | None = None
    token_len: Callable[[str], int] | None = None
    # Native pages whose table regions fall below confidence route to olmOCR.
    route_low_confidence_tables_to_visual: bool = True
    # Embed page-image records for native pages too (provisional; M1 tunes
    # the modality mix — default off, visual pages always qualify).
    page_images_for_native_pages: bool = False


def _transition(repo: CatalogRepository, job: Job, version_id: uuid.UUID,
                state: IngestionState, session) -> None:
    repo.update_job_state(job.id, state)
    repo.update_version_status(version_id, state)
    session.commit()
    log.info("job %s → %s", job.id, state.value)


def build_production_deps() -> PipelineDeps:
    """RQ workers must not run with mock embeddings or a logging-only sink."""
    from .embeddings import SentenceTransformersGemmaBackend
    from .milvus_sink import MilvusIndexSink

    from services.embedding.settings import EmbeddingServiceSettings
    from services.embedding.vllm_backend import VllmEmbeddingBackend

    embedding_settings = EmbeddingServiceSettings()
    if embedding_settings.backend == "mock":
        raise RuntimeError("mock embeddings are forbidden in production ingestion")
    required = ("MINILM_TOKENIZER_PATH",
                "RUSTFS_ENDPOINT", "RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY",
                "RUSTFS_BUCKET", "MILVUS_URI")
    if embedding_settings.backend == "st":
        required += ("EMBEDDING_MODEL_PATH",)
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("production ingestion missing: " + ", ".join(missing))
    return PipelineDeps(
        embedding_backend=(
            VllmEmbeddingBackend(embedding_settings)
            if embedding_settings.backend == "vllm"
            else SentenceTransformersGemmaBackend()
        ),
        index_sink=MilvusIndexSink(
            os.environ["MILVUS_URI"],
            os.environ.get("MILVUS_ALIAS", "project_knowledge_base"),
        ),
        object_store=RustFSObjectStore(
            os.environ["RUSTFS_ENDPOINT"],
            os.environ["RUSTFS_ACCESS_KEY"],
            os.environ["RUSTFS_SECRET_KEY"],
            os.environ["RUSTFS_BUCKET"],
        ),
    )


def run_ingestion_job(
    job_id: str,
    *,
    session_factory=None,
    deps: PipelineDeps | None = None,
) -> str:
    """RQ worker entry point. Returns the terminal job state name.

    ``session_factory`` is a callable returning a SQLAlchemy Session; the
    default reads ``DATABASE_URL`` from the environment.
    """
    owns_deps = deps is None
    deps = deps if deps is not None else build_production_deps()
    session = session_factory() if session_factory else _default_session()
    log.info("worker start: ingestion job=%s", job_id)
    try:
        from services.catalog.work_lock import document_work_lock
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            raise ValueError(f"job {job_id} not found")
        with document_work_lock(session, job.document_id, wait=True):
            return _run(session, uuid.UUID(job_id), deps)
    finally:
        session.close()
        if owns_deps and hasattr(deps.embedding_backend, "close"):
            deps.embedding_backend.close()


def _default_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    with _SESSION_MAKER_LOCK:
        maker = _SESSION_MAKERS.get(url)
        if maker is None:
            engine = create_engine(url, pool_pre_ping=True)
            maker = sessionmaker(bind=engine)
            _SESSION_MAKERS[url] = maker
    return maker()


def _run(session, job_id: uuid.UUID, deps: PipelineDeps) -> str:
    # Rebuild the trusted context from the job's PERSISTED fields (§4).
    # This is the one legitimate source besides live authentication.
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")
    if job.state in (IngestionState.READY, IngestionState.FAILED,
                     IngestionState.DELETED, IngestionState.REVIEW_REQUIRED):
        log.info("job %s already terminal (%s); skipping", job_id, job.state.value)
        return job.state.value

    context = RequestContext(
        user_id=job.trusted_user_id,
        group_id=job.trusted_group_id,
        roles=tuple(job.trusted_roles or ()),
        request_id=f"job-{job.id}",
    )
    repo = CatalogRepository(session, context)
    repo.revalidate_job_context(job)  # fail closed when tenant went inactive
    repo.get_document_for_write(job.document_id)  # tombstones never re-ingest
    job.attempt_count += 1
    session.commit()

    version_id = job.document_version_id
    assert version_id is not None, "ingestion job without a version"
    version = repo.get_version(version_id)
    group_id = context.group_id
    tenant_prefix = f"{group_id}/{version.document_id}/{version.id}"

    try:
        # ---- PARSING ----
        _transition(repo, job, version_id, IngestionState.PARSING, session)
        parse_out = _parse_document(repo, job, version, tenant_prefix, deps, session)

        # ---- CHUNKING ----
        _transition(repo, job, version_id, IngestionState.CHUNKING, session)
        chunks = _chunk_trees(parse_out["trees"], deps)

        # ---- EMBEDDING ----
        _transition(repo, job, version_id, IngestionState.EMBEDDING, session)
        records = _embed_chunks(
            chunks, parse_out, version, group_id, tenant_prefix, deps, session
        )

        # ---- INDEXING ----
        _transition(repo, job, version_id, IngestionState.INDEXING, session)
        repo.update_version_status(version_id, IngestionState.INDEXING,
                                   chunk_count_actual=len(records))
        session.commit()
        indexed = deps.index_sink.index_chunks(records)
        if indexed != len(records):
            raise RuntimeError("index sink did not confirm every inserted record")

        # ---- ACTIVATION (range check + quarantine gate live in catalog) ----
        activate_version(session, context, version_id)
        session.commit()
        repo.update_job_state(job.id, IngestionState.READY)
        session.commit()
        log.info("job %s READY: %d records", job.id, len(records))
        return IngestionState.READY.value

    except _ReviewRequired as exc:
        # Pages parked in quarantine: not an error, not retryable.
        # Unreviewed pages never become searchable.
        session.rollback()  # only uncommitted work; the park is committed
        repo.update_job_state(job.id, IngestionState.REVIEW_REQUIRED)
        repo.update_version_status(version_id, IngestionState.REVIEW_REQUIRED)
        session.commit()
        log.info("job %s REVIEW_REQUIRED: %s", job.id, exc)
        return IngestionState.REVIEW_REQUIRED.value
    except ActivationError as exc:
        # Quarantine / range failures are not retryable blindly.
        session.rollback()
        repo.update_job_state(job.id, IngestionState.FAILED, error=str(exc))
        repo.update_version_status(version_id, IngestionState.FAILED)
        session.commit()
        log.error("job %s FAILED (activation): %s", job.id, exc)
        return IngestionState.FAILED.value
    except Exception as exc:  # noqa: BLE001 — mapped to retry/failed states
        session.rollback()
        if job.attempt_count < job.max_attempts:
            repo.update_job_state(job.id, IngestionState.RETRY_PENDING,
                                  error=f"attempt {job.attempt_count}: {exc}")
            repo.update_version_status(version_id, IngestionState.RETRY_PENDING)
            session.commit()
            log.warning("job %s RETRY_PENDING: %s", job.id, exc)
            return IngestionState.RETRY_PENDING.value
        repo.update_job_state(job.id, IngestionState.FAILED,
                              error=f"exhausted after {job.attempt_count}: {exc}")
        repo.update_version_status(version_id, IngestionState.FAILED)
        session.commit()
        log.error("job %s FAILED: %s", job.id, exc)
        return IngestionState.FAILED.value


def _parse_document(repo, job: Job, version, tenant_prefix: str,
                    deps: PipelineDeps, session) -> dict:
    """PARSING stage: native extract → route → tables/visual → trees."""
    from .native_worker import render_pages_png

    payload = job.payload or {}
    pdf_key = payload.get("pdf_key")
    if not pdf_key:
        raise ValueError("job payload missing 'pdf_key' (S3 key of the original)")
    pdf_bytes = deps.object_store.get_bytes(pdf_key)
    import tempfile

    # pdftext needs a real path (file-likes can't cross the process boundary).
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(pdf_bytes)
        tmp.close()
        pdf_path = tmp.name

        pages: list[NativePage] = extract_pages_native(pdf_path, config=deps.config)
        trees: list[Node] = []
        routing: dict[int, dict] = {}
        visual_pages: list[int] = []
        native_pages: list[NativePage] = []
        png_cache: dict[int, bytes] = {}

        for page in pages:
            decision: RouterDecision = route_page(page.geometry, deps.config.router)
            routing[page.page_index] = {
                "route": decision.route.value,
                "signals": signals_to_dict(decision.signals),
                "reasons": decision.reasons,
            }
            low_conf_tables = [
                r for r in page.geometry.table_regions
                if r.confidence < deps.config.router.min_table_confidence
            ]
            if decision.route == PageRoute.SKIP_BLANK:
                continue
            if (decision.route == PageRoute.VISUAL
                    or (deps.route_low_confidence_tables_to_visual and low_conf_tables
                        and decision.signals.table_area_ratio >= deps.config.router.table_area_ratio_heavy
                        and decision.route == PageRoute.NATIVE)):
                visual_pages.append(page.page_index)
            else:
                native_pages.append(page)

        # Native path: high-confidence tables via pdftext table_output.
        table_texts: dict[int, list[str]] = {}
        confident: dict[int, list[TableRegion]] = {
            p.page_index: [r for r in p.geometry.table_regions
                           if r.confidence >= deps.config.router.min_table_confidence]
            for p in native_pages
        }
        confident = {k: v for k, v in confident.items() if v}
        if confident:
            table_texts = extract_tables_native(pdf_path, confident)
        if native_pages:
            trees.append(build_tree_from_native(
                str(version.document_id), str(version.version_number), native_pages,
                table_texts, parser_revision=PARSER_REVISION_NATIVE))

        # Visual path: render → olmOCR → tree; low quality → quarantine.
        if visual_pages:
            rendered_batch = render_pages_png(
                pdf_path, visual_pages,
                longest_dim_px=deps.config.olmocr.render_longest_dim_px,
            )
            client = OlmOcrClient(deps.olmocr_config)
            try:
                results = []
                for page_index in visual_pages:
                    png = rendered_batch[page_index]
                    png_cache[page_index] = png
                    render_key = (f"{tenant_prefix}/renders/page-{page_index:04d}.png")
                    deps.object_store.put_bytes(render_key, png, "image/png")
                    result = client.process_page(pdf_path, page_index, png_bytes=png)
                    if result.quality_status != "accepted":
                        repo.quarantine_page(
                            document_id=version.document_id,
                            document_version_id=version.id,
                            job_id=job.id,
                            page_index=page_index,
                            page_render_ref=render_key,
                            parse_attempts={"visual": {
                                "markdown": result.markdown,
                                "metadata": result.metadata,
                                "error": result.error}},
                            routing_diagnostics=routing[page_index],
                        )
                        session.commit()
                        continue
                    results.append(result)
                if results:
                    trees.append(build_tree_from_olmocr(
                        str(version.document_id), str(version.version_number), results,
                        parser_revision=PARSER_REVISION_OLMOCR))
            finally:
                client.close()

        if repo.pending_quarantine_count(version.id) > 0:
            # Parked for review — never silently index partial content.
            repo.update_job_state(job.id, IngestionState.REVIEW_REQUIRED)
            repo.update_version_status(version.id, IngestionState.REVIEW_REQUIRED)
            session.commit()
            raise _ReviewRequired(
                f"{repo.pending_quarantine_count(version.id)} page(s) quarantined")

        # Persist derived artifacts: canonical trees + markdown + diagnostics.
        for tree in trees:
            key = f"{tenant_prefix}/trees/{tree.parser}.json"
            import json

            deps.object_store.put_bytes(
                key, json.dumps(tree.to_dict()).encode("utf-8"), "application/json")
            md_key = f"{tenant_prefix}/trees/{tree.parser}.md"
            deps.object_store.put_bytes(md_key, tree_to_markdown(tree).encode("utf-8"),
                                       "text/markdown")
        return {"trees": trees, "routing": routing, "png_cache": png_cache,
                "visual_pages": visual_pages}
    finally:
        os.unlink(tmp.name)


def _chunk_trees(trees: list[Node], deps: PipelineDeps) -> list[Chunk]:
    chunker = StructuralChunker(
        settings=deps.config.chunker,
        token_len=deps.token_len,
    )
    if not trees:
        raise ValueError("chunking produced zero chunks")
    if len(trees) == 1:
        chunks = chunker.chunk_document(trees[0])
    else:
        # Merge page nodes from native and visual trees in true document page
        # order so chunk_order is strictly monotonic and heading_path propagates
        # across native/visual page transitions.
        all_pages: list[Node] = []
        for tree in trees:
            all_pages.extend(tree.children)
        all_pages.sort(key=lambda p: (p.page_index is None, p.page_index or 0))
        active_headings: list[str] = []
        for page_node in all_pages:
            for child in page_node.children:
                if child.heading_path:
                    active_headings = list(child.heading_path)
                elif active_headings:
                    child.heading_path = list(active_headings)
        from dataclasses import replace

        merged = replace(
            trees[0],
            parser="mixed",
            children=all_pages,
        )
        chunks = chunker.chunk_document(merged)
    if not chunks:
        raise ValueError("chunking produced zero chunks")
    return chunks


def _embed_chunks(chunks: list[Chunk], parse_out: dict, version,
                  group_id: str, tenant_prefix: str, deps: PipelineDeps,
                  session) -> list[dict]:
    """EMBEDDING stage: chunk vectors + page-image records → Milvus records."""
    backend = deps.embedding_backend
    document = session.get(Document, version.document_id)
    filename = document.display_name if document is not None else "document"
    texts = [build_document_text(c, filename=filename) for c in chunks]
    vectors = backend.embed_texts(texts)

    records: list[dict] = []
    for chunk, vector in zip(chunks, vectors):
        record = chunk.to_milvus_record(group_id)
        record["dense_vector"] = vector
        record["metadata"]["filename"] = filename
        records.append(record)

    # Page-image records: visual pages always; native pages only when enabled.
    png_cache: dict[int, bytes] = parse_out.get("png_cache", {})
    visual_pages: list[int] = parse_out.get("visual_pages", [])
    pages_for_images = list(visual_pages)
    if deps.page_images_for_native_pages:
        from .native_worker import render_page_png  # noqa: F401
        # (renders produced on demand in a future pass; see gap list)
    if png_cache and pages_for_images:
        from .embeddings import make_page_image_record

        pngs = [png_cache[i] for i in pages_for_images if i in png_cache]
        img_vectors = backend.embed_images(pngs)
        for page_index, vector in zip(
                [i for i in pages_for_images if i in png_cache], img_vectors):
            rec = make_page_image_record(
                document_id=str(version.document_id),
                document_version=str(version.version_number),
                page_index=page_index,
                render_ref=f"{tenant_prefix}/renders/page-{page_index:04d}.png",
                filename=filename,
                vector=vector,
                group_id=group_id,
            )
            img_record = rec.to_milvus_record()
            img_record["metadata"]["filename"] = filename
            records.append(img_record)
    return records


# ---------------------------------------------------------------------------
# Deletion driver (§12)
# ---------------------------------------------------------------------------


def run_deletion_job(job_id: str, *, session_factory=None,
                     deps: PipelineDeps | None = None) -> str:
    """Retry-safe cleanup: commit invisibility before external side effects."""
    from services.catalog.work_lock import document_work_lock
    owns_deps = deps is None
    deps = deps if deps is not None else build_production_deps()
    session = session_factory() if session_factory else _default_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            raise ValueError(f"job {job_id} not found")
        with document_work_lock(session, job.document_id, wait=True):
            job = session.get(Job, uuid.UUID(job_id))
            if job.state == IngestionState.DELETED:
                return job.state.value
            context = RequestContext(user_id=job.trusted_user_id,
                group_id=job.trusted_group_id, roles=tuple(job.trusted_roles or ()),
                request_id=f"job-{job.id}")
            repo = CatalogRepository(session, context)
            repo.revalidate_job_context(job)
            doc = repo.get_document_for_write(job.document_id, include_deleted=True)
            if not doc.is_deleted:
                repo.soft_delete_document(doc.id)
            job.state = IngestionState.DELETING
            job.attempt_count += 1
            job.error = None
            session.commit()  # tombstone survives every subsequent failure
            try:
                deps.index_sink.delete_document(str(doc.id), context.group_id)
                job.payload = {**job.payload, "index_deleted": True}
                session.commit()
                deps.object_store.delete_prefix(f"{context.group_id}/{doc.id}/")
                job.payload = {**job.payload, "objects_deleted": True}
                job.state = IngestionState.DELETED
                session.commit()
                return job.state.value
            except Exception as exc:
                session.rollback()
                job = session.get(Job, uuid.UUID(job_id))
                job.state = IngestionState.FAILED
                job.error = f"cleanup failed: {exc}"
                session.commit()
                raise
    finally:
        session.close()
        if owns_deps and hasattr(deps.embedding_backend, "close"):
            deps.embedding_backend.close()


def ensure_tenant(session, name: str) -> Tenant:
    """Get-or-create tenant row (used by tests and admin tooling)."""
    from sqlalchemy import select

    tenant = session.execute(
        select(Tenant).where(Tenant.name == name)).scalars().one_or_none()
    if tenant is None:
        tenant = Tenant(name=name)
        session.add(tenant)
        session.commit()
    return tenant


__all__ = [
    "IndexSink", "LoggingIndexSink", "ObjectStore", "MemoryObjectStore",
    "RustFSObjectStore", "PipelineDeps", "enqueue_ingestion_job",
    "run_ingestion_job", "run_deletion_job", "ensure_tenant",
    "PARSER_REVISION_NATIVE", "PARSER_REVISION_OLMOCR",
]
