# ingestion — pdftext workers (RQ). CPU only.
# HARD CONSTRAINT: PDFium is not thread-safe — workers use processes only,
# never threads (see spec §5). Do not add threaded executors here.
# Shared code (services.common, services.catalog) is copied as a tree so
# `from services.catalog...` / `from services.common...` imports work.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/ingestion/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/__init__.py /app/services/__init__.py
COPY services/common/ /app/services/common/
COPY services/catalog/ /app/services/catalog/
COPY services/ingestion/ /app/
# Models mount read-only from the host artifact registry (see .env.example).
CMD ["rq", "worker", "--url", "${REDIS_URL}", "ingest", "delete"]
