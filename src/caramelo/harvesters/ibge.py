"""Harvest the IBGE municipality dimension: codes, geography, population.

Sources (no auth):
- https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado
- https://servicodados.ibge.gov.br/api/v3/agregados/6579 (population estimates,
  latest period)
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

MUNICIPIOS_URL = ("https://servicodados.ibge.gov.br/api/v1/localidades/"
                  "municipios?view=nivelado")
POPULACAO_URL = ("https://servicodados.ibge.gov.br/api/v3/agregados/6579/"
                 "periodos/-1/variaveis/9324?localidades=N6[all]")

SCHEMA = pa.schema([
    ("codigo_ibge", pa.string()),
    ("nome", pa.string()),
    ("uf", pa.string()),
    ("uf_nome", pa.string()),
    ("regiao", pa.string()),
    ("populacao", pa.int64()),
    ("populacao_ano", pa.int16()),
])


def fetch_populacao() -> dict[str, tuple[int, int]]:
    """codigo_ibge -> (populacao, ano)"""
    body = get(POPULACAO_URL, timeout=180.0).json()
    out: dict[str, tuple[int, int]] = {}
    for serie in body[0]["resultados"][0]["series"]:
        values = serie["serie"]
        ano, pop = next(iter(values.items()))
        try:
            out[serie["localidade"]["id"]] = (int(pop), int(ano))
        except (TypeError, ValueError):
            continue
    return out


def harvest(data_dir: Path) -> Path:
    municipios = get(MUNICIPIOS_URL, timeout=120.0).json()
    populacao = fetch_populacao()

    rows: list[dict] = []
    for m in municipios:
        code = str(m["municipio-id"])
        pop = populacao.get(code)
        rows.append({
            "codigo_ibge": code,
            "nome": m["municipio-nome"],
            "uf": m["UF-sigla"],
            "uf_nome": m["UF-nome"],
            "regiao": m["regiao-nome"],
            "populacao": pop[0] if pop else None,
            "populacao_ano": pop[1] if pop else None,
        })

    out_path = data_dir / "municipios.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    with_pop = sum(1 for r in rows if r["populacao"] is not None)
    print(f"municipios: {len(rows)} rows ({with_pop} with population) "
          f"-> {out_path}")
    return out_path
