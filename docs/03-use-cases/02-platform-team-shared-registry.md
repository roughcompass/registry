<!--
  title: Use case — Platform team running a shared registry
  audience: operator, integrator (producer)
  archetype: explanation (use-case scenario)
  summary: How a platform team provisions tenants, publishes capabilities, and governs lifecycle across many consuming teams.
-->

# Use case: Platform team running a shared registry

A platform team that maintains shared infrastructure — APIs, libraries, design systems, agent frameworks — needs a single durable catalog where consuming product teams can discover what exists, track what they depend on, and receive advance notice of breaking changes. The registry provides the multi-tenant isolation model, progression governance, and event delivery that make this work at organizational scale.

The platform team operates as both administrator (provisioning tenants, managing progression definitions) and producer (registering and lifecycling capabilities). Consumer teams each get their own tenant, declare adoptions of capabilities they depend on, and subscribe to lifecycle events so they are notified before a deprecation or breaking change lands.

---

## Sections to fill

- Preconditions (operator credentials, database access, tenants provisioned)
- Step 1 — Provision producer and consumer tenants
- Step 2 — Define a progression (state machine, attribute gates)
- Step 3 — Register a capability and attach initial attributes
- Step 4 — Control visibility (`private` → `tenant-shared` → `public`)
- Step 5 — Advance through lifecycle states with progression gates
- Step 6 — Consumer teams declare adoptions
- Step 7 — Notify consumers of a breaking change preview
- Step 8 — Triage and close consumer annotations
- Governance patterns: override policy, audit trail, blast-radius queries via edge graph
- Related guides and reference docs
