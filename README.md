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

## Status

Early design. See [ARCHITECTURE.md](ARCHITECTURE.md) for the plan and
[RESEARCH.md](RESEARCH.md) for the verified source map (every endpoint listed
was probed live before inclusion).

## Roadmap (v0.x)

1. Emendas harvester (bulk, auth-free) + author resolution vs Câmara
2. Câmara bulk harvester (votações, votos, proposições, CEAP)
3. SICONFI + IBGE dimension tables
4. Git-scraping pipeline: harvest → diff → event log → webhooks
5. REST API, then MCP server

## License

MIT
