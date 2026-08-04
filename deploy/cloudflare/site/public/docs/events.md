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

## Subscribing

The subscription API is currently admin-gated while in early operation
(public self-service signup is planned). A subscription is:

```json
{ "url": "https://example.org/hook", "types": ["emenda."], "uf": "RR" }
```

- `types`: optional list of type prefixes (`"emenda."` matches both
  `emenda.created` and `emenda.paid`); omitted = all types
- `uf`: optional UF filter for events that carry one

Deliveries are `POST` with the JSON event as body and header
`X-Caramelo-Event: <type>`. Non-2xx responses are retried (up to 5 times,
60s+ apart). Respond `410 Gone` to permanently drop a delivery.

Consuming without webhooks: poll `latest/manifest.json` for `generated_at`
changes and read new `events/<ts>.jsonl` files — that is exactly what the
delivery worker itself does.
