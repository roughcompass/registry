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

## Sections to fill

- The pipeline shape
  - Stages in order: intake, product definition, architecture, development, testing, observability, deployment
  - What each stage produces (artifacts, structured outputs, metadata)
  - How stages hand off to the next (adoption events, webhook triggers, annotations)
- Each stage as a registered capability
  - Agents, skills, and artifacts framed as capability attributes
  - Lifecycle state per stage: how a stage moves from draft to stable
  - MCP surface: how agents discover and invoke a stage via `search_capabilities` and `adopt_capability`
- Autonomous vs human-driven stages
  - The registry is agnostic to whether a stage is driven by a human or an agent
  - How to model a human-approval gate as a lifecycle transition
- Feedback loops — observability and testing publishing back
  - Observability stage posting telemetry as annotations on upstream capabilities
  - Testing stage recording defect rates; how this affects capability health signals
  - Deployment outcomes updating adoption metadata for consumers
  - Lineage: tracing a real-world incident back through stage outputs to the originating capability version
- Composing the full lifecycle as a higher-order capability
  - Registering the AISDLC pipeline itself as a single capability with the stages as dependencies
  - Versioning the pipeline; what a pipeline upgrade means for consumers
- Where to read next
  - Reference: [MCP tools](../05-reference/02-mcp-tools.md)
  - Guide: [Subscribe to events](../04-guides/02-subscribe-to-events.md)
  - Use case: [Layered abstractions](06-layered-abstractions.md)
  - Use case: [Event-driven consumers](04-event-driven-consumers.md)
  - Use case: [AI agent capability discovery](01-ai-agent-capability-discovery.md)
