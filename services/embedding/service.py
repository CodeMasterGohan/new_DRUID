"""EmbeddingGemma 2 API adapter and optional local serving logic (spec §8).

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
from collections import OrderedDict
from contextlib import nullcontext
import logging
import math
import threading

from . import reuse
from .settings import EmbeddingServiceSettings
from .vllm_backend import VllmEmbeddingBackend

log = logging.getLogger(__name__)

# Task names accepted by POST /embed, mapped to the sibling's query tasks.
_TASK_TO_SIBLING_TASK = {
    "question_answering": "qa",
    "code_retrieval": "code",
    "search_result": "search",
}


class EmbeddingService:
    """Preserve the prefix/API contract while sharing vLLM inference."""

    def __init__(
        self,
        settings: EmbeddingServiceSettings | None = None,
        *,
        cache_size: int = 512,
    ) -> None:
        self.settings = settings or EmbeddingServiceSettings()
        self._ing = reuse.load_ingestion_embeddings()
        self._backend = self._build_backend()
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._verified = False

    # -- backend lifecycle -------------------------------------------------
    def _build_backend(self):
        ing = self._ing
        if self.settings.backend == "mock":
            log.warning("embedding backend=mock: deterministic fake vectors, NOT semantic")
            return ing.MockEmbeddingBackend(dimension=self.settings.dimension)
        if self.settings.backend == "vllm":
            return VllmEmbeddingBackend(
                model_id=self.settings.model_id,
                base_url=self.settings.vllm_url,
                api_key=self.settings.vllm_api_key,
                timeout_s=self.settings.vllm_timeout_s,
                dimension=self.settings.dimension,
                normalize=self.settings.normalize,
                modality_config=self.settings.modality_config,
            )
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
        # Only the local encoder needs serialization. Let vLLM batch concurrent
        # HTTP requests; the semaphore still bounds adapter concurrency.
        guard = nullcontext() if self.settings.backend == "vllm" else self._lock
        with guard:
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

        # Deduplicate across the request and check the bounded LRU cache so
        # repeated queries/chunks do not burn extra CPU forward passes.
        unique_missing: list[str] = []
        missing_seen: set[str] = set()
        resolved: dict[str, list[float]] = {}
        if self._cache_size > 0:
            with self._cache_lock:
                for t in formatted:
                    cached = self._cache.get(t)
                    if cached is not None:
                        self._cache.move_to_end(t)
                        resolved[t] = list(cached)
                    elif t not in missing_seen:
                        missing_seen.add(t)
                        unique_missing.append(t)
        else:
            for t in formatted:
                if t not in missing_seen:
                    missing_seen.add(t)
                    unique_missing.append(t)

        if unique_missing:
            encoded_missing: list[list[float]] = []
            async with self._semaphore:
                for i in range(0, len(unique_missing), self.settings.batch_size):
                    batch = unique_missing[i : i + self.settings.batch_size]
                    encoded_missing.extend(await asyncio.to_thread(self._encode_texts, batch))
            for t, vec in zip(unique_missing, encoded_missing):
                resolved[t] = vec
            if self._cache_size > 0:
                with self._cache_lock:
                    for t, vec in zip(unique_missing, encoded_missing):
                        self._cache[t] = tuple(vec)
                        self._cache.move_to_end(t)
                        while len(self._cache) > self._cache_size:
                            self._cache.popitem(last=False)

        return [list(resolved[t]) for t in formatted]

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
            vectors = await asyncio.to_thread(self._encode_images, [image])
        self._verify_vector(vectors[0])
        return {
            "vector": vectors[0],
            "dimension": self.settings.dimension,
            "normalized": self.settings.normalize,
            "metadata": {"modality": "page_image"},
            "text": page_label,
        }

    def _encode_images(self, images: list[bytes]) -> list[list[float]]:
        guard = nullcontext() if self.settings.backend == "vllm" else self._lock
        with guard:
            return self._backend.embed_images(images)

    def describe(self) -> dict:
        """Model/card info for GET /health."""
        return {
            "model": self.settings.model_id,
            "modality_config": self.settings.modality_config,
            "dimension": self.settings.dimension,
            "normalize": self.settings.normalize,
            "backend": self.settings.backend,
            "model_path": (self.settings.model_path or None) if self.settings.backend == "st" else None,
            "vllm_url": self.settings.vllm_url if self.settings.backend == "vllm" else None,
            "model_loaded": self._verified,
            "device": "remote" if self.settings.backend == "vllm" else "cpu",
        }


def decode_image_b64(image_b64: str) -> bytes:
    try:
        return base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_b64 is not valid base64") from exc
