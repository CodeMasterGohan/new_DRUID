# embedding — EmbeddingGemma 2 service (sentence-transformers). CPU ONLY.
# §2 GPU arbitration: the H200 belongs to vLLM/olmOCR. This image must never
# request GPU resources. torch is installed from the CPU index.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=8
WORKDIR /app
COPY services/embedding/requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
 && pip install --no-cache-dir -r requirements.txt
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
# Model artifacts mount READ-ONLY at /models (host registry). Missing model
# dir must fail startup loudly — never download at runtime.
EXPOSE 8003
CMD ["uvicorn", "services.embedding.main:app", "--host", "0.0.0.0", "--port", "8003"]
