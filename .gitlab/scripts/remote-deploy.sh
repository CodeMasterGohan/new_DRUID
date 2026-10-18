#!/bin/sh
# Executed exclusively on an operator-managed deployment host, via SSH.
# This script never accepts credentials and never edits .env or host volumes.
set -eu
kind=${1:?app name required}
root=${2:?absolute path required}
prefix=${3:?image prefix required}
tag=${4:?immutable commit SHA required}
operation=${5:?operation required}

case "$root" in /*) ;; *) echo "DEPLOY_PATH must be absolute" >&2; exit 2;; esac
case "$root" in *[!a-zA-Z0-9_./-]*|"") echo "Unsafe DEPLOY_PATH" >&2; exit 2;; esac
case "$prefix" in *[!a-zA-Z0-9_:./-]*|"") echo "Invalid registry image prefix" >&2; exit 2;; esac
case "$tag" in *[!a-fA-F0-9]*|"") echo "Use immutable Git commit SHA image tag" >&2; exit 2;; esac

cd "$root"
test -f deploy/docker-compose.yml || { echo "Compose config missing" >&2; exit 3; }
test -f .gitlab/images.compose.yml || { echo "image overlay missing" >&2; exit 3; }
mkdir -p .gitlab
case "$kind" in
  druid)
    file=.env
    schema=catalog
    infrastructure="postgres redis rustfs etcd milvus"
    images="catalog milvus_setup gateway document_api mcp_server web_ui embedding retrieval ingestion"
    running="gateway document_api mcp_server web_ui embedding retrieval ingestion"
    test -d services || :  # images contain source; only deployment manifests required
    ;;
  sage)
    file=deploy/.env
    schema=catalog-migrate
    infrastructure="postgres redis rustfs etcd milvus-standalone"
    images="catalog-migrate milvus_setup embedding retrieval mcp-server ingestion-worker crawler-worker scheduler devdocs-scheduler"
    running="embedding retrieval mcp-server ingestion-worker crawler-worker scheduler devdocs-scheduler"
    ;;
  *) echo "Unsupported application" >&2; exit 2;;
esac
test -f "$file" || { echo "Expected host-managed $file, never upload secrets from CI" >&2; exit 3; }
export CI_IMAGE_PREFIX="$prefix" CI_IMAGE_TAG="$tag"
# Compose reads only locally managed secrets. Shell expansion of service lists
# is intentional; they are constants above, never input from pipeline users.
compose() { docker compose --env-file "$file" -f deploy/docker-compose.yml -f .gitlab/images.compose.yml "$@"; }

case "$operation" in
  migrate)
    compose config --quiet
    # Infrastructure is explicitly started, without starting application services.
    # Remote host must already have persistent storage, networks, and models staged.
    # shellcheck disable=SC2086
    compose up -d --no-build $infrastructure
    compose run --rm --no-deps "$schema"
    compose run --rm --no-deps milvus_setup
    # Migration is approved for this exact set of prebuilt image versions.
    : > ".gitlab/migrations-approved-$tag"
    printf 'Migration/index setup passed for %s\n' "$tag"
    ;;
  deploy)
    test -f ".gitlab/migrations-approved-$tag" || {
      echo "Missing migration approval for $tag; run manual migrate job first" >&2
      exit 4
    }
    compose config --quiet
    # Pull the EXACT immutable registry-tagged images; never build on target.
    # shellcheck disable=SC2086
    compose pull $images
    if test -f .gitlab/current-image-tag; then
      old=$(cat .gitlab/current-image-tag)
      case "$old" in *[!a-fA-F0-9]*|"") echo "Invalid previous tag" >&2; exit 4;; esac
      if test "$old" != "$tag"; then
        printf '%s\n' "$old" > .gitlab/previous-image-tag
      fi
    fi
    # Prevent Compose from auto-starting one-shot schema services.
    # shellcheck disable=SC2086
    compose up -d --no-build --no-deps $running
    printf '%s\n' "$tag" > .gitlab/current-image-tag
    compose ps
    ;;
  rollback)
    test -f .gitlab/previous-image-tag || {
      echo "No prior image SHA available for rollback" >&2; exit 4
    }
    previous=$(cat .gitlab/previous-image-tag)
    case "$previous" in *[!a-fA-F0-9]*|"") echo "Invalid previous tag" >&2; exit 4;; esac
    current=$(cat .gitlab/current-image-tag || true)
    export CI_IMAGE_TAG="$previous"
    # Image-only rollback: this does NOT revert database migrations/schema.
    # Operator must verify backward schema compatibility before running.
    compose config --quiet
    # shellcheck disable=SC2086
    compose pull $images
    # shellcheck disable=SC2086
    compose up -d --no-build --no-deps $running
    printf '%s\n' "$previous" > .gitlab/current-image-tag
    if test -n "$current"; then printf '%s\n' "$current" > .gitlab/previous-image-tag; fi
    compose ps
    ;;
  status)
    compose config --quiet
    compose ps
    ;;
  gpu-up)
    test "$kind" = druid || { echo "GPU plane is DRUID-only" >&2; exit 4; }
    test -f deploy/docker-compose.gpu.yml || { echo "GPU compose missing" >&2; exit 3; }
    # The H200, NVIDIA Container Toolkit and external druid-net/cora networks
    # must be prepared by the operator. We never pull model weights into CI.
    docker compose --env-file "$file" -f deploy/docker-compose.gpu.yml up -d vllm-olmocr
    ;;
  *)
    echo "Unknown action" >&2; exit 2;;
esac
