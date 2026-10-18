# retrieval — hybrid search + RRF + MiniLM rerank (FastAPI). CPU only.
# Shared code (services.common, services.catalog) is copied as a tree so
# `from services.catalog...` / `from services.common...` imports work.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=/app
WORKDIR /app
COPY services/retrieval/requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
 && pip install --no-cache-dir -r requirements.txt
COPY services/__init__.py /app/services/__init__.py
COPY services/common/ /app/services/common/
COPY services/catalog/ /app/services/catalog/
COPY services/retrieval/ /app/services/retrieval/
EXPOSE 8004
CMD ["uvicorn", "services.retrieval.main:app", "--host", "0.0.0.0", "--port", "8004"]
