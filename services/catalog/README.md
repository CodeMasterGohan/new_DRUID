# Document catalog (`services/catalog`)

PostgreSQL system of record for new_DRUID (spec §12): tenant ownership,
document versions, job state, review quarantine, ACLs, and parser-revision
chunk-count baselines.

## Layout

| File | Purpose |
|---|---|
| `models.py` | SQLAlchemy models: `Tenant`, `Document`, `DocumentVersion`, `Job`, `ReviewQuarantineItem`, `DocumentACL`, `ParserRevisionStats`, plus the `IngestionState` enum |
| `repository.py` | `CatalogRepository` — the §4 tenant-scoped repository. Every query is filtered to the trusted context's tenant; writes take `tenant_id` from the context and ignore caller-supplied values. Also hosts the searchability hard filter (`searchable_versions_query`: `is_active` + `status == READY` + document not deleted) |
| `activation.py` | `activate_version()` — transactional check-and-swap per §12: version must be `INDEXING`, chunk count inside the recorded per-revision range, no pending quarantine pages; then the old active version is deactivated and the document pointer moves |
| `errors.py` | `CatalogError` hierarchy (`TenantContextError`, `AuthorizationError`, `NotFoundError`, `ConflictError`, `ActivationError`) — all fail closed |
| `alembic/` | Migrations. `alembic/versions/0001_initial_schema.py` creates the full schema |

## Usage

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from services.catalog.repository import CatalogRepository
from services.catalog.activation import activate_version
from services.common.request_context import RequestContext

engine = create_engine("postgresql+psycopg://druid:druid@localhost:5432/druid")
with Session(engine) as session:
    repo = CatalogRepository(session, context)  # raises TenantContextError if invalid
    doc = repo.create_document(display_name="report.pdf", content_checksum="…")
    version = repo.create_version(doc.id, parser="native",
                                  parser_revision="pdftext-0.9", page_count=42)
    # … workers move version through UPLOADED → … → INDEXING …
    activate_version(session, context, version.id)
    session.commit()
```

## Migrations

```bash
cd services/catalog
DATABASE_URL=postgresql+psycopg://druid:druid@localhost:5432/druid alembic upgrade head
```

## Tests

```bash
cd /home/hatch/workspace/new_druid/build
python -m pytest services/catalog/tests -q   # SQLite, no database needed
```

## Shared context type

`RequestContext` lives in `services/common/request_context.py` (frozen
dataclass: `user_id`, `group_id`, `roles`, `request_id`). The repository
resolves `group_id` (the tenant *name* from the token claim) to the tenant
UUID and requires the tenant row to be active — a forged claim string alone
is useless.
