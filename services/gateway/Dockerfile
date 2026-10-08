# gateway — auth gateway (FastAPI). CPU only.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
WORKDIR /app
COPY services/gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Shared packages: gateway code does `from services.common...`, so the
# services tree layout must exist in the image (not just gateway's files).
COPY services/__init__.py /app/services/__init__.py
COPY services/common/ /app/services/common/
COPY services/gateway/ /app/
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
