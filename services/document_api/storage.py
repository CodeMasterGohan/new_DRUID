"""Tenant-scoped object storage boundary (spec §5).

The production implementation is RustFS (S3-compatible); the in-memory fake
is for tests and local dev. Storage keys are tenant-scoped
(``{group_id}/{document_id}/{version_id}/...``) so no key can escape its
tenant's prefix. Original filenames are display metadata only and never
appear in storage keys.
"""

from __future__ import annotations

import abc
import datetime as dt


class ObjectStore(abc.ABC):
    """Minimal object-store interface used by the document API."""

    @abc.abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> None:
        """Store bytes under ``key``."""

    @abc.abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Fetch bytes stored under ``key``; raise KeyError when missing."""

    @abc.abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete every key starting with ``prefix``; return the count."""

    @abc.abstractmethod
    def presigned_get_url(self, key: str, expires_seconds: int = 3600) -> str | None:
        """Return a time-limited download URL, or None when unsupported."""


class MemoryObjectStore(ObjectStore):
    """In-memory store for tests and local dev (no persistence)."""

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

    def presigned_get_url(self, key: str, expires_seconds: int = 3600) -> str | None:
        # No URL signing in memory; callers fall back to returning the key.
        return None


class RustFSObjectStore(ObjectStore):
    """Production S3-compatible object store backed by RustFS (via the minio Python client); the bucket must already exist."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool = False,
    ) -> None:
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError("minio package is required for RustFSObjectStore") from exc
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._bucket = bucket

    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> None:
        import io

        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type or "application/octet-stream",
        )

    def get_bytes(self, key: str) -> bytes:
        return self._client.get_object(self._bucket, key).read()

    def delete_prefix(self, prefix: str) -> int:
        count = 0
        for obj in self._client.list_objects(
            self._bucket, prefix=prefix, recursive=True
        ):
            self._client.remove_object(self._bucket, obj.object_name)
            count += 1
        return count

    def presigned_get_url(self, key: str, expires_seconds: int = 3600) -> str | None:
        return self._client.presigned_get_object(
            self._bucket, key, expires=dt.timedelta(seconds=expires_seconds)
        )


def storage_key(group_id: str, document_id: str, version_id: str, name: str) -> str:
    """Build a tenant-scoped storage key.

    ``group_id`` is the trusted tenant name from the request context —
    never a caller-supplied value.
    """
    return f"{group_id}/{document_id}/{version_id}/{name}"


__all__ = [
    "MemoryObjectStore",
    "RustFSObjectStore",
    "ObjectStore",
    "storage_key",
]
