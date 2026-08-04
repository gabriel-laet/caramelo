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
caramelo harvest votacoes    # Câmara bulk: 41k votações, 468k roll-call votes,
                             # 15k party orientations (2023-2026)
caramelo harvest municipios  # IBGE: all 5,571 municípios + population
caramelo harvest siconfi     # Tesouro RREO budget reports (default: 27 UFs)
caramelo resolve autores     # author-name -> person crosswalk (both chambers)
```

`examples/demo_rankings.py` shows what the joined tables already answer: top
emendas-Pix authors, Pix money per capita by município, and deputy
governismo / party-discipline indexes over 468k roll-call votes.

Resolution is person-level: an author row carries a `deputado_id`, a
`senador_id`, or both (many parliamentarians served in both chambers), with
homonyms split by the author's modal destination UF. Coverage for individual
emendas from 2021 on: **99.2% of rows and of paid value (R$ 85.5B)** resolve
to a person; 0.1% ambiguous (4 homonym names); 0.6% unmatched. Roll-call
votes join the deputados table with 100% id integrity.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the plan and
[RESEARCH.md](RESEARCH.md) for the verified source map (every endpoint listed
was probed live before inclusion).

## Roadmap (v0.x)

1. ~~Emendas harvester (bulk, auth-free) + author resolution~~ ✅
2. ~~Person-level author crosswalk (dual-chamber ids, homonyms split by UF)~~ ✅
3. ~~Câmara bulk harvester: votações, votos, orientações~~ ✅
4. ~~SICONFI + IBGE dimension tables~~ ✅
5. CEAP expenses harvester (bulk lives on a separate endpoint)
6. Full municipal SICONFI sweep (5,570 entes) with rate limiting
7. Git-scraping pipeline: harvest → diff → event log → webhooks
8. REST API, then MCP server

## License

MIT
