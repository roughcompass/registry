# How to subscribe to capability lifecycle events

<!--
  title: Subscribe to lifecycle events
  audience: integrator (consumer or producer role)
  status: current
-->

This guide walks a consumer team through declaring an adoption, subscribing to lifecycle events via webhook, and consuming event notifications — both push (webhook) and pull (polling API).

**Preconditions:**

- A bearer token with the `consumer` (or `producer`) role for your tenant. See [`overview/authentication.md`](../01-overview/04-authentication.md) for how to obtain one and [`overview/authorization.md`](../01-overview/05-authorization.md) for how roles are resolved. In local dev, run `make dev-jwt` to mint a short-lived token.
- The UUID or slug-form name of the capability you want to subscribe to.
- A publicly accessible HTTPS endpoint able to receive POST requests, if using webhooks.

**What this guide covers:**

- [Declare an adoption](#declare-an-adoption)
- [Create a webhook subscription](#create-a-webhook-subscription)
- [Verify webhook signatures](#verify-webhook-signatures)
- [Consume notifications via polling](#consume-notifications-via-polling)
- [Update or cancel a subscription](#update-or-cancel-a-subscription)
- [Replay failed deliveries](#replay-failed-deliveries)

---

## Declare an adoption

Declaring an adoption records that your tenant depends on a specific provider capability. It creates a `provides_to` graph edge between the provider and your tenant, which the provider can use to understand blast radius before making breaking changes.

Adoptions are capability-scoped:

```bash
curl -s -X POST \
  "https://api.example.com/v1/capabilities/payment-gateway/adoptions" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "intent": "We use this for card-present checkout.",
    "version_pin": "1.0.0"
  }' | jq .
```

| Field | Required | Meaning |
|---|---|---|
| `intent` | no | Free-text description of how your team uses this capability |
| `version_pin` | no | The version your team has pinned to; informational only |

The response includes `adoption_id`. Store it — you need it to remove the adoption later.

```json
{
  "adoption_id": "d4e5f6a7-0000-0000-0000-000000000002",
  "provider_capability_id": "a1b2c3d4-0000-0000-0000-000000000001",
  "consumer_tenant_id": "<your-tenant-id>",
  "intent": "We use this for card-present checkout.",
  "version_pin": "1.0.0"
}
```

**Idempotency:** send `Idempotency-Key: <uuid>` to make the create safe to retry. Same key + same body replays the original 201.

**Errors:**
- `404` — the capability does not exist or is not visible to your tenant.
- `403` — caller token lacks the `producer` or `admin` role.
- `409` — an active adoption already exists for this `(consumer_tenant, capability)` pair.

---

## Create a webhook subscription

After adopting, subscribe to the capability's lifecycle events. Subscriptions are also capability-scoped. You supply a webhook URL, a shared signing secret (used to verify delivery authenticity), and the event kinds you want to receive.

Generate a signing secret before creating the subscription:

```python
import secrets
signing_secret = secrets.token_urlsafe(32)
print(signing_secret)
# e.g.: xY9mK2pL8nQrVwZsD3cGjHuAb6tEiFo7
```

Then create the subscription:

```bash
curl -s -X POST \
  "https://api.example.com/v1/capabilities/payment-gateway/subscriptions" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "event_kinds": ["lifecycle.changed", "interface.updated", "adoption.created"],
    "webhook_url": "https://your-service.example.com/webhooks/registry",
    "webhook_hmac_secret_ref": "REGISTRY_WEBHOOK_SECRET"
  }' | jq .
```

The `webhook_hmac_secret_ref` field is the name of the environment variable your service reads at delivery-verify time to retrieve the raw secret value. The registry stores only the reference string — never the secret itself.

The response is:

```json
{"subscription_id": "b2c3d4e5-0000-0000-0000-000000000003"}
```

Retrieve the full subscription record via `GET /v1/capabilities/payment-gateway/subscriptions`.

**Valid `event_kinds`** include: `lifecycle.changed`, `interface.updated`, `adoption.created`, `adoption.removed`, `annotation.created`. Use `GET /v1/admin/vocabularies/notification_event_kind` (admin role) to list all values registered for your tenant.

---

## Verify webhook signatures

Every delivery carries an `X-Registry-Signature-256` header. The value is `sha256=<hex>` where the hex is HMAC-SHA256 computed over the raw request body, keyed by your signing secret.

Reject any delivery that fails this check — do not process the payload first.

```python
import hashlib
import hmac
import os
from fastapi import Header, HTTPException, Request

SIGNING_SECRET = os.environ["REGISTRY_WEBHOOK_SECRET"].encode()

async def receive_registry_event(
    request: Request,
    x_registry_signature_256: str | None = Header(None),
) -> None:
    body = await request.body()

    if not x_registry_signature_256:
        raise HTTPException(status_code=401, detail="missing signature header")

    expected = "sha256=" + hmac.new(SIGNING_SECRET, body, hashlib.sha256).hexdigest()

    # Constant-time comparison prevents timing attacks.
    if not hmac.compare_digest(expected, x_registry_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Safe to parse the payload now.
    import json
    event = json.loads(body)
    ...
```

**When to reject:**
- Header is absent → 401.
- Digest does not match → 401.
- Never truncate the comparison or short-circuit before `compare_digest` finishes — that turns a constant-time comparison into a timing oracle.

---

## Consume notifications via polling

Polling suits agent-loop consumers and services that lack a public HTTPS endpoint. Notifications are scoped to your tenant and cursor-paginated, newest first.

**List unread notifications:**

```bash
curl -s \
  "https://api.example.com/v1/notifications?status=unread&page_size=50" \
  -H "Authorization: Bearer <token>" | jq .
```

| Query param | Default | Meaning |
|---|---|---|
| `status` | `unread` | `unread`, `read`, or `all` |
| `cursor` | — | Opaque cursor from a previous response's `next_cursor` |
| `page_size` | `50` | 1–500 |

Iterate pages until `next_cursor` is `null`:

```bash
CURSOR=""
while true; do
  PARAMS="status=unread&page_size=100"
  [ -n "$CURSOR" ] && PARAMS="$PARAMS&cursor=$CURSOR"
  RESP=$(curl -s "https://api.example.com/v1/notifications?$PARAMS" \
    -H "Authorization: Bearer <token>")
  echo "$RESP" | jq '.items[] | {id: .notification_id, event: .event_kind}'
  CURSOR=$(echo "$RESP" | jq -r '.next_cursor // empty')
  [ -z "$CURSOR" ] && break
done
```

**Acknowledge a notification:**

```bash
curl -s -X POST \
  "https://api.example.com/v1/notifications/<notification_id>:mark-read" \
  -H "Authorization: Bearer <token>"
# Returns 204 No Content. Idempotent — repeated calls succeed silently.
```

Each notification item contains a `fetch_url` pointing at the canonical capability record — follow it to get the full current state after the event.

---

## Update or cancel a subscription

**Update** — change the webhook URL, toggle `is_enabled`, or change the event kinds filter:

```bash
curl -s -X PATCH \
  "https://api.example.com/v1/subscriptions/<subscription_id>" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "webhook_url": "https://your-service.example.com/webhooks/registry-v2",
    "event_kinds": ["lifecycle.changed", "interface.updated"],
    "is_enabled": true
  }' | jq .
```

All fields are optional — only supplied fields are updated. The response is the full updated subscription record.

**Cancel (soft-delete):**

```bash
curl -s -X DELETE \
  "https://api.example.com/v1/subscriptions/<subscription_id>" \
  -H "Authorization: Bearer <token>"
# Returns 204 No Content. Idempotent.
```

Cancellation sets `t_invalidated_at` on the row; the webhook delivery worker stops enqueuing new deliveries for the subscription. Historical delivery records are retained for audit purposes.

**Remove the adoption** when you are fully done consuming the capability:

```bash
curl -s -X DELETE \
  "https://api.example.com/v1/capabilities/payment-gateway/adoptions/<adoption_id>" \
  -H "Authorization: Bearer <token>"
# Returns 204 No Content.
```

---

## Replay failed deliveries

The delivery worker retries failed webhook deliveries with exponential back-off. When retries are exhausted — for example because your endpoint returned 4xx errors for an extended period — deliveries land in a dead-letter state.

Once your endpoint is fixed, an operator can replay those deliveries. See [`operations/ops.md`](../06-operations/01-ops.md) for the SQL-backed replay procedure. The replay procedure re-enqueues the original payload; no new event is generated.

---

**See also:**

- [`overview/vocabulary.md`](../01-overview/03-vocabulary.md) — adoption, subscription, notification vocabulary
- [`reference/api.md`](../05-reference/01-api.md#subscriptions-and-notifications) — full endpoint reference
- [`operations/ops.md`](../06-operations/01-ops.md) — operator procedure for replaying failed deliveries and rotating webhook secrets
