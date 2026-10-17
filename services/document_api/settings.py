"""document_api configuration (spec §4 quotas, §12 storage).

Environment variable names follow the shared ``.env.example`` at the build
root (no ``DRUID_`` prefix). Object-store credentials use the RustFS
server-side names ``RUSTFS_ACCESS_KEY`` / ``RUSTFS_SECRET_KEY``.
"""

from __future__ import annotations

from pydantic.fields import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic.aliases import AliasChoices


class DocumentApiSettings(BaseSettings):
    """document_api configuration from the environment."""

    model_config = SettingsConfigDict(extra="ignore")
    druid_auth_mode: str = "legacy"

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

    # --- object storage (RustFS, tenant-scoped keys, §5) ---
    object_store: str = "rustfs"  # "rustfs" | "memory"
    rustfs_endpoint: str = "rustfs:9000"
    rustfs_access_key: str = Field(
        default="druid",
        validation_alias=AliasChoices("RUSTFS_ACCESS_KEY"),
    )
    rustfs_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("RUSTFS_SECRET_KEY"),
    )
    rustfs_bucket: str = "druid-artifacts"
    rustfs_secure: bool = False
    rustfs_presign_expiry_seconds: int = 3600

    # --- upload limits (§4 quotas) ---
    max_upload_mb: int = 200
    max_pages_per_document: int = 2000

    # --- two-phase upload tickets (§5) ---
    # TTL for the single-use ticket issued by POST /v1/documents/register.
    upload_ticket_ttl_seconds: int = 900

    # --- quota limits mirrored from the gateway (§4, admin views) ---
    # Must match the gateway's QUOTA_INGEST_PAGES_PER_DAY / QUOTA_QUERY_QPS:
    # the admin quotas endpoint reads usage from the gateway's Redis
    # counters and reports it against these configured defaults.
    quota_ingest_pages_per_day: int = 1000
    quota_query_qps: int = 10
