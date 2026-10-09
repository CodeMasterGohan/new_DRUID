# Shared artifact & model policy

The offline-first model and artifact policy shared by new_DRUID and New_sage.
Grounded in the specs and the artifact manifests at
`services/artifacts/manifests/` in each build tree.

## The offline-first rule

Production services load **only** pre-staged artifacts from the internal
registry. They **fail startup rather than downloading anything at runtime**.
There is no network fetch path in production — missing artifacts are a hard
startup error, not a fetch.

## Artifact manifests

Every approved artifact has one YAML manifest (one per model, plus
New_sage's seed-corpus manifest). A manifest records:

- `artifact_name` — e.g. `allenai/olmOCR-2-7B-1025-FP8`
- `upstream_source` — the canonical HTTPS origin (Hugging Face or ghcr.io)
- `upstream_revision` — an **immutable** pin (commit hash / image digest).
  Branch-style revisions (`main`, `master`, `latest`, `stable`, `dev`,
  `develop`, `head`) are rejected.
- `internal_location` — where the staged artifact lives on the box
  (`/models/...` or `/artifacts/...`); this is the source of truth for the
  verifier.
- `license` — every approved model and dependency needs a recorded license.
- `files` — per-file `sha256` checksums.
- `approval_record` — the signed approval ID.

Template entries (zeroed `sha256` values, `<immutable-commit-hash>`
placeholders) are only acceptable during drafting. They must be completed
during controlled staging before the manifest is approved.

## Approved models

Verified against the manifests in
`new_druid/build/services/artifacts/manifests/` and
`new_sage/build/services/artifacts/manifests/`:

| Model | Used by | Purpose | License | Manifest / approval |
|---|---|---|---|---|
| `allenai/olmOCR-2-7B-1025-FP8` | new_DRUID | Visual PDF pages (vLLM on H200) | Apache-2.0 | `olmocr-2-7b-1025-fp8.yaml` · APPROVAL-2026-001 |
| `google/embeddinggemma-2` | both | Embeddings, text+image config | Apache-2.0 | `embeddinggemma-2-text-image.yaml` (DRUID) / `embeddinggemma-2.yaml` (SAGE) · APPROVAL-2026-002 |
| `cross-encoder/ms-marco-MiniLM-L6-v2` | both | Reranker | Apache-2.0 | `minilm-l6-v2.yaml` · APPROVAL-2026-003 |

New_sage additionally pins its seed corpus in `seed-corpus.yaml`: the
DevDocs image digest, the MDN content git SHA, and the tldr-pages git SHA
(seed-corpus pins signed under APPROVAL-2026-101). New_sage's two model
manifests are byte-identical copies of new_DRUID's completed drafts, and the
models may share one `/models` tree when the pins are identical.

## Prohibited sources

Downloads from the following — or from any other unapproved endpoint — are
prohibited:

- `modelscope.cn` (and subdomains)
- `hf-mirror.com`, `hf-mirror.aliyuncs.com`
- any Chinese mirror repository
- any host not on the staging allowlist

`ghcr.io` is allowlisted **only** for the DevDocs image. The staging
validator enforces this (`artifact_manifest.py`:
`DEFAULT_PROHIBITED_HOST_PATTERNS`), rejecting prohibited hosts and
non-HTTPS sources at manifest validation time.

## Staging workflow (summary)

1. **Validate manifest** — source allowlist, HTTPS scheme, immutable revision.
2. **Verify checksums** — every file's sha256 under the artifact root must
   match the manifest.
3. **Mount** — the registry is mounted read-only into the service containers.

Full details: [Offline-first operations](shared-offline-ops.html).
