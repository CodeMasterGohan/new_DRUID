"""EmbeddingGemma 2 CPU serving logic (spec §8).

The §8 prefix contract is owned by the ingestion sibling
(``services/ingestion/embeddings.py``) and reused here via :mod:`reuse` —
never duplicated. This module owns: input formatting through the sibling's
contract, batching, CPU concurrency control, width/normalization guards, and
the fail-closed backend lifecycle.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
import threading

from . import reuse
from .settings import EmbeddingServiceSettings

log = logging.getLogger(__name__)

# Task names accepted by POST /embed, mapped to the sibling's query tasks.
_TASK_TO_SIBLING_TASK = {
    "question_answering": "qa",
    "code_retrieval": "code",
    "search_result": "search",
}


class EmbeddingService:
    """CPU-only EmbeddingGemma 2 service (§8, §2 GPU arbitration)."""

    def __init__(self, settings: EmbeddingServiceSettings | None = None) -> None:
        self.settings = settings or EmbeddingServiceSettings()
        self._ing = reuse.load_ingestion_embeddings()
        self._backend = self._build_backend()
        self._lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._verified = False

    # -- backend lifecycle -------------------------------------------------
    def _build_backend(self):
        ing = self._ing
        if self.settings.backend == "mock":
            log.warning("embedding backend=mock: deterministic fake vectors, NOT semantic")
            return ing.MockEmbeddingBackend(dimension=self.settings.dimension)
        # Production: sentence-transformers, CPU, pre-staged artifact only.
        st_settings = ing.EmbeddingSettings(
            model_id=self.settings.model_id,
            modality_config=self.settings.modality_config,
            dimension=self.settings.dimension,
            normalize=self.settings.normalize,
        )
        return ing.SentenceTransformersGemmaBackend(
            settings=st_settings,
            model_path=self.settings.model_path or None,
        )

    @property
    def backend(self):
        return self._backend

    # -- §8 prefix contract (delegated to the sibling; byte-identical) -----
    def format_document(self, *, title: str, text: str, is_code: bool = False) -> str:
        """Format a document-side input per the contractual prefix pair."""
        contract = self._ing.PrefixContract
        template = contract.DOC_CODE_TITLE if is_code else contract.DOC_PROSE_TITLE
        return template.format(heading=title, text=text)

    def format_query(self, *, text: str, task: str = "question_answering") -> str:
        """Format a query-side input per the contractual prefix pair."""
        sibling_task = _TASK_TO_SIBLING_TASK.get(task)
        if sibling_task is None:
            raise ValueError(f"unknown task {task!r}")
        return self._ing.build_query_text(text, task=sibling_task)

    # -- embedding ----------------------------------------------------------
    def _guard_text(self, text: str) -> None:
        if len(text) > self.settings.max_input_chars:
            raise ValueError(
                f"input too long ({len(text)} chars > "
                f"{self.settings.max_input_chars}): inputs must arrive "
                "pre-chunked (≤420 tokens, §7). Refusing to silently truncate."
            )

    def _verify_vector(self, vector: list[float]) -> None:
        """Fail-closed width + normalization check (first vector only)."""
        if self._verified:
            return
        expected = self.settings.dimension
        if len(vector) != expected:
            raise RuntimeError(
                f"backend returned width {len(vector)}, expected {expected}: "
                "refusing to silently mix widths (§8 migration rule)."
            )
        if self.settings.normalize:
            norm = math.sqrt(sum(v * v for v in vector))
            if abs(norm - 1.0) > 1e-3:
                raise RuntimeError(
                    f"backend returned unnormalized vector (L2={norm:.4f}) "
                    "with normalize=true: fail closed."
                )
        self._verified = True

    def _encode_texts(self, formatted: list[str]) -> list[list[float]]:
        # sentence-transformers encode is run under a lock: CPU inference is
        # throughput-bound and the encoder is not guaranteed thread-safe.
        with self._lock:
            vectors = self._backend.embed_texts(formatted)
        for v in vectors:
            self._verify_vector(v)
        return vectors

    async def embed_texts(self, formatted: list[str]) -> list[list[float]]:
        """Embed pre-formatted texts (prefixes already applied)."""
        if not formatted:
            raise ValueError("texts must be non-empty")
        for t in formatted:
            self._guard_text(t)
        out: list[list[float]] = []
        async with self._semaphore:
            for i in range(0, len(formatted), self.settings.batch_size):
                batch = formatted[i : i + self.settings.batch_size]
                out.extend(await asyncio.to_thread(self._encode_texts, batch))
        return out

    async def embed_documents(
        self,
        items: list[dict],
        *,
        task: str = "question_answering",
    ) -> list[list[float]]:
        """Embed document-side inputs, applying the §8 document prefix."""
        formatted = [
            self.format_document(
                title=item.get("title", ""),
                text=item["text"],
                is_code=(task == "code_retrieval") or bool(item.get("is_code")),
            )
            for item in items
        ]
        return await self.embed_texts(formatted)

    async def embed_queries(
        self, queries: list[str], *, task: str = "question_answering"
    ) -> list[list[float]]:
        """Embed query-side inputs, applying the §8 query prefix."""
        formatted = [self.format_query(text=q, task=task) for q in queries]
        return await self.embed_texts(formatted)

    async def embed_image_bytes(self, image: bytes, *, page_label: str) -> dict:
        """Embed a page render into the unified 768-dim space (§8).

        Returns the vector plus the page_image record metadata. The ``text``
        field carries the page label — never OCR output.
        """
        if not image:
            raise ValueError("image bytes must be non-empty")
        async with self._semaphore:
            with self._lock:
                vectors = self._backend.embed_images([image])
        self._verify_vector(vectors[0])
        return {
            "vector": vectors[0],
            "dimension": self.settings.dimension,
            "normalized": self.settings.normalize,
            "metadata": {"modality": "page_image"},
            "text": page_label,
        }

    def describe(self) -> dict:
        """Model/card info for GET /health."""
        return {
            "model": self.settings.model_id,
            "modality_config": self.settings.modality_config,
            "dimension": self.settings.dimension,
            "normalize": self.settings.normalize,
            "backend": self.settings.backend,
            "model_path": self.settings.model_path or None,
            "model_loaded": self._verified,
            "device": "cpu",
        }


def decode_image_b64(image_b64: str) -> bytes:
    try:
        return base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_b64 is not valid base64") from exc
