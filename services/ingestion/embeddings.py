"""EmbeddingGemma 2 embedding client (spec §8).

Model: ``google/embeddinggemma-2`` (Apache 2.0), text+image configuration
(440M effective, audio encoder disabled), CPU-served via
sentence-transformers, normalize=true, 768 dims.

Task-aware asymmetric prefixes are CONTRACTUAL (§8): the wrong prefix is a
quality bug. Defaults (validated on M1, not assumed):

    Document (prose/table/list):  title: {heading_path} | text: {chunk}
    Document (code):              title: {filename or heading_path} | text: {code}
    Query (Q&A):                  task: question answering | query: {q}
    Query (code):                 task: code retrieval | query: {q}

Page renders are embedded with the vision encoder into the SAME 768-dim
space (``modality="page_image"``, ``page_start == page_end``, text holds a
page label — never OCR output).

Backends are abstracted: :class:`SentenceTransformersGemmaBackend` is the
production backend (pre-staged artifact, fail-closed when missing);
:class:`MockEmbeddingBackend` is deterministic and dependency-free for
tests/dev. NO model downloads anywhere — the production backend refuses
to resolve from upstream (local_files_only + HF_HUB_OFFLINE).
"""

from __future__ import annotations

import abc
import hashlib
import math
import os
import random
from dataclasses import dataclass, field

from .chunker import Chunk
from .config import EmbeddingSettings


class PrefixContract:
    """The contractual task-aware prefix pairs (§8)."""

    DOC_PROSE_TITLE = "title: {heading} | text: {text}"
    DOC_CODE_TITLE = "title: {heading} | text: {text}"
    QUERY_QA = "task: question answering | query: {query}"
    QUERY_CODE = "task: code retrieval | query: {query}"
    QUERY_SEARCH = "task: search result | query: {query}"  # approved alternative


def build_document_text(chunk: Chunk, *, filename: str = "") -> str:
    """Format a chunk's embedding input per the prefix contract."""
    heading = " > ".join(chunk.heading_path) if chunk.heading_path else filename
    content_type = chunk.metadata.get("content_type", "")
    if content_type == "code" or _looks_like_code_chunk(chunk):
        return PrefixContract.DOC_CODE_TITLE.format(heading=heading, text=chunk.text)
    return PrefixContract.DOC_PROSE_TITLE.format(heading=heading, text=chunk.text)


def _looks_like_code_chunk(chunk: Chunk) -> bool:
    # Chunks built from CodeBlock nodes carry content_type=code; this is a
    # backstop for chunks assembled by other paths.
    return chunk.metadata.get("content_type") == "code"


def build_query_text(query: str, *, task: str = "qa") -> str:
    """Format a query's embedding input per the prefix contract.

    task: "qa" (default) | "code" | "search".
    """
    if task == "code":
        return PrefixContract.QUERY_CODE.format(query=query)
    if task == "search":
        return PrefixContract.QUERY_SEARCH.format(query=query)
    return PrefixContract.QUERY_QA.format(query=query)


def build_page_image_label(*, filename: str, page_index: int) -> str:
    """Page label for page_image records — a label, never OCR output (§8)."""
    return f"Page {page_index + 1} of {filename or 'document'}"


@dataclass
class PageImageRecord:
    """One embedded page render (§8 page-image embeddings)."""

    record_id: str
    document_id: str
    document_version: str
    group_id: str  # set server-side at insert time
    page_index: int
    render_ref: str  # MinIO object-storage key
    label: str
    vector: list[float] = field(default_factory=list)

    def to_milvus_record(self) -> dict:
        return {
            "chunk_id": f"{self.group_id}:{self.record_id}",
            "group_id": self.group_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "parent_id": "",
            "page_start": self.page_index,
            "page_end": self.page_index,
            "chunk_order": 0,
            "text": self.label,
            "identifiers": [],
            "dense_vector": self.vector,
            "metadata": {"modality": "page_image", "render_ref": self.render_ref},
        }


class EmbeddingBackend(abc.ABC):
    """Inference boundary — the real model wires in at deploy time."""

    dimension: int = 768

    @abc.abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed pre-prefixed document texts. Returns normalized vectors."""

    @abc.abstractmethod
    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        """Embed page-render PNG bytes. Returns normalized vectors."""

    def embed_query(self, query: str, *, task: str = "qa") -> list[float]:
        return self.embed_texts([build_query_text(query, task=task)])[0]

    def embed_chunk(self, chunk: Chunk, *, filename: str = "") -> list[float]:
        return self.embed_texts([build_document_text(chunk, filename=filename)])[0]


class SentenceTransformersGemmaBackend(EmbeddingBackend):
    """Production backend: EmbeddingGemma 2 via sentence-transformers, CPU.

    Loads from ``EMBEDDING_MODEL_PATH`` (pre-staged artifact). Fails closed
    when the artifact is missing — it never downloads.
    """

    def __init__(
        self,
        settings: EmbeddingSettings | None = None,
        model_path: str | None = None,
    ) -> None:
        self.settings = settings or EmbeddingSettings()
        self.model_path = model_path or os.environ.get("EMBEDDING_MODEL_PATH", "")
        if not self.model_path:
            raise RuntimeError(
                "EMBEDDING_MODEL_PATH is not set: EmbeddingGemma 2 must be "
                "pre-staged per the artifact policy (§3). Refusing to download."
            )
        self.dimension = self.settings.dimension
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is required for the production "
                    "embedding backend."
                ) from exc
            if not os.path.isdir(self.model_path):
                raise RuntimeError(
                    f"embedding artifact missing at {self.model_path!r}: "
                    "production services fail startup rather than downloading (§3)."
                )
            # text+image config: audio encoder disabled per spec §8.
            model_kwargs: dict = {}
            if self.settings.modality_config == "text+image":
                model_kwargs["config_kwargs"] = {"audio_config": None}
            elif self.settings.modality_config == "text-only":
                model_kwargs["config_kwargs"] = {
                    "vision_config": None,
                    "audio_config": None,
                }
            self._model = SentenceTransformer(
                self.model_path,
                device="cpu",
                trust_remote_code=False,
                model_kwargs=model_kwargs,
            )
        return self._model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            texts, normalize_embeddings=self.settings.normalize,
            show_progress_bar=False,
        )
        return [self._truncate([float(v) for v in row]) for row in vectors]

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        import io

        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required to embed page images") from exc
        model = self._load()
        pil_images = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
        vectors = model.encode(
            pil_images, normalize_embeddings=self.settings.normalize,
            show_progress_bar=False,
        )
        return [self._truncate([float(v) for v in row]) for row in vectors]

    def _truncate(self, vector: list[float]) -> list[float]:
        """Matryoshka truncation — permitted only after M1 validation (§8)."""
        width = self.settings.dimension
        if width not in self.settings.allowed_matryoshka_widths:
            raise ValueError(f"unvalidated Matryoshka width: {width}")
        if width == 768 or len(vector) <= width:
            return vector
        truncated = vector[:width]
        if self.settings.normalize:
            norm = math.sqrt(sum(v * v for v in truncated)) or 1.0
            truncated = [v / norm for v in truncated]
        return truncated


class MockEmbeddingBackend(EmbeddingBackend):
    """Deterministic, dependency-free backend for tests and dev.

    Vectors are seeded by input content (stable across runs) and L2
    normalized. NOT a semantic model — never use for quality evaluation.
    """

    def __init__(self, dimension: int = 768) -> None:
        self.dimension = dimension

    def _vector(self, seed_text: str) -> list[float]:
        digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        vec = [rng.gauss(0, 1) for _ in range(self.dimension)]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._vector(f"image:{hashlib.sha256(b).hexdigest()}") for b in images]


def make_page_image_record(
    *,
    document_id: str,
    document_version: str,
    page_index: int,
    render_ref: str,
    filename: str,
    vector: list[float],
    group_id: str = "",
) -> PageImageRecord:
    """Assemble a page_image record (§8): modality tag, page label as text."""
    seed = f"{document_id}|{document_version}|page_image|{page_index}"
    record_id = "p_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
    return PageImageRecord(
        record_id=record_id,
        document_id=document_id,
        document_version=document_version,
        group_id=group_id,
        page_index=page_index,
        render_ref=render_ref,
        label=build_page_image_label(filename=filename, page_index=page_index),
        vector=vector,
    )
