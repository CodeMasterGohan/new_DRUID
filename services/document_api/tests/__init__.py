"""Make the build tree and the document_api package importable for tests."""

from __future__ import annotations

import os
import sys

SERVICE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUILD_ROOT = os.path.abspath(os.path.join(SERVICE_DIR, "..", ".."))

for _p in (SERVICE_DIR, BUILD_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
