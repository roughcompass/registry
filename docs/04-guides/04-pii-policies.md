# How to configure PII scanning policies

<!--
  title: Configure PII scanning policies
  audience: operator
  status: stub
-->

The PII scanner runs at write time on annotation bodies and triage notes. Its behavior — block or warn — is configurable per tenant. This guide covers setting up and tuning PII policies for a tenant.

**Preconditions:**

- An admin-level token for the tenant you are configuring.
- Understanding of which data categories your deployment must protect (e.g., CONTACT, FINANCE, HEALTH).

**What this guide covers:**

- [How the scanner runs](#how-the-scanner-runs)
- [View current policies for a tenant](#view-current-policies-for-a-tenant)
- [Set a block policy](#set-a-block-policy)
- [Set a warn policy](#set-a-warn-policy)
- [Understand PII categories](#understand-pii-categories)
- [Add custom PII patterns](#add-custom-pii-patterns)
- [Test a policy without writing data](#test-a-policy-without-writing-data)

---

## How the scanner runs

<!-- stub: runs at write time on annotation body and triage_note fields; does not run on reads; block policy rejects with HTTP 422 + pii_detected error code; warn policy allows write and adds warnings[] to response; scanner does not run on capability name/attributes (only free-text fields) -->

## View current policies for a tenant

<!-- stub: GET /v1/admin/tenants/{tenant_id}/pii-policies — list active policies; fields: category, action (block|warn), created_at -->

## Set a block policy

<!-- stub: POST /v1/admin/tenants/{tenant_id}/pii-policies — category + action=block; effect: writes containing detected category are rejected with 422 + pii_detected code -->

## Set a warn policy

<!-- stub: POST /v1/admin/tenants/{tenant_id}/pii-policies — category + action=warn; effect: write proceeds, response includes warnings[] array with field and categories -->

## Understand PII categories

<!-- stub: built-in pattern modules in registry/registry/security/pii_patterns/; list known categories (CONTACT, FINANCE, HEALTH, etc.); how to determine which category fired from the API response -->

## Add custom PII patterns

<!-- stub: subclass or add a pattern module under registry/registry/security/pii_patterns/; register in the scanner; note this is a code-level change requiring a deployment; no API path for adding patterns at runtime -->

## Test a policy without writing data

<!-- stub: no dry-run API; recommended approach — submit a test annotation with known PII content against a staging tenant; inspect the response for pii_detected or warnings[]; clean up with DELETE /v1/annotations/{id} -->

---

**See also:**

- [`overview/vocabulary.md`](../01-overview/03-vocabulary.md) — PII scanner concept, warn vs block behavior, warnings[] response field
- [`reference/api.md`](../05-reference/01-api.md) — `/v1/admin/tenants/{tenant_id}/pii-policies` admin endpoint; annotation endpoint error codes
