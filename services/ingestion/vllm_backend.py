"""Use the shared model server with ingestion's chunk/query prefix helpers."""

from services.embedding.vllm_backend import VllmEmbeddingBackend as RemoteBackend

from .embeddings import EmbeddingBackend


class VllmEmbeddingBackend(RemoteBackend, EmbeddingBackend):
    """Remote text/image encoding plus the ingestion EmbeddingBackend contract."""
