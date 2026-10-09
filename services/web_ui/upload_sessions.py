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
    created_at: float = field(default_factory=time.time)
    chunks: dict[int, bytes] = field(default_factory=dict)

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
        self, filename: str, content_type: str, total_chunks: int
    ) -> UploadSession:
        self._gc()
        session = UploadSession(
            session_id=uuid.uuid4().hex,
            filename=filename,
            content_type=content_type,
            total_chunks=total_chunks,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> UploadSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session.created_at > self._ttl:
            del self._sessions[session_id]
            return None
        return session

    def add_chunk(self, session_id: str, index: int, data: bytes) -> UploadSession:
        session = self.get(session_id)
        if session is None:
            raise KeyError("unknown or expired upload session")
        if not 0 <= index < session.total_chunks:
            raise ValueError(f"chunk index {index} out of range")
        session.chunks[index] = data
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
