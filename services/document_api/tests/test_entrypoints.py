"""The RQ entry-point contract with services.ingestion.pipeline.

document_api enqueues by dotted-path string so its image stays free of the
ingestion dependency tree. These tests assert the strings actually resolve
to the worker entry points (skipped when the ingestion package is not
importable in the test env).
"""

from __future__ import annotations

import importlib

import pytest

import job_queue

ingestion = pytest.importorskip(
    "services.ingestion.pipeline",
    reason="ingestion package not importable in this environment",
)


def _resolve(dotted: str):
    module_name, _, attr = dotted.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def test_ingestion_entrypoint_resolves():
    fn = _resolve(job_queue.INGESTION_ENTRYPOINT)
    assert fn is ingestion.run_ingestion_job


def test_deletion_entrypoint_resolves():
    fn = _resolve(job_queue.DELETION_ENTRYPOINT)
    assert fn is ingestion.run_deletion_job
