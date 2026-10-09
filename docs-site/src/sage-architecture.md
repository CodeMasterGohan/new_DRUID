# New_sage Architecture

## System layout

```
                          ┌─────────────┐
                          │  MCP client │  (coding assistant)
                          └──────┬──────┘
                                 │  Streamable HTTP, no auth
                          ┌──────▼──────┐
                          │ FastMCP     │  resolve-library-id
                          │ server      │  get-library-docs
                          └──────┬──────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
       ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
       │ Retrieval   │    │ Catalog     │    │ Crawl queue │
       │ service     │    │ (Postgres)  │    │ (Redis)     │
       └──────┬──────┘    └─────────────┘    └──────┬──────┘
              │                                    │
              ▼                                    ▼
       ┌─────────────┐                      ┌─────────────┐
       │ Milvus      │                      │ Ingestion   │
       │ hybrid      │                      │ workers     │
       │ dense+BM25  │                      │ (trafilatura│
       │ +identifier │                      │  → MD)      │
       └─────────────┘                      └──────┬──────┘
                                                   ▼
                                            ┌─────────────┐
                                            │ RustFS      │
                                            │ raw snapshots│
                                            └─────────────┘
```

## Services

The stack is defined in `deploy/docker-compose.yml` (all services on the
`sage-net` network, plain HTTP, no auth). Container ports are the contract;
host ports can be remapped for side-by-side operation with new_DRUID.

| Service | Port | Role |
|---|---|---|
| `mcp-server` | 8000 | FastMCP server, mounts the Streamable HTTP app at `/mcp`. Exposes the two tools. Origin validation only (DNS-rebinding protection for browser clients); no auth. |
| `retrieval` | 8001 | `POST /retrieve` + `GET /health`. Version resolution, three-branch Milvus search, RRF fusion, MiniLM rerank, token-budget truncation. |
| `embedding` | 8002 | CPU EmbeddingGemma 2 service: `POST /embed` (query-side), `POST /embed_documents` (doc-side), `GET /health`. Applies the PrefixContract prompt strings server-side. |
| `ingestion-worker` | — | RQ worker on the `sage-ingest` queue: chunk → embed → Milvus upsert → catalog → version activation. Scalable (`--scale ingestion-worker=4`). |
| `crawler-worker` | — | RQ worker on the `sage-crawl` queue: polite fetch, version detection, repo-clone pinning, RustFS snapshots. |
| `postgres` | 5432 | Catalog: libraries, versions, pages, crawl jobs. Migrated by the `catalog-migrate` one-shot (Alembic). |
| `redis` | 6379 | Crawl/ingest job queues + hot `get-library-docs` response cache. |
| `rustfs` | 9000 / 9001 | Raw HTML snapshots and seed checkouts — the rebuild source if Milvus ever needs reconstructing. |
| `milvus-standalone` | 19530 / 9091 | `sage_chunks` collection; shares RustFS (bucket `sage-milvus`) for object storage. Needs the internal `etcd` sidecar (embedded etcd was removed in Milvus 2.6.x). |
| `catalog-migrate` | — | One-shot: runs Alembic migrations, then exits. |
| `milvus_setup` | — | One-shot: provisions the `sage_chunks` collection + indexes idempotently. |

The `eval` and `artifacts` components are not runtime services: `eval` is an
offline harness for the retrieval-quality gate, and `artifacts` holds the
model/seed-corpus manifests and validator CLI.

## How a query flows

A `get-library-docs` call with a `topic` takes this path:

1. **mcp-server** validates arguments (pydantic) and resolves the
   `library_id` against the resolver table (hand-written `libraries_seed.json`
   in the build; a catalog-backed loader exists as the production path).
2. It calls the retrieval service (`RETRIEVAL_URL`, default
   `http://retrieval:8001`) with `library_id`, `version`, `topic`,
   `top_k=50`, and the token budget.
3. **retrieval** resolves the version: exact version string → its version id;
   unresolvable or omitted → the `is_latest` version, with
   `version_fallback: true`.
4. The query text is embedded by the **embedding** service using the
   `web_search_query` PrefixContract prompt (byte-identical to new_DRUID's).
5. Three Milvus branches run with **one identical `(library_id, version_id)`
   predicate**:
   - **Dense:** EmbeddingGemma 2 vectors, HNSW, cosine.
   - **BM25:** server-side `text_to_bm25` function over the indexed `text`
     field (chunk text + headings weighted ×2 + code identifiers weighted ×3).
   - **Identifier:** exact/alias match on library name boosts all chunks of
     the resolved library — this is why "react hooks useEffect" can never
     return Vue docs no matter what dense retrieval thinks.
6. **RRF fusion** (`k=60`) over the three branches, identifier hits get a
   fixed +1.0 boost, then **MiniLM reranks the top 50** on CPU.
7. Finalists are truncated to the token budget (always keeping ≥1 passage),
   and each passage is wrapped in explicit provenance delimiters:
   `<<<sage:passage library="..." version="..." is_latest="..."
   url="..." section="..." trust="untrusted-document-content">>>`.

With no `topic`, retrieval skips embedding and rerank and runs in overview
mode: top chunks from pages whose heading breadcrumbs indicate
getting-started/overview (fallback: lowest-`ordinal` chunks in the version).

## The Milvus `sage_chunks` collection

Provisions by `milvus_setup` per the schema contract:

```
chunk_id    VARCHAR(64) PK
library_id  VARCHAR(64)
version_id  VARCHAR(64)
dense       FLOAT_VECTOR(768)      ← HNSW index, COSINE metric
sparse      SPARSE_FLOAT_VECTOR    ← generated by the text_to_bm25 function
text        VARCHAR(65535)         ← analyzer enabled (standard tokenizer);
                                    BM25 input: chunk text + headings ×2 + identifiers ×3
headings    VARCHAR(4096)         ← JSON breadcrumb, e.g. ["API Reference","Client","connect()"]
url         VARCHAR(2048)
token_count INT64
ordinal     INT64                 ← chunk order within page (overview-mode ordering)
```

The `sparse` field is **never written by clients**: the server-side BM25
function generates it from `text`. Retrieval requires the function and fails
closed without it (see [sage-ingestion-freshness.html](sage-ingestion-freshness.html)).

## Catalog (Postgres)

Five tables, Alembic-managed: `library` (canonical name, aliases, ecosystem,
docs_url, repo_url), `library_version` (version string, revision, source,
status, `crawled_at`, `is_latest`), `page` (URL, title, section path,
`raw_snapshot_key`, content hash), `chunk` (page, ordinal, text, token count,
headings), `crawl_job` (job kind/status for crawl and refresh work).

`library_version` is immutable once indexed: re-crawls insert new rows with a
revision counter instead of updating rows in place. Details in
[sage-ingestion-freshness.html](sage-ingestion-freshness.html).

## Data-not-instructions

Every retrieved passage is untrusted third-party documentation. Tool
descriptions instruct the calling agent to treat passage `text` strictly as
data — never as instructions that could alter its identity, permissions, tool
choices, or behavior — and the provenance delimiters carry
`trust="untrusted-document-content"` so the boundary survives copy-paste.
