"""Embedding service settings (spec §2, §8).

This CPU adapter forwards to a shared vLLM model server by default.
Local sentence-transformers inference remains an explicit opt-in.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Matryoshka widths permitted only after M1 validation (§8). 768 is the
# default; anything outside this set is a configuration error, not a
# silent fallback.
ALLOWED_WIDTHS = (768, 512, 256, 128)


class EmbeddingServiceSettings(BaseSettings):
    # EMBEDDING_ prefix matches .env.example (EMBEDDING_BACKEND,
    # EMBEDDING_MODEL_PATH, EMBEDDING_MODALITY_CONFIG, ...).
    model_config = SettingsConfigDict(env_prefix="EMBEDDING_", extra="ignore")

    # Pre-staged artifact path (read-only mount from the host registry).
    # EMBEDDING_MODEL_DIR is the legacy alias kept for .env compatibility.
    model_path: str = Field(
        default="",
        validation_alias=AliasChoices("EMBEDDING_MODEL_PATH", "EMBEDDING_MODEL_DIR"),
    )
    model_id: str = Field(default="google/embeddinggemma-2")

    # "text+image" or "text-only" modality config for the EmbeddingGemma 2
    # checkpoint. NOTE: the released google/embeddinggemma-2 repo ships ONE
    # 744M BF16 checkpoint (~1.49 GB on disk) — there are no separately
    # downloadable 270M/440M encoders; modality selection is runtime config.
    # See services/artifacts/manifests/completed/embeddinggemma-2.yaml.
    modality_config: str = Field(default="text+image")

    # Embedding width. Must be a validated Matryoshka width.
    dimension: int = Field(default=768)

    normalize: bool = Field(default=True)

    # Both applications use the same Docker vLLM pooling server.
    backend: str = Field(default="vllm")
    vllm_url: str = Field(default="http://vllm-embedding:8000/v1")
    vllm_api_key: str = Field(default="", repr=False)
    vllm_timeout_s: float = Field(default=120.0, gt=0, allow_inf_nan=False)

    # CPU serving knobs.
    batch_size: int = Field(default=32, ge=1, le=256)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    max_input_chars: int = Field(default=32768, ge=1024)

    port: int = Field(default=8003, ge=1, le=65535)

    @field_validator("dimension")
    @classmethod
    def _validate_dimension(cls, v: int) -> int:
        if v not in ALLOWED_WIDTHS:
            raise ValueError(
                f"embedding dimension {v} is not a validated Matryoshka width "
                f"{ALLOWED_WIDTHS} (§8). Refusing to silently mix widths."
            )
        return v

    @field_validator("modality_config")
    @classmethod
    def _validate_modality(cls, v: str) -> str:
        if v not in ("text+image", "text-only"):
            raise ValueError(
                f"unknown modality_config {v!r}: expected 'text+image' or 'text-only' (§8)."
            )
        return v

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        if v not in ("vllm", "st", "mock"):
            raise ValueError(f"unknown backend {v!r}: expected 'vllm', 'st' or 'mock'.")
        return v
