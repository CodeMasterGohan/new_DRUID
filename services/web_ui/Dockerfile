# web_ui — tenant document-management UI (FastAPI + Jinja2 + htmx). CPU only.
# Shared upload limits are included; the UI talks to the
# gateway over HTTP with the user's own Bearer <redacted>
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    PYTHONPATH=/app
WORKDIR /app
COPY services/web_ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/web_ui/ /app/
COPY services/common/ /app/services/common/
COPY services/__init__.py /app/services/__init__.py
RUN useradd --system --uid 10001 --create-home druid && chown -R druid:druid /app
USER druid

EXPOSE 8005
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
