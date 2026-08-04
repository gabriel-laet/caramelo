"""Harvest SICONFI budget-execution reports (RREO) from Tesouro Nacional.

Base: https://apidatalake.tesouro.gov.br/ords/siconfi/tt (ORDS, no auth).
One call per ente/exercicio/periodo returns every anexo's accounts; results
paginate at 5000 items via offset/hasMore.

id_ente: 7-digit IBGE code for municípios, 2-digit UF code for states.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

RREO_URL = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt/rreo"

# All 26 states + DF (2-digit IBGE UF codes)
UF_ENTES = (
    "11", "12", "13", "14", "15", "16", "17",              # Norte
    "21", "22", "23", "24", "25", "26", "27", "28", "29",  # Nordeste
    "31", "32", "33", "35",                                # Sudeste
    "41", "42", "43",                                      # Sul
    "50", "51", "52", "53",                                # Centro-Oeste
)

SCHEMA = pa.schema([
    ("exercicio", pa.int16()),
    ("periodo", pa.int8()),
    ("cod_ibge", pa.string()),
    ("instituicao", pa.string()),
    ("uf", pa.string()),
    ("esfera", pa.string()),
    ("populacao", pa.int64()),
    ("anexo", pa.string()),
    ("rotulo", pa.string()),
    ("coluna", pa.string()),
    ("cod_conta", pa.string()),
    ("conta", pa.string()),
    ("valor", pa.float64()),
])


def fetch_rreo(id_ente: str, exercicio: int, periodo: int) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        body = get(RREO_URL, params={
            "an_exercicio": exercicio, "nr_periodo": periodo,
            "co_tipo_demonstrativo": "RREO", "id_ente": id_ente,
            "offset": offset,
        }, timeout=120.0).json()
        for it in body.get("items", []):
            items.append({
                "exercicio": it["exercicio"], "periodo": it["periodo"],
                "cod_ibge": str(it["cod_ibge"]),
                "instituicao": it["instituicao"], "uf": it["uf"],
                "esfera": it["esfera"], "populacao": it.get("populacao"),
                "anexo": it["anexo"], "rotulo": it.get("rotulo"),
                "coluna": it.get("coluna"), "cod_conta": it.get("cod_conta"),
                "conta": it.get("conta"), "valor": it.get("valor"),
            })
        if not body.get("hasMore"):
            return items
        offset += body.get("limit", 5000)


def harvest(data_dir: Path, exercicio: int, periodo: int,
            entes: tuple[str, ...] = UF_ENTES) -> Path:
    rows: list[dict] = []
    for ente in entes:
        items = fetch_rreo(ente, exercicio, periodo)
        print(f"siconfi rreo {exercicio}/{periodo} ente {ente}: {len(items)} items")
        rows.extend(items)

    out_path = data_dir / "siconfi_rreo.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"siconfi_rreo: {len(rows)} rows -> {out_path}")
    return out_path
