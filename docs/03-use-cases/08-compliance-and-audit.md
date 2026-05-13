<!--
  title: Use case — Compliance and audit over a regulated capability inventory
  audience: operator, operator agent
  archetype: explanation (use-case scenario)
  summary: How to use the bi-temporal data model, audit partitioning, and PII scanning to maintain a compliant, auditable capability inventory.
-->

# Use case: Compliance and audit over a regulated capability inventory

Organizations subject to change-management requirements or data-handling regulations need a capability inventory that is not just current but fully auditable: every write must be traceable, historical states must be reconstructible without touching current data, and sensitive field values must be identified and controlled. The registry's bi-temporal model, audit partition archival, and PII scanning policies address all three.

Every mutable row carries both valid-time (when the fact was true in the world) and transaction-time (when it was recorded) axes, so any historical state can be reconstructed with a point-in-time query. Audit partitions are archived on a configurable schedule. The PII scanner runs on fact and attribute writes and applies per-tenant field policies to flag or redact sensitive values before they are stored.

---

## Sections to fill

- Preconditions (audit archival configured, PII policies in place, auditor role minted)
- Bi-temporal queries: how to reconstruct a past state with `as_of` parameters
- Audit partition management: archival schedule, retention, off-site export
- PII scanner: category definitions, per-tenant field policies, scan-on-write enforcement
- Progression override audit trail: every bypass is recorded before insertion
- Generating a change history report for a specific capability
- Role separation: auditor role vs. admin role capabilities
- Related guides and reference docs
