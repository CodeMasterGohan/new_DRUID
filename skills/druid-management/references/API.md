# DRUID management REST API — authoritative endpoints

Base URL: `https://<DRUID-host>/v1` (gateway, not direct document_api).
Header: `Authorization: Bearer <Keycloak RS256 access token>`.
Optional `X-Group-Selection: engineering` selects **only a group the user
currently belongs to**, unless the authenticated actor is a system admin.
Response errors use standard HTTP 401/403/404/409/422/429.

Only DRUID system administrators can call `/v1/admin/*` identity management
endpoints; the API checks the current DRUID user record, not Keycloak roles.

## Current identity and group switching

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | /v1/identity/me | OIDC subject, roles, selected group and all active memberships (name, role) |

The group selector is a request header, not a separate mutable user setting.
A multi-group agent can perform independent search requests per authorized
group. The gateway checks the group against PostgreSQL on every request.

## System administration

| Method | Endpoint | JSON / meaning |
| --- | --- | --- |
| GET | /v1/admin/users | `{"users":[...]}` |
| POST | /v1/admin/users | `{"subject":"<keycloak sub>","username":"...", "email":"...", "is_system_admin":false}` |
| PATCH | /v1/admin/users/{subject} | `{"is_active":false}`, `{"is_system_admin":true}`, display name/email |
| GET | /v1/admin/groups | `{"groups":[{"name":"...","is_active":true,...}]}` |
| POST | /v1/admin/groups | `{"name":"engineering"}` (lowercase names) |
| PATCH | /v1/admin/groups/{name} | `{"is_active":false}` or `true` |
| GET | /v1/admin/groups/{name}/members | `{"group":"...","members":[...]}` |
| PUT | /v1/admin/groups/{name}/members/{subject} | Idempotent add/change `{"subject":"...","role":"member"|"manager"}` |
| DELETE | /v1/admin/groups/{name}/members/{subject} | Remove one membership |
| GET | /v1/admin/documents | Operator documents; optional `tenant`, `status`, `limit`, `offset` |
| GET | /v1/admin/jobs | Operator jobs; optional `tenant`, `limit`, `offset` |
| GET | /v1/admin/quotas | Quota settings and current usage |

Do not attempt to rename a group: its name is embedded in existing Milvus
authorization predicates and object storage keys. Deactivate it instead,
or conduct a carefully planned migration/reindex. Deactivating a group
blocks all its members. Existing documents are retained unless deleted
through the normal document-deletion lifecycle.

`PATCH /v1/admin/users/{subject}` prevents an administrator from removing
their own administrator privileges and prevents disabling the last active
system administrator.

## Group-scoped document and review operations

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | /v1/documents/upload | Upload multipart PDF with `file` |
| GET | /v1/documents | List documents in selected group |
| GET | /v1/documents/{uuid} | Document metadata, versions and artifact references |
| DELETE | /v1/documents/{uuid} | Queue full deletion (member must be uploader, manager/admin may remove any in their selected group) |
| GET | /v1/documents/{uuid}/status | Latest ingestion status |
| GET | /v1/review/items | Review items for selected group |
| GET | /v1/review/items/{uuid} | Review-item details |
| POST | /v1/review/items/{uuid}/resolve | `{"decision":"accept"|"reject"|...,"detail":{}}`; reviewer or admin role required |
| POST | /mcp | Gateway-mediated authenticated FastMCP JSON-RPC, including `search_knowledge_base`, `ingest_document`, `list_documents`, `get_document`, `delete_document`, `get_ingestion_status`, `get_review_item`, `resolve_review_item` |

The MCP endpoint uses the gateway's verified, database-derived roles. Never
expose the internal MCP container port publicly. For long-running automation,
use short-lived tokens and refresh them through an approved Keycloak flow.

## Operational boundaries

The current REST implementation does not yet expose arbitrary Docker control,
unrestricted Milvus schema edits, database dumps, system backups/restores or
certificate installation. AI agents should not silently invoke these
high-impact host actions through a management API. Add independently audited
operator endpoints only where there is a clear requirement, with explicit
authorization and confirmation.
