# DRUID hardening: implementation, verification and rollout

This document describes the fixes in the draft PR against `main`.
None of these changes has been deployed to or benchmarked on the H200.
Do not treat mock-backed CI as production acceptance.

## Changes and API compatibility

1. **Functional search**: the retrieval app constructs a Postgres-backed
   searchable-version catalog, a Milvus vector store, an HTTP embedding client
   and an offline CPU MiniLM reranker at startup. Startup fails on missing
   configuration/model artifacts. The HTTP embedding client strips the
   three *known* prompt prefixes before calling `/embed`: the embedding
   service applies the task prefix exactly once.
2. **Real ingestion**: RQ entrypoints now use actual offline embedding
   weights, RustFS and Milvus by default. Tests may still inject fakes
   explicitly. Unexpected upsert counts fail before activation.
3. **Version contract**: indexed `document_version` now matches
   `DocumentVersion.version_number` (not its UUID). The UUID remains in
   the catalog, job IDs and tenant storage paths. Existing Milvus data from
   older builds may use the obsolete UUID and therefore be invisible under
   current catalog filters. Back up and **reingest/reindex** before rollout.
4. **Provenance**: new text and visual indexes carry the uploaded display
   filename so citations can name the right document. Page start/end and
   heading breadcrumbs remain the citation granularity. Coordinates and
   literal text-span offsets are *not* yet provided.
5. **Reranking / multimodal selection**: when text and visual records
   both exist, the final-result budget reserves a bounded page-image slot.
   Single-result searches still prefer reranked text when available.
6. **Tokenizer**: the offline MiniLM tokenizer is cached per worker by
   local model path; it must never download artifacts at runtime.
7. **Deployment**: backend containers are removed from shared `cora`.
   Workers use the private default network for DB/Redis, plus `druid-net`
   for the OCR GPU service. MCP defaults to loopback host binding.
   Configure `MCP_BIND_IP` to the explicit Tailscale interface address
   only when remote clients must connect.
8. **Diagnostics**: an internal `POST /search` request with
   `{"query":"...","diagnostics":true}` returns a summary *only* when
   the HMAC-signed caller has the `admin` role and the retrieval container
   has `DRUID_RETRIEVAL_DIAGNOSTICS=1`. The trace gives counts, durations
   and ranked chunk IDs, not raw document text.
9. **Evaluation**: `services.retrieval.eval_live` evaluates **real**
   authenticated search responses. It reports document recall@k (when
   curated gold IDs are present), version hit@k, citation metadata coverage,
   latency p50/p95, request failures and explicitly labeled forbidden-document
   leaks. It does **not** measure LLM hallucinations or answer faithfulness.

## Additional release blockers fixed

- MCP JWT tenant identity now uses `group_id`, consistent with the gateway
  and Web UI. Legacy-only `grp` tokens are rejected. When both claims
  exist, they must agree. Validate real issuer/audience separation with
  signed integration tokens before production deployment.
- Internal PDF page indexes are zero-based. The human citation formatter
  and citation metadata convert to one-based numbering; existing stored
  page indexes are not modified.
- Login `next` destinations are restricted to local absolute paths,
  including percent-decoded forms.
- Web UI now binds to `127.0.0.1:8005` by default. External browser
  access requires an operator-installed HTTPS reverse proxy and valid
  certificates. An example host-level Nginx configuration is in
  `deploy/nginx-web-ui-https.conf.example`. Do not enable the example
  without replacing domain/certificate settings and testing Secure cookies.
- No automatic legacy-vector migration is attempted: index version UUIDs
  must be reingested with catalog `version_number` into a staged collection.
  Rollback requires a preserved snapshot and alias.

## Keycloak SSO, DRUID groups, and API-only administration

The production auth mode is `DRUID_AUTH_MODE=keycloak`. Browser login uses
Keycloak Authorization Code + PKCE, OIDC nonce/state checks, RS256 identity
and access JWT validation and secure HttpOnly cookies. The paste-token login
route is rejected in this mode. The Keycloak client must supply an access
token with `aud=druid-api`; login ID tokens target the `OIDC_CLIENT_ID`.
The Keycloak realm must be reachable from the web_ui and gateway containers.

**Keycloak is the identity provider, not the DRUID authorization database.**
The only trusted external user identifier is the verified `sub` claim. User
accounts, active/disabled status, system-admin status, groups, memberships
and `member` / `manager` roles are stored in Postgres by the 0002 migration.
Neither Keycloak `roles` nor `groups` are DRUID permissions. One user can
belong to several groups. `X-Group-Selection` is a request-scoped selector:
it must be DB-authorized by the gateway on *every* request; backend document
APIs re-check current membership before servicing signed gateway requests.
Disabling a group or removing membership revokes it without token refresh.

Each document now stores the verified uploader's Keycloak subject. Group
members can delete their **own** documents; group managers and system
administrators can delete documents within the selected group. Previous
documents with `created_by=NULL` can only be deleted by managers/admins.
This avoids silently attributing legacy uploads to an arbitrary user.

For first boot, set `DRUID_BOOTSTRAP_ADMIN_SUB` to the exact Keycloak `sub`
of the initial system administrator, and stage `OIDC_SESSION_SECRET` and
`INTERNAL_SECRET` as independent high-entropy secrets of at least 32
characters. On the first authenticated request for that identity (only if no
admin exists), the gateway provisions the first admin. Remove the bootstrap
setting after successful bootstrap. Additional users and administrators
are managed exclusively through REST, which checks the current system-admin
DB row for each privileged operation.

System administration and all Web UI mutations are available via
`/v1/admin/*` and `/v1/*` behind the authenticated gateway. See
`skills/druid-management/references/API.md`. The reusable AI skill at
`skills/druid-management/SKILL.md` includes a helper CLI. For unattended
agents, provision a dedicated Keycloak service-account identity and grant
only the required DRUID permissions; do not place a browser user's refresh
token or client secret in the skill.

Web UI and gateway ports bind to localhost; the example Nginx TLS
configuration routes `/` to the UI and `/v1/*` and `/mcp` to the
gateway. The MCP backend has **no published port**, and in Keycloak mode
verifies the gateway's signed, DB-derived identity instead of accepting
roles directly from the Keycloak JWT.

**Still required for production acceptance:** configure the real Keycloak
realm/client/audience, test real signed OIDC browser login and service-account
API access, exercise membership revocation under load, run migration and
reindex older vectors against a backup, and perform a backup/restore drill.
CI uses SQLite for the membership contract and mocked network/JWKS for OIDC;
it is not a live Keycloak or H200 integration test.

## CI and local testing

Run the regression suite (does not require GPU/model weights):

```bash
python -m compileall -q services tests
python -m pytest -q services/retrieval/tests tests/test_p0_wiring.py tests/test_deploy_wiring.py
cp .env.example .env   # use placeholder values ONLY for Compose validation
docker compose --env-file .env -f deploy/docker-compose.yml config --quiet
docker compose --env-file .env -f deploy/docker-compose.gpu.yml config --quiet
```

CI runs the same lightweight suite using mocks, not a live database. The
historical `tests/test_live_infra.py` contains deliberately failing
placeholders if its environment flags are enabled; do not set those flags
until each real backend test is implemented.

## Required H200 staging and acceptance

1. Back up Postgres, Milvus/etcd, Redis and RustFS, recording the active
   Milvus alias and collection. Preserve the ability to roll back.
2. Set unique secrets in `.env`, replacing `change-me` examples for
   PostgreSQL, RustFS, `JWT_SECRET` and `INTERNAL_SECRET`. Restrict
   access to `.env` and never commit it.
3. Stage EmbeddingGemma and MiniLM weights under `MODEL_DIR`. Compose maps
   them read-only into containers as `/models/google/embeddinggemma-2`
   and `/models/cross-encoder/ms-marco-MiniLM-L6-v2`.
4. Validate `docker compose ... config` and build the embedding,
   retrieval, gateway and ingestion images with their pinned Python
   dependencies on the target platform. No image-build or H200 proof has
   been performed by ChatGPT.
5. Start infrastructure, migrate catalog, provision Milvus, start workers
   and services; verify retrieval *actually starts and loads MiniLM*.
6. Use a fresh tenant and upload a known PDF through the authenticated API;
   run ingestion to READY. Assert that its chunks exist in Milvus with
   tenant ID and a `document_version` matching the catalog
   `version_number`. Assert the filename, page references and vector
   dimensions are correct. Verify MCP search returns that PDF.
7. Check that searches from a second tenant cannot see that PDF. Check that
   rejected/review-required, inactive and deleting versions never appear.
   Repeat during document deactivation to test reconciliation.
8. Benchmark mixed text/image document queries and larger tenant version
   sets; watch p95 latency, Milvus expression complexity and false positives.
9. Only after production-quality data migration and canary verification
   should old indexes be retired. The current changes intentionally do not
   automatically rewrite existing vectors or collections.

## Real retrieval evaluation

Create a JSON Lines dataset using documents **actually indexed** for a
dedicated evaluation tenant, e.g.:

```json
{"id":"case-001","query":"What is the inspection interval?","expected_document_id":"<known-document-uuid>","expected_version":"1","forbidden_document_id":"<uuid-owned-by-other-tenant>"}
```

Each item requires a non-empty `query`; gold fields are optional but
missing labels produce `null` metrics, not invented successes.
Use a test tenant and avoid production-sensitive documents.

Copy the curated file into the retrieval container (with the stack up):

```bash
docker cp curated.jsonl "$(docker compose --env-file .env -f deploy/docker-compose.yml ps -q retrieval)":/tmp/druid-eval.jsonl
docker compose --env-file .env -f deploy/docker-compose.yml exec \
  -e DRUID_EVAL_USER=<trusted-user-id> \
  -e DRUID_EVAL_GROUP=<test-tenant-group-id> \
  retrieval python -m services.retrieval.eval_live \
  --cases /tmp/druid-eval.jsonl --k 8 --report-out /tmp/druid-eval-report.json
```

The evaluator signs HMAC headers using `INTERNAL_SECRET` already present
in the retrieval container. This is a **privileged internal** workflow,
not a public service and not a replacement for user JWT authorization.

## Deferred improvements

- PDF geometry/text-span attribution: requires extracting and storing reliable
  node-to-chunk offsets and validating page boxes. Do not invent coordinates.
- Large-tenant filter acceleration: benchmark the size of catalog-approved
  document/version filters under real Milvus and schema, then assess an
  indexed representation that preserves fail-closed authorization.
- Code-specific API identifier ranking: DRUID currently targets arbitrary
  uploaded documents, not exclusively source-code documentation. Add
  an opt-in domain-specific extractor only after labeling a code corpus.
- Full H200 integration tests: implement actual Postgres/RustFS/Milvus/RQ/
  OCR tests and separate model-loaded acceptance CI. Existing lightweight
  CI never asserts live quality.
