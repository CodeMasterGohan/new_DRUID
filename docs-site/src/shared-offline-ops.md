# Shared offline-first operations

How new_DRUID and New_sage stay offline in production. Grounded in the
Dockerfiles, `docker-compose*.yml`, and `.env.example` in each build tree.

## Environment switches

Every service runs with these set (see `.env.example` and the vLLM olmOCR
service in `docker-compose.gpu.yml`):

```bash
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

With both set, Hugging Face Hub and the transformers library never attempt a
network fetch. Any code path that would download a model fails immediately
instead of reaching out.

## Read-only model mounts

The compose stacks mount the artifact registry read-only:

- new_DRUID: the host model tree (e.g. `$MODEL_DIR`) mounted read-only; the
  FP8 olmOCR checkpoint must already be staged at
  `$MODEL_DIR/allenai/olmOCR-2-7B-1025-FP8`.
- New_sage: the `/artifacts` volume mounted read-only, holding `models/`
  (EmbeddingGemma 2, MiniLM) and `seed-corpus/` (DevDocs digest, MDN and
  tldr-pages checkouts).

Services load weights from the mount. Nothing writes to the registry at
runtime — staging is a separate, operator-driven step.

## Staging workflow

1. **Validate manifest** — the staging validator checks the source against
   the allowlist, requires HTTPS, and requires an immutable revision pin
   (see [Artifact & model policy](shared-artifact-policy.html)).
2. **Verify checksums** — sha256 of every file under the artifact root must
   match the manifest before the artifact is accepted.
3. **Mount** — the registry is mounted read-only into the service
   containers; `internal_location` in the manifest is the source of truth
   for where the verifier looks.

## Missing artifacts: fail closed

Startup assertions abort the service instead of downloading:

- **Embedding service** — checksum verification under the EmbeddingGemma 2
  root; any missing file or mismatch aborts startup.
- **Retrieval / reranker** — same check for the MiniLM root.
- **Seed ingestion (New_sage)** — verifies the MDN and tldr-pages checkouts
  resolve to the pinned commit SHAs and the DevDocs digest matches; any
  drift aborts the seed job before a single chunk is embedded.
- **vLLM olmOCR service (new_DRUID)** — the FP8 checkpoint must already be
  staged at the expected `$MODEL_DIR` path; without it, the service fails
  to start.

Production never downloads. If an artifact is missing or its checksums
don't match, the service exits at startup — loudly, and before serving
traffic.
