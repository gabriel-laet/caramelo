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

Money columns are BRL floats (`valor_empenhado`, `valor_liquidado`,
`valor_pago`, `valor_rap_*`). Author names before 2021 are partially
"Sem informação" upstream.

## Provenance

Every harvester's exact upstream endpoint is documented in
[RESEARCH.md](https://raw.githubusercontent.com/gabriel-laet/caramelo/main/RESEARCH.md).
All upstream sources are official government open data (no scraping), except
`media`, which snapshots search-engine results via SearchApi.io with a
per-run credit ledger.
