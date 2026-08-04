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

v0.1 — the first harvesters work end-to-end:

```bash
pip install -e .
caramelo harvest emendas     # 94k emendas 2014-2026 (bulk, no auth) -> Parquet
caramelo harvest deputados   # Câmara API, legislaturas 55-57
caramelo harvest senadores   # Senado API, legislaturas 55-57
caramelo resolve autores     # author-name -> parliamentarian-id crosswalk
```

Current resolution coverage for individual emendas from 2021 on: **93.9% of
rows / 92.9% of paid value (R$ 80B) matched exactly** to a Câmara or Senado
id; 5.5% ambiguous (homonyms); 0.6% unmatched.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the plan and
[RESEARCH.md](RESEARCH.md) for the verified source map (every endpoint listed
was probed live before inclusion).

## Roadmap (v0.x)

1. ~~Emendas harvester (bulk, auth-free) + author resolution~~ ✅
2. Disambiguate homonym authors by UF (emenda and mandate both carry one)
3. Câmara bulk harvester (votações, votos, proposições, CEAP)
4. SICONFI + IBGE dimension tables
5. Git-scraping pipeline: harvest → diff → event log → webhooks
6. REST API, then MCP server

## License

MIT
