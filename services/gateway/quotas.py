"""Per-tenant quotas enforced at the gateway (spec §4).

Defaults (spec §4, open decision #2 — validate with a throughput burn-in):

* ``ingest_pages_per_day``: 1000
* ``query_qps``: 10
* ``max_upload_mb``: 200
* ``max_pages_per_document``: 2000

Quota exhaustion returns 429 with a ``Retry-After`` hint — never silent
degradation.

Two store implementations share the :class:`QuotaStore` interface:

* :class:`InMemoryQuotaStore` — single-process; used for tests and local dev.
* :class:`RedisQuotaStore` — the production store (Redis is already the
  queue broker / cache, so no new infrastructure).
"""

from __future__ import annotations

import abc
import datetime as dt
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaConfig:
    # Field names map to the shared .env.example: QUOTA_INGEST_PAGES_PER_DAY,
    # QUOTA_QUERY_QPS, MAX_UPLOAD_MB, MAX_PAGES_PER_DOCUMENT.
    ingest_pages_per_day: int = 1000
    query_qps: int = 10
    max_upload_mb: int = 200
    max_pages_per_document: int = 2000

    @classmethod
    def from_env(cls, env=None) -> "QuotaConfig":
        env = env or os.environ
        return cls(
            ingest_pages_per_day=int(env.get("QUOTA_INGEST_PAGES_PER_DAY", 1000)),
            query_qps=int(env.get("QUOTA_QUERY_QPS", 10)),
            max_upload_mb=int(env.get("MAX_UPLOAD_MB", 200)),
            max_pages_per_document=int(env.get("MAX_PAGES_PER_DOCUMENT", 2000)),
        )


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float | None = None
    scope: str = ""  # e.g. "ingestion:tenant-a" — for logs, never for auth


class QuotaStore(abc.ABC):
    """Quota backend interface. All methods are per-tenant (group_id)."""

    @abc.abstractmethod
    def consume_ingestion_pages(
        self, tenant: str, pages: int, today: dt.date | None = None
    ) -> QuotaDecision:
        """Atomically consume ``pages`` of the tenant's daily ingestion quota."""

    @abc.abstractmethod
    def check_query(self, tenant: str, now: float | None = None) -> QuotaDecision:
        """Sliding-window check of the tenant's query QPS quota."""


class InMemoryQuotaStore(QuotaStore):
    """Single-process quota store. Correct for tests and local dev only."""

    def __init__(self, config: QuotaConfig) -> None:
        self.config = config
        self._ingest: dict[str, tuple[dt.date, int]] = {}
        self._queries: dict[str, deque[float]] = defaultdict(deque)

    def consume_ingestion_pages(
        self, tenant: str, pages: int, today: dt.date | None = None
    ) -> QuotaDecision:
        if pages < 1:
            raise ValueError("pages must be positive")
        today = today or dt.date.today()
        limit = self.config.ingest_pages_per_day
        day, used = self._ingest.get(tenant, (today, 0))
        if day != today:
            used = 0
        if used + pages > limit:
            return QuotaDecision(
                allowed=False,
                limit=limit,
                remaining=max(0, limit - used),
                retry_after_seconds=_seconds_until_midnight(),
                scope=f"ingestion:{tenant}",
            )
        self._ingest[tenant] = (today, used + pages)
        return QuotaDecision(
            allowed=True, limit=limit, remaining=limit - used - pages,
            scope=f"ingestion:{tenant}",
        )

    def check_query(self, tenant: str, now: float | None = None) -> QuotaDecision:
        now = time.time() if now is None else now
        limit = self.config.query_qps
        window = self._queries[tenant]
        cutoff = now - 1.0
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(0.0, 1.0 - (now - window[0]))
            return QuotaDecision(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after_seconds=retry_after,
                scope=f"query:{tenant}",
            )
        window.append(now)
        return QuotaDecision(
            allowed=True, limit=limit, remaining=limit - len(window),
            scope=f"query:{tenant}",
        )


class RedisQuotaStore(QuotaStore):
    """Production quota store backed by Redis (atomic Lua scripts)."""

    _INGEST_LUA = """
    local current = tonumber(redis.call('GET', KEYS[1]) or '0')
    local used = current + tonumber(ARGV[1])
    if used > tonumber(ARGV[3]) then return {0, current} end
    redis.call('SET', KEYS[1], used, 'EX', ARGV[2])
    return {1, used}
    """
    _QUERY_LUA = """
    local key, now, window, limit = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
    local count = redis.call('ZCARD', key)
    if count < limit then
      redis.call('ZADD', key, now, now .. ':' .. ARGV[4])
      redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
      return {1, limit - count - 1}
    end
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    return {0, 0, window - (now - tonumber(oldest[2]))}
    """

    def __init__(self, redis_client, config: QuotaConfig) -> None:
        self.redis = redis_client
        self.config = config
        self._ingest_sha = None
        self._query_sha = None

    def _ingest_script(self):
        if self._ingest_sha is None:
            self._ingest_sha = self.redis.script_load(self._INGEST_LUA)
        return self._ingest_sha

    def _query_script(self):
        if self._query_sha is None:
            self._query_sha = self.redis.script_load(self._QUERY_LUA)
        return self._query_sha

    def consume_ingestion_pages(
        self, tenant: str, pages: int, today: dt.date | None = None
    ) -> QuotaDecision:
        if pages < 1:
            raise ValueError("pages must be positive")
        today = today or dt.date.today()
        limit = self.config.ingest_pages_per_day
        key = f"druid:quota:ingest:{tenant}:{today.isoformat()}"
        allowed, used = self.redis.evalsha(self._ingest_script(), 1, key, pages, 2 * 86400, limit)
        used = int(used)
        if not allowed:
            return QuotaDecision(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after_seconds=_seconds_until_midnight(),
                scope=f"ingestion:{tenant}",
            )
        return QuotaDecision(
            allowed=True, limit=limit, remaining=limit - used,
            scope=f"ingestion:{tenant}",
        )

    def check_query(self, tenant: str, now: float | None = None) -> QuotaDecision:
        now = time.time() if now is None else now
        limit = self.config.query_qps
        key = f"druid:quota:query:{tenant}"
        unique = f"{os.getpid()}:{time.monotonic_ns()}"
        result = self.redis.evalsha(
            self._query_script(), 1, key, now, 1.0, limit, unique
        )
        allowed = bool(result[0])
        if allowed:
            return QuotaDecision(
                allowed=True, limit=limit, remaining=int(result[1]),
                scope=f"query:{tenant}",
            )
        retry_after = max(0.0, float(result[2]))
        return QuotaDecision(
            allowed=False, limit=limit, remaining=0,
            retry_after_seconds=retry_after, scope=f"query:{tenant}",
        )


def _seconds_until_midnight() -> float:
    now = dt.datetime.now().astimezone()
    midnight = (now + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (midnight - now).total_seconds()


def quota_store_from_env(config: QuotaConfig) -> QuotaStore:
    """Build the production store when ``REDIS_URL`` is set."""
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        import redis as redis_lib

        return RedisQuotaStore(
            redis_lib.Redis.from_url(redis_url, decode_responses=False), config
        )
    return InMemoryQuotaStore(config)


__all__ = [
    "InMemoryQuotaStore",
    "QuotaConfig",
    "QuotaDecision",
    "QuotaStore",
    "RedisQuotaStore",
    "quota_store_from_env",
]
