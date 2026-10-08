# catalog — document catalog library + Alembic migration runner.
# Not a long-running server: this image runs migrations, then exits.
# The services tree layout is preserved because alembic/env.py does
# `from services.catalog.models import Base`.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY services/catalog/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY services/__init__.py /app/services/__init__.py
COPY services/common/ /app/services/common/
COPY services/catalog/ /app/services/catalog/
WORKDIR /app/services/catalog
CMD ["alembic", "upgrade", "head"]
