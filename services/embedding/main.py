"""EmbeddingGemma 2 API adapter — shared vLLM inference by default.

Endpoints:
  POST /embed       — embed texts with the §8 contractual task prefixes
  POST /embed_image — embed a page render into the unified vector space
  GET  /health      — model/card status (fail-closed)

Startup verifies the configured backend with an embedding request. An
unavailable shared server fails startup without falling back to local inference.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .service import EmbeddingService, decode_image_b64
from .settings import EmbeddingServiceSettings

log = logging.getLogger(__name__)

service: EmbeddingService | None = None


class Task(str, Enum):
    question_answering = "question_answering"
    code_retrieval = "code_retrieval"
    search_result = "search_result"


class DocumentInput(BaseModel):
    text: str = Field(min_length=1)
    title: str = Field(default="")
    is_code: bool = Field(default=False)


class EmbedRequest(BaseModel):
    # Document-side inputs (prefixes applied server-side per §8).
    documents: list[DocumentInput] = Field(default_factory=list)
    # Query-side inputs (prefixes applied server-side per §8).
    queries: list[str] = Field(default_factory=list)
    task: Task = Field(default=Task.question_answering)
    modality: Literal["text"] = Field(default="text")


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    dimension: int
    normalized: bool
    model: str
    task: str


class EmbedImageRequest(BaseModel):
    image_b64: str = Field(default="", min_length=1)
    # object_key is accepted by the schema but not yet wired: page renders
    # live in RustFS and resolution is a deploy-time integration.
    object_key: str = Field(default="")
    page_label: str = Field(min_length=1)


class EmbedImageResponse(BaseModel):
    vector: list[float]
    dimension: int
    normalized: bool
    metadata: dict
    text: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    settings = EmbeddingServiceSettings()
    log.info(
        "starting embedding service: model=%s modality=%s dim=%d backend=%s",
        settings.model_id,
        settings.modality_config,
        settings.dimension,
        settings.backend,
    )
    try:
        service = EmbeddingService(settings)
        # Warmup forces the backend to load NOW: missing/invalid artifacts
        # fail startup loudly instead of on first request (§3).
        await service.embed_texts(["druid warmup"])
    except Exception:
        log.critical("embedding service failed startup — refusing to run degraded", exc_info=True)
        raise
    log.info("embedding service ready")
    yield
    service = None


app = FastAPI(title="new_DRUID embedding service", version="2.1.0", lifespan=lifespan)


def _svc() -> EmbeddingService:
    if service is None:
        raise HTTPException(status_code=503, detail="embedding backend not ready")
    return service


@app.get("/health")
def health() -> dict:
    if service is None:
        raise HTTPException(status_code=503, detail="embedding backend not ready")
    info = service.describe()
    info["status"] = "ok" if info["model_loaded"] else "degraded"
    return info


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    svc = _svc()
    if not req.documents and not req.queries:
        raise HTTPException(status_code=400, detail="documents and queries are both empty")
    started = time.monotonic()
    log.info(
        "embed start: documents=%d queries=%d task=%s",
        len(req.documents),
        len(req.queries),
        req.task.value,
    )
    try:
        vectors: list[list[float]] = []
        if req.documents:
            vectors.extend(
                await svc.embed_documents(
                    [d.model_dump() for d in req.documents],
                    task=req.task.value,
                )
            )
        if req.queries:
            vectors.extend(await svc.embed_queries(req.queries, task=req.task.value))
    except ValueError as exc:
        log.warning("embed rejected → 400: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.error("embed failed → 500: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    elapsed_ms = (time.monotonic() - started) * 1000.0
    log.info(
        "embed finish: %d vectors in %.1fms task=%s",
        len(vectors),
        elapsed_ms,
        req.task.value,
    )
    return EmbedResponse(
        vectors=vectors,
        dimension=svc.settings.dimension,
        normalized=svc.settings.normalize,
        model=svc.settings.model_id,
        task=req.task.value,
    )


@app.post("/embed_image", response_model=EmbedImageResponse)
async def embed_image(req: EmbedImageRequest) -> EmbedImageResponse:
    svc = _svc()
    if req.object_key:
        log.warning(
            "embed_image → 501: object_key resolution not wired (page_label=%r)",
            req.page_label,
        )
        raise HTTPException(
            status_code=501,
            detail="object_key resolution (RustFS fetch) is not wired in this "
            "build; send image_b64 without object_key. See gap list.",
        )
    started = time.monotonic()
    log.info("embed_image start: page_label=%r", req.page_label)
    try:
        image = decode_image_b64(req.image_b64)
    except ValueError as exc:
        log.warning("embed_image rejected → 400: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = await svc.embed_image_bytes(image, page_label=req.page_label)
    except ValueError as exc:
        log.warning("embed_image rejected → 400: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.error("embed_image failed → 500: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    elapsed_ms = (time.monotonic() - started) * 1000.0
    log.info(
        "embed_image finish: page_label=%r in %.1fms",
        req.page_label,
        elapsed_ms,
    )
    return EmbedImageResponse(
        vector=result["vector"],
        dimension=result["dimension"],
        normalized=result["normalized"],
        metadata=result["metadata"],
        text=result["text"],
    )
