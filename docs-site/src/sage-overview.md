# New_sage Overview

## What it is

New_sage is a **local-first Context7 alternative**: a self-hosted documentation
retrieval system that gives a coding assistant fresh, version-pinned library
documentation over MCP, without sending queries to a third party and without
relying on stale training-data cutoffs.

The primary flow:

```
assistant: "how do I do X with library L vN?"
  → resolve-library-id("L")            → canonical library record + known versions
  → get-library-docs(id, version=N, topic="X")
  → version-pinned doc passages with provenance (library, version, page URL, section)
```

## The Context7 trick, kept and extended

Context7's value is not a giant pre-indexed corpus: it resolves a library name
to its canonical docs source, then fetches version-specific docs on demand.
New_sage keeps that trick and adds what a hosted service cannot:

- **A persistent local versioned index.** Every `(library, version)` is crawled
  at most once and then served from the index indefinitely. Repeat queries
  never re-crawl.
- **Hybrid retrieval instead of fetch-and-hope.** Dense embeddings +
  BM25 + exact identifier matching, fused with RRF and reranked with MiniLM.
- **Version pinning that survives re-crawls.** Re-crawls insert new immutable
  revision rows rather than mutating indexed rows, so anything already retrieved
  stays reproducible.

The MCP surface mirrors Context7's two tools exactly
(`resolve-library-id`, `get-library-docs`), so a client already configured for
Context7 can point at New_sage as a drop-in replacement. See
[sage-mcp-tools.html](sage-mcp-tools.html) for the full tool reference.

## Key properties

| Property | Value |
|---|---|
| Serving model | FastMCP, Streamable HTTP, mounted at `/mcp` |
| Authentication | **None.** Single-user personal infrastructure; the host firewall / Tailscale interface is the security boundary. Do not expose it to the public internet. |
| GPU requirement | **None.** EmbeddingGemma 2 and MiniLM are CPU-served. Runs on any spare box. |
| Corpus | Public documentation only (DevDocs docsets, MDN, tldr, versioned official docs, READMEs) |
| Offline-first | Every model and dependency ships with a manifest (pinned revision, sha256, license); services fail startup rather than download at runtime |
| Deployment | Single `docker compose` stack |
| Retrieval | Milvus hybrid: dense (768-dim EmbeddingGemma 2, HNSW) + server-side BM25 + identifier match → RRF → MiniLM rerank over top 50 |
| Chunking | Heading-based, 420-token target with 48-token overlap; code blocks are never split |

## Build status

The build is complete and the full test suite is green (**399 passed,
2 skipped, 0 failed** as of 2026-10-09). The following still need James's
hardware or sign-off before the system can serve real traffic:

- **Model weights are not staged.** The `model-artifacts` volume needs the
  EmbeddingGemma 2 checkpoint and the MiniLM reranker + tokenizer copied in
  before first boot (services fail startup otherwise — this is deliberate).
- **The seed corpus has not been ingested.** DevDocs/MDN/tldr ingestion runs
  against the deployment target.
- **Eval calibration is pending.** The BM25 branch is measured
  (recall@1 = 0.940 on a 50-question offline corpus); dense + rerank numbers
  need the weights and live infra.

## New_sage vs new_DRUID

They share the same stack philosophy (offline-first artifacts, Milvus hybrid
retrieval, Postgres catalog, MinIO snapshots), but they solve different problems:

| | New_sage | new_DRUID |
|---|---|---|
| Purpose | Public library documentation for coding assistants | Enterprise private-document knowledge system (RAG over PDFs) |
| Corpus | Public docs: HTML sites, docsets, READMEs | Proprietary documents: PDFs via pdftext/olmOCR |
| Auth / tenancy | None — one user, one corpus | JWT, tenants, ACLs, quotas |
| GPU | Not required | Needed for olmOCR visual PDF parsing |
| Ingestion input | HTML/docs-sites, git checkouts | PDFs |
| MCP tools | `resolve-library-id`, `get-library-docs` (Context7-compatible) | Document search/Q&A tools |

Rule of thumb: if the question is "how do I use library X version N," that is
New_sage. If the question is "what does this private document say," that is
new_DRUID. See [druid-overview.html](druid-overview.html) for the new_DRUID
side.
