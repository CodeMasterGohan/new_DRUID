# mcp_server — FastMCP server (Streamable HTTP, authenticated). CPU only.
# The stdio adapter (src/mcp_server/stdio_adapter.py) runs as a separate
# local process, not in this image.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    PYTHONPATH=/app/src
WORKDIR /app
COPY services/mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/mcp_server/src/ ./src/
RUN useradd -r -u 10001 druid && chown -R druid:druid /app
USER druid
EXPOSE 8002
# Required env: JWT_SECRET (HS256) or JWT_PUBLIC_KEY_PEM (RS256), JWT_ISSUER, JWT_AUDIENCE,
# RETRIEVAL_URL, DOCUMENT_API_URL, INTERNAL_SECRET (HMAC secret for signing
# downstream identity headers).
# Optional: MCP_ALLOWED_ORIGINS (comma-separated),
# MCP_DOWNSTREAM_TIMEOUT_SECONDS (default 30), DRUID_MCP_DEV_FAKE_CLIENTS=1
# (tests/dev only — never in production).
CMD ["uvicorn", "--factory", "mcp_server.server:create_app", "--host", "0.0.0.0", "--port", "8002"]
