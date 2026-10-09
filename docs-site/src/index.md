# DRUID & SAGE documentation

Two sibling systems on one retrieval stack. **New_sage** is a personal
Context7 alternative: fast retrieval over *public* library documentation —
no login, no auth. **new_DRUID** is a self-hosted enterprise RAG + MCP
knowledge system over *your own* documents, running on a single NVIDIA H200.

## When to use which

| | New_sage | new_DRUID |
|---|---|---|
| Purpose | Personal Context7 alternative — pull public library docs into coding sessions | Enterprise local-first RAG + MCP over your own documents |
| Corpus | Public docs: DevDocs image, MDN, tldr-pages seed; on-demand crawler for the rest | Your documents: native PDF parsing via pdftext, visual pages via olmOCR 2, table detection |
| Authentication | None — public corpus only | GitHub OAuth + JWT; `admin` role with cross-tenant operator views |
| Retrieval | Milvus hybrid dense+BM25 · EmbeddingGemma 2 · MiniLM reranker | Same retrieval stack (RRF + MiniLM) |
| Interface | MCP server (8 tools) | MCP server + web UI (Upload, Documents, Review queue, Quarantine, Search) |
| Deployment target | Same H200 box, alongside new_DRUID | Single H200, Docker Compose, offline-first artifacts |

## Quickstart

- [New_sage overview](sage-overview.html) — what it does, the MCP tools, and the public-docs corpus
- [new_DRUID overview](druid-overview.html) — what it does, the web UI, and the document pipeline
- [Artifact & model policy](shared-artifact-policy.html) — offline-first rules, manifests, approved models
- [Offline-first operations](shared-offline-ops.html) — env switches, read-only mounts, staging workflow
- [New_sage deployment](sage-deployment.html) — staging artifacts and standing up the box
- [new_DRUID deployment runbook](druid-deployment.html) — the single-H200 compose deployment
