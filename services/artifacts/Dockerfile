# artifacts — artifact manifest tooling (validate / verify). CLI tool,
# run as a one-shot or mounted into CI. No server, no network at runtime
# except the optional --check-redirects staging step.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
WORKDIR /app
COPY services/artifacts/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/artifacts/ /app/
ENTRYPOINT ["python", "-m", "src.cli"]
RUN useradd --system --uid 10001 --create-home druid && chown -R druid:druid /app
USER druid

CMD ["--help"]
