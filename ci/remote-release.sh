#!/usr/bin/env bash
# Runs on the TARGET Docker host. The pipeline invokes this over SSH.
# Secrets stay in $DEPLOY_ROOT/.env on the host; no database/volume rollback.
set -euo pipefail
MODE="${1:?mode: apply|rollback|smoke}"
DEPLOY_ROOT="${2:?absolute deployment root}"
PROJECT="${3:?druid|sage}"
TAG="${4:-}"
REGISTRY="${5:-}"
RUST_SAGE="${6:-0}"
ACK_SCHEMA="${7:-0}"
[[ "${DEPLOY_ROOT}" =~ ^/[A-Za-z0-9_./-]+$ && "${DEPLOY_ROOT}" != *..* ]] || { echo 'Unsafe deployment path' >&2; exit 2; }
[[ "${PROJECT}" == druid || "${PROJECT}" == sage ]] || exit 2
[[ "${TAG}" =~ ^[0-9a-f]{40}$ ]] || { echo "Full commit SHA required" >&2; exit 2; }
[[ "${REGISTRY}" =~ ^[A-Za-z0-9._:/-]+$ ]] || { echo "Unsafe registry path" >&2; exit 2; }
[[ "${RUST_SAGE}" =~ ^[01]$ ]] || exit 2
[[ "${ACK_SCHEMA}" =~ ^[01]$ ]] || exit 2
command -v docker >/dev/null
command -v python3 >/dev/null
command -v flock >/dev/null
mkdir -p "${DEPLOY_ROOT}"
exec 8>"${DEPLOY_ROOT}/deployment.lock"
flock -n 8 || { echo "Another deployment is running" >&2; exit 1; }
[[ -s "${DEPLOY_ROOT}/.env" ]] || { echo "Preprovision ${DEPLOY_ROOT}/.env (do not copy CI template secrets)" >&2; exit 1; }
cd "${DEPLOY_ROOT}"
if [[ "${MODE}" == rollback ]]; then
  [[ -s previous_release ]] || { echo "No previous release; cannot rollback" >&2; exit 1; }
  TAG="$(cat previous_release)"
  [[ "${TAG}" =~ ^[0-9a-f]{40}$ ]] || exit 2
  [[ -f "releases/${TAG}/release.metadata" ]] || exit 1
  # No implicit DB/collection migrations on rollback. Operator MUST verify
  # backwards-compatible schema independently; application rollback != data restore.
  IFS=' ' read -r REGISTRY RUST_SAGE < "releases/${TAG}/release.metadata"
  [[ "${REGISTRY}" =~ ^[A-Za-z0-9._:/-]+$ && "${RUST_SAGE}" =~ ^[01]$ ]] || exit 2
fi
RELEASE="${DEPLOY_ROOT}/releases/${TAG}"
[[ -f "${RELEASE}/deploy/docker-compose.yml" && -f "${RELEASE}/ci/render-compose-images.py" ]] || {
  echo "Release files missing: ${TAG}" >&2; exit 1;
}
[[ -e "${RELEASE}/.env" ]] || ln -s "${DEPLOY_ROOT}/.env" "${RELEASE}/.env"
python3 "${RELEASE}/ci/render-compose-images.py" "${PROJECT}" "${REGISTRY}" "${TAG}" \
  $( [[ "${RUST_SAGE}" == 1 ]] && echo --rust-sage ) > "${RELEASE}/deploy/docker-compose.images.yml"
compose() {
  docker compose --env-file "${DEPLOY_ROOT}/.env" \
    -f "${RELEASE}/deploy/docker-compose.yml" \
    -f "${RELEASE}/deploy/docker-compose.images.yml" "$@"
}
compose config --quiet
if [[ "${MODE}" == smoke ]]; then
  compose ps --all
  # Some one-shot migration containers legitimately exit successfully.
  # This is infrastructure visibility, NOT full Keycloak/MCP/AI smoke testing.
  exit 0
fi
if [[ "${MODE}" == apply ]]; then
  [[ "${ACK_SCHEMA}" == 1 ]] || {
    echo "Deployment requires acknowledged backup/schema readiness (ACK_SCHEMA=1)." >&2; exit 1;
  }
fi
if [[ "${PROJECT}" == druid ]]; then
  docker network inspect druid-net >/dev/null 2>&1 || {
    echo "Create/pre-provision shared GPU Docker network druid-net before deployment." >&2; exit 1;
  }
  infra=(postgres redis rustfs etcd milvus)
  apps=(gateway document_api mcp_server web_ui embedding retrieval ingestion)
else
  infra=(postgres redis rustfs etcd milvus-standalone)
  apps=(embedding retrieval mcp-server ingestion-worker crawler-worker scheduler devdocs-scheduler)
fi
# A deployment/rollback never updates GPU images: GPU is separately approved.
compose pull --quiet
if [[ "${MODE}" == apply ]]; then
  compose up -d --no-build --wait --wait-timeout 300 "${infra[@]}"
  if [[ "${PROJECT}" == druid ]]; then
    compose run --rm --no-deps catalog
  else
    compose run --rm --no-deps catalog-migrate
  fi
  compose run --rm --no-deps milvus_setup
  # Application services depend on one-shot successful migrations in Sage.
  compose up -d --no-build --wait --wait-timeout 300 "${apps[@]}"
else
  # Preserve current database/vector schemas and volumes during rollback.
  compose up -d --no-deps --no-build --wait --wait-timeout 300 "${apps[@]}"
fi
previous=""
[[ -f current_release ]] && previous="$(cat current_release)"
[[ -z "${previous}" || "${previous}" =~ ^[0-9a-f]{40}$ ]] || exit 2
if [[ "${previous}" != "${TAG}" ]]; then
  if [[ -n "${previous}" ]]; then printf '%s\n' "${previous}" > previous_release.tmp; mv previous_release.tmp previous_release; fi
  printf '%s\n' "${TAG}" > current_release.tmp
  mv current_release.tmp current_release
fi
if [[ "${MODE}" == apply ]]; then printf '%s %s\n' "${REGISTRY}" "${RUST_SAGE}" > "${RELEASE}/release.metadata"; fi
printf 'Activated %s commit %s; no application secrets or data were overwritten.\n' "${PROJECT}" "${TAG}"
