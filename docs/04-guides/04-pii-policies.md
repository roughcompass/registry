# How to configure PII scanning policies

<!--
  title: Configure PII scanning policies
  audience: operator
  status: current
-->

The PII scanner runs at write time on annotation bodies and triage notes. Its behavior — block or warn — is configurable per tenant through two endpoint families: one for pattern registration and one for per-field policy overrides. This guide covers setting up and tuning those policies.

**Preconditions:**

- A bearer token with the `admin` role for the tenant you are configuring. In local dev, run `make dev-jwt` to mint a short-lived token.
- Understanding of which data categories your deployment must protect (e.g., `CONTACT`, `FINANCIAL`, `GOVERNMENT_ID`).

**What this guide covers:**

- [How the scanner runs](#how-the-scanner-runs)
- [View current patterns and policies](#view-current-patterns-and-policies)
- [Set a block policy](#set-a-block-policy)
- [Set a warn policy](#set-a-warn-policy)
- [Understand PII categories](#understand-pii-categories)
- [Add custom PII patterns](#add-custom-pii-patterns)
- [Test a policy without writing data permanently](#test-a-policy-without-writing-data-permanently)

---

## How the scanner runs

The scanner intercepts write requests on free-text fields before they reach the database. It runs on:

- `annotation_body` — the main body of an annotation record.
- `annotation_triage_note` — the triage note attached to an annotation.

It does **not** run on reads, and does not scan structured fields such as capability names, attribute keys, or lifecycle values.

Three policy levels control what happens when a pattern matches:

| Policy | HTTP status | Effect |
|---|---|---|
| `block` | `422 Unprocessable Entity` | Write is rejected. Response body contains `{"code": "pii_detected", ...}`. |
| `warn` | `200 OK` | Write proceeds. Response body includes a `warnings[]` array listing the matched field and category. |
| `advisory` | `200 OK` | Write proceeds. Match is recorded internally only; no response field is populated. |

The effective policy for a given `(field_type, pattern)` pair is resolved in this order:
1. A per-field policy override (`pii-field-policies`) for that exact `(field_type, pattern_id)` pair, if one exists.
2. The `policy_override` on the pattern itself, if set.
3. The tenant-level default (typically `advisory` unless changed).

---

## View current patterns and policies

**List all patterns** — both built-in (seeded at tenant creation) and any custom patterns your team has added:

```bash
curl -s https://api.example.com/v1/admin/pii-patterns \
  -H "Authorization: Bearer <admin-token>" | jq '.[] | {name, category, is_system, policy_override, is_enabled}'
```

Built-in patterns have `is_system: true` and cannot be modified or deleted via the API — only their `is_enabled` state can be changed through the PATCH endpoint if a tenant-level override is needed, and system rows return `403` on any PATCH attempt to protected fields.

**List all per-field policy overrides:**

```bash
curl -s https://api.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" | jq .
```

---

## Set a block policy

A block policy rejects writes when a matching pattern fires on the target field. Use it for categories where no free-text occurrence is acceptable in the database.

**Option 1 — pattern-level block (applies across all scanned fields):**

```bash
# First, find the pattern_id for the built-in SSN pattern.
SSN_ID=$(curl -s https://api.example.com/v1/admin/pii-patterns \
  -H "Authorization: Bearer <admin-token>" \
  | jq -r '.[] | select(.name == "ssn") | .pattern_id')

# Set policy_override to block on the pattern row.
curl -s -X PATCH \
  "https://api.example.com/v1/admin/pii-patterns/$SSN_ID" \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"policy_override": "block"}' | jq '{name, policy_override}'
```

**Option 2 — field-specific block (scopes the policy to one field type):**

```bash
curl -s -X POST https://api.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "field_type": "annotation_body",
    "pattern_id": "<ssn-pattern-id>",
    "policy": "block"
  }' | jq .
```

When a block fires, the write endpoint returns:

```json
HTTP 422
{
  "code": "pii_detected",
  "detail": "PII detected in annotation_body: GOVERNMENT_ID"
}
```

The write is not persisted. The caller must sanitize the input before retrying.

---

## Set a warn policy

A warn policy allows the write to proceed but signals to the caller that PII was detected. Use it when you want visibility into PII without hard-blocking writes — useful during a rollout period before enforcing `block`.

```bash
curl -s -X POST https://api.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "field_type": "annotation_triage_note",
    "policy": "warn"
  }' | jq .
```

Omitting `pattern_id` creates a catch-all override for that field type — any pattern match on `annotation_triage_note` will warn.

When a warn fires, the response body includes:

```json
{
  "annotation_id": "...",
  "warnings": [
    {
      "field": "annotation_triage_note",
      "category": "CONTACT"
    }
  ]
}
```

The write is committed. Callers that do not inspect `warnings[]` will not notice — make sure your client handles this field.

---

## Understand PII categories

Built-in patterns and their categories:

| Pattern name | Category | What it detects |
|---|---|---|
| `email` | `CONTACT` | RFC 5322-lite e-mail addresses |
| `phone` | `CONTACT` | Common North American and international phone number formats |
| `ssn` | `GOVERNMENT_ID` | US Social Security Numbers (NNN-NN-NNNN; Luhn-validated) |
| `credit_card` | `FINANCIAL` | Visa, Mastercard, Amex, Discover card numbers (Luhn-checked) |
| `aws_access_key` | `CREDENTIAL` | AWS access key IDs (`AKIA...`) |
| `aws_secret_key` | `CREDENTIAL` | AWS secret access key patterns |
| `jwt_token` | `CREDENTIAL` | JSON Web Tokens (`eyJ...`) |

Custom patterns you register via `POST /v1/admin/pii-patterns` can use any category string — the value is stored as-is and appears in `warnings[]` responses.

To determine which category fired from an API response, inspect the `warnings[].category` field (warn policy) or the `detail` string (block policy).

---

## Add custom PII patterns

Register a custom regex-based pattern for your deployment's specific sensitive data:

```bash
curl -s -X POST https://api.example.com/v1/admin/pii-patterns \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "name": "acme_employee_id",
    "category": "EMPLOYEE_ID",
    "regex": "\\bEMP-[0-9]{6}\\b",
    "policy_override": "block",
    "is_enabled": true
  }' | jq .
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique name within the tenant |
| `category` | yes | Category label that appears in warnings and block responses |
| `regex` | yes | Python-compatible regex; validated server-side (`422` on invalid pattern) |
| `policy_override` | no | `advisory`, `warn`, or `block`; falls back to tenant default when absent |
| `is_enabled` | yes | Set to `false` to register a pattern without activating it |

Custom patterns have `is_system: false` and can be updated with `PATCH /v1/admin/pii-patterns/{pattern_id}` or deleted with `DELETE /v1/admin/pii-patterns/{pattern_id}`.

**Disable a pattern** without deleting it:

```bash
curl -s -X PATCH \
  "https://api.example.com/v1/admin/pii-patterns/<pattern_id>" \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"is_enabled": false}' | jq .is_enabled
```

---

## Test a policy without writing data permanently

There is no dry-run endpoint. The recommended approach is to submit a test annotation to a staging tenant that has the same policy configuration, inspect the response, and then delete the annotation.

```bash
# 1. Write a test annotation with known PII content on the staging tenant.
ANN_ID=$(curl -s -X POST \
  "https://staging.example.com/v1/capabilities/payment-gateway/annotations" \
  -H "Authorization: Bearer <staging-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "body": "Contact the owner at test.user@example.com for escalations.",
    "category": "triage_note"
  }' | jq -r '.annotation_id')

# 2. Inspect the response for pii_detected (block) or warnings[] (warn).
#    If the call returned 422, the body contains code: pii_detected.
#    If it returned 200, check for a warnings[] field.

# 3. Delete the annotation so staging data stays clean.
curl -s -X DELETE \
  "https://staging.example.com/v1/annotations/$ANN_ID" \
  -H "Authorization: Bearer <staging-token>"
```

If you need to test on production (e.g., to validate a newly added custom pattern), use a dedicated test capability and annotation, and delete the annotation immediately after confirming the response.

---

**See also:**

- [`overview/vocabulary.md`](../01-overview/03-vocabulary.md) — PII scanner concept, warn vs block behavior, `warnings[]` response field
- [`reference/api.md`](../05-reference/01-api.md) — PII pattern and field-policy admin endpoints; annotation endpoint error codes
