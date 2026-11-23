# Caramelo — Data Documentation

Base URL: `https://data.caramelo.dev.br`

- `latest/<table>.parquet` — current version of each table (zstd Parquet)
- `latest/manifest.json` — row counts, byte sizes, sha256 per table
- `harvests/<ts>/manifest.json` — manifest history
- `events/<ts>.jsonl` — typed events derived at each publish

Query any table directly with DuckDB, no download step:

```sql
SELECT nome_autor, sum(valor_pago) AS pago
FROM 'https://data.caramelo.dev.br/latest/emendas.parquet'
WHERE is_transferencia_especial AND ano >= 2023
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

## Tables

| Table | Rows (approx.) | Source | Cadence |
|-------|----------------|--------|---------|
| `emendas` | 94k | Portal da Transparência bulk (2014-2026) | daily |
| `deputados` | 3.1k | Câmara API, legislaturas 55-57 | daily |
| `senadores` | 740 | Senado API, legislaturas 55-57 | daily |
| `votacoes` | 41k | Câmara bulk 2023-2026 | daily |
| `votos` | 469k | Câmara bulk (individual roll-call votes) | daily |
| `orientacoes` | 15k | Câmara bulk (party orientations) | daily |
| `ceap` | 761k | Câmara cota parlamentar bulk 2023-2026 | daily |
| `municipios` | 5,571 | IBGE localidades + population estimates | daily |
| `siconfi_rreo` | 93k | Tesouro SICONFI RREO (27 UFs, current period) | daily |
| `redes_sociais` | 1.2k | declared accounts on Câmara profiles | daily |
| `media` | growing | SearchApi.io news snapshots (append-only) | daily, budgeted |
| `autores_crosswalk` | 1.5k | derived: emenda author -> person resolution | daily |
| `indicadores_municipio` | ~50k | long format, one row per (município, indicador): IBGE Censo 2022 (water, sewage, favelas, population), SINISA 2024 (water/sewage network), Ministério da Saúde APS coverage (latest month), Inep Ideb 2025 anos iniciais | daily |
| `financas_municipio` | ~11k | Tesouro SICONFI DCA, one row per (município, exercício): receita corrente/capital, IPTU, ISS, ITBI, FPM, despesa by function (saúde, educação, cultura, difusão cultural, urbanismo, saneamento) | daily, fills gaps incrementally |
| `proposicoes` | ~27k | Câmara bulk 2023-2026, legislative types only (PL, PLP, PEC, PDL, MPV, PLV, PRC, PLN), current status | daily |
| `proposicoes_autores` | ~48k | Câmara bulk: authors of each bill (deputies and others) | daily |
| `proposicoes_temas` | ~60k | Câmara bulk: thematic classification of each bill | daily |
| `senado_proposicoes` | ~3.4k | Senado `/processo` API: PL, PLP, PEC, PDL, PRS authored by senators of the 57th legislature | daily |
| `senado_proposicoes_autores` | ~7.7k | (processo, senador) authorship pairs | daily |
| `pix_beneficiarios` | ~58k | derived from TransfereGov planos de ação (`pix_planos_acao`): one row per (emenda Pix, beneficiary) with the beneficiary resolved to an IBGE code | daily |

## Key columns and joins

- `municipios.codigo_ibge` (7-digit string) joins `emendas.codigo_ibge_municipio`
  and `siconfi_rreo.cod_ibge`.
- `deputados.id` joins `votos.deputado_id`, `ceap.deputado_id`,
  `redes_sociais.parlamentar_id` (casa = "camara") and
  `autores_crosswalk.deputado_id`.
- `senadores.codigo` joins `autores_crosswalk.senador_id`.
- `autores_crosswalk.nome_autor` joins `emendas.nome_autor` and
  `media.nome_autor`; `match` is `exact`, `uf`, `ambiguous` or `none`.
- Emendas Pix = `emendas.is_transferencia_especial = true`.
- Most Pix money is filed under `emendas.localidade = 'MÚLTIPLO'` with no
  município. `pix_beneficiarios` places it: `codigo_emenda` joins
  `emendas.codigo_emenda`, `cod_ibge` is the município (7 digits, `esfera =
  'municipio'`) or the UF code for state governments (`esfera = 'estado'`).
  `match` is `exact`, `aproximado` (renamed or respelled município),
  `estado` or `none`. Filter `situacao = 'CIENTE'`: those plans add up to
  the Portal's Pix `valor_empenhado` per year; `IMPEDIDO*` plans were
  blocked. The API prorates each emenda's Portal values by
  `valor_total` share.
- `indicadores_municipio.cod_ibge` and `financas_municipio.cod_ibge` join
  `municipios.codigo_ibge`. Every indicator row carries `referencia`
  (period), `fonte` and `url`. Pivot with
  `PIVOT indicadores_municipio ON indicador USING first(valor) GROUP BY cod_ibge`.
- Indicator caveats: `aps_cobertura_pct` is *cobertura potencial* and is not
  capped at 100; `esgoto_rede_sinisa_pct` exists only where a provider
  reported (absence is not zero); `favela_*` is 0 where IBGE mapped no
  favela; Ideb is absent where Inep did not publish (too few SAEB takers).
- `financas_municipio.despesa_cultura` is function 13 (Cultura) as the
  município classified it; 0 means no such line was reported. Events are
  sometimes booked under Turismo (23.695) instead, so treat it as a lower
  bound for show spending.
- `proposicoes.id` joins `proposicoes_autores.id_proposicao`,
  `proposicoes_temas.id_proposicao` and `votacoes.proposicao_id` (when a
  vote is on a bill); `proposicoes_autores.id_deputado` joins `deputados.id`.
  `id_principal` points to the bill a proposição is attached (apensada) to.
- `senado_proposicoes.id` joins `senado_proposicoes_autores.id_processo`;
  `codigo_senador` joins `senadores.codigo`.

Money columns are BRL floats (`valor_empenhado`, `valor_liquidado`,
`valor_pago`, `valor_rap_*`). Author names before 2021 are partially
"Sem informação" upstream.

## Provenance

Every harvester's exact upstream endpoint is documented in
[RESEARCH.md](https://raw.githubusercontent.com/gabriel-laet/caramelo/main/RESEARCH.md).
All upstream sources are official government open data (no scraping), except
`media`, which snapshots search-engine results via SearchApi.io with a
per-run credit ledger.
