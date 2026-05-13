<!--
  title: Use case — Mirroring an external source of truth
  audience: operator
  archetype: explanation (use-case scenario)
  summary: How to ingest GitHub repositories, OpenAPI specs, or other external sources into the registry via sync connectors.
-->

# Use case: Mirroring an external source of truth

Many platform teams already publish capability metadata somewhere external — GitHub repositories, OpenAPI spec files, npm manifests, or ADR corpora. Rather than manually re-entering that data in the registry, operators configure sync connectors that pull from the external source on a schedule and populate entity facts automatically. The fetch/parse separation keeps connector logic pure and testable; credentials come exclusively from environment variables at runtime and are never stored in the database.

The result is a registry that stays current with the external source without human intervention: GitHub topics become entity attributes, OpenAPI operation lists become structured facts, release notes populate the bi-temporal fact store as timestamped observations.

---

## Sections to fill

- Preconditions (operator access, env vars for connector credentials, target tenant provisioned)
- Connector types supported: GitHub, GitLab, OpenAPI spec, npm, markdown/ADR corpus
- Credential configuration (env-var ref strings, rotation without restart)
- Step 1 — Configure a connector via env vars
- Step 2 — Run a manual sync (CLI invocation)
- Step 3 — Schedule continuous sync
- Step 4 — Verify ingested facts and attributes
- Troubleshooting: partial parse failures, credential rotation, re-sync after schema change
- Related guides and reference docs
