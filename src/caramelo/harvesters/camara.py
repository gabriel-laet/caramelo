"""Harvest deputados from the Câmara dos Deputados open-data API.

Base: https://dadosabertos.camara.leg.br/api/v2 (no auth).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API_BASE = "https://dadosabertos.camara.leg.br/api/v2"

SCHEMA = pa.schema([
    ("id", pa.int64()),
    ("nome", pa.string()),
    ("partido", pa.string()),
    ("uf", pa.string()),
    ("legislatura", pa.int16()),
])


def fetch_deputados(legislatura: int) -> list[dict]:
    url = f"{API_BASE}/deputados"
    params: dict | None = {
        "idLegislatura": legislatura, "itens": 100,
        "ordem": "ASC", "ordenarPor": "nome",
    }
    out: list[dict] = []
    while url:
        resp = get(url, params=params, headers={"Accept": "application/json"})
        params = None  # next links already carry the query string
        body = resp.json()
        for dep in body["dados"]:
            out.append({
                "id": dep["id"],
                "nome": dep["nome"],
                "partido": dep.get("siglaPartido"),
                "uf": dep.get("siglaUf"),
                "legislatura": legislatura,
            })
        url = next((l["href"] for l in body["links"] if l["rel"] == "next"), None)
    return out


def harvest(data_dir: Path, legislaturas: tuple[int, ...] = (55, 56, 57)) -> Path:
    rows: list[dict] = []
    for leg in legislaturas:
        deps = fetch_deputados(leg)
        print(f"deputados: legislatura {leg} -> {len(deps)}")
        rows.extend(deps)

    out_path = data_dir / "deputados.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"deputados: {len(rows)} rows -> {out_path}")
    return out_path
