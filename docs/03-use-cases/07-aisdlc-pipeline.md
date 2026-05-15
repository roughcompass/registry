<!--
  title: Use case — AISDLC pipeline: capabilities for each stage of an AI-driven SDLC
  audience: integrator (producer), end-user agent, operator
  archetype: explanation (use-case scenario)
  summary: How a multi-stage AI Software Development Lifecycle can be expressed as a set of registered capabilities, with each stage operating as a producer that feeds the next and publishes feedback back into the registry.
-->

# Use case: AISDLC pipeline — capabilities for each stage of an AI-driven SDLC

An AI Software Development Lifecycle (AISDLC) is a multi-stage pipeline where each stage — intake, product definition, architecture, development, testing, observability, deployment — is itself a distinct capability registered in the registry, with its own agents, skills, and artifacts. Each stage produces structured outputs consumed by the next, forming a chain of registered producers and consumers. Telemetry from observability, defect rates from testing, and deployment outcomes feed back into the registry as annotations and adoption events, so real-world results continuously inform how downstream consumers evaluate the capabilities they depend on.

This use case shows how the registry serves as the substrate for agentic SDLC workflows: the MCP surface exposes each stage to the agents that drive it; the event system propagates handoffs between stages; and the capability lifecycle model governs when a stage is ready for downstream consumption, when it is being revised, and when it is deprecated in favor of an improved implementation.

---

## The pipeline shape

Seven stages, in dependency order. Each is its own capability in the registry, with explicit `depends_on` edges to upstream stages and `composes` edges to the artifacts it produces.

| Stage | What it produces | Consumes from |
|---|---|---|
| `intake` | Triaged requests + raw context | external systems |
| `product-definition` | PRDs, user stories, acceptance criteria | `intake` |
| `architecture` | Design docs, ADRs, threat models, interface contracts | `product-definition` |
| `development` | Implementation artifacts (code, migrations, configs) | `architecture` |
| `testing` | Test plans, defect reports, coverage maps | `development` |
| `observability` | Telemetry pipelines, dashboards, SLOs | `development`, `testing` |
| `deployment` | Release records, rollout state, post-deploy health checks | `development`, `observability` |

Each stage's capability carries `entity_type=capability` with attributes that name the agents, skills, and templates it runs, and `lifecycle_state` that marks its readiness (`alpha` → `beta` → `ga` → `deprecated` → `retired`).

---

## Each stage as a registered capability

Registering the `architecture` stage:

```bash
curl -X POST https://registry.example.com/v1/capabilities \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "aisdlc-architecture",
    "entity_type": "capability",
    "display_name": "AISDLC — Architecture",
    "description": "Turns a PRD into a system design, ADR set, threat model, and interface contracts.",
    "lifecycle": "beta",
    "visibility": "tenant-shared",
    "attributes": {
      "stage": "architecture",
      "agents": ["architect", "stride-reviewer"],
      "skills": ["adr-write", "threat-model", "interface-design"],
      "inputs": ["prd"],
      "outputs": ["adr", "data-model", "interface-contract", "stride-report"]
    }
  }'
```

Linking the stage to its upstream dependency (`product-definition`):

```bash
curl -X POST https://registry.example.com/v1/capabilities \
  ... (omitted: same shape as above for aisdlc-product-definition) ...

# After both stages exist:
curl -X POST https://registry.example.com/v1/capabilities/<architecture_id>/dependencies \
  -d '{"target_entity_id": "<product_definition_id>", "edge_type": "depends_on"}'
```

The pipeline shape becomes browsable: `GET /v1/capabilities/<architecture_id>/dependencies?depth=5` walks back through the chain to `intake`; `GET /v1/capabilities/<intake_id>/dependents?depth=5` walks forward to `deployment`.

---

## How agents discover and invoke a stage

Agents that drive the pipeline use the MCP surface for discovery:

- `search_capabilities` with `q="aisdlc-architecture"` or by attribute filter to find the stage.
- `get_capability` to read its interface contract and attribute schema before invoking it.
- `get_dependencies` to confirm the stage's upstream is in a ready state (lifecycle ≥ `ga`).
- `lookup_by_external_id` if the stage is mirrored from an external source (e.g. a `github` repo holding agent definitions).

There is **no `adopt_capability` MCP tool.** Adoption is a REST-only action; an agent that has decided to depend on a stage records it via `POST /v1/capabilities/{stage_id}/adoptions`. This creates the audit trail that the stage's producer can use to assess the impact of breaking changes.

---

## Autonomous vs human-driven stages

The registry is agnostic to whether a stage is run by a human or an agent. Both are modelled the same way: a capability with `actors` claimed against it and `lifecycle_state` indicating readiness. A human-approval gate is modelled as a progression definition that adds a manual transition step:

```bash
curl -X POST https://registry.example.com/v1/admin/tenants/<tenant_id>/progression-definitions \
  -d '{
    "name": "aisdlc-stage-progression",
    "states": ["alpha", "beta", "ga", "deprecated", "retired"],
    "gates": [
      {"from": "beta", "to": "ga", "requires_attribute": "human_approved"}
    ]
  }'
```

Agents that try to advance a stage from `beta` to `ga` without setting `human_approved=true` on the capability get HTTP 422 with `gate_failed`. An operator can clear the gate via `POST /v1/admin/tenants/<tenant_id>/entities/<stage_id>/progression-overrides` — every override writes to the audit log before the override row is inserted.

---

## Feedback loops — observability and testing publishing back

The observability and testing stages are also consumers — they publish results back as facts and annotations on the stages they observe.

**Testing stage** records defect rates as facts on the stage it tested:

```bash
curl -X POST https://registry.example.com/v1/capabilities/<development_stage_id>/artifacts \
  -d '{
    "category": "defect_report",
    "title": "Defect rate 2026-W21",
    "body": "47 defects in 312 generated artefacts (15.1%). Top failure: hallucinated import paths.",
    "body_format": "markdown"
  }'
```

**Observability stage** posts telemetry-derived annotations on consumer capabilities when they exhibit problematic patterns:

```bash
curl -X POST https://registry.example.com/v1/capabilities/<consumer_id>/annotations \
  -d '{
    "category": "feedback",
    "body": "p99 latency on payments-v3 has trended above 800ms for 7 days; consider downgrading to advisory lifecycle.",
    "version_target": "v3.4.2"
  }'
```

The capability's owner triages those annotations using `PATCH /v1/annotations/{annotation_id}` (or the `triage_annotation` MCP tool).

**Deployment stage** updates adoption metadata when a rollout completes — recording the version that was deployed and how downstream consumers are affected. This closes the loop: an incident traced back through `GET /v1/capabilities/<entity_id>/dependents?as_of=<incident_time>` shows exactly which consumers were on which version of which stage when the incident occurred, all reconstructed via bi-temporal queries.

---

## Composing the full lifecycle as a higher-order capability

The pipeline itself can be registered as a single composite capability with `composes` edges to each stage:

```bash
curl -X POST https://registry.example.com/v1/capabilities \
  -d '{
    "name": "aisdlc-pipeline-v1",
    "entity_type": "capability",
    "display_name": "AISDLC pipeline (v1)",
    "lifecycle": "beta",
    "attributes": {
      "kind": "composite",
      "version": "1.0.0"
    }
  }'
```

Then `composes` edges to each stage. Consumers adopt the composite capability and inherit the dependency on every stage; upgrading the pipeline to v2 is a single `lifecycle.transitioned` event downstream consumers can subscribe to.

---

## Where to read next

- [MCP tools reference](../05-reference/02-mcp-tools.md) — full tool catalog including `search_capabilities`, `get_capability`, traversal tools, and `submit_annotation`
- [Subscribe to events](../04-guides/02-subscribe-to-events.md) — webhook setup for stage-handoff and lifecycle events
- [Layered abstractions](06-layered-abstractions.md) — when one tenant operates the pipeline and downstream tenants consume the composite
- [Event-driven consumers](04-event-driven-consumers.md) — how downstream consumers subscribe to stage-output events
- [AI agent capability discovery](01-ai-agent-capability-discovery.md) — how agents find a stage's interface contract before invoking it
