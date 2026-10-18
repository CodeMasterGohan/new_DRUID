# Intent-to-API router (grounded in DRUID's current implementation)

Use the **user's meaning**, not only literal keywords. Check current
`/v1/identity/me` for active groups first when group is relevant.
Where possible prefer an already-connected authenticated MCP tool; REST
always remains the authority for admin, uploads and jobs.

| Plain-language intent | Preferred action | Scope and precautions |
| --- | --- | --- |
| "What's in our security policy?" / "find references to patching" | MCP `search_knowledge_base(query, top_k)` or gateway `POST /mcp` | Search only selected authorized group, cite `source_reference` and filename |
| "Search all my groups for the architecture diagram" | `GET /v1/identity/me` → MCP search per group | Fan-out as separate authorized calls, attach group to every source, never mix permissions |
| "Show documents in finance" | `GET /v1/documents?limit=100&offset=0` | `X-Group-Selection: finance`, paginate if more results |
| "Show uploads waiting for review" | `GET /v1/documents?review_required=true`, and/or `GET /v1/review/items?status=pending` | Filenames need document ID resolution; review items have unique item IDs |
| "Find the report I uploaded" | `GET /v1/documents`, optionally `GET /v1/documents/{id}` | Search display names in returned list; pagination; no filename-as-ID shortcuts |
| "Tell me the version or status of this file" | `GET /v1/documents/{id}`, `GET /v1/documents/{id}/status` | Return status and version, note pending/failed/review states |
| "Did yesterday's upload finish?" | `GET /v1/jobs/{job_id}` | Use saved job ID if available; otherwise get document ID then status |
| "Upload the attached handbook to engineering" | `POST /v1/documents/upload` multipart `file` | Requires actual local file bytes, selected group, accepts queue result; never imply READY |
| "Register an upload ticket" (advanced) | `POST /v1/documents/register` JSON, then `POST /v1/documents/{id}/bytes` | Registration **does not upload bytes**; do not expose ticket |
| "Remove the duplicate document" | GET group docs → resolve exact IDs → explicit confirmation → `DELETE /v1/documents/{id}` | Soft/delete job queued, member must be uploader unless manager/admin |
| "Show OCR pages requiring review" | `GET /v1/review/items`, `GET /v1/review/items/{id}` | Returned page_index is zero-based diagnostic data, not a source citation |
| "Accept the OCR for page 2" | GET review item → confirm item/page/group → `POST /v1/review/items/{id}/resolve` | REST decision is exactly `accepted`; reviewer/admin role required |
| "Re-route the page" | Same with `decision="re-routed"` | Confirm; record rationale as `detail` object |
| "Reject the page" | Same with `decision="rejected"` | Confirm and explain loss of searchability if relevant |
| "Which groups am I in?" / "what can I access?" | `GET /v1/identity/me` | Show group names/roles; Keycloak realm groups are unrelated |
| "Switch to research" | Verify `GET /v1/identity/me`, then select via request header | `X-Group-Selection: research` on subsequent calls; no global server-side switch API |
| "Create a group for support" | `POST /v1/admin/groups` JSON | System admin only; verify name and intended membership |
| "Add Alice to Engineering" | `GET /v1/admin/users` → identify Keycloak `sub` → `PUT /v1/admin/groups/{name}/members/{subject}` | Needs exact subject, `member` vs `manager`; do not assume privilege |
| "Remove Bob's access" | GET user & membership → explicit authorization → `DELETE /v1/admin/groups/{name}/members/{subject}` | Revokes that one membership, not Keycloak identity |
| "Disable a user" | `PATCH /v1/admin/users/{subject}` with `{"is_active":false}` | Destructive. Cannot disable last active admin; confirm exact subject |
| "List accounts and group membership" | `GET /v1/admin/users`, `GET /v1/admin/groups`, `GET /v1/admin/groups/{name}/members` | System admin only, do not dump sensitive personal fields unnecessarily |
| "Show ingestion jobs" / "quota usage" | `GET /v1/admin/jobs` / `GET /v1/admin/quotas` | Admin routes; no made-up cancel/retry/requeue endpoint |
| "Rebuild the whole index" / "restore database" / "rename group" | Explain unsupported REST operation, point to operator procedure | Never improvise direct privileged host or database actions |

## Role/action translation

A `member` may search, list and upload within an authorized group and
delete their own uploads. A `manager` may review/delete documents within
that group. A `system_admin` can manage users/groups and operate in any
active group. Always let the API decide final authorization. Do not
preemptively claim a refusal merely because a role isn't visible in the
Keycloak access token.

## Disambiguation rules

- **Read across groups**: a request like "all my groups" authorizes a
  series of separate reads, not a new cross-group API. Check membership,
  run each group separately; de-duplicate only when preserving group
  provenance. Do not grant an app permission by copying a claim.
- **Write target group**: ask one question if multiple groups are eligible
  and none was named; never silently choose an arbitrary default.
- **Resource identity**: names are not stable identifiers. If "delete the
  old handbook" yields more than one candidate, offer options with
  filename, group, version/update date, and document ID. After selection,
  confirm exact deletion.
- **Missing token**: request that the user connect/sign into DRUID through
  the approved client/integration, not paste a bearer token into chat.
- **Unknown document**: search/list metadata; if still unresolved, say so,
  rather than guessing a UUID.
- **"Share this doc with Alice"**: groups control access; a direct
  document-sharing or ACL edit endpoint is not documented in current
  user/admin API. Can add Alice to the containing group only if the
  user explicitly authorizes granting access to **all** that group's
  documents and has group-administration rights.
- **"Create a scheduled reindex"**: no public reindex/scheduler API in
  this branch. Do not manufacture one. Background jobs exist only for
  supported ingestion/deletion workflows.
- **Scope**: "Help me log in" refers to Keycloak browser OIDC.
  User/password are **not** stored by DRUID; app groups are separate.
