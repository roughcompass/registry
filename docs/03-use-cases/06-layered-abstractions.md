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

## Sections to fill

- The scenario
  - Design system example: platform primitives → design system components → product teams
  - Other domains: API gateway, data products, ML feature pipelines
- The role flip — same tenant is a consumer upstream and a producer downstream
  - How the registry represents both relationships simultaneously
  - Visibility rules when layers cross tenant boundaries
- Adoption and re-publish workflow
  - Adopting an upstream capability and registering a derived capability
  - Setting provenance metadata (source capability, version pin)
  - Publishing the derived capability with its own lifecycle state
- Lifecycle ripples — upstream deprecation propagates downstream
  - How deprecation of a primitive surfaces to dependent derived capabilities
  - Notifications and event subscriptions for upstream lifecycle changes
  - Options: re-pin to a stable version, migrate to replacement, deprecate in turn
- Provenance and lineage across layers
  - Querying the full dependency graph from a leaf capability back to primitives
  - Using bi-temporal attributes to pin interface contracts at adoption time
- Where to read next
  - Concepts: [How the registry is structured](../01-overview/02-how-its-structured.md)
  - Guide: [Publish a capability](../04-guides/01-publish-a-capability.md)
  - Guide: [Subscribe to events](../04-guides/02-subscribe-to-events.md)
  - Reference: [API reference](../05-reference/01-api.md)
  - Use case: [Event-driven consumers](04-event-driven-consumers.md)
