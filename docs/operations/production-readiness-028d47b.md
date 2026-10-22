# Production-readiness changes based on 028d47b

Status: local implementation for review; no push, merge or deployment. The three P1 defects have fixes and regression coverage. Production approval still requires the infrastructure and security gates below.

## Changes and regression locations

| Priority | Fix | Implementation | Regression coverage |
| --- | --- | --- | --- |
| P1 | Apply document read ACL before listing/pagination, detail, versions, quarantine and job status; denied reads cannot obtain signed download links | `services/catalog/repository.py`, `services/document_api/routes/jobs.py` | `services/document_api/tests/test_security_regressions.py` |
| P1 | Count actual uploaded pages and atomically admit each version once against a tenant's UTC-day quota, including two-phase uploads | `services/document_api/ingestion_admission.py`, `services/document_api/routes/documents.py`, `services/catalog/models.py`, migration `0003_ingestion_admission.py` | security regression tests; concurrent admission in `tests/test_production_integration.py` |
| P1 | Commit deletion tombstone before external cleanup; persist failures; retry incomplete cleanup without restoring visibility | `services/ingestion/pipeline.py`, document/job routes | `services/ingestion/tests/test_pipeline.py` |
| P2 | Serialize workers, retry and deletion by document across commits; reset retry state before enqueue; avoid overwriting fast-worker state | `services/catalog/work_lock.py`, pipeline and document/job routes, `services/catalog/activation.py` | security regressions, pipeline terminal replay tests, PostgreSQL lock integration test |
| P2 | Import measured parser baseline and fail readiness until baseline/schema exist | `services/catalog/baselines.py`, `services/document_api/main.py` | `services/catalog/tests/test_baselines.py`, fresh migration/activation integration test |
| P2 | Gate full contract suite, PostgreSQL/Redis checks and security scans | `.github/workflows/druid-hardening.yml`, `.gitlab-ci.yml`, `ci/requirements-test.txt` | workflow configuration; remote execution pending |
| P2 | Bound multipart ingress, stream object upload, assemble UI chunks on disk | `services/common/upload_limits.py`, API storage/validation/routes, gateway proxy, `services/web_ui/upload_sessions.py` | `tests/test_upload_limits.py`, API and UI suites |
| P2 | Batch index upserts and partition exact authorized document/version predicates; merge global search top K | `services/ingestion/milvus_sink.py`, `services/retrieval/production.py` | `services/ingestion/tests/test_milvus_sink.py`, `services/retrieval/tests/test_production.py` |

The initial migration also explicitly owns enum creation, preventing table creation from attempting to recreate the PostgreSQL enum on fresh installs. Existing migrated databases are unaffected.

## Safe rollout

1. Back up the catalog and preserve originals. Rehearse on a disposable staging database and object/index services. Drain uploads, retries and ingestion/deletion workers before migration; mixed old/new writers are unsupported.
2. Run `cd services/catalog && alembic upgrade head` with the intended `DATABASE_URL`. Migration 0003 backfills server-recorded positive page counts for versions created in the current UTC day. Historical gateway accounting is not imported. Reconcile unusual legacy records before reopening ingestion; a UTC-day boundary maintenance window avoids ambiguous prior-day registrations. Keep the ledger through rollback; migration downgrade intentionally refuses to discard it.
3. Import reviewed measurements for the deployed parser revision using `python -m services.catalog.baselines /path/to/measurements.json` from repository root. Manifest fields: `parser`, `parser_revision`, and a nonempty `samples` list with source `sha256`, positive `pages` and positive `chunks`. The current readiness check expects `mixed` / `router-v1`. Use real representative corpus measurements, retain the manifest and printed hash, and review derived bounds. No fabricated production baseline is shipped. Changed bounds require a new parser revision.
4. Deploy API, gateway, UI and workers together. Use PostgreSQL direct connections or session pooling: session advisory locks are incompatible with transaction-pooling proxies. Provision enough connections for active workers; a blocked duplicate delivery holds a connection. SQLite locks are only a development substitute.
5. Route readiness checks to document API `/readyz`; `/healthz` is liveness. Verify authorized/denied document reads and both upload flows. Keep the document API internal with signed identity verification enabled.
6. Test a deliberately interrupted deletion. Its tombstone must remain invisible to search/listing; owner/admin can inspect the job and POST `/v1/jobs/{job_id}/retry`. Failed and retry-pending jobs, stuck deletion jobs and jobs whose enqueue was never recorded are supported. Already queued/active ingestion crash recovery still depends on the queue's failed-job/requeue operations; do not reset catalog state while a worker may be active. `REVIEW_REQUIRED` remains parked for human review.
7. Configure temporary disk capacity and `UPLOAD_CONCURRENCY` (positive integer; default 4 per process). Multipart ingress is spooled with a size limit and 120-second receive timeout. Parsers and embedding models still have their own memory requirements. UI chunk sessions have a separate in-process capacity bound; restarts invalidate incomplete sessions.
8. Tune `MILVUS_UPSERT_BATCH_SIZE` and `MILVUS_FILTER_BATCH_SIZE` (default 256; range 1–4096) only after a representative load test. Partitioning bounds request expressions, but latency still grows with authorized pair count; it is not a capacity certification.

## Verification and release gates

Local Python 3.12 verification: 507 tests passed, 20 skipped in the backend/root suite; 43 UI tests passed separately. `pip check` passed. Alembic generated the full PostgreSQL migration SQL successfully. SQL generation does not prove that migrations execute against a live server.

Run from repository root:

```sh
python -m pip install -r ci/requirements-test.txt
python -m pip check
python -m pytest -q services tests --ignore=services/web_ui/tests
(cd services/web_ui && python -m pytest -q tests)
```

Four skips require disposable `DRUID_TEST_DATABASE_URL` and `DRUID_TEST_REDIS_URL`; CI supplies these and runs `tests/test_production_integration.py`. The database fixture creates and drops a randomly named schema; never point it at production. Sixteen other optional tests remain skipped. The new remote jobs have not run because no changes have been pushed.

Before release, require successful `complete-contracts`, `database-broker-contracts` and `security-gate` checks in branch protection (repository settings are not changed here). Security scanners are now blocking; existing dependencies/images may produce findings requiring remediation. No successful fresh security scan is claimed. Require real Milvus/object-store deletion and partial-failure checks, a representative ingestion/load test, calibrated parser measurements, and migration rehearsal. These are remaining release gates, not evidence supplied by mocked tests.

Rollback must retain admission records and tombstones. Do not restore the old ACL/quota paths to live traffic as a rollback strategy; drain traffic and forward-fix. Previously issued signed object URLs cannot be revoked by catalog ACL changes alone: consider their expiry and object-store access controls during incident review.
