"""Build-root pytest bootstrap.

Every service keeps its own `tests/` package (`services/<svc>/tests/`),
so the whole tree runs in ONE pytest invocation::

    python -m pytest services/ -q          # or just: python -m pytest
    (see `testpaths` in pyproject.toml)

Test modules import as `services.<svc>.tests.test_*` (unique names — the
old ImportPathMismatchError is gone). Three path notes:

* most services import `from services.<svc>...` -> needs the build root
* mcp_server tests import the real `mcp_server` package from
  `services/mcp_server/src/` (src layout) -> that dir must precede the
  build root on sys.path so `import mcp_server` never resolves to the
  (empty) `services.mcp_server` package marker.
* document_api tests (and `services/document_api/main.py` itself) import
  the service's modules top-level (`from main import create_app`,
  `from routes.documents import ...`), mirroring the Docker image where
  the service is copied flat to /app/. Only document_api uses these
  unqualified names, so its dir goes first.
"""
import os
import sys

_BUILD_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (
    os.path.join(_BUILD_ROOT, "services", "document_api"),
    os.path.join(_BUILD_ROOT, "services", "mcp_server", "src"),
    _BUILD_ROOT,
):
    if _p not in sys.path:
        sys.path.insert(0, _p)
