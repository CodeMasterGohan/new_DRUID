"""document_api configuration (spec §4 quotas, §12 storage).

Environment variable names follow the shared ``.env.example`` at the build
root (no ``DRUID_`` prefix). MinIO client credentials reuse the server-side
``MINIO_ROOT_USER`` / ``MINIO_ROOT_PASSWORD`` names so no new variables are
introduced (``MINIO_ACCESS_KEY`` / ``MINIO_SECRET_KEY`` are also accepted).
"""

from __future__ import annotations

from pydantic.fields import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic.aliases import AliasChoices


class DocumentApiSettings(BaseSettings):
    """document_api configuration from the environment."""

    model_config = SettingsConfigDict(extra="ignore")

    # --- catalog (PostgreSQL) ---
    database_url: str = (
        "postgresql+psycopg://druid:change-me@postgres:5432/druid_catalog"
    )

    # --- internal identity (gateway → this service, §4) ---
    # HMAC secret the gateway uses to sign X-Druid-* headers. Empty means
    # misconfigured: every request fails closed (see internal_auth).
    internal_secret: str = ""

    # --- job queue (Redis-backed RQ, §12) ---
    redis_url: str = "redis://redis:6379/0"
    ingest_queue: str = "ingest"
    delete_queue: str = "delete"

    # --- object storage (MinIO, tenant-scoped keys, §5) ---
    object_store: str = "minio"  # "minio" | "memory"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = Field(
        default="druid",
        validation_alias=AliasChoices("MINIO_ACCESS_KEY", "MINIO_ROOT_USER"),
    )
    minio_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD"),
    )
    minio_bucket: str = "druid-artifacts"
    minio_secure: bool = False
    minio_presign_expiry_seconds: int = 3600

    # --- upload limits (§4 quotas) ---
    max_upload_mb: int = 200
    max_pages_per_document: int = 2000
