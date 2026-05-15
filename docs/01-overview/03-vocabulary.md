# Concepts

Vocabulary for API integrators. Each entry answers "what does this term mean when you see it in an API request or response?" For the architectural rationale behind these concepts, see [how-its-structured.md](02-how-its-structured.md).

---

## Roles

Every token is scoped to a **tenant** and carries one or more **roles** that gate access to endpoints:

| Role | Who holds it | What it unlocks |
|------|-------------|-----------------|
| `consumer` | Any authenticated caller | Read operations: list, get, search, graph traversal |
| `producer` | Teams that publish capabilities | Write operations: create, update, delete, annotate, subscribe |
| `admin` | Tenant administrators | All operations including user management and vocabulary changes |
| `auditor` | Read-only audit access | Audit log queries |

Roles are checked per-request. A request with a valid token but an insufficient role returns HTTP 403. Confirm your token's roles by calling `GET /v1/whoami` (REST) or the `whoami` MCP tool.

---

## Tenant

A **tenant** is the top-level isolation scope. Every resource in the API — capabilities, annotations, subscriptions, and so on — belongs to exactly one tenant. A token scoped to tenant A cannot read or write tenant B's data.

In responses, `tenant_id` is a UUID. The human-readable identifier is the `slug` (e.g. `"platform-eng"`). Use `GET /v1/whoami` to confirm which tenant your token resolves to before making write calls.

Tenants are provisioned by an operator. In local development, `make dev-token` seeds a `dev` tenant automatically.

---

## Entity

An **entity** is the primary tracked object. The API uses "capability" and "entity" somewhat interchangeably: the `/v1/capabilities` endpoints are the main CRUD surface; the `/v1/entities` endpoint is a generic lookup by external ID. Every entity carries:

- `entity_id` — UUID, stable forever. Safe to store in external systems.
- `name` — slug-form string (lowercase, hyphens, 1–200 chars). Most endpoints accept this in place of the UUID.
- `entity_type` — a tenant-scoped closed vocabulary value (e.g. `service`, `library`, `component`). The vocabulary is managed via the admin API.
- `lifecycle` — the entity's current lifecycle state. See [Lifecycle states](#lifecycle-states) below.
- `visibility` — controls which tenants can read this entity. See [Visibility](#visibility) below.

### Entity handles

Wherever an endpoint accepts `{entity_id}` in the path, you may supply either the UUID or the slug-form name:

```
GET /v1/capabilities/salt-design-system
GET /v1/capabilities/01234567-89ab-cdef-0123-456789abcdef
```

Both resolve to the same record. Prefer UUIDs when storing references; slugs can be renamed.

### Entity sub-types

The API exposes specialized sub-types as first-class resources. Each is an entity with additional semantics:

| Sub-type | Endpoint prefix | Meaning |
|----------|----------------|---------|
| **Capability** | `/v1/capabilities` | A published artefact (service, library, design system, agent, etc.) |
| **Operation** | `/v1/operations` | A callable action within a capability's interface |
| **Concept** | `/v1/concepts` | A domain concept documented within a capability |
| **Artifact** | `/v1/capabilities/{id}/artifacts` | A build output, release note, or published package attached to a capability |
| **Interface** | `/v1/capabilities/{id}/interface` | The declared API surface of a capability (JSON Schema or OpenAPI 3.x) |

---

## Lifecycle states

Each entity carries a `lifecycle` label. The set of valid values is a closed vocabulary managed per tenant; the default progression is:

```
alpha → beta → ga → deprecated → retired
```

Valid forward transitions are `alpha→beta`, `beta→ga`, `ga→deprecated`, `deprecated→retired`. `alpha→deprecated` and `alpha→retired`, `beta→deprecated` and `beta→retired` are also valid (early retirement path). `retired` is terminal — no transitions out.

Lifecycle does not enforce any state-machine rules by itself. **Progression definitions** (see below) add gate enforcement on top of the lifecycle vocabulary. A lifecycle transition that fails a gate returns HTTP 422.

The `lifecycle` field appears on every capability in list and get responses. You can filter list endpoints by `?lifecycle=<value>`.

---

## Attribute

**Attributes** are typed key-value pairs attached to an entity. They are **bi-temporal**: each attribute row carries a valid-time range (`t_valid_from` / `t_valid_to`) and a transaction-time record. What this means for API callers:

- `t_valid_from` / `t_valid_to` in an attribute response tell you the time window during which this value was considered current.
- `?as_of=<ISO-8601 UTC>` on any read endpoint returns data as it was valid at that point in time. Omitting `as_of` returns current state.

See [Bi-temporal time travel](#bi-temporal-time-travel) below for the full `?as_of=` contract.

---

## Fact

**Facts** are timestamped observations or notes attached to an entity — free-text or structured. They are vectorized for semantic search (`GET /v1/search`). Facts appear in entity responses and are the primary carrier of narrative metadata (descriptions, decisions, links).

---

## Edge

**Edges** record directed relationships between entities. Edge types are drawn from a per-tenant closed vocabulary; common values are `depends_on`, `composes`, and `uses`.

Edges are bi-temporal (same `?as_of=` semantics as attributes). Graph traversal endpoints use edges:

- `GET /v1/graph/{entity_id}/dependencies` — forward traversal (what this entity depends on)
- `GET /v1/graph/{entity_id}/dependents` — reverse traversal (what depends on this entity)
- `GET /v1/graph/{entity_id}/blast-radius` — full transitive closure

---

## Bi-temporal time travel

Most read endpoints accept `?as_of=<ISO-8601 UTC datetime>`. Supplying it returns data as it was valid at that moment. Omitting it returns current state.

```
GET /v1/capabilities?as_of=2025-01-01T00:00:00Z
```

This applies to capabilities, attributes, edges, and interfaces. For a full list of endpoints that support `?as_of=`, see [api.md](../05-reference/01-api.md).

---

## Visibility

Every entity has a `visibility` value that determines which tenants can read it. The service returns HTTP 404 (not 403) when a caller requests an entity outside their visibility scope, to avoid leaking the existence of private entities.

| Value | Who can read |
|-------|-------------|
| `private` | Owner tenant only (default) |
| `tenant-shared` | Owner tenant plus tenants listed in `shared_with_tenants` |
| `public` | All tenants in the deployment |

The owner tenant changes an entity's visibility via `PATCH /v1/capabilities/{entity_id}/visibility`. The `tenant-shared` mode requires a non-empty `shared_with_tenants` list (UUIDs of the tenants to grant access to).

**Effect on annotations:** if a capability is not visible to the caller, `POST` on its annotations endpoint returns HTTP 404 and `GET` returns an empty list — not an error.

---

## Adoption

An **adoption** is a durable record that a consumer tenant depends on a capability. Adoptions are written by the consumer (`POST /v1/adoptions`) and are visible to the capability's owner. They create a `provides_to` edge in the dependency graph and can trigger an automatic subscription to lifecycle events.

Use adoptions when you want the capability owner to see who is depending on them (for impact assessment when breaking changes are planned).

---

## Subscription and notification

A **subscription** registers a webhook URL to receive events when a capability changes. The caller provides a webhook URL and a signing secret; the service delivers a signed POST for each event.

A **notification** is the event record. Notifications can also be read via the polling API (`GET /v1/notifications`) without setting up a webhook — useful for agents that prefer to poll rather than receive pushes.

Webhook deliveries are signed with HMAC-SHA256 in the `X-Registry-Signature-256: sha256=<hex>` header. Retries use exponential backoff capped at 24 hours. A delivery that fails with a 4xx response (except 408/429) is marked `failed` immediately — those are caller errors, not transient failures.

See [api.md](../05-reference/01-api.md#subscriptions-and-notifications) for endpoint details.

---

## External ID

An **external ID** maps an entity to its identifier in an external system — for example, an npm package name (`@salt-ds/core`) or a GitHub repository slug. The mapping is registered via `POST /v1/external-ids`.

Callers use external IDs to look up an entity when they have a package name or other upstream identifier but not the registry UUID:

- REST: `GET /v1/entities?external_system=npm&external_id=@salt-ds/core`
- MCP: `lookup_by_external_id` tool

The `external_system` value is a slug registered by an admin (`/v1/admin/tenants/{id}/sync-sources`).

---

## Annotation

An **annotation** is structured feedback submitted against a capability by any tenant that can see it. Categories, statuses, and visibility rules are described in [api.md](../05-reference/01-api.md#annotations). The key integrator-vocabulary points:

**Category vocabulary** (closed; all five values are valid at submit time):

| Category | Meaning |
|----------|---------|
| `bug` | Defect in the capability's behavior |
| `doc_gap` | Missing or incorrect documentation |
| `feedback` | General feedback |
| `question` | Question for the producer |
| `suggestion` | Enhancement request |

**Status vocabulary** (the triage lifecycle):

| Status | Meaning |
|--------|---------|
| `open` | Newly submitted; not yet reviewed |
| `triaged` | Producer has acknowledged and classified it |
| `acknowledged` | Producer has committed to a response |
| `closed` | Resolved, won't fix, or superseded |

New annotations always start as `open`. Any status transition is valid in either direction — there is no enforced order. Setting the status to its current value is a no-op (returns HTTP 200, no audit entry written).

**Visibility rules:**
- The **provider** (capability owner tenant) can see all annotations on their capabilities.
- A **consumer** can see only the annotations they authored on a capability.
- Annotations from different consumer tenants are not visible to each other.
- If the underlying capability is not visible to the caller, annotation endpoints return 404 or an empty list, not 403.

**Deletion:** Annotations are soft-deleted (`DELETE /v1/annotations/{id}`). The author or any actor in the capability-owner tenant with `producer` or `admin` role may delete. Calling delete on an already-deleted annotation returns HTTP 204 without error.

---

## Progression definition

A **progression definition** is a state machine attached to an entity type within a tenant. It defines which lifecycle states are valid, which transitions between states are allowed, and which **gates** (attribute conditions) must be satisfied before a transition proceeds.

When a lifecycle transition is attempted, the active progression definition is checked. If a required gate is not satisfied the transition is rejected with HTTP 422, unless:

- The definition is in **advisory mode** (`is_advisory=true`) — the violation is recorded but the write proceeds.
- A **progression override** is active for that entity and gate.

The operator manages progression definitions via the admin API. See [operations/progression.md](../06-operations/02-progression.md) for procedures.

---

## Progression override

A **progression override** allows a single entity to bypass one specific gate on one specific transition, within a stated time window and reason. Each override is single-use: once consumed it cannot be applied again, and an audit record is written before the override row is inserted.

Overrides are created and managed via `/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides`.

---

## Breaking change

A **breaking change preview** (`POST /v1/capabilities/{capability_id}/preview-version`) is a read-only advisor: provide a proposed version bump and receive a diff classification, per-element changes, and an anonymized impact list of affected consumers. No write occurs — the endpoint does not publish anything.

Use the preview before publishing an interface update to assess impact before consumers see the change.

---

## RSAM auth mode

In the default `oidc` auth mode, the caller's tenant scope is embedded in the JWT claims. In `rsam` mode the service calls an external entitlement reference API to resolve which tenants the caller holds authority over.

For API callers, the practical difference is: in RSAM mode, your token may resolve to multiple tenants. Call `GET /v1/whoami` to confirm the resolved tenant scope before making write calls. See [authorization.md](05-authorization.md#rsam-mode--internal-directory-authority) for configuration details.

---

## PII scanner

The PII scanner runs at write time on annotation bodies and triage notes. If a scan returns a `block` policy, the write is rejected with HTTP 422 and a `pii_detected` error code. If a scan returns a `warn` policy, the write proceeds and the response includes a `warnings` field listing the detected categories.

```json
{
  "annotation_id": "...",
  "warnings": [{"field": "body", "categories": ["CONTACT"]}]
}
```

The scanner does not run on reads. PII scan policies are configured per tenant via the admin API.

---

## Where to go next

| I want to… | Go to |
|---|---|
| Run the service locally | [get-started/quickstart.md](../02-get-started/01-quickstart.md) |
| Understand who's making a request (OIDC, JWT, claims) | [overview/authentication.md](04-authentication.md) |
| Understand what they can do (entitlements, roles, RSAM) | [overview/authorization.md](05-authorization.md) |
| Find every API endpoint | [reference/api.md](../05-reference/01-api.md) |
| Configure the service | [reference/configuration.md](../05-reference/03-configuration.md) |
| Operate progressions | [operations/progression.md](../06-operations/02-progression.md) |
| Operate the database / DR | [operations/ops.md](../06-operations/01-ops.md) |
| Call from an AI agent | [reference/mcp-tools.md](../05-reference/02-mcp-tools.md) |
| Understand architectural design decisions | [overview/how-its-structured.md](02-how-its-structured.md) |
