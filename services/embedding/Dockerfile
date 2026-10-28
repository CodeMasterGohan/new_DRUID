# Lightweight EmbeddingGemma 2 API adapter. Inference uses shared vLLM.
# Local CPU sentence-transformers is an explicit build/runtime opt-in.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=8
WORKDIR /app
COPY services/embedding/requirements*.txt ./
ARG INSTALL_LOCAL_EMBEDDINGS=false
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 torchvision==0.29.1 \
      && pip install --no-cache-dir -r requirements-local.txt; \
    fi
COPY services/__init__.py /app/services/__init__.py
# §8 prefix contract lives in the ingestion sibling (single source of truth).
# Copy ONLY the stdlib-only contract files — never services/ingestion/__init__.py
# (it imports pdftext and other heavy deps this CPU image must not carry).
# services/embedding/reuse.py loads them without executing the package init.
COPY services/ingestion/embeddings.py /app/services/ingestion/embeddings.py
COPY services/ingestion/config.py /app/services/ingestion/config.py
COPY services/ingestion/chunker.py /app/services/ingestion/chunker.py
COPY services/ingestion/tree.py /app/services/ingestion/tree.py
COPY services/ingestion/identifiers.py /app/services/ingestion/identifiers.py
COPY services/embedding/ /app/services/embedding/
# Only the optional local backend needs a read-only artifact mount.
RUN useradd --system --uid 10001 --create-home druid && chown -R druid:druid /app
USER druid

EXPOSE 8003
CMD ["uvicorn", "services.embedding.main:app", "--host", "0.0.0.0", "--port", "8003"]
