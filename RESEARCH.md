# Brazilian Public Data Sources — Verified Research

> Probed live in August 2026. Every endpoint below was actually called and the
> response shape inspected before being listed.

---

## TL;DR

| Source | What | Auth | Format | Verified |
|--------|------|------|--------|----------|
| Portal da Transparência (bulk) | Emendas incl. Pix, 2014-2026 | **none** | zip/CSV latin1 `;` | 32MB zip, 94k rows |
| Portal da Transparência (API) | Incremental queries | free key (email signup) | JSON | 401 without key |
| Câmara Dados Abertos (bulk) | Roll-call votes, proposições, CEAP | **none** | CSV/JSON per year | 175k votes in 2025 file |
| Câmara Dados Abertos (API) | Deputados, despesas, discursos | **none** | JSON | all probed OK |
| Senado Dados Abertos | Senators, votes | **none** | XML/JSON | 200 OK |
| SICONFI (Tesouro) | RREO/RGF/DCA all 5,570 municípios + states | **none** | JSON | SP 2025 RREO OK |
| IBGE Localidades | Município codes, geo hierarchy | **none** | JSON | OK |
| TSE CKAN | Candidates, results, campaign finance | **none** | CKAN/CSV | 43 datasets, CC-BY |
| Querido Diário | Municipal official gazettes, full-text search | **none** | JSON + PDF links | search hit OK |

Everything needed for an MVP is auth-free. The only key (Portal API) is a free
email registration, needed only for incremental freshness later.

---

## 1. Emendas Parlamentares (Portal da Transparência)

- **Bulk**: `https://portaldatransparencia.gov.br/download-de-dados/emendas-parlamentares/UNICO`
  → zip with 3 files (checked 2026-08-04, dump dated 2026-07-29):
  - `EmendasParlamentares.csv` — 94,289 rows, 2014-2026. Columns: author
    name+code, emenda type, year, locality + **IBGE município code**,
    função/subfunção, programa/ação, empenhado/liquidado/pago/RAP values.
  - `EmendasParlamentares_Convenios.csv` — 25MB, execution via convênios.
  - `EmendasParlamentares_PorFavorecido.csv` — 180MB, payments per beneficiary
    **CNPJ** → supplier-level analysis.
- **API** (needs key): `api.portaldatransparencia.gov.br/api-de-dados/emendas`
  — header `chave-api-dados`, signup via email on the portal.
- **Verified facts from the data**:
  - Emendas Pix = type `Emenda Individual - Transferências Especiais` (5,108 rows).
  - Paid values by year: 2020 R$ 621M → 2021 R$ 2.0B → 2023 R$ 7.1B →
    2024 R$ 7.7B → 2025 R$ 6.9B → 2026 R$ 4.5B (partial). ~R$ 30B total.
  - Author names complete from 2021 on; 2014 fully "Sem informação",
    2016-2017 partially.
- **Gotchas**: latin1 encoding, `;` delimiter, Brazilian decimal commas,
  authors are uppercase name strings (no id) → needs resolution vs Câmara.

## 2. Câmara dos Deputados

- **Base**: `https://dadosabertos.camara.leg.br/api/v2/` (JSON via Accept
  header) + **bulk**: `https://dadosabertos.camara.leg.br/arquivos/{dataset}/csv/{dataset}-{year}.csv`
- Verified bulk files (2025): `votacoesVotos` (58MB, 175k rows — one row per
  deputy per roll-call, with party/UF), `votacoes` (8.7MB), `proposicoes` (90MB).
  Also available: `despesasCeap`, `discursos`, per year back to the 1990s.
- Verified API endpoints: `/deputados`, `/deputados/{id}/despesas` (per-receipt
  CEAP with supplier CNPJ), `/deputados/{id}/discursos` (keywords + full text
  links), `/votacoes/{id}/votos`.
- **Gotchas**: most votações are symbolic — only some have roll-calls (the
  `/votos` endpoint returns empty otherwise); use `votacoesVotos` bulk to find
  nominal ones. Party orientation per votação available (`/orientacoes`).

## 3. SICONFI — budgets of every ente (pillar-1 backbone)

- `https://apidatalake.tesouro.gov.br/ords/siconfi/tt/rreo?an_exercicio=2025&nr_periodo=1&co_tipo_demonstrativo=RREO&id_ente=3550308`
  → verified: standardized accounts for São Paulo city, population included.
- Same pattern: `rgf`, `dca` (annual accounts), `msc_*` (monthly trial
  balances). `id_ente` = IBGE code; loop over all municípios/states.
- ORDS API, paginated, no auth. Historical FINBRA data also available.

## 4. Support sources

- **IBGE**: `servicodados.ibge.gov.br/api/v1/localidades/municipios/{code}` —
  canonical geo dimension. Population estimates via `/api/v3/agregados`.
- **TSE**: `dadosabertos.tse.jus.br/api/3/action/package_search` — CKAN, 43
  datasets (candidates, results, **campaign donations**), CC-BY.
- **Querido Diário (OKBr)**: `api.queridodiario.ok.org.br/gazettes?querystring=...`
  — full-text search over municipal gazettes, returns excerpts + PDF URLs.
  Use for oversight signals (MP recommendations, TCE decisions, contracts).
- **Senado**: `legis.senado.leg.br/dadosabertos/` — 200 OK, XML-first.
- **TransfereGov / Base dos Dados (BigQuery)** — not yet probed; BD is a
  shortcut for historical backfills, Caramelo should not depend on it at runtime.

---

## Prior art / ecosystem

- **Serenata de Amor (OKBr)** — dormant since ~2019-2020; audited CEAP only;
  Rosie's rule catalog (price ceilings, CNPJ checks, geographic
  impossibilities) is MIT-licensed and worth mining. Jarbas dashboard offline.
- **Querido Diário** — the active OKBr project; complementary (we consume it).
- **Base dos Dados** — cleaned datasets on BigQuery; complementary, not
  event-driven, not AI-native.
- Gap Caramelo fills: nobody serves this data with events/webhooks, typed
  clients, entity resolution, or an MCP interface.
