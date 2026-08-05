# 🐕 Caramelo

> The loyal, agnostic data layer for Brazilian public data.
> Named after the vira-lata caramelo: belongs to everyone, watches everything.

Caramelo harvests, normalizes, and re-serves Brazilian public data — budgets
(SICONFI), parliamentary amendments (emendas, including emendas Pix),
roll-call votes, parliamentary expenses (CEAP), speeches, and municipal
gazettes — through modern interfaces:

- **REST API** — typed, cached, read-only
- **Webhooks / events** — upstream sources are pull-only; Caramelo diffs each
  harvest and emits events (`emenda.paid`, `votacao.nominal.registered`, ...)
- **Bulk Parquet** — normalized dumps, queryable in-browser via DuckDB-WASM
- **MCP server** — native access for AI agents

Caramelo is politically neutral by design: it ships data and provenance, never
opinions. Scoring, rankings, and editorial products belong in downstream
projects.

Canonical home: [caramelo.dev.br](https://caramelo.dev.br)

## Status

Live and self-running: a daily cloud pipeline harvests ~15 official sources,
resolves entities, enriches, and publishes ~29 Parquet tables (3M+ rows) to a
public bucket — served through a REST API, an MCP server, signed webhooks, and
a browsable explorer.

```bash
pip install -e .
caramelo run-all             # harvest everything -> resolve -> enrich -> publish
# or a single source:
caramelo harvest emendas     # 94k emendas 2014-2026 (bulk, no auth)
caramelo harvest favorecidos # 816k payments by final recipient
caramelo harvest transferegov# emendas-Pix planos + declared purposes
caramelo harvest gazetas     # municipal gazette mentions (Querido Diário)
caramelo harvest tse         # federal candidacies + campaign donations
caramelo harvest x           # deputy/senator X timelines (budget-aware)
caramelo resolve autores     # author-name -> person crosswalk (both chambers)
caramelo enrich categorias-llm  # rule + LLM emenda categorization
```

Author resolution is person-level (a row carries `deputado_id`, `senador_id`,
or both), homonyms split by modal destination UF: **99.2% of 2021+
individual-emenda rows and paid value resolve to a person**. `examples/`
shows the joined tables in action (top Pix authors, per-capita anomalies,
the Pix∩gazette show-detector, governismo indexes).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the plan and
[RESEARCH.md](RESEARCH.md) for the verified source map (every endpoint listed
was probed live before inclusion).

## Roadmap

### v0.x — data foundation ✅

1. ~~Emendas harvester (bulk, auth-free) + author resolution~~ ✅
2. ~~Person-level author crosswalk (dual-chamber ids, homonyms split by UF)~~ ✅
3. ~~Câmara bulk harvester: votações, votos, orientações~~ ✅
4. ~~SICONFI + IBGE dimension tables~~ ✅
5. ~~CEAP expenses harvester~~ ✅
6. ~~Sharded/resumable municipal SICONFI sweep~~ ✅

### v0.2 — full source map ✅

7. ~~Favorecidos (816k emenda payments by final recipient, w/ legal nature)~~ ✅
8. ~~TransfereGov — emendas-Pix planos de ação/trabalho + declared purposes~~ ✅
9. ~~Senado — nominal votes + CEAPS expense quota~~ ✅
10. ~~TSE — federal candidacies + campaign donations~~ ✅
11. ~~Querido Diário — municipal gazette mentions (show contracts, etc.)~~ ✅
12. ~~CNPJ/CNAE + QSA registry enrichment (minhareceita)~~ ✅
13. ~~Social & media — declared handles, X timelines, Google News/YouTube/Trends~~ ✅

### v0.3 — pipeline & delivery ✅

14. ~~Cloud pipeline: daily cron → harvest → resolve → publish → R2 (100% Cloudflare)~~ ✅
15. ~~Diff-driven typed events → Queues → **public signed webhooks** (HMAC, self-signup)~~ ✅
16. ~~REST API + MCP server (`api.` / `mcp.caramelo.dev.br`)~~ ✅
17. ~~Enrichment tier: rule + LLM (Cloudflare AI Gateway) emenda categorization~~ ✅
18. ~~Landing page, docs, `llms.txt`, and a public data explorer with charts~~ ✅

### Next

- Map view — per-capita / show-detector choropleth (the shareable artifact)
- Chart interaction layer (hover/tooltips) and shareable case cards (OG images)
- Deeper enrichment: gazette-excerpt extraction, `votacoes_temas`,
  speech/post stance profiles, donor-owner (QSA ↔ TSE) unmasking
- The opinionated **product** layer — a separate project with its own name
  that consumes this data through the public API, and finally has a point of view

## The stack, live

| Surface | URL |
|---------|-----|
| Landing + explorer + docs | [caramelo.dev.br](https://caramelo.dev.br) · [/explorar](https://caramelo.dev.br/explorar.html) |
| Data lake (Parquet, free egress) | [data.caramelo.dev.br](https://data.caramelo.dev.br/latest/manifest.json) |
| REST API | [api.caramelo.dev.br/docs](https://api.caramelo.dev.br/docs) |
| MCP server | `https://mcp.caramelo.dev.br/` |
| Webhooks | [/docs/events.md](https://caramelo.dev.br/docs/events.md) |

~29 tables, 3M+ rows, harvested daily and published autonomously.

## License

MIT
