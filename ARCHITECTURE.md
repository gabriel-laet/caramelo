# Caramelo - Canonical Brazilian Public Data Layer

> The loyal, agnostic data infrastructure for Brazilian public money and politics.
> Named after the vira-lata caramelo: belongs to everyone, watches everything.

## Vision

An open-source, politically-neutral data layer that harvests, normalizes, and
re-serves Brazilian public data (budgets, emendas, votes, expenses, gazettes)
through modern interfaces: REST API, webhooks/events, bulk Parquet, and an MCP
server for AI agents. Opinionated products (scoring, rankings, coherence
indexes) are built *on top of* Caramelo, never inside it — the layer stays
canonical and citable.

**Why it needs to exist:** every upstream source is pull-only (REST + bulk CSV
dumps, no webhooks anywhere), schemas are inconsistent (latin1 CSVs, semicolon
delimiters, uppercase author names with no IDs), and the last project that
occupied this space (Serenata de Amor) is dormant. Nobody offers an
event-driven or AI-native interface to this data today.

---

## The Three Layers

```
┌─────────────────────────────────────────────────────────────┐
│ 3. DELIVERY                                                 │
│    REST API │ Webhooks + event log │ Parquet dumps │ MCP    │
├─────────────────────────────────────────────────────────────┤
│ 2. CANONICAL MODELS + ENTITY RESOLUTION                     │
│    Politician, Emenda, Vote, Expense, BudgetReport, ...     │
│    Keys: IBGE code, Câmara deputado_id, CNPJ/CPF, TSE id    │
├─────────────────────────────────────────────────────────────┤
│ 1. HARVESTERS (one connector per upstream source)           │
│    Câmara │ Senado │ Portal Transparência │ SICONFI │ IBGE  │
│    TSE │ Querido Diário │ TransfereGov                      │
└─────────────────────────────────────────────────────────────┘
```

### 1. Harvesters

One connector per source, each handling: bulk-file download + incremental REST,
pagination, retry/backoff, rate limits, latin1→utf8, schema drift detection.
Bulk-first strategy: prefer the official yearly/monthly dumps (verified: no
auth needed for emendas, votes, CEAP, SICONFI), use keyed REST APIs only for
incremental freshness.

### 2. Canonical models

Pydantic-typed entities, stored as Parquet + DuckDB:

| Entity | Primary key | Sources joined |
|--------|-------------|----------------|
| `Municipality` | IBGE 7-digit code | IBGE, SICONFI, emendas |
| `Politician` | internal id ↔ Câmara id ↔ TSE id | Câmara, Senado, TSE |
| `Mandate` | politician + legislature | Câmara, Senado |
| `Emenda` | Portal code (or synthetic) | Portal Transparência |
| `EmendaPayment` | emenda + favorecido CNPJ | PorFavorecido dump |
| `Votacao` / `Vote` | Câmara votação id / + deputado id | Câmara bulk |
| `Proposicao` | Câmara id | Câmara bulk |
| `Speech` | deputado + timestamp | Câmara API |
| `Expense` (CEAP) | codDocumento | Câmara API/bulk |
| `BudgetReport` | ente + exercicio + periodo + anexo | SICONFI |
| `GazetteMention` | gazette id + excerpt hash | Querido Diário |

**Entity resolution is the core asset.** Emendas name authors as uppercase
strings ("ALICE PORTUGAL"); Câmara has proper ids; TSE has electoral ids;
suppliers appear as CNPJ. A dedicated `resolution/` module maintains the
crosswalk tables (name+legislature matcher, CNPJ registry join) with manual
override files for the tail.

### 3. Delivery

- **REST API** (FastAPI): `/municipios/{ibge}/orcamento`, `/emendas?autor=`,
  `/politicos/{id}/votos`, etc. Read-only, aggressively cached.
- **Events + webhooks** — the differentiator. Upstream has no push, so
  Caramelo *creates* it: each harvest is diffed against the previous state and
  emits typed events to an append-only log; subscribers register webhook URLs
  filtered by event type / UF / município / politician. Event taxonomy v0:
  - `emenda.created`, `emenda.updated`, `emenda.paid`
  - `votacao.nominal.registered` (roll-call available)
  - `expense.ceap.created`
  - `budget.report.published` (SICONFI new period)
  - `gazette.mention` (watched-term hit in a diário oficial)
- **Bulk dumps**: normalized Parquet published per harvest (GitHub Releases
  first, object storage later) — the "just give me the data" path.
- **MCP server**: expose search/lookup tools so any AI agent can query
  Brazilian public data natively. No one offers this today; it makes Caramelo
  the default context source for AI civic apps.

---

## MVP: the git-scraping pattern (zero-cost)

Phase 1 runs entirely on GitHub's free tier for public repos:

1. GitHub Actions cron (daily) runs harvesters → writes normalized Parquet/CSV.
2. Commit to a `data` branch — **git history is the event log for free**:
   diffs between commits ARE the `*.created/updated` events.
3. A tiny worker replays new commits into webhook deliveries.
4. Data served via raw GitHub URLs / Releases; DuckDB-WASM can query Parquet
   over HTTP directly from a browser.

This gives auditability (every data point traceable to a commit), zero infra
cost, and a trivially forkable public good. Graduate to hosted API + Postgres
only when consumers demand it.

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | Python 3.12 + Pydantic v2 | data ecosystem norm; Serenata heritage |
| Storage | Parquet + DuckDB | free, fast, queryable over HTTP (WASM) |
| API | FastAPI | typed, OpenAPI for free |
| Scheduling | GitHub Actions cron | zero-cost for public repos |
| Events | append-only JSONL + webhook worker | simple, replayable |
| MCP | FastMCP / official SDK | AI-agent access |
| Packages | `caramelo-br` (PyPI), `@caramelo-br/*` (npm) | bare names squatted |

---

## Roadmap

- [ ] v0.1 — harvester: emendas bulk (no auth) → canonical Parquet + author
      resolution against Câmara deputados
- [ ] v0.2 — harvester: Câmara bulk (votações, votos, proposições, CEAP)
- [ ] v0.3 — harvester: SICONFI (RREO/RGF/DCA per ente) + IBGE dimension tables
- [ ] v0.4 — git-scraping pipeline + diff→event log + webhook worker
- [ ] v0.5 — REST API (read-only) over DuckDB
- [ ] v0.6 — MCP server
- [ ] v0.7 — Querido Diário + TSE + TransfereGov harvesters
- [ ] v1.0 — stable event taxonomy + schema contracts + docs site

## Non-goals (live upstairs, not here)

- Scoring, rankings, red flags, coherence indexes — product layer
- Editorial content, case dossiês — product layer
- Anything requiring a political opinion — Caramelo stays neutral so its data
  is citable by anyone (press, academia, either side of the aisle)
