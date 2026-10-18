#!/bin/sh
# GitLab job-side SSH transport. Requires File-type masked CI variables:
# DEPLOY_SSH_KEY_FILE and DEPLOY_KNOWN_HOSTS_FILE.
set -eu
kind=${1:?app}
target_host=${2:?host}
target_user=${3:?user}
target_path=${4:?absolute path}
image_prefix=${5:?registry image prefix}
image_sha=${6:?commit sha}
operation=${7:?deploy or migrate or rollback or status}
: "${DEPLOY_SSH_KEY_FILE:?Define GitLab File CI variable DEPLOY_SSH_KEY_FILE}"
: "${DEPLOY_KNOWN_HOSTS_FILE:?Define GitLab File CI variable DEPLOY_KNOWN_HOSTS_FILE}"

case "$target_host" in *[!a-zA-Z0-9_.:-]*|"") echo "Unsafe deploy host" >&2; exit 2;; esac
case "$target_user" in *[!a-zA-Z0-9_-]*|"") echo "Unsafe deploy username" >&2; exit 2;; esac
case "$target_path" in /*) ;; *) echo "Target path must be absolute" >&2; exit 2;; esac
case "$target_path" in *[!a-zA-Z0-9_./-]*|"") echo "Unsafe target path" >&2; exit 2;; esac
case "$image_prefix" in *[!a-zA-Z0-9_:./-]*|"") echo "Unsafe registry path" >&2; exit 2;; esac
case "$image_sha" in *[!a-fA-F0-9]*|"") echo "Expected immutable commit SHA" >&2; exit 2;; esac

mkdir -p "${HOME}/.ssh"
chmod 700 "${HOME}/.ssh"
cp "$DEPLOY_SSH_KEY_FILE" "${HOME}/.ssh/gitlab_deploy_key"
cp "$DEPLOY_KNOWN_HOSTS_FILE" "${HOME}/.ssh/known_hosts"
chmod 600 "${HOME}/.ssh/gitlab_deploy_key"
chmod 600 "${HOME}/.ssh/known_hosts"
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o ConnectTimeout=15 -i ${HOME}/.ssh/gitlab_deploy_key -o UserKnownHostsFile=${HOME}/.ssh/known_hosts"
server="${target_user}@${target_host}"
# The host's .env stays intact; only versioned Compose manifests and remote
# deployment script are copied. Never SCP CI secrets or model weights.
# shellcheck disable=SC2086
ssh $SSH_OPTS "$server" "mkdir -p '$target_path/deploy' '$target_path/.gitlab'"
# shellcheck disable=SC2086
scp $SSH_OPTS deploy/docker-compose.yml "$server:$target_path/deploy/docker-compose.yml"
# shellcheck disable=SC2086
scp $SSH_OPTS .gitlab/images.compose.yml "$server:$target_path/.gitlab/images.compose.yml"
if [ "$kind" = druid ]; then
  # shellcheck disable=SC2086
  scp $SSH_OPTS deploy/docker-compose.gpu.yml "$server:$target_path/deploy/docker-compose.gpu.yml"
fi
# shellcheck disable=SC2086
ssh $SSH_OPTS "$server" "sh -s -- '$kind' '$target_path' '$image_prefix' '$image_sha' '$operation'" < .gitlab/scripts/remote-deploy.sh
