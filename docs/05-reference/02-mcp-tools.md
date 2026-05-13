# MCP Tools Reference

The registry service exposes an MCP (Model Context Protocol) surface at `/mcp`. Agent callers connect via SSE transport:

- `GET /mcp/sse` — SSE connection endpoint
- `POST /mcp/messages/` — client-to-server message channel

Authentication uses the same bearer token as the REST API. The token is passed in the `Authorization: Bearer <token>` header of the SSE connection request. The MCP layer validates the token identically to the REST middleware — same hash, same database check, same tenant context.

**Before calling any tool:** call `whoami` first to confirm which tenant the token resolves to and which roles the caller holds.

---

## whoami

Return the actor, tenant, and roles the current credential resolves to.

**When to use:** First call in any session, or when debugging a 403 — confirms the token's tenant scope before attempting writes.

**Inputs:** None.

**Returns:** JSON object.

| Field | Type | Description |
|---|---|---|
| `actor_id` | string (UUID) | The authenticated actor's UUID |
| `actor_display_name` | string | Display name of the actor |
| `actor_email` | string or null | Actor's email address, if set |
| `tenant_id` | string (UUID) | The tenant this credential is scoped to |
| `tenant_slug` | string | URL-safe tenant identifier |
| `tenant_display_name` | string | Human-readable tenant name |
| `roles` | array of string | Role names granted to this actor |
| `token_id` | string (UUID) or null | ID of the API token, if this is a token-based credential |
| `token_expires_at` | ISO-8601 datetime or null | Token expiry, if set |

**Example response:**

```json
{
  "actor_id": "01234567-89ab-cdef-0123-456789abcdef",
  "actor_display_name": "dev-admin",
  "actor_email": null,
  "tenant_id": "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
  "tenant_slug": "dev",
  "tenant_display_name": "Dev Tenant",
  "roles": ["consumer", "producer", "admin", "auditor"],
  "token_id": "99998888-7777-6666-5555-444433332222",
  "token_expires_at": null
}
```

---

## search_capabilities

Hybrid semantic + lexical + graph search across capabilities visible to the caller's tenant.

**When to use:** When you have a description or keyword and need to find the matching capability. Combines vector similarity (embedding-based), full-text search, and graph proximity for ranked results.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | yes | — | Free-text search query |
| `top_k` | integer | no | 10 | Max results to return (1–100) |
| `as_of` | string (ISO-8601 UTC) | no | null | Time-travel: retrieve state valid at this timestamp |
| `entity_type` | string | no | null | Filter by entity type slug (e.g. `service`, `library`) |
| `lifecycle` | string | no | null | Filter by lifecycle label (e.g. `active`, `deprecated`) |

**Returns:** JSON array of result objects. Each item includes entity metadata, a relevance score, and matched fact snippets.

**Example:**

```json
[
  {
    "entity_id": "01234567-...",
    "name": "salt-design-system",
    "entity_type": "library",
    "lifecycle": "active",
    "score": 0.91,
    "summary": "Goldman Sachs open-source design system..."
  }
]
```

---

## get_capability

Retrieve a single capability record by UUID or slug-form name.

**When to use:** When you know the capability's UUID or name and want its full record including attributes, facts, and edges.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_id` | string | yes | — | UUID or slug-form name (e.g. `salt-design-system`) |
| `as_of` | string (ISO-8601 UTC) | no | null | Time-travel timestamp |
| `include` | string | no | null | Comma-separated sub-resources to expand: `components`, `depends_on`, `external_ids`, `interface`. Each capped at 200 items. |

**Returns:** JSON object with the full capability record. When `include` is specified, the response also contains the expanded sub-resource objects.

**Common errors:**

| Error | Cause |
|---|---|
| `ToolError: not found` | No capability with that UUID or name in the caller's tenant |

---

## lookup_by_external_id

Resolve a capability by its identifier in an external system (npm, GitHub, internal registry, etc.).

**When to use:** When you have a package name, repo slug, or other external identifier and need the corresponding registry entry without doing a search. For example, a coding agent that sees `@salt-ds/core` in a `package.json` can resolve it directly.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `external_system` | string | yes | External-system slug as registered in the admin API (e.g. `npm`, `github`) |
| `external_id` | string | yes | Identifier inside that system (e.g. `@salt-ds/core`, `jpmorganchase/salt-ds`) |

**Returns:** JSON object. On a match: full capability record (same shape as `get_capability`). On no match:

```json
{
  "found": false,
  "external_system": "npm",
  "external_id": "@salt-ds/core"
}
```

---

## get_dependencies

k-hop forward traversal: capabilities that the given entity depends on.

**When to use:** When you need to understand what a capability pulls in — its direct and transitive dependencies.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_id` | string | yes | — | UUID or slug-form name of the root capability |
| `depth` | integer | no | 2 | Traversal depth (1–5) |
| `as_of` | string (ISO-8601 UTC) | no | null | Time-travel timestamp |

**Returns:** JSON object.

| Field | Type | Description |
|---|---|---|
| `root_entity_id` | string (UUID) | Resolved UUID of the root |
| `depth` | integer | Depth used |
| `as_of` | string or null | Effective time |
| `edges` | array | Directed edge objects in the subgraph |

---

## get_dependents

Reverse traversal: capabilities that depend on the given entity.

**When to use:** When assessing the blast radius of a change — which other capabilities consume this one transitively.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_id` | string | yes | — | UUID or slug-form name of the root capability |
| `depth` | integer | no | 2 | Max hop count (1–5) |
| `edge_types` | array of string | no | null | Edge relationship types to follow. Null follows all dependency relationships. |
| `as_of` | string (ISO-8601 UTC) | no | null | Time-travel timestamp |

**Returns:** JSON object matching the `TraversalResult` shape: `root_entity_id`, `depth`, `direction`, `as_of`, `nodes`, `edges`, `version_satisfied`, `cache_hit`.

---

## get_blast_radius

Full transitive closure from a capability, backed by the closure cache.

**When to use:** When you need the complete set of entities reachable from a root — e.g., "everything that would be affected if this library changed." Faster than `get_dependents` for large graphs because it uses the pre-computed closure cache. Falls back to a recursive CTE when the cache is cold or the query is older than 90 days.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_id` | string | yes | — | UUID or slug-form name |
| `direction` | string | no | `reverse` | `forward` (what this depends on) or `reverse` (what depends on this) |
| `edge_types` | array of string | no | null | Edge types to follow. Null follows all dependency relationships. |
| `depth` | integer | no | 5 | Max hop count (1–5) |
| `as_of` | string (ISO-8601 UTC) | no | null | Time-travel timestamp. Values older than 90 days force the CTE fallback. |

**Returns:** JSON object matching the `TraversalResult` shape: `root_entity_id`, `depth`, `direction`, `as_of`, `nodes`, `edges`, `version_satisfied`, `cache_hit`.

---

## list_capabilities

Paginated list of capabilities visible to the caller's tenant.

**When to use:** When you want a broad list to browse or scan — rather than searching for a specific item.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `lifecycle` | string | no | null | Filter by lifecycle label |
| `entity_type` | string | no | null | Filter by entity type slug |
| `page` | integer | no | 1 | Page number (1-based) |
| `page_size` | integer | no | 20 | Items per page (1–200) |
| `as_of` | string (ISO-8601 UTC) | no | null | Time-travel timestamp |

**Returns:** JSON object.

```json
{
  "items": [ /* array of capability summary objects */ ],
  "page": 1,
  "page_size": 20
}
```

---

## list_notifications

List capability-event notifications for the caller's tenant. Only available when the `NotificationService` is wired into the MCP server (the default).

**When to use:** When a polling agent needs to consume the event stream without setting up a webhook subscription.

**Required role:** `consumer`

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `since` | string (ISO-8601 UTC) | no | null | Cursor: returns rows strictly older than this timestamp. Null returns the first page (newest first). |
| `status` | string | no | `unread` | `unread`, `read`, or `all` |
| `page_size` | integer | no | 50 | Items per page (1–500) |

**Returns:** JSON object.

```json
{
  "items": [ /* array of CapabilityRegistryEvent objects */ ],
  "next_cursor": "2026-05-10T14:23:00.000000Z"
}
```

Pass `next_cursor` as `since` on the next call to page through the event stream. When `next_cursor` is null the page is the last one.

Notification payloads carry only structured event fields — no free-text entity body content.

---

## submit_annotation

Submit a new annotation on a capability. Use this to attach structured feedback — a bug report, documentation gap, question, or suggestion — to a capability the calling tenant can see.

**When to use:** When a consumer agent identifies an issue or has feedback about a capability and wants to record it for the capability owner to triage.

**Required role:** `consumer`, `producer`, or `admin`

**Before calling:** The calling tenant must be able to see the capability. An invisible or non-existent capability returns `ToolError: Capability not visible or not found`.

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `capability_id` | string (UUID) | yes | — | UUID of the capability to annotate. Must be a UUID — slugs are not accepted. |
| `body` | string | yes | — | Annotation text. Must be at least one character. PII-scanned before storage. |
| `category` | string | yes | — | One of: `feedback`, `bug`, `suggestion`, `question`, `doc_gap`. |
| `version_target` | string | no | null | Optional version string this annotation targets (e.g. `v2.3`). |
| `triage_note` | string | no | null | Optional initial provider note. Stored but not used for routing. |

**Returns:** JSON object with the created annotation.

| Field | Type | Description |
|---|---|---|
| `annotation_id` | string (UUID) | Unique identifier for the new annotation |
| `capability_id` | string (UUID) | The capability this annotation is attached to |
| `author_actor_id` | string (UUID) | Actor who submitted the annotation |
| `author_tenant_id` | string (UUID) | Tenant the author belongs to |
| `body` | string | Annotation text |
| `category` | string | Category value as submitted |
| `status` | string | Always `open` on creation |
| `version_target` | string or null | Version string if provided |
| `triage_note` | string or null | Initial note if provided |
| `created_at` | ISO-8601 datetime | Creation timestamp |
| `updated_at` | ISO-8601 datetime | Last update timestamp |
| `warnings` | array or absent | Present only when the PII scanner returned a warn-level match on `body`. Each entry has `field` and `categories`. |

**Common errors:**

| Message pattern | Cause | Action |
|---|---|---|
| `Capability not visible or not found` | Capability UUID doesn't exist or is outside the caller's visibility scope | Confirm the UUID with `get_capability` first |
| `Invalid category: '...'. Must be one of: feedback, bug, suggestion, question, doc_gap` | Category not in closed vocabulary | Use one of the listed values |
| `Annotation rejected: PII detected in body [<categories>]` | Body triggered a block-level PII policy | Remove or redact the sensitive content |
| `capability_id must be a valid UUID` | A slug was passed instead of a UUID | Resolve the slug to a UUID via `get_capability` first |

**Relationship to REST:** This tool wraps the same service method as `POST /v1/capabilities/{capability_id}/annotations`. The state model, status lifecycle, and authorization rules are identical. One difference: the MCP tool requires a UUID for `capability_id`; the REST endpoint also accepts slug-form names. For the full annotation state model and triage workflow, see the Annotations section in [api.md](01-api.md).

**Example:**

```json
{
  "tool": "submit_annotation",
  "arguments": {
    "capability_id": "cap00000-0000-0000-0000-000000000001",
    "body": "The retry-after header is not documented anywhere.",
    "category": "doc_gap"
  }
}
```

---

## list_my_annotations

List annotations authored by the calling actor's tenant on a specific capability.

**When to use:** When a consumer agent wants to review the feedback it has previously submitted on a capability — for example, to check whether a bug report it filed has been triaged.

**Required role:** `consumer`, `producer`, or `admin`

**Precondition:** `capability_id` is required. When omitted the tool returns an empty list — cross-capability scanning is not supported.

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `capability_id` | string (UUID) | no | null | UUID of the capability to filter to. When omitted, returns `{"items": [], "next_cursor": null}`. Must be a UUID — slugs are not accepted. |
| `status` | string | no | null | Filter by annotation status. One of: `open`, `triaged`, `acknowledged`, `closed`. Null returns all statuses. |
| `cursor` | string | no | null | Opaque pagination cursor from a previous call's `next_cursor`. Omit to start from the first page. |

**Returns:** JSON object.

| Field | Type | Description |
|---|---|---|
| `items` | array | Annotation objects matching the caller's authored annotations on the capability. Each item has the same shape as the `submit_annotation` response (without `warnings`). |
| `next_cursor` | string or null | Cursor for the next page. Pass as `cursor` on the next call. `null` when no further pages exist. |

The result is always filtered to the caller's own annotations — the `author_tenant_id` always equals the caller's tenant. Non-provider callers cannot see annotations from other tenants on the same capability.

**Common errors:**

| Message pattern | Cause | Action |
|---|---|---|
| `capability_id must be a valid UUID` | A slug was passed instead of a UUID | Resolve the slug to a UUID via `get_capability` first |

**Relationship to REST:** This tool delegates to the same `list_annotations` service method used by `GET /v1/capabilities/{capability_id}/annotations`, applying the author path (non-provider filter). The REST endpoint is capability-scoped by path; this tool accepts `capability_id` as an input parameter instead, but the filtering logic is identical. See the Annotations section in [api.md](01-api.md) for the full access-path description.

**Example:**

```json
{
  "tool": "list_my_annotations",
  "arguments": {
    "capability_id": "cap00000-0000-0000-0000-000000000001",
    "status": "open"
  }
}
```

Response:

```json
{
  "items": [
    {
      "annotation_id": "a1b2c3d4-0000-0000-0000-000000000001",
      "capability_id": "cap00000-0000-0000-0000-000000000001",
      "author_actor_id": "actor000-0000-0000-0000-000000000001",
      "author_tenant_id": "tenant00-0000-0000-0000-000000000002",
      "body": "The retry-after header is not documented anywhere.",
      "category": "doc_gap",
      "status": "open",
      "created_at": "2026-05-12T12:00:00+00:00",
      "updated_at": "2026-05-12T12:00:00+00:00"
    }
  ],
  "next_cursor": null
}
```

---

## triage_annotation

Triage an annotation on a capability the calling tenant owns. Updates the annotation's status and optionally records a provider note alongside the transition.

**When to use:** When a provider agent needs to acknowledge, progress, or close feedback that a consumer has submitted. Only the capability's owner tenant may triage; consumer agents that submitted the original annotation should use `list_my_annotations` to check its current status.

**Required role:** `producer` or `admin`

**Before calling:** The calling tenant must be the owner of the capability the annotation belongs to. Attempting to triage an annotation on a capability owned by a different tenant returns `ToolError: forbidden`. Verify your tenant with `whoami` first.

**Inputs:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `annotation_id` | string (UUID) | yes | — | UUID of the annotation to triage. Must be a UUID — slugs are not accepted. |
| `new_status` | string | yes | — | Target status. One of: `open`, `triaged`, `acknowledged`, `closed`. |
| `triage_note` | string | no | null | Optional provider note to record with this transition. PII-scanned before storage; a block-level match raises a ToolError. |
| `version_target` | string | no | null | Accepted but not applied. See "Relationship to REST" below. |

**State transitions:** Both forward and reverse transitions are accepted — there is no enforced direction. Setting `new_status` to the annotation's current status is a no-op: the tool returns the unchanged annotation and no audit entry is written. For the full status vocabulary and lifecycle description, see the Annotations section in [api.md](01-api.md).

**Returns:** JSON object with the updated annotation.

| Field | Type | Description |
|---|---|---|
| `annotation_id` | string (UUID) | Unique identifier of the annotation |
| `capability_id` | string (UUID) | The capability this annotation is attached to |
| `tenant_id` | string (UUID) | The capability-owner tenant (authorization scope) |
| `author_actor_id` | string (UUID) | Actor who originally submitted the annotation |
| `author_tenant_id` | string (UUID) | Tenant the author belongs to |
| `body` | string | Annotation text |
| `triage_note` | string or null | Provider note as set by this call, or the prior note if `triage_note` was not supplied |
| `category` | string | Category value from submission |
| `status` | string | New status as set by this call |
| `version_target` | string or null | Version string from original submission (not updated by this call) |
| `created_at` | ISO-8601 datetime | Original submission timestamp |
| `updated_at` | ISO-8601 datetime | Timestamp of this update |
| `warnings` | array or absent | Present only when the PII scanner returned a warn-level match on `triage_note`. Each entry has `field` and `categories`. |

**Common errors:**

| Message pattern | Cause | Action |
|---|---|---|
| `annotation_id must be a valid UUID: ...` | Non-UUID value passed for `annotation_id` | Use the UUID returned by `submit_annotation` or `list_my_annotations` |
| `Annotation <id> not found` | Annotation does not exist or has been deleted | Confirm the UUID; deleted annotations cannot be triaged |
| `forbidden` | Caller's tenant does not own the capability the annotation belongs to | Only the provider tenant may triage; verify with `whoami` |
| `Invalid status '...'. Must be one of: ...` | `new_status` is not in the closed vocabulary | Use one of: `open`, `triaged`, `acknowledged`, `closed` |
| `pii_detected in annotation.triage_note` | `triage_note` triggered a block-level PII policy | Remove or redact the sensitive content before retrying |

**Relationship to REST:** This tool wraps the same service method as `PATCH /v1/annotations/{annotation_id}`. The authorization rule (caller's tenant must own the capability), the state-transition semantics, and the PII policy on `triage_note` are identical. Two differences: first, the MCP tool requires a UUID for `annotation_id`; the REST endpoint also accepts UUID-only (no slug form for annotation IDs). Second, the `version_target` parameter is accepted by the MCP tool signature but is not forwarded to the service — it has no effect. The REST `PATCH` endpoint does apply a supplied `version_target` to the annotation row. If updating `version_target` is required, use the REST endpoint directly. For the full annotation state model, see the Annotations section in [api.md](01-api.md).

**Example:**

```json
{
  "tool": "triage_annotation",
  "arguments": {
    "annotation_id": "a1b2c3d4-0000-0000-0000-000000000001",
    "new_status": "triaged",
    "triage_note": "Confirmed — adding to the next docs sprint."
  }
}
```

Response:

```json
{
  "annotation_id": "a1b2c3d4-0000-0000-0000-000000000001",
  "capability_id": "cap00000-0000-0000-0000-000000000001",
  "tenant_id": "tenant00-0000-0000-0000-000000000001",
  "author_actor_id": "actor000-0000-0000-0000-000000000002",
  "author_tenant_id": "tenant00-0000-0000-0000-000000000002",
  "body": "The retry-after header is not documented anywhere.",
  "triage_note": "Confirmed — adding to the next docs sprint.",
  "category": "doc_gap",
  "status": "triaged",
  "version_target": null,
  "created_at": "2026-05-12T12:00:00+00:00",
  "updated_at": "2026-05-12T12:05:00+00:00"
}
```

---

## Error handling

All tools raise a `ToolError` on failure. The error message is a human-readable string. Common conditions:

| Message pattern | Cause | Action |
|---|---|---|
| `not found` | No entity with that ID/name in caller's tenant | Check the UUID or name; verify tenant scope with `whoami` |
| `forbidden` | Token lacks required role | Check roles in `whoami`; contact tenant admin |
| `top_k must be between 1 and 100` | Parameter out of range | Clamp the value |
| `depth must be between 1 and 5` | Parameter out of range | Clamp the value |
| `direction must be 'forward' or 'reverse'` | Invalid enum value | Use exact string |
