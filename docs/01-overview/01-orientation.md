<!--
  title: Orientation
  audience: evaluator, new reader
  archetype: explanation (orientation)
  summary: What the registry is, why it exists, and the scenarios it enables.
-->

# Orientation

## What this is

**In one sentence:** The registry is a multi-tenant capability catalog that platform teams use to publish and govern shared services, libraries, agents, and other reusable artefacts — and that consumer teams and AI agents use to discover, adopt, and track those artefacts over time.

The registry stores named capabilities and the structured data that makes them useful to reason about: lifecycle state, typed attributes, interface definitions, dependency graphs, adoption records, and consumer feedback. Every piece of data carries a tenant boundary and a full audit trail. Two parallel surfaces expose this data — a REST API at `/v1/` and a Model Context Protocol surface at `/mcp/sse` — so both human developers and AI agents can read and write using the same endpoints without any custom integration layer.

## Why it exists

Platform teams produce shared foundations — APIs, libraries, design systems, data pipelines — that many other teams depend on. As the number of producers and consumers grows, a recurring set of problems appears: consumers can't easily find what exists, producers have no reliable view of who depends on what, breaking changes land without warning, and compliance questions ("who was using this capability in Q3, and was it audited?") require manual investigation.

The registry addresses these problems by giving the catalog a single authoritative home with machine-readable contracts. Producers manage their capability's lifecycle state and interface definition in one place. Consumers declare adoptions so producers have a real impact list before shipping a breaking change. Lifecycle events are pushed to subscribers so no team needs to poll or check release notes by hand. And every mutation carries a bi-temporal timestamp, so the historical record is always queryable without reconstructing it from logs.

The MCP surface extends the same model to AI agents: an agent planning a build can look up what exists, check interface contracts, and submit feedback — with no custom integration beyond a bearer token.

## Use cases

The scenarios below show the registry applied to concrete situations. Each links to a full walkthrough.

**[AI agent capability discovery](../03-use-cases/01-ai-agent-capability-discovery.md)** — An AI agent uses the MCP surface to search the catalog, traverse the dependency graph, evaluate interface contracts, and declare adoptions — all within its tool-calling loop, without custom integration beyond a bearer token.

**[Platform team running a shared registry](../03-use-cases/02-platform-team-shared-registry.md)** — A platform team provisions tenants, registers capabilities with progression governance, controls visibility, and notifies consumer teams of breaking changes before they land.

**[Mirroring an external source of truth](../03-use-cases/03-mirroring-external-sources.md)** — An operator configures sync connectors to ingest GitHub repositories, OpenAPI specs, npm manifests, or ADR corpora automatically, keeping the catalog current without manual entry.

**[Event-driven consumers](../03-use-cases/04-event-driven-consumers.md)** — A product team subscribes to lifecycle events on capabilities it depends on, receives signed webhook deliveries, verifies signatures, and replays missed notifications from the log.

**[Consumer feedback and feature requests](../03-use-cases/05-consumer-feedback-and-requests.md)** — A consumer that encounters a gap submits the signal (bug, feature request, suggestion, question, or doc gap) directly onto the capability it depends on; the annotation routes to the producer's human or agentic triage queue with full cross-tenant lineage intact.

**[Layered abstractions — consumers becoming producers](../03-use-cases/06-layered-abstractions.md)** — A tenant that adopts upstream primitives republishes higher-level abstractions to its own downstream consumers, forming a multi-layer dependency graph with lifecycle propagation at each level.

**[AISDLC pipeline](../03-use-cases/07-aisdlc-pipeline.md)** — Each stage of a multi-stage AI Software Development Lifecycle is registered as a capability; agents discover and invoke stages via MCP, and telemetry from observability and testing feeds back into the registry as annotations and adoption events.

**[Compliance and audit over a regulated capability inventory](../03-use-cases/08-compliance-and-audit.md)** — Bi-temporal queries reconstruct historical states without touching current data; audit partitions are archived on a configurable schedule; PII scanning applies per-tenant field policies at write time.
