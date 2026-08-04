"""Harvest senadores from the Senado Federal open-data API.

Base: https://legis.senado.leg.br/dadosabertos (no auth; JSON via Accept).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API_BASE = "https://legis.senado.leg.br/dadosabertos"

SCHEMA = pa.schema([
    ("codigo", pa.int64()),
    ("nome_parlamentar", pa.string()),
    ("nome_completo", pa.string()),
    ("uf", pa.string()),
    ("legislatura", pa.int16()),
])


def fetch_senadores(legislatura: int) -> list[dict]:
    resp = get(f"{API_BASE}/senador/lista/legislatura/{legislatura}",
               headers={"Accept": "application/json"})
    parlamentares = (resp.json()["ListaParlamentarLegislatura"]
                     ["Parlamentares"]["Parlamentar"])
    out: list[dict] = []
    for p in parlamentares:
        ident = p["IdentificacaoParlamentar"]
        mandatos = p.get("Mandatos", {}).get("Mandato", [])
        if isinstance(mandatos, dict):
            mandatos = [mandatos]
        uf = mandatos[0].get("UfParlamentar") if mandatos else None
        out.append({
            "codigo": int(ident["CodigoParlamentar"]),
            "nome_parlamentar": ident.get("NomeParlamentar"),
            "nome_completo": ident.get("NomeCompletoParlamentar"),
            "uf": uf,
            "legislatura": legislatura,
        })
    return out


def harvest(data_dir: Path, legislaturas: tuple[int, ...] = (55, 56, 57)) -> Path:
    rows: list[dict] = []
    for leg in legislaturas:
        sens = fetch_senadores(leg)
        print(f"senadores: legislatura {leg} -> {len(sens)}")
        rows.extend(sens)

    out_path = data_dir / "senadores.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"senadores: {len(rows)} rows -> {out_path}")
    return out_path
