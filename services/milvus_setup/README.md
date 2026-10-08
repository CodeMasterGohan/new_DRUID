# milvus_setup

One-shot Milvus provisioning job for new_DRUID (spec §9, v2.1). Runs after
Milvus is healthy (it retries the connection internally — compose does not
health-gate this job), then exits.

## What it creates

- Versioned collection `project_knowledge_base_v1` with the full §9 schema:
  `chunk_id` (PK), `group_id` (partition key), `document_id`,
  `document_version`, `parent_id`, `page_start`/`page_end`, `chunk_order`,
  analyzer-enabled `text`, `identifiers` array, `dense_vector` (768-dim,
  unified text/page-image space), `sparse_vector`, `metadata` JSON.
- BM25 function `text_to_bm25` (`text` → `sparse_vector`).
- Indexes: HNSW/COSINE (`M=16`, `efConstruction=200`) on `dense_vector`;
  `SPARSE_INVERTED_INDEX`/BM25 on `sparse_vector`.
- Stable alias `project_knowledge_base` → the versioned collection.

`partitionkey.isolation` is **off** by default (spec §9: test-first, never
the authorization boundary). Pass `--enable-partition-isolation` only after
compatibility testing with Milvus 2.5.6.

## Usage

```bash
python -m create_collection                 # create-if-missing, ensure alias
python -m create_collection --check-only    # verify, change nothing
python -m create_collection --recreate --yes  # DROP + rebuild (DATA LOSS)
python -m create_collection --set-alias project_knowledge_base_v2 --yes
python -m create_collection --enable-partition-isolation
```

Env (see build-root `.env.example`): `MILVUS_URI`, `MILVUS_COLLECTION`,
`MILVUS_ALIAS`, `MILVUS_WAIT_TIMEOUT_S`, `MILVUS_ENABLE_PARTITION_ISOLATION`.

## Cutover / rollback

Per the spec §8 migration rule, a model/modality/dimension change requires
a new versioned collection + full re-embedding + alias cutover:

1. Run with `MILVUS_COLLECTION=project_knowledge_base_v2` to build v2.
2. Re-embed the corpus into v2; validate retrieval on the M1 corpus.
3. `python -m create_collection --set-alias project_knowledge_base_v2 --yes`
4. Rollback: same command pointed back at `project_knowledge_base_v1`.

## Tenant-scoped deletion

`deletion.delete_document(client, group_id, document_id)` issues
`group_id == "<tenant>" and document_id == "<doc>"` through the alias.
IDs are validated against the opaque-ID contract (`^[A-Za-z0-9_-]{1,128}$`);
anything else raises instead of being interpolated. Deletion is authorized
in the catalog first — this module only executes the Milvus side.
