# Backup & restore — new_DRUID (spec §13 operational acceptance)

A single H200 is a single point of failure. Backups are load-bearing, and
**restore is tested on a schedule, not just documented.**

## What gets backed up

| Store | Tool | Scope |
|---|---|---|
| Milvus | `milvus-backup` | `project_knowledge_base_v*` collections + aliases |
| PostgreSQL | `pg_basebackup` + WAL archiving (or nightly `pg_dump`) | Full catalog: tenants, documents, versions, jobs, review quarantine |
| RustFS | `rclone sync` to an S3 remote (RustFS ships no bundled mc) | Originals, page renders, parsed trees, markdown, provenance |

Backup the **catalog and Milvus together** (same window): the catalog's
active-version pointers must be consistent with the indexed collections.
A chunk without a catalog record is garbage; a catalog record without its
chunks is a hole the hard filter (§10) will correctly hide — but restore
should not rely on that.

## Schedule (initial)

- Milvus: nightly `milvus-backup create`, retain 7 days.
- PostgreSQL: continuous WAL archiving + weekly base backup, retain 4 weeks.
- RustFS: continuous `rclone sync` to an S3 remote (via `rclone sync --watch`-style
  polling or a cron-triggered run), plus nightly snapshot, retain 30 days.

## Restore procedure

1. Stop all services except the infrastructure (`postgres`, `rustfs`, `milvus`).
2. Restore PostgreSQL from base backup + WAL to the backup timestamp T.
3. Restore RustFS objects to T.
4. Restore Milvus collections with `milvus-backup restore` to T.
5. Run `milvus_setup` in **verify mode** (must confirm, not recreate) that the
   alias `project_knowledge_base` resolves and collection schema matches §9.
6. Run the catalog/Milvus **reconciliation check**: every `READY` document
   version in the catalog must have its chunks present; every chunk's
   `document_version` must resolve to an active catalog record. Quarantine
   mismatches to `REVIEW_REQUIRED` — never silently expose them.
7. Bring services up; run the §13 smoke queries (known-answer set from M1).

## Restore testing

- Full restore drill **monthly** to an isolated namespace/host.
- Drill is pass/fail: system must reach queryable state and pass the M1
  known-answer spot checks.
- Record each drill (date, duration, issues) — a drill that isn't recorded
  didn't happen.

## RTO / RPO targets (initial, tune after first drill)

- RPO: ≤ 24 h (nightly Milvus/PG backups; RustFS continuous).
- RTO: ≤ 4 h for full restore on equivalent hardware.

## Deletion interplay (§12)

Backups retain deleted data until retention expires. Document this in the
tenant-facing retention policy: "deleted" means invisible to retrieval
immediately (catalog hard filter), physically gone from live stores after
compaction, and gone from backups after the retention window.
