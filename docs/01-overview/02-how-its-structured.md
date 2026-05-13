# How the registry is structured

This page explains the shape of the system: the core objects, how isolation works, how the API surfaces are organized, and where the moving parts live. For what the registry is and why it exists, see [orientation.md](01-orientation.md).

---

## Tenants and isolation

Every row in every table carries a `tenant_id`. A caller authenticated with
tenant A cannot read or write tenant B's data, no matter what query they issue.
That guarantee is enforced at a single service-layer chokepoint
(`service/visibility.py`): all entity-returning queries funnel through
`filter_entities()` or `assert_visible()`. The conformance suite enforces this
invariant on every PR — bypassing the chokepoint is how cross-tenant data
exposure would happen, so the test gate is a hard block.

Tenants are provisioned by an operator with database access. Each tenant has a
`slug` and a `display_name`. Entities within a tenant can be shared with
specific other tenants (`tenant-shared` visibility) or opened to all tenants in
the deployment (`public`). The default is `private`.

---

## The data model

An **[entity](03-vocabulary.md#entity)** is the primary tracked object. It carries a small set of fixed columns (name, type, lifecycle, visibility, timestamps); richer data is attached via [attributes](03-vocabulary.md#attribute), [facts](03-vocabulary.md#fact), [edges](03-vocabulary.md#edge), and [annotations](03-vocabulary.md#annotation) — see [vocabulary.md](03-vocabulary.md) for the definitions of each term.

[Bi-temporality](03-vocabulary.md#bi-temporal-time-travel) means two independent time axes are tracked for every mutable row: when the data was *valid in the world* (valid time) and when it was *recorded in the database* (transaction time). This lets any caller ask "what did this entity look like as of last quarter?" without touching current data, and makes every write auditable. A transitive closure cache over edges enables fast blast-radius queries without recursive SQL on hot paths.

---

## Progression and governance

Each entity type can have a progression definition: a state machine that
specifies valid lifecycle states (alpha → beta → ga → deprecated → retired),
allowed transitions, and attribute gates that must be satisfied before a
transition proceeds. Definitions are bi-temporal — you can update them without
losing the history of what was enforced before.

An override mechanism allows a single entity to bypass one specific gate within
a time window, for a stated reason. Each override is single-use and generates
an audit record before it is inserted, so the bypass is always traceable.

---

## API surfaces

The registry exposes two parallel surfaces:

- **REST API** at `/v1/` — resource-oriented HTTP endpoints for capabilities,
  attributes, facts, edges, annotations, adoption tracking, subscriptions,
  notifications, external ID mapping, interface/artifact/operation/concept
  management, breaking-change previews, and admin operations. Mutation verbs
  (PATCH, DELETE) can be run in POST-tunneled mode for environments that
  restrict non-GET/POST HTTP methods.
- **MCP surface** at `/mcp/sse` — Model Context Protocol tools for AI-agent
  callers. Tools cover capability lookup, graph traversal, semantic search,
  entity retrieval, notification access, and annotation submission and triage.
  Auth uses the same bearer token as the REST API.

Both surfaces are served by the same FastAPI process. The OpenAPI spec is live
at `/openapi.json`; the MCP tool catalog is validated by the conformance suite
on every PR.

---

## Ingest and sync

The `sync/` package ingests external sources — GitHub repositories, OpenAPI
specs, npm `package.json` files, markdown and ADR corpora, release notes — and
populates entity facts automatically. Each connector follows a two-step
pattern: `fetch` pulls raw data from the external source, `parse` is a pure
function that produces structured records with no I/O or side effects. Connector
credentials come exclusively from environment variables at runtime; they are
never stored in the database.

---

## Extension points

| Extension point | Where to look | What to implement |
|---|---|---|
| New sync connectors | `registry/sync/connectors/` | Subclass `Connector`; implement `fetch` and `parse` |
| Custom PII patterns | `registry/registry/security/pii_patterns/` | Add a pattern module; register in the scanner |
| Progression definitions | Admin API — `POST /v1/admin/progression-definitions` | JSON schema; no code change required |
| Custom vocabulary | Admin API — `POST /v1/admin/vocabulary` | Operator-provisioned; scoped per tenant |
| Additional MCP tools | `registry/registry/api/routers/mcp.py` | Register a new `@mcp_server.tool` handler |

For the API contract shapes, see [reference/api.md](../05-reference/01-api.md) and [reference/mcp-tools.md](../05-reference/02-mcp-tools.md).
