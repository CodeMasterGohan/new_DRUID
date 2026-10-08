# milvus_setup — one-shot job: creates the versioned collection, BM25
# function, and indexes per spec §9, then exits. Run after Milvus is healthy.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
WORKDIR /app
COPY services/milvus_setup/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Keep the package layout: create_collection.py uses package-relative
# imports, so it must run as milvus_setup.create_collection, not as a
# top-level module.
COPY services/milvus_setup/ /app/milvus_setup/
CMD ["python", "-m", "milvus_setup.create_collection"]
