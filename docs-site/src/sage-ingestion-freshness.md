# New_sage Ingestion & Freshness

Nothing is ever crawled per-query. Every `(library, version)` is ingested at
most once, then served from the index indefinitely. Refresh happens only
through the two automated paths below, plus stale-while-revalidate on read.

## Seed corpus

The initial corpus is ingested once from pinned local checkouts, through the
same pipeline as crawls so chunking, embeddings, and provenance are uniform:

| Source | Content | Ingest path |
|---|---|---|
| DevDocs docsets | dozens of libraries, single-HTML docsets | docset HTML → sections |
| MDN content repo | full web-platform docs | Markdown in git |
| tldr-pages | CLI cheatsheets | Markdown in git |
| Versioned official docs | PyTorch `site` branch, Python docs, etc. | HTML → Markdown |
| npm/PyPI READMEs | long-tail packages | Markdown |

The seed pins are recorded in `services/artifacts/manifests/seed-corpus.yaml`
and were signed off by James (APPROVAL-2026-101, 2026-10-09): DevDocs ghcr
image digest `sha256:8623d9b9…b3d66a60`, MDN content @ `fd0b11ad` (CC-BY-SA),
tldr-pages @ `6bbd7cc4` (CC-BY-SA 4.0). The lookups that resolved these pins
were metadata-only; no artifacts were downloaded.

**Seed ingestion has not run yet** — it targets James's H200 box.

## DevDocs refresh: sync, not crawl

DevDocs-sourced libraries are **never** re-crawled from upstream sites. DevDocs
publishes monthly images (`ghcr.io/freecodecamp/devdocs:latest`) with
pre-scraped docsets, and refresh is a bulk sync job:

1. Pull the monthly image (or build from the repo).
2. Extract docsets; each carries a version stamp.
3. **Diff** versions against the catalog — re-ingest **only** docsets whose
   version changed. Unchanged docsets are untouched: no re-chunk, no re-embed.
4. DevDocs' own per-docset index (`db.json`) can be parsed to detect
   per-page changes without any network fetching.

Cost: one pull + diff per month, plus re-ingestion of only the changed
libraries. No per-site crawling, no politeness budget spent.

Implementation note: the diff (`devdocs_diff` in the crawler) is pure logic
tested offline. The `docker pull` step is a clearly-marked production stub,
and calling the sync without an explicit manifest raises
`NotImplementedError` rather than pretending to sync — real DevDocs
image-manifest parsing is an open gap, not a finished feature.

## On-demand crawler: at most once per (library, version)

`get-library-docs` for an unknown or stale `(library, version)` enqueues a
crawl job on the Redis `sage-crawl` queue instead of failing:

1. The resolver maps the requested name → canonical library record.
2. Version detection determines the docs URL for the requested version:
   Sphinx `versions.json`, Docusaurus `versions.json` / version-dropdown
   scrape, `/latest/` redirect resolution, or `git ls-remote --tags`.
3. The crawler fetches the sitemap (one-level index recursion, same-origin +
   base-path scope filter, 5000-URL safety cap), downloads pages politely,
   and stores raw HTML snapshots in MinIO.
4. Pipeline: trafilatura/readability → Markdown → heading chunking →
   EmbeddingGemma 2 → Milvus upsert → catalog marks the version ready.
5. The original query is then served from the fresh index.

**Politeness rules** (non-negotiable; this is personal-use infrastructure —
don't get blocklisted): obey `robots.txt` (a disallow fails closed; a fetch
failure of robots.txt itself fails open), 1 request/second per host,
exponential backoff on 429/5xx (max 4 retries, honors `Retry-After`),
identifiable user-agent `NewSageBot/1.0 (+personal research crawler; contact:
local)`.

## Version pinning: repo clone first, crawl as fallback

When a library record has a `repo_url`, version pinning does **not** use the
website:

1. `git clone --branch <tag> --depth 1` — tags are the version, no
   detection heuristics needed (exact, `v`-prefixed, or `v`-stripped tag
   matching).
2. Ingest `README.md` + `docs/**/*.md` (+ `CHANGELOG.md` for "what changed"
   queries) directly as Markdown — no HTML extraction.
3. Snapshot = the commit SHA (recorded on the catalog version row; repo
   clones are reproducible from the tag, so no tarball is stored).

Web crawling is the fallback for libraries whose docs live only on a website
(Docusaurus/Sphinx sites with no docs in the repo). Cross-version dedup
compares page `content_hash` against the previous ready version; unchanged
pages are aliased, not re-chunked or re-embedded.

## Chunking: 420/48, code never split

Docs chunk by structure, not by page count:

- Split on H1/H2/H3 into sections; sections longer than ~420 tokens split
  further on paragraph boundaries, with 48 tokens of prose overlap.
- Code blocks are never split mid-block. A section that is one large code
  block stays whole; a single block over 2000 tokens is emitted whole with an
  `oversized_code` flag.
- Every chunk records its `headings[]` breadcrumb (e.g.
  `["API Reference", "Client", "connect()"]`) — rendered in provenance and
  indexed for BM25.
- The chunker is tokenizer-aware via an injected `token_len` callable;
  production wires the pre-staged MiniLM tokenizer
  (`MINILM_TOKENIZER_PATH`, `local_files_only=True`) and fails closed when
  the artifact is missing.

A PDF fallback exists for projects that only publish PDF manuals: the
new_DRUID pdftext path, CPU-only. It is off by default. (olmOCR is the GPU
part of new_DRUID's PDF path and is **not** used here.)

## BM25 is server-side only

The Milvus collection carries a server-side BM25 function (`text_to_bm25`:
`text` → `sparse`, standard analyzer). The ingestion pipeline writes only
the dense vector plus an augmented `text` field:

```
BM25 input text = chunk text + headings ×2 + code identifiers ×3
```

The local sparse-vector computation that ingestion briefly had was deleted
(James's 2026-10-09 decision): ingestion inserts never include a `sparse`
key, and the retrieval BM25 branch requires the Milvus function, failing
closed without it. The chunk's raw `text` stays the embedding input;
only the Milvus `text` field carries the augmentation, which means retrieved
passages render the augmented text (headings/identifiers appended, repeated
for BM25 weight).

## Immutable `library_version` revisions (Alembic 0003)

Re-crawls do **not** update version rows in place. They insert a new revision
row; existing rows are never mutated:

- Schema: `library_version.revision` INTEGER NOT NULL DEFAULT 1 (existing
  rows backfilled to 1); the unique constraint moved from
  `UNIQUE(library_id, version)` to `UNIQUE(library_id, version, revision)`.
- Active-version rule: exactly one revision is active per
  `(library, version)` — the newest READY row wins. An explicitly requested
  version string always resolves to its active revision, else falls back to
  `is_latest`. Retrieval filters `status='ready'` ordered by revision
  descending.
- Each revision gets **fresh page + chunk rows** — no cross-revision aliasing
  within the same version string, so every revision is self-contained and
  independently prunable. (Cross-version dedup for *different* version
  strings is unchanged.)
- The stale sweep only considers active revisions; superseded rows are never
  re-crawled. After a successful re-crawl, the crawler deletes Milvus chunk
  IDs present in the old revision but absent from the new one, running
  post-READY so there is no serving gap (stale-while-revalidate preserved).

## Automated refresh

Two RQ job functions implement the §17.6 automation decision:

- **`refresh_stale()`** — weekly sweep: `list_stale_versions()` (versions
  where `crawled_at + refresh_after_days < now`; default 30 days) → one crawl
  job per stale version.
- **`devdocs_sync()`** — monthly DevDocs image sync (diff, §"DevDocs
  refresh" above).

**Gap:** the reviewed sources contain the job functions and the RQ workers
that consume them, but **no scheduler is wired into the compose stack** — the
crawler README says `refresh_stale` "runs on the compose scheduler," yet no
scheduler service exists in `deploy/docker-compose.yml`. Until one is added
(a host cron invoking the jobs, or an in-stack scheduler), these jobs must be
triggered manually.
