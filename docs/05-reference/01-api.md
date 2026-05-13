# API Reference

The registry service exposes a REST API under `/v1` and an MCP surface at `/mcp`. This document explains how to discover the live schema, covers the major resource groups and their semantics, and describes common headers and error shapes.

For MCP tool reference, see [mcp-tools.md](02-mcp-tools.md). For authentication, see [overview/auth.md](../01-overview/04-auth.md).

---

## Discovering the live schema

The authoritative, always-current API schema is the OpenAPI document the service generates at runtime:

| Path | Format |
|---|---|
| `/openapi.json` | Machine-readable OpenAPI 3.x JSON |
| `/docs` | Swagger UI (interactive browser) |
| `/redoc` | ReDoc (read-only browser) |

Do not hand-maintain an API schema — always derive it from the live document or the committed `openapi.json` in the repo (regenerated with `make openapi-export`).

---

## Common conventions

### Authentication

All endpoints require a bearer token:

```
Authorization: Bearer <token>
```

The service resolves the token to a `TenantContext` (tenant + actor + roles). Calls without a valid token return HTTP 401. Calls with a token that lacks the required role return HTTP 403.

### Tenant isolation

Every request is scoped to exactly one tenant. The token's tenant scope is determined at authentication time. A caller cannot read or write another tenant's data, regardless of query parameters.

### Base URL

All endpoints below are relative to the service root (e.g., `http://localhost:8000`).

### Content type

Write endpoints (`POST`, `PUT`, `PATCH`) require `Content-Type: application/json`. Responses are `application/json`.

### Bi-temporal time travel

Most read endpoints accept an `?as_of=<ISO-8601 UTC datetime>` query parameter to retrieve the state of the data as it was valid at that point in time. Omitting `as_of` returns current state (equivalent to `as_of=now()`).

Example:

```
GET /v1/capabilities?as_of=2025-01-01T00:00:00Z
```

### Entity handles

Endpoints that accept `{entity_id}` in the path also accept a slug-form name. Example:

```
GET /v1/capabilities/salt-design-system
GET /v1/capabilities/01234567-89ab-cdef-0123-456789abcdef
```

Both resolve to the same record.

### Pagination

Two pagination styles are used across the API. The resource determines which style applies.

#### Offset pagination

No resource group currently uses offset pagination. The `?page=N` offset parameter was previously supported on some list endpoints. Sending `?page=N` to any current list endpoint returns HTTP 422 with code `page_param_deprecated`.

#### Cursor (keyset) pagination

Most list endpoints use cursor-based pagination. The response carries a `next_cursor` field; pass its value as `?cursor=<value>` to fetch the next page. Omit `cursor` on the first request.

**Request parameters:**

| Parameter | Type | Description |
|---|---|---|
| `cursor` | string | Opaque keyset cursor from the previous response's `next_cursor`. Omit to start from the first page. |
| `page_size` | integer | Number of items per page. Range and default vary per endpoint (see Swagger). Not accepted on all cursor-paginated endpoints — see per-endpoint notes. |

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `items` | array | Page of results. |
| `next_cursor` | string or null | Cursor for the next page. `null` when no further pages exist. |

The cursor is opaque — it encodes the sort key of the last item in the page. Do not construct or parse it. Passing a tampered or invalid cursor returns HTTP 422.

**Pagination style by resource group:**

| Resource group | Pagination style | Notes |
|---|---|---|
| Capabilities (list) | cursor | `?cursor` + `?page_size` (default 20, max 200) |
| Annotations (list) | cursor | `?cursor` only; page size is fixed at 50, max 200; not a query parameter |
| Artifacts (list) | cursor | `?cursor` + `?page_size` (default 20, max 200) |
| Notifications (list) | cursor | `?cursor` + `?page_size` (default 50, max 500) |
| Adoptions | bounded | `next_cursor` is always `null`; at most one active row per tenant per capability |
| External IDs | bounded | `next_cursor` is always `null`; typically 1–10 rows per entity |
| Subscriptions | bounded | `next_cursor` is always `null`; typically 1–5 rows per capability per tenant |

### Idempotency

`POST` endpoints that create resources accept an optional `Idempotency-Key: <uuid>` header. Repeating the same key within the idempotency window returns the original response without creating a duplicate.

### ETags

GET endpoints on mutable resources return an `ETag` header. Supply it back as `If-Match: <etag>` on mutation requests to prevent lost-update races. Mismatched ETags return HTTP 412.

---

## Resource groups

Resource groups documented here vary in depth. **Capabilities** and **Annotations** are documented with full per-endpoint request/response schemas. **Artifacts**, **Interfaces**, **Operations**, **Concepts**, and **Breaking Changes** are documented at summary level — the live OpenAPI document at `/openapi.json` (or the interactive browser at `/docs`) is the authoritative reference for their full schemas, validators, and error shapes.

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness probe. Returns `{"status":"ok"}` with HTTP 200 when the process is alive. No auth required. |

---

### Capabilities

Capabilities are the primary entities. The `capability` resource shape is the richest read surface — it includes the entity's attributes, facts, edges, and computed fields.

| Method | Path | Role required | Description |
|---|---|---|---|
| `GET` | `/v1/capabilities` | `consumer` | Cursor-paginated list of capabilities visible to the caller's tenant. Parameters: `?cursor`, `?page_size` (default 20, max 200), `?lifecycle`, `?entity_type`, `?as_of`. |
| `POST` | `/v1/capabilities` | `producer` | Create a new capability. |
| `GET` | `/v1/capabilities/{entity_id}` | `consumer` | Retrieve a single capability by UUID or slug name. Supports `?as_of=` and `?include=`. |
| `PATCH` | `/v1/capabilities/{entity_id}` | `producer` | Partial update (name, description, lifecycle, attributes). |
| `DELETE` | `/v1/capabilities/{entity_id}` | `producer` | Soft-delete (sets `t_valid_to = now()`). |
| `PATCH` | `/v1/capabilities/{entity_id}/visibility` | `producer` or `admin` | Set visibility (`private`, `tenant-shared`, `public`). |

**Composite retrieval (`?include=`):** The `GET /v1/capabilities/{entity_id}` endpoint accepts `?include=` with a comma-separated list of sub-resources to expand inline. Expansions are capped at 50 items each; overflow is signalled with `truncated: true` and a `next` URL.

| `include` value | What it adds |
|---|---|
| `components` | Outgoing `composes` edges expanded to full entity records |
| `depends_on` | Outgoing `depends_on` edges expanded to full entity records |
| `external_ids` | `entity_external_ids` mappings (npm name, GitHub slug, …) |
| `interface` | Latest registered interface surface (JSON Schema / OpenAPI 3.x) |

---

### Retrieval (search + traversal)

| Method | Path | Role required | Description |
|---|---|---|---|
| `GET` | `/v1/search` | `consumer` | Hybrid semantic + lexical + graph search. Parameters: `q` (required), `top_k` (default 10, max 100), `entity_type`, `lifecycle`, `as_of`. |
| `GET` | `/v1/graph/{entity_id}/dependencies` | `consumer` | k-hop forward traversal (entities this one depends on). Parameters: `depth` (1–5, default 2), `as_of`. |
| `GET` | `/v1/graph/{entity_id}/dependents` | `consumer` | k-hop reverse traversal (entities that depend on this one). Parameters: `depth` (1–5), `edge_types`, `as_of`. |
| `GET` | `/v1/graph/{entity_id}/blast-radius` | `consumer` | Full transitive closure, backed by `closure_cache`. Falls back to recursive CTE if cache is cold or `as_of` is older than 90 days. Parameters: `direction` (`forward`\|`reverse`, default `reverse`), `edge_types`, `depth` (1–5, default 5), `as_of`. |

---

### Entities (generic)

| Method | Path | Role required | Description |
|---|---|---|---|
| `GET` | `/v1/entities` | `consumer` | Generic entity list. Supports `?external_system=<slug>&external_id=<id>` to resolve an entity from an upstream identifier. |

---

### Adoptions

Tracks which tenants consume which capabilities.

| Method | Path | Role required | Description |
|---|---|---|---|
| `GET` | `/v1/adoptions` | `consumer` | List adoptions for the caller's tenant. |
| `POST` | `/v1/adoptions` | `producer` | Record an adoption. |
| `DELETE` | `/v1/adoptions/{adoption_id}` | `producer` | Remove an adoption. |

---

### Annotations

Annotations let any tenant that can see a capability submit structured feedback — bug reports, suggestions, questions, or documentation gaps — against it. The capability's owner tenant triages those annotations by moving them through a status lifecycle. Annotations from different consumer tenants are not visible to each other; only the owner tenant sees the full set.

**Tenant isolation.** Annotation access is scoped through the parent capability. If the capability is invisible to the caller (outside their visibility scope), the service returns HTTP 404 on `POST` and an empty list on `GET` rather than exposing that annotations exist. There is no separate per-annotation visibility grant.

**Status vocabulary.** Annotations carry one of four statuses: `open`, `triaged`, `acknowledged`, `closed`. A new annotation always starts as `open`. Transitions are unrestricted — any forward or reverse move is valid. Setting the status to its current value is a documented no-op: it returns HTTP 200 with the unchanged annotation and does not write an audit entry.

**Category vocabulary.** Exactly five categories are accepted: `bug`, `doc_gap`, `feedback`, `question`, `suggestion`.

| Method | Path | Role required | Description |
|---|---|---|---|
| `POST` | `/v1/capabilities/{capability_id}/annotations` | `consumer`, `producer`, or `admin` | Submit a new annotation on a capability. Returns HTTP 201 with the full annotation resource. |
| `GET` | `/v1/capabilities/{capability_id}/annotations` | `consumer`, `producer`, or `admin` | List active annotations on a capability. Provider sees all; non-provider sees only their own. Cursor-paginated. |
| `PATCH` | `/v1/annotations/{annotation_id}` | `producer` or `admin` | Triage an annotation — set its status and optionally attach a triage note. Only the capability-owner tenant may call this. |
| `DELETE` | `/v1/annotations/{annotation_id}` | `consumer`, `producer`, or `admin` | Soft-delete an annotation. Idempotent. Authorized for the annotation's author or any actor in the capability-owner tenant. |

#### POST /v1/capabilities/{capability_id}/annotations

The caller must have visibility to the capability. The service checks this before writing any row; an invisible capability returns HTTP 404.

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `body` | string | yes | Annotation text. Must be at least one character. |
| `category` | string | yes | One of: `bug`, `doc_gap`, `feedback`, `question`, `suggestion`. |
| `triage_note` | string | no | Optional provider note. Stored but not used for routing. |
| `version_target` | string | no | Optional version string this annotation targets (e.g. `v2.3`). |

**Response (HTTP 201):**

```json
{
  "annotation_id": "a1b2c3d4-0000-0000-0000-000000000001",
  "capability_id": "cap00000-0000-0000-0000-000000000001",
  "author_actor_id": "actor000-0000-0000-0000-000000000001",
  "author_tenant_id": "tenant00-0000-0000-0000-000000000002",
  "body": "The retry header is undocumented.",
  "category": "doc_gap",
  "status": "open",
  "version_target": null,
  "triage_note": null,
  "created_at": "2026-05-12T12:00:00+00:00",
  "updated_at": "2026-05-12T12:00:00+00:00"
}
```

The `warnings` field appears in the response only when the PII scanner fires a warn-level policy on `body`. When absent, treat it as an empty list.

```json
{
  "annotation_id": "a1b2c3d4-0000-0000-0000-000000000001",
  ...
  "warnings": [{"field": "body", "categories": ["CONTACT"]}]
}
```

**Errors:**

| Status | Cause |
|---|---|
| 403/404 | Capability is not visible to the caller's tenant (returns 404 to avoid leaking existence). |
| 422 | `category` not in the closed vocabulary, `body` is empty, or PII scanner blocked the body. |

**Example:**

```bash
curl -X POST https://<host>/v1/capabilities/<capability_id>/annotations \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"body": "The retry header is undocumented.", "category": "doc_gap"}'
```

#### GET /v1/capabilities/{capability_id}/annotations

Returns a cursor-paginated list of active (non-deleted) annotations. Two access paths apply automatically:

- **Provider path** — the caller's tenant owns the capability: returns all active annotations, optionally filtered by status. This is the intended triage view.
- **Author path** — the caller's tenant does not own the capability: returns only annotations where `author_tenant_id` matches the caller's tenant. An empty result is not a 403; it means the caller has no authored annotations on this capability.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Filter by annotation status. One of: `open`, `triaged`, `acknowledged`, `closed`. |
| `cursor` | string | Opaque keyset pagination cursor from a previous response's `next_cursor`. |

**Response (HTTP 200):**

```json
{
  "items": [
    {
      "annotation_id": "a1b2c3d4-0000-0000-0000-000000000001",
      "capability_id": "cap00000-0000-0000-0000-000000000001",
      "author_actor_id": "actor000-0000-0000-0000-000000000001",
      "author_tenant_id": "tenant00-0000-0000-0000-000000000002",
      "body": "The retry header is undocumented.",
      "category": "doc_gap",
      "status": "open",
      "created_at": "2026-05-12T12:00:00+00:00",
      "updated_at": "2026-05-12T12:00:00+00:00"
    }
  ],
  "next_cursor": "eyJ0IjogIjIwMjYtMDUtMTJUMTI6MDA6MDAiLCAiaWQiOiAiYTFiMmMzZDQifQ=="
}
```

`next_cursor` is `null` when no further pages exist. Pass it as `?cursor=<value>` to fetch the next page. The cursor encodes the last item's ingestion timestamp and ID; it is opaque and must not be constructed or parsed by the client. An invalid cursor returns HTTP 422.

Default page size is 50; maximum is 200. The page size is not currently exposed as a query parameter.

#### PATCH /v1/annotations/{annotation_id}

Updates the annotation's status and optionally its triage note. Only the capability-owner tenant may call this endpoint; the token's tenant must match the capability's owner tenant (`producer` or `admin` role required).

Both forward and reverse status transitions are accepted. Setting the status to its current value returns HTTP 200 with the unchanged annotation and no audit entry.

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | string | yes | New status. One of: `open`, `triaged`, `acknowledged`, `closed`. |
| `triage_note` | string | no | Optional provider note to store with this transition. |
| `version_target` | string | no | Optional version string this annotation targets. |

**Response (HTTP 200):** Same shape as the `POST` response above.

**Errors:**

| Status | Cause |
|---|---|
| 403 | Caller's tenant does not own the capability the annotation belongs to. |
| 404 | Annotation does not exist or has been deleted. |
| 422 | `status` not in the closed vocabulary, or PII scanner blocked `triage_note`. |

**Example:**

```bash
curl -X PATCH https://<host>/v1/annotations/<annotation_id> \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"status": "triaged", "triage_note": "Confirmed — adding to the next docs sprint."}'
```

#### DELETE /v1/annotations/{annotation_id}

Soft-deletes the annotation. The row is not removed from the database; `t_invalidated_at` is set to the deletion timestamp and the annotation no longer appears in list or triage responses.

This endpoint is idempotent. Calling it on an already-deleted annotation returns HTTP 204 without error and without emitting a duplicate audit entry.

Two actors may delete an annotation: the actor who authored it (any tenant, any role), or any actor in the capability-owner tenant with `producer` or `admin` role. Authorization is evaluated at the row level, not at the tenant level alone — the author check uses the actor ID, not the tenant ID.

**Response (HTTP 204):** No body.

**Errors:**

| Status | Cause |
|---|---|
| 403 | Caller is neither the annotation's author nor a member of the capability-owner tenant with the required role. |
| 404 | Annotation ID does not exist at all (never created). An already-deleted annotation returns 204, not 404. |

**Example:**

```bash
curl -X DELETE https://<host>/v1/annotations/<annotation_id> \
  -H "Authorization: Bearer <token>"
```

---

### Subscriptions and notifications

Subscriptions register a webhook URL to receive events when a capability changes.

| Method | Path | Role required | Description |
|---|---|---|---|
| `POST` | `/v1/capabilities/{capability_id}/subscriptions` | `consumer`, `producer`, or `admin` | Create a subscription for a specific capability. Accepts UUID or slug-form name. Accepts `Idempotency-Key`. Returns `{"subscription_id": "<uuid>"}`. |
| `GET` | `/v1/capabilities/{capability_id}/subscriptions` | `consumer`, `producer`, or `admin` | List the caller's active subscriptions for a capability. Accepts UUID or slug-form name. Returns only the caller's tenant's subscriptions. `next_cursor` is always `null` (bounded; typically 1–5 rows). Accepts `?view=default\|audit`. |
| `GET` | `/v1/subscriptions` | `consumer` | List all active subscriptions for the caller's tenant (across capabilities). |
| `PATCH` | `/v1/subscriptions/{subscription_id}` | `consumer`, `producer`, or `admin` | Update a subscription (event kinds, webhook URL, enabled state). Respects `If-Match`. |
| `DELETE` | `/v1/subscriptions/{subscription_id}` | `consumer`, `producer`, or `admin` | Remove a subscription. Idempotent. |
| `GET` | `/v1/notifications` | any authenticated role | Cursor-paginated inbox. Parameters: `?cursor`, `?page_size` (default 50, max 500), `?status=unread\|read\|all` (default `unread`). |
| `POST` | `/v1/notifications/{notification_id}:mark-read` | `consumer`, `producer`, or `admin` | Mark a notification as read. Idempotent; unknown IDs succeed silently. |

Webhook delivery semantics are documented in [overview/vocabulary.md](../01-overview/03-vocabulary.md#subscription-and-notification).

---

### External IDs

| Method | Path | Role required | Description |
|---|---|---|---|
| `GET` | `/v1/external-ids` | `consumer` | List external-ID mappings for the caller's tenant. |
| `POST` | `/v1/external-ids` | `producer` | Register an external-system identifier for an entity. |
| `DELETE` | `/v1/external-ids/{mapping_id}` | `producer` | Remove a mapping. |

---

### Interfaces, artifacts, operations, concepts, breaking changes

| Method | Path | Role required | Description |
|---|---|---|---|
| `PUT` | `/v1/capabilities/{capability_id}/interface` | `producer` or `admin` | Replace the capability's declared interface surface (JSON Schema or OpenAPI). Normalizes, soft-supersedes the prior version. |
| `GET` | `/v1/capabilities/{capability_id}/interface` | any authenticated | Read the active interface surface. Parameters: `?as_of=` (time-travel), `?view=default\|audit`. |
| `GET` | `/v1/capabilities/{capability_id}/artifacts` | any authenticated | Cursor-paginated artifact list. Parameters: `?cursor`, `?page_size` (default 20, max 200), `?category` (comma-separated), `?fields` (sparse field selection), `?view=default\|audit`. |
| `POST` | `/v1/capabilities/{capability_id}/artifacts` | `producer` or `admin` | Attach a new artifact (build output, release note, published package). PII-scanned before write. |
| `GET` | `/v1/capabilities/{capability_id}/artifacts/{fact_id}` | any authenticated | Retrieve a single artifact. Parameters: `?fields`, `?view=default\|audit`. |
| `DELETE` | `/v1/capabilities/{capability_id}/artifacts/{fact_id}` | `producer` or `admin` | Soft-delete an artifact. Idempotent. |
| `GET` | `/v1/operations/{entity_id}` | any authenticated | Retrieve a single operation entity. Returns `ETag`. |
| `POST` | `/v1/operations` | `producer` or `admin` | Create an operation entity, optionally linked to a parent capability via `operation_of` edge. |
| `PATCH` | `/v1/operations/{entity_id}` | `producer` or `admin` | Update operation attributes. Respects `If-Match`. |
| `DELETE` | `/v1/operations/{entity_id}` | `producer` or `admin` | Soft-delete. Idempotent. |
| `GET` | `/v1/concepts/{entity_id}` | any authenticated | Retrieve a single concept entity. Returns `ETag`. |
| `POST` | `/v1/concepts` | `producer` or `admin` | Create a concept entity, optionally linked to a parent capability via `concept_of` edge. |
| `PATCH` | `/v1/concepts/{entity_id}` | `producer` or `admin` | Update concept attributes. Respects `If-Match`. |
| `DELETE` | `/v1/concepts/{entity_id}` | `producer` or `admin` | Soft-delete. Idempotent. |
| `POST` | `/v1/capabilities/{capability_id}/preview-version` | `producer` or `admin` | Read-only advisor: preview the breaking-change impact of a proposed version bump. Returns diff classification, per-element changes, affected-consumer list (cross-tenant entries anonymised), and a release-notes scaffold. |

Full request/response schemas, including all field types and validation rules, are in the OpenAPI document at `/openapi.json`. The interactive browser is at `/docs`.

#### Artifact query parameters

Both artifact list and single-artifact GET accept two additional query parameters.

**`?fields=<csv>`** — Sparse field selection. Controls which response fields are populated.

| Context | Default fields | Body included by default? |
|---|---|---|
| List (`GET /artifacts`) | `fact_id`, `category`, `title`, `body_format`, `created_at`, `created_by_display_name` | No — `body` must be requested explicitly |
| Single-get (`GET /artifacts/{fact_id}`) | All fields above plus `body` | Yes |

Allowed field names: `fact_id`, `category`, `title`, `body`, `body_format`, `created_at`, `created_by_display_name`. `fact_id` is always included regardless of the value passed. An unknown field name returns HTTP 422.

Example — request body on the list endpoint:

```
GET /v1/capabilities/<capability_id>/artifacts?fields=fact_id,title,body
```

**`?view=default|audit`** — Response shape selector. `default` (the default) returns the standard UI-shaped response. `audit` additionally includes the bitemporal columns below. An unknown value returns HTTP 422.

| Audit field | Type | Description |
|---|---|---|
| `tenant_id` | string (UUID) | Tenant that owns the artifact |
| `entity_id` | string (UUID) | Parent capability UUID |
| `is_authoritative` | boolean | Whether this fact row is the authoritative source (vs sync-ingested) |
| `valid_from` | ISO-8601 datetime | Start of the fact's valid-time interval |
| `valid_to` | ISO-8601 datetime or null | End of the valid-time interval; null means currently valid |
| `ingested_at` | ISO-8601 datetime | When the fact row was written |
| `invalidated_at` | ISO-8601 datetime or null | When the fact was soft-deleted; null means active |

The interface endpoint (`GET /v1/capabilities/{capability_id}/interface`) also accepts `?view=default|audit` for client uniformity. The interface service returns a composed record rather than raw attribute rows, so `view=audit` is a no-op there — no additional bitemporal fields are added. Use `?as_of=` for interface time-travel instead.

---

### Admin endpoints

Admin endpoints are under `/v1/admin/`. They require the `admin` role unless noted.

| Path prefix | What it manages |
|---|---|
| `/v1/admin/tenants` | Tenant CRUD |
| `/v1/admin/tenants/{tenant_id}/actors` | Actor (user/service account) management |
| `/v1/admin/tenants/{tenant_id}/tokens` | API token mint + revoke |
| `/v1/admin/tenants/{tenant_id}/roles` | Role assignment |
| `/v1/admin/tenants/{tenant_id}/vocabulary` | Closed-vocabulary management |
| `/v1/admin/tenants/{tenant_id}/sync-sources` | External-source connector configuration |
| `/v1/admin/tenants/{tenant_id}/pii-policies` | PII scanning policies |
| `/v1/admin/tenants/{tenant_id}/progression-definitions` | Progression state-machine management |
| `/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides` | Single-use gate bypass |
| `/v1/admin/audit` | Audit log query |
| `/v1/admin/lifecycle` | Lifecycle management |

---

### Whoami

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/whoami` | Returns the actor, tenant, roles, and token metadata for the current credential. Useful as a first call to confirm which tenant a token resolves to. |

---

## Error shape

All error responses use a consistent JSON envelope:

```json
{
  "detail": "Human-readable error message",
  "code": "machine_readable_code"
}
```

Common HTTP status codes:

| Status | Meaning |
|---|---|
| 400 | Malformed request (missing required field, invalid value) |
| 401 | Missing or invalid bearer token |
| 403 | Valid token but insufficient role |
| 404 | Entity or resource not found (within the caller's tenant scope) |
| 409 | Conflict (pre-flight scan timeout, ETag mismatch, idempotency collision) |
| 412 | `If-Match` header does not match current ETag |
| 422 | Validation error (unknown vocabulary value, progression gate rejection) |
| 429 | Rate limit exceeded |

---

## HTTP method routing

By default (`REGISTRY_HTTP_METHODS_MODE=rest`) the service registers standard HTTP verbs (`PATCH`, `DELETE`). For deployments behind proxies that strip non-GET/POST verbs, set `REGISTRY_HTTP_METHODS_MODE=post_only` to expose POST-tunneled aliases instead:

| Mode | Mutation verb | Example |
|---|---|---|
| `rest` (default) | `PATCH` / `DELETE` | `PATCH /v1/capabilities/{id}` |
| `post_only` | `POST` with action suffix | `POST /v1/capabilities/{id}:update` |
| `both` | both registered | both forms active simultaneously |

The separator between the resource path and the action suffix is controlled by `REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR` (`colon` → `/{id}:update`; `slash` → `/{id}/update`).

See [configuration.md](03-configuration.md) for the full env-var reference.
