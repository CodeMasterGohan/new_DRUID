# new_DRUID — Overview

new_DRUID is a **self-hosted, multi-tenant, local-first RAG + MCP document knowledge system**. Organizations run it on their own hardware: documents are ingested, parsed, chunked, embedded, and indexed locally, and retrieval is exposed through an authenticated REST API, a tenant web UI, and an MCP server for AI agents. Nothing leaves the box; there is no external model provider and no cloud dependency.

**Status:** the build (all services, wiring, and unit/integration tests) is complete and green — latest full sweep: **273 passed, 4 skipped** (the skips are live-Milvus tests). Model weights are **not staged**, full ingestion has **not run**, and live-infrastructure tests have **not run**; those need the target hardware (see [Deployment](druid-deployment.html)).

## Deployment target

One machine with a single **NVIDIA H200 (141 GB HBM3e)**. All services except vLLM run on CPU/host RAM. vLLM serving the olmOCR vision model is the **only GPU consumer** — embedding and the reranker run on CPU by policy, not by accident (see [Architecture](druid-architecture.html)).

## Key properties

- **Local-first / offline-first.** Production services load pre-staged model artifacts from a read-only host mount and talk only to approved internal services. Missing artifacts fail startup loudly — they never download at runtime. `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are baked into every image.
- **Multi-tenant by construction.** Tenant context comes only from the validated JWT (the `group_id` claim, resolved to a tenant row), never from document content, tool arguments, or headers. Every catalog query is tenant-scoped; cross-tenant access returns 404.
- **Fail-closed everywhere.** No partial visibility: inactive, deleting, failed, or unreviewed document versions are hard-filtered at the catalog layer and never returned as searchable.
- **Retrieved text is untrusted data.** Responses carry provenance tags (document, filename, page range) and a trust marker; retrieved content can never alter identity, authorization, or instructions.
- **Approved artifacts only.** Every model and dependency has a manifest (`services/artifacts/manifests/`) with a pinned upstream revision, sha256 checksums, and license. ModelScope, Chinese mirrors, and unapproved download endpoints are prohibited; see [Artifact Policy](shared-artifact-policy.html).
- **Quotas and review.** Per-tenant ingest pages/day and query QPS quotas enforced at the gateway; pages the parser cannot confidently route land in a review queue for a human (reviewer role) to accept, re-route, or reject.

## new_DRUID vs New_sage

| | new_DRUID | New_sage |
|---|---|---|
| Purpose | Enterprise multi-tenant document knowledge | Personal-use Context7 alternative (docs corpus) |
| Auth | Required — JWT gateway, roles, per-tenant isolation | None (public-docs corpus) |
| Corpus | Tenant-private documents | Public documentation |
| Ingestion | Full pipeline (pdftext → page router → olmOCR 2) | Public doc crawling |
| Access surfaces | REST API, MCP (8 tools), web UI with admin views | MCP only |
| Compliance | Quotas, review queue, deletion §12, backups, restore drills | Not applicable |

They share infrastructure patterns (FastMCP, Milvus hybrid retrieval, MiniLM reranker, offline-first artifacts, single-H200 target), but they solve different problems: **new_DRUID guards private enterprise documents; New_sage serves public documentation with no identity at all.**

## Where to go next

- [Architecture & services](druid-architecture.html) — service map and data flow
- [MCP tools](druid-mcp-tools.html) — the 8 agent tools
- [Web UI](druid-web-ui.html) — the tenant UI guide
- [API reference](druid-api-reference.html) — document & admin routes
- [Deployment](druid-deployment.html) — runbook, GPU arbitration, model staging
- [Operations](druid-operations.html) — backups, quotas, logging, job queue
- [Troubleshooting](druid-troubleshooting.html) — known failures and fixes
- [Configuration](druid-configuration.html) — every env var
