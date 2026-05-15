<!--
  title: Use case — AI agent capability discovery
  audience: end-user agent, integrator (agent builder)
  archetype: explanation (use-case scenario)
  summary: How an AI agent uses the MCP surface to find, evaluate, and adopt capabilities at planning time.
-->

# Use case: AI agent capability discovery

An AI agent planning a new build needs to know what shared capabilities already exist before deciding what to implement from scratch. The registry's MCP surface gives the agent a single, [tenant](../01-overview/03-vocabulary.md#tenant)-scoped view of the capability catalog — with semantic search, graph traversal for dependency mapping, and structured lifecycle state — all readable without leaving the agent's tool-calling loop.

The agent authenticates with the same bearer token used for REST, calls `whoami` to confirm its tenant scope, then uses `search_capabilities` with a natural-language description of what it needs. It can traverse edges to understand dependencies, read bi-temporal attributes to check current interface contracts, and submit annotations to flag gaps back to the producer — all within a single session.

**Before calling any tool:** the [tenant](../01-overview/03-vocabulary.md#tenant) must be provisioned, the MCP endpoint must be reachable at `GET /mcp/sse`, and a valid bearer token must be available. See [authentication.md](../01-overview/04-authentication.md) for how to obtain a token.

---

## The flow

### Confirm tenant scope

Every MCP session begins with `whoami`. The response names the tenant the token resolves to, the roles the caller holds, and the token expiry. An agent that skips this step risks calling write tools with insufficient roles and seeing unhelpful `ToolError: forbidden` responses.

```json
{ "tool": "whoami", "arguments": {} }
```

A typical response:

```json
{
  "tenant_id": "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
  "tenant_slug": "product-eng",
  "roles": ["consumer", "producer"],
  "token_expires_at": "2026-06-01T00:00:00Z"
}
```

The `roles` array determines what the agent can do: `consumer` covers all read operations (search, get, graph traversal, list); `producer` and above are required for writes (annotations, adoptions). See [vocabulary.md — Roles](../01-overview/03-vocabulary.md#roles).

### Search the catalog

With the tenant scope confirmed, the agent calls `search_capabilities` with a free-text query. The search combines vector similarity, full-text matching, and graph proximity to return ranked results, all scoped to the caller's tenant automatically.

```json
{
  "tool": "search_capabilities",
  "arguments": {
    "q": "design system component library",
    "lifecycle": "active",
    "top_k": 5
  }
}
```

Each result carries an `entity_id`, `name`, `entity_type`, `lifecycle`, and a relevance `score`. The agent uses these to identify candidates worth inspecting further. Optional filters — `entity_type` and `lifecycle` — narrow results when the agent already knows what kind of capability it needs. The `as_of` parameter applies bi-temporal time travel if the agent is evaluating what was available at an earlier point in time.

Alternatively, if the agent is examining existing code and encounters a known external identifier — an npm package name in a `package.json`, a GitHub repo slug in a Dockerfile — it can call `lookup_by_external_id` directly instead of searching:

```json
{
  "tool": "lookup_by_external_id",
  "arguments": {
    "external_system": "npm",
    "external_id": "@salt-ds/core"
  }
}
```

This resolves the identifier to the full capability record without a search step, as long as the mapping has been registered by an admin. If no mapping exists, the tool returns `{"found": false, ...}`.

### Inspect a capability

Once the agent identifies a promising result, it calls `get_capability` with the capability's UUID or slug-form name. The response includes the full record: typed attributes, facts, current interface contract, and lifecycle state.

```json
{
  "tool": "get_capability",
  "arguments": {
    "entity_id": "salt-design-system",
    "include": "interface,depends_on"
  }
}
```

The `include` parameter expands sub-resources inline. `interface` returns the declared API surface (JSON Schema or OpenAPI 3.x). `depends_on` returns the direct dependency edges. `components` expands sub-components; `external_ids` lists the capability's external-system mappings. Each expansion is capped at 200 items — a `truncated: true` flag and a `next` URL signal overflow.

The [lifecycle](../01-overview/03-vocabulary.md#lifecycle-states) field tells the agent where the capability sits in the progression (`alpha`, `beta`, `ga`, `deprecated`, `retired`). An agent evaluating a `beta` capability for a production dependency may treat that differently from a `ga` one. The bi-temporal `as_of` parameter lets the agent ask "what was this capability's interface contract as of three months ago?" — useful when tracing a regression.

### Map dependencies

Before adopting, a well-designed agent checks what a candidate capability brings in transitively. `get_dependencies` performs a k-hop forward traversal (what the capability depends on); `get_dependents` performs the reverse (what depends on it). For a complete transitive closure — e.g., "everything that would be affected if this library changed" — `get_blast_radius` uses the pre-computed closure cache and is faster for large graphs.

```json
{
  "tool": "get_dependencies",
  "arguments": {
    "entity_id": "salt-design-system",
    "depth": 2
  }
}
```

The response enumerates directed edge objects across the subgraph, letting the agent reason about transitive risk before committing to an adoption. For graph concepts, see [vocabulary.md — Edge](../01-overview/03-vocabulary.md#edge).

### Declare an adoption via REST

There is no `adopt_capability` MCP tool. Adoptions are recorded through the REST API: `POST /v1/capabilities/{provider_cap_id}/adoptions`. Adoption is capability-scoped — the URL identifies the provider capability the consumer tenant is declaring a dependency on. The adoption row appears in the producer's impact list and can trigger automatic subscription to lifecycle events on that capability.

An agent that has completed its discovery session and decided to depend on a capability should use the REST endpoint to record the adoption. See [vocabulary.md — Adoption](../01-overview/03-vocabulary.md#adoption) for the full contract.

### Submit an annotation if a gap is found

If the agent evaluates a capability and finds a problem — a missing interface field, an undocumented behavior, a suspected bug — it calls `submit_annotation` to attach structured feedback to the capability record. The capability owner can then triage it.

```json
{
  "tool": "submit_annotation",
  "arguments": {
    "capability_id": "01234567-89ab-cdef-0123-456789abcdef",
    "body": "The retry-after header is not documented in the interface contract.",
    "category": "doc_gap"
  }
}
```

`capability_id` must be a UUID here — slugs are not accepted. Use the `entity_id` from the `get_capability` response. The `category` must be one of the closed vocabulary values: `feedback`, `bug`, `suggestion`, `question`, `doc_gap`. The body is PII-scanned before storage; a block-level match raises a `ToolError` naming the detected categories. Annotations with `warnings` (a warn-level match) are stored but the response includes the warnings array.

An agent can check the status of its own previously submitted annotations with `list_my_annotations`, passing the capability UUID to scope the results. The annotation lifecycle runs `open → triaged → acknowledged → closed`; only the capability owner's tenant can advance it with `triage_annotation`.

---

## What this surface gives an agent that a raw catalog would not

An agent calling the registry's MCP surface gets several properties that a flat catalog does not provide:

- **Tenant-scoped results by default.** Every tool call is bounded to the caller's tenant. The agent never needs to filter — the service enforces isolation at the query layer. Capabilities outside the caller's visibility scope are invisible, not merely hidden.
- **Structured lifecycle and interface metadata.** The agent can evaluate whether a capability is `ga` or still `beta`, read its declared interface in machine-parseable form, and time-travel to earlier states with `as_of`. This is enough to make adoption decisions without reading documentation.
- **Graph-native dependency traversal.** Multi-hop dependency and blast-radius queries are first-class operations. An agent evaluating a candidate can immediately answer "what does this pull in?" and "what would break if this changed?" without constructing traversal logic itself.
- **Audit trail of adoptions.** Recording an adoption via REST creates a durable consumer-visible record that the capability owner can use for impact assessment. This is the channel through which an agent's dependency decisions become visible to the team maintaining the capability.

---

## Where this use case connects to others

If the agent wants to subscribe to lifecycle events on a capability it has adopted — receiving a webhook notification when a breaking change is previewed or a deprecation is announced — that is covered in [event-driven consumers](04-event-driven-consumers.md). Subscriptions require the REST API; `list_notifications` in the MCP surface provides a polling alternative for agents that prefer pull access.

For agents operating as both consumers and producers — adopting upstream capabilities and republishing higher-level abstractions to their own downstream consumers — see [layered abstractions](06-layered-abstractions.md).

For the AISDLC pattern, where each pipeline stage is a registered capability and agents discover and invoke stages via this same MCP flow, see [AISDLC pipeline](07-aisdlc-pipeline.md).

---

## Read next

- [MCP Tools Reference](../05-reference/02-mcp-tools.md) — full parameter tables and response shapes for every tool mentioned on this page
- [Authentication](../01-overview/04-authentication.md) — how to obtain a bearer token for the MCP surface
- [Authorization](../01-overview/05-authorization.md) — how role grants and tenant selection scope the token
- [How the registry is structured](../01-overview/02-how-its-structured.md) — data model, tenant isolation, and API surface overview
- [Subscribe to events](../04-guides/02-subscribe-to-events.md) — guide for setting up webhook delivery after an adoption
- [Event-driven consumers](04-event-driven-consumers.md) — use case for the full subscription and notification lifecycle
