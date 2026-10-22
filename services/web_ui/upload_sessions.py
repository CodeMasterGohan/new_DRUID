"""Chunked, resumable upload sessions (spec §16: Upload page).

The browser splits a large file into chunks and POSTs each one; the final
``complete`` call assembles the bytes server-side and forwards ONE multipart
upload to the gateway (which enforces the authoritative size/quota limits).
A dropped connection resumes by asking which chunk indexes are present.

Sessions live in-process with a TTL. This is correct for the single-replica
compose deployment; a multi-replica UI would need shared temp storage.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class UploadSession:
    session_id: str
    filename: str
    content_type: str
    total_chunks: int
    owner_user_id: str | None = None
    owner_group_id: str | None = None
    max_bytes: int | None = None
    created_at: float = field(default_factory=time.time)
    chunks: dict[int, bytes] = field(default_factory=dict)
    current_bytes: int = 0

    def matches_owner(
        self,
        *,
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
    ) -> bool:
        if (
            owner_group_id is not None
            and self.owner_group_id is not None
            and self.owner_group_id != owner_group_id
        ):
            return False
        if (
            owner_user_id is not None
            and self.owner_user_id is not None
            and self.owner_user_id != owner_user_id
        ):
            return False
        return True

    def is_complete(self) -> bool:
        return len(self.chunks) == self.total_chunks and all(
            i in self.chunks for i in range(self.total_chunks)
        )

    def assemble(self) -> bytes:
        return b"".join(self.chunks[i] for i in range(self.total_chunks))


class UploadSessionStore:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, UploadSession] = {}

    def create(
        self,
        filename: str,
        content_type: str,
        total_chunks: int,
        *,
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
        max_bytes: int | None = None,
    ) -> UploadSession:
        self._gc()
        session = UploadSession(
            session_id=uuid.uuid4().hex,
            filename=filename,
            content_type=content_type,
            total_chunks=total_chunks,
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
            max_bytes=max_bytes,
        )
        self._sessions[session.session_id] = session
        return session

    def get(
        self,
        session_id: str,
        *,
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
    ) -> UploadSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session.created_at > self._ttl:
            del self._sessions[session_id]
            return None
        if not session.matches_owner(
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
        ):
            return None
        return session

    def add_chunk(
        self,
        session_id: str,
        index: int,
        data: bytes,
        *,
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
    ) -> UploadSession:
        session = self.get(
            session_id,
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
        )
        if session is None:
            raise KeyError("unknown or expired upload session")
        if not 0 <= index < session.total_chunks:
            raise ValueError(f"chunk index {index} out of range")
        prev_len = len(session.chunks.get(index, b""))
        new_bytes = session.current_bytes - prev_len + len(data)
        if session.max_bytes is not None and new_bytes > session.max_bytes:
            raise ValueError("accumulated chunks exceed maximum upload size")
        session.chunks[index] = data
        session.current_bytes = new_bytes
        return session

    def pop(self, session_id: str) -> UploadSession | None:
        return self._sessions.pop(session_id, None)

    def _gc(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items() if now - s.created_at > self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]


__all__ = ["UploadSession", "UploadSessionStore"]
