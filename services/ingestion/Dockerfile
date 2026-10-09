# ingestion — pdftext workers (RQ). CPU only.
# HARD CONSTRAINT: PDFium is not thread-safe — workers use processes only,
# never threads (see spec §5). Do not add threaded executors here.
# Shared code (services.common, services.catalog) is copied as a tree so
# `from services.catalog...` / `from services.common...` imports work.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    PYTHONPATH=/app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/ingestion/requirements.txt .
# torch from the CPU index FIRST (mirrors the embedding service): the
# default PyPI torch wheel bundles CUDA and must never land here (§2 GPU
# arbitration — the H200 belongs to vLLM/olmOCR).
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
 && pip install --no-cache-dir -r requirements.txt
# Full services tree (not a flat copy): the RQ worker must import the
# dotted-path entry points `services.ingestion.pipeline.run_ingestion_job`
# / `run_deletion_job` that the document API enqueues, and pipeline.py does
# `from services.catalog...` / `from services.common...` imports.
# PYTHONPATH=/app makes the `services.*` tree importable for both.
COPY services/__init__.py /app/services/__init__.py
COPY services/common/ /app/services/common/
COPY services/catalog/ /app/services/catalog/
COPY services/ingestion/ /app/services/ingestion/
# Models mount read-only from the host artifact registry (see .env.example).
# Shell form (NOT exec form): Docker does not expand ${...} in exec-form
# CMD, so the exec form would hand rq the literal string "${REDIS_URL}".
CMD rq worker --url "${REDIS_URL}" ingest delete
