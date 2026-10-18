#!/usr/bin/env bash
# GitLab CI runner transport. Target host must already be provisioned.
# Required GitLab protected, environment-scoped variables:
# DEPLOY_HOST, DEPLOY_USER, DEPLOY_ROOT, DEPLOY_SSH_KEY (file),
# DEPLOY_KNOWN_HOSTS (file), and registry pull credentials on TARGET.
set -euo pipefail
MODE="${1:?apply|rollback|smoke}"
PROJECT="${2:?druid|sage}"
[[ "${MODE}" =~ ^(apply|rollback|smoke)$ && "${PROJECT}" =~ ^(druid|sage)$ ]] || exit 2
: "${DEPLOY_HOST:?}"
: "${DEPLOY_USER:?}"
: "${DEPLOY_ROOT:?}"
: "${DEPLOY_SSH_KEY:?GitLab file variable path}"
: "${DEPLOY_KNOWN_HOSTS:?GitLab known_hosts file variable path}"
: "${CI_REGISTRY_IMAGE:?}"
: "${CI_COMMIT_SHA:?}"
[[ "${DEPLOY_HOST}" =~ ^[A-Za-z0-9.:-]+$ ]] || { echo 'Invalid host' >&2; exit 2; }
[[ "${DEPLOY_USER}" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || exit 2
[[ "${DEPLOY_ROOT}" =~ ^/[A-Za-z0-9_./-]+$ && "${DEPLOY_ROOT}" != *..* ]] || exit 2
[[ "${CI_COMMIT_SHA}" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ -s "${DEPLOY_SSH_KEY}" && -s "${DEPLOY_KNOWN_HOSTS}" ]] || { echo 'Missing SSH key/known hosts file variables' >&2; exit 2; }
chmod 600 "${DEPLOY_SSH_KEY}"
SSH_OPTS=(
  -o BatchMode=yes -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes -o ConnectTimeout=15
  -o UserKnownHostsFile="${DEPLOY_KNOWN_HOSTS}"
  -i "${DEPLOY_SSH_KEY}"
)
TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
RUST="${SAGE_USE_RUST_IMAGES:-0}"
SCHEMA="${ACK_SCHEMA_CHANGES:-0}"
[[ "${RUST}" =~ ^[01]$ && "${SCHEMA}" =~ ^[01]$ ]] || exit 2
if [[ "${MODE}" == apply ]]; then
  # Only committed/tracked source; .env, secrets and working tree are excluded.
  git archive --format=tar HEAD |
    ssh "${SSH_OPTS[@]}" "${TARGET}" \
      "mkdir -p '${DEPLOY_ROOT}/releases/${CI_COMMIT_SHA}' && tar -xf - -C '${DEPLOY_ROOT}/releases/${CI_COMMIT_SHA}'"
fi
ssh "${SSH_OPTS[@]}" "${TARGET}" bash -s -- \
  "${MODE}" "${DEPLOY_ROOT}" "${PROJECT}" "${CI_COMMIT_SHA}" \
  "${CI_REGISTRY_IMAGE}" "${RUST}" "${SCHEMA}" < ci/remote-release.sh
