"""Reuse the ingestion sibling's embedding contract (spec §8).

Single source of truth: ``services/ingestion/embeddings.py`` owns the
contractual task-aware prefix pairs, the page-image record shape, and the
backend abstractions (``SentenceTransformersGemmaBackend``,
``MockEmbeddingBackend``). This module loads that file WITHOUT executing
``services/ingestion/__init__.py`` — the package init imports pdftext and
other heavy ingestion dependencies that this CPU-only service must not
carry (and that are not installed in its Docker image).

The sibling's transitive imports (``chunker``, ``config``, ``tree``,
``identifiers``) are stdlib-only, so they load safely. The Docker image
copies exactly those files (see Dockerfile); no duplication of the prefix
logic lives here — a test asserts byte-equality with the sibling's
formatters.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_MODULE_NAME = "services.ingestion.embeddings"
_PACKAGE_NAME = "services.ingestion"
_PARENT_NAME = "services"

# The five sibling files this service reuses (all stdlib-only).
_SIBLING_FILES = (
    "embeddings.py",
    "chunker.py",
    "config.py",
    "tree.py",
    "identifiers.py",
)


def _build_root() -> Path:
    # services/embedding/reuse.py -> parents[2] is the build root.
    return Path(__file__).resolve().parents[2]


def _ensure_stub_package(name: str, path: Path) -> None:
    """Register a package module with ``__path__`` but no executed __init__."""
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


def load_ingestion_embeddings():
    """Return the sibling ``services.ingestion.embeddings`` module.

    The module is loaded once and cached in ``sys.modules``. Raises
    RuntimeError if the sibling tree is absent (fail closed — the prefix
    contract is mandatory, never optional).
    """
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]

    build_root = _build_root()
    ingestion_dir = build_root / "services" / "ingestion"
    missing = [f for f in _SIBLING_FILES if not (ingestion_dir / f).is_file()]
    if missing:
        raise RuntimeError(
            "embedding service requires the ingestion sibling's contract "
            f"files, missing: {missing} (expected under {ingestion_dir}). "
            "The §8 prefix contract is mandatory — refusing to start with a "
            "duplicated or absent contract."
        )

    _ensure_stub_package(_PARENT_NAME, build_root / "services")
    _ensure_stub_package(_PACKAGE_NAME, ingestion_dir)

    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, ingestion_dir / "embeddings.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create import spec for {_MODULE_NAME}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module
