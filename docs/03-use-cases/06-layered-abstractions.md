<!--
  title: Use case — Layered abstractions: consumers becoming producers
  audience: integrator (producer), integrator (consumer), operator
  archetype: explanation (use-case scenario)
  summary: How the same tenant can consume upstream capabilities and republish higher-level abstractions to its own downstream consumers, forming a layered dependency graph.
-->

# Use case: Layered abstractions — consumers becoming producers

The registry is not a flat producer-to-consumer graph. Any tenant that adopts capabilities from an upstream producer can itself publish new capabilities built on top of those primitives, becoming a producer to its own downstream consumers. A design system team, for example, consumes low-level color tokens and spacing primitives from a platform team, composes them into Button, Card, and Modal components, and publishes those components to product teams. The same pattern appears in API gateways wrapping raw services, data products composed from raw datasets, and ML feature pipelines assembled from base feature stores.

This use case explains how adoption and publication interact across layers, how lifecycle changes in upstream capabilities propagate, and how the registry tracks provenance through the full dependency chain so that any consumer can trace a capability back to its original source.

---

## The scenario

The design system team sits between two layers. Upstream, they depend on a platform team that publishes color tokens, spacing scales, and icon sets as primitive capabilities. Downstream, they publish Button, Card, Modal, and Form components that product teams adopt. The registry captures both sides: the design system tenant has adoption records pointing up to the primitives, and a separate set of published capabilities that product teams adopt in turn.

The same pattern appears across domains:

- An API gateway team consumes raw service capabilities from backend teams and publishes composed, rate-limited facades to external partners.
- A data platform team consumes raw dataset capabilities and publishes curated, schema-validated data products.
- An ML team consumes base feature-store capabilities and publishes derived feature pipelines.

In all three cases, the middle layer tenant is simultaneously a consumer (from the upstream layer's perspective) and a producer (from the downstream layer's perspective). The registry represents both relationships through standard adoption and capability records — no special mode is required.

---

## The role flip — being consumer and producer at once

When the design system team adopts `color-tokens` from the platform team, an adoption record is created under the platform team's capability. The design system team's `author_tenant_id` is stamped on that record. The platform team, as provider, can see all their adopters; the design system team sees only their own adoption.

Separately, the design system team registers their own `button-component` capability. Product teams adopt that capability. The design system team is the provider here — they see all their adopters; each product team sees only their own adoption.

**Neither role cancels the other.** A single tenant can have:

- An arbitrary number of adoption records (as consumer) pointing at other tenants' capabilities.
- An arbitrary number of capability records (as producer) that other tenants adopt.

Visibility rules apply at each layer independently. An upstream primitive can be `tenant-shared` with the design system team while the design system team publishes its components as `public`. The crossing of tenant boundaries is handled by the visibility chokepoint on every read — each layer controls who can see its own capabilities.

---

## Adoption and re-publish workflow

### Step 1 — Adopt the upstream primitive

The design system team adopts the platform team's `color-tokens` capability with a version pin that anchors the interface contract at adoption time:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<color-tokens-entity-id>/adoptions" \
  -H "Authorization: Bearer <design-system-consumer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "version_pin": "3.2.0",
    "intent": "Color tokens consumed by button, card, and modal components"
  }' | jq '{adoption_id, version_pin}'
```

The `version_pin` is stored on the adoption record. When the platform team later issues version `4.0.0` with breaking changes, the design system team's pin is still `3.2.0` — the registry can surface the drift.

### Step 2 — Register the derived capability

The design system team registers `button-component` as their own capability, recording provenance in attributes. There is no special provenance field type — use structured attributes to make the lineage explicit:

```bash
curl -s -X POST https://registry.example.com/v1/capabilities \
  -H "Authorization: Bearer <design-system-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "button-component",
    "entity_type": "capability",
    "attributes": {
      "description": "Accessible button primitive for all product surfaces",
      "composes": "<color-tokens-entity-id>",
      "interface_version": "1.0.0",
      "owner_team": "design-system"
    }
  }' | jq '{entity_id, name}'
```

The `composes` attribute value is the UUID of the upstream capability. This makes the dependency legible to any reader of the registry — human or agent — without requiring a separate graph edge API call (though edge-based queries are more powerful for traversal; see below).

### Step 3 — Declare an edge for graph traversal

To make the dependency traversable via the graph endpoints, add an explicit edge. Edge relationship types are per-tenant vocabulary; `composes` and `depends_on` are common choices:

```bash
# First, seed the edge relationship vocabulary if not already present
curl -s -X POST \
  https://registry.example.com/v1/admin/vocabularies/edge_rel \
  -H "Authorization: Bearer <design-system-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"value": "composes"}' | jq .value
```

Then record the relationship as an attribute with that vocabulary value (the graph endpoints traverse the `depends_on` / `composes` attribute edges stored on capabilities).

### Step 4 — Set visibility and lifecycle for the derived capability

```bash
# Make button-component visible to product teams
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<button-component-entity-id>/visibility:set-visibility" \
  -H "Authorization: Bearer <design-system-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{"visibility": "public"}' | jq '{entity_id, visibility}'

# Advance to beta
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<button-component-entity-id>/lifecycle:update" \
  -H "Authorization: Bearer <design-system-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{"state": "beta"}' | jq .lifecycle
```

---

## Lifecycle ripples — when upstream deprecates

When the platform team deprecates `color-tokens` v3.x and introduces `design-tokens` as the replacement, the change propagates through the registry in observable steps.

### What the design system team sees

The platform team advances `color-tokens` to `deprecated` and links the replacement with a `replaced_by` attribute:

```bash
# Platform team marks color-tokens deprecated and names the replacement
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<color-tokens-entity-id>/lifecycle:update" \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{"state": "deprecated"}' | jq .lifecycle
```

The design system team's subscription delivers a `lifecycle.state_changed` event to their webhook. If they haven't configured a webhook, the event appears in `GET /v1/notifications`:

```bash
curl -s "https://registry.example.com/v1/notifications?status=unread" \
  -H "Authorization: Bearer <design-system-consumer-token>" \
  | jq '.items[] | select(.kind == "lifecycle.state_changed") | {notification_id, capability_id: .payload.entity_id, new_state: .payload.state}'
```

### Options for the design system team

**Option A — Re-pin to the replacement.** Adopt `design-tokens`, update the `button-component`'s `composes` attribute to point at the new capability, and update the `version_pin` on the adoption:

```bash
# Adopt the replacement
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<design-tokens-entity-id>/adoptions" \
  -H "Authorization: Bearer <design-system-consumer-token>" \
  -H "Content-Type: application/json" \
  -d '{"version_pin": "1.0.0", "intent": "Migration from color-tokens to design-tokens"}' \
  | jq .adoption_id

# Update button-component's provenance attribute
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<button-component-entity-id>:update" \
  -H "Authorization: Bearer <design-system-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{"attributes": {"composes": "<design-tokens-entity-id>", "interface_version": "1.1.0"}}' \
  | jq '{entity_id, name}'
```

**Option B — Pin to a historical version using `?as_of`.** If the design system team needs to freeze development temporarily and cannot migrate immediately, they can read the `color-tokens` capability as it was at any past point:

```bash
# Read the capability as it existed before the deprecation landed
curl -s \
  "https://registry.example.com/v1/capabilities/<color-tokens-entity-id>?as_of=2026-03-01T00:00:00Z" \
  -H "Authorization: Bearer <design-system-consumer-token>" \
  | jq '{entity_id, name, lifecycle, attributes}'
```

The `as_of` parameter time-travels the read along the valid-time axis. The registry returns the state of the capability as it was at that ISO 8601 timestamp — useful for auditing and for consumers that need a stable reference while planning a migration.

**Option C — Deprecate in turn.** If `button-component` depends on the deprecated primitive and there is no migration path yet, the design system team deprecates `button-component` as well, pushing the lifecycle ripple to their downstream product team consumers. The pattern is the same: advance lifecycle to `deprecated`, set a `replaced_by` attribute pointing at any successor, and let subscriptions carry the event downstream.

---

## Provenance and lineage across layers

To reconstruct the full dependency chain from a leaf capability (`button-component`) back to the original primitive (`color-tokens`), use the blast-radius endpoint traversing upstream:

```bash
curl -s \
  "https://registry.example.com/v1/capabilities/<button-component-entity-id>/blast-radius?direction=upstream&depth=5" \
  -H "Authorization: Bearer <design-system-consumer-token>" \
  | jq '{node_count, nodes: [.nodes[] | {entity_id, name, tenant_id}]}'
```

To walk the graph from the provider perspective — seeing everything that depends on a primitive — use the downstream direction:

```bash
curl -s \
  "https://registry.example.com/v1/capabilities/<color-tokens-entity-id>/blast-radius?direction=downstream&depth=5" \
  -H "Authorization: Bearer <platform-producer-token>" \
  | jq '{node_count, edge_count}'
```

For cross-tenant graph views, the provider and consumer graph projections summarize what a tenant ships and what it consumes:

```bash
# What does the design system tenant consume?
curl -s "https://registry.example.com/v1/graph/consumer" \
  -H "Authorization: Bearer <design-system-token>" \
  | jq '{total_capabilities, items: [.items[] | {name, provider_tenant_id, version_pin}]}'

# What does the design system tenant publish?
curl -s "https://registry.example.com/v1/graph/provider" \
  -H "Authorization: Bearer <design-system-token>" \
  | jq '{total_capabilities, items: [.items[] | {name, adoption_count}]}'
```

**Bi-temporal attribute pinning.** Every attribute write is stamped with the transaction time at which it was recorded. This means that if the design system team recorded `composes: <color-tokens-entity-id>` at version `3.2.0` and later updated it to point at `design-tokens`, the history of that attribute is preserved. Read historical attribute state with `?as_of=<timestamp>` on the capability endpoint.

---

## See also

- [How the registry is structured](../01-overview/02-how-its-structured.md) — entity types, edge vocabulary, and visibility model
- [Platform team shared registry](02-platform-team-shared-registry.md) — how the upstream layer (platform team) operates
- [Event-driven consumers](04-event-driven-consumers.md) — subscription and webhook delivery for lifecycle events
- [Compliance and audit](08-compliance-and-audit.md) — `?as_of` queries, audit log, and PII scanning
- [API reference](../05-reference/01-api.md) — endpoint contracts for adoptions, capabilities, graph, and notifications
