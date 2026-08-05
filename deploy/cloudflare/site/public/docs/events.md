# Caramelo — Events & Webhooks

Every publish diffs the new tables against the previously published state and
writes typed events to `events/<ts>.jsonl` in the public bucket. A delivery
worker fans these out to registered webhook URLs via a queue with retries.

## Event taxonomy (v0)

| Type | Fired when | Payload highlights |
|------|-----------|--------------------|
| `dataset.updated` | a table's content hash changed | `dataset`, `rows`, `prev_rows` |
| `emenda.created` | a new emenda code appears | `codigo`, `ano`, `autor`, `municipio`, `uf`, `valor_empenhado` |
| `emenda.paid` | an emenda's paid value increased | `codigo`, `autor`, `municipio`, `uf`, `valor_pago`, `delta` |
| `rede.added` | a parliamentarian declared a new social account | `nome`, `rede`, `handle`, `url` |
| `rede.removed` | a declared social account disappeared | `nome`, `rede`, `handle` |

All events carry `type` and `at` (UTC ISO timestamp of the publish).

## Subscribing (public, self-service)

Endpoint base: `https://caramelo-events.gabriel-laet-2cd.workers.dev`

**1. Create a subscription:**

```bash
curl -X POST $BASE/subscriptions -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/hook", "types": ["emenda."], "uf": "RR"}'
```

- `url` (required, https): where deliveries are POSTed
- `types` (optional): type prefixes; `"emenda."` matches `emenda.created`
  and `emenda.paid`. Omitted = all types.
- `uf` (optional): only events carrying that UF

Response returns `{id, secret, status, verify_token?}`. Keep the `secret` —
it signs your deliveries and authorizes managing the subscription.

**2. Prove you own the URL.** On signup we POST a `subscription.verify`
challenge to your `url`. Activate either by echoing the `verify_token` in
that response body, or by calling:

```bash
curl -X POST $BASE/subscriptions/<id>/verify \
  -H "Content-Type: application/json" -d '{"token": "<verify_token>"}'
```

**3. Receive signed deliveries.** Each event is POSTed with headers:

- `X-Caramelo-Event: <type>`
- `X-Caramelo-Delivery: <uuid>` (unique per attempt)
- `X-Caramelo-Signature: sha256=<hex>` — HMAC-SHA256 of the raw body with
  your `secret`. Verify it to authenticate the delivery came from Caramelo.

Respond `2xx` to acknowledge. Failed deliveries retry independently per
subscription; an endpoint failing persistently is auto-suspended. Respond
`410 Gone` to unsubscribe immediately.

**Manage:** `GET /subscriptions/<id>` (Bearer `<secret>`) returns status and
the last 20 delivery attempts; `DELETE /subscriptions/<id>` removes it.

Consuming without webhooks: poll `latest/manifest.json` for `generated_at`
changes and read new `events/<ts>.jsonl` files — that is exactly what the
delivery worker itself does.
