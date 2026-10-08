"""document_api service package (spec §11 API surface).

FastAPI service behind the gateway. Identity comes ONLY from the gateway's
HMAC-signed ``X-Druid-*`` headers (see :mod:`internal_auth`); it is never
accepted from request bodies, query params, or unvalidated headers (§4).
"""

from __future__ import annotations
