"""Harvest SICONFI budget-execution reports (RREO) from Tesouro Nacional.

Base: https://apidatalake.tesouro.gov.br/ords/siconfi/tt (ORDS, no auth).
One call per ente/exercicio/periodo returns every anexo's accounts; results
paginate at 5000 items via offset/hasMore.

id_ente: 7-digit IBGE code for municípios, 2-digit UF code for states.

Sweeps are sharded: each ente's raw response lands in
data/raw/siconfi/{exercicio}-{periodo}/{ente}.json, already-harvested entes
are skipped on re-runs (resume for free), and live requests are rate-limited
to stay polite with the API. The combined Parquet is rebuilt from all shards.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

RREO_URL = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt/rreo"

REQUEST_INTERVAL = 0.7  # seconds between live API calls

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
        items.extend(body.get("items", []))
        if not body.get("hasMore"):
            return items
        offset += body.get("limit", 5000)


def municipio_entes(data_dir: Path, uf: str | None = None) -> tuple[str, ...]:
    """Ente codes for municípios, optionally filtered to one UF."""
    table = pq.read_table(data_dir / "municipios.parquet",
                          columns=["codigo_ibge", "uf"])
    return tuple(r["codigo_ibge"] for r in table.to_pylist()
                 if uf is None or r["uf"] == uf.upper())


def harvest(data_dir: Path, exercicio: int, periodo: int,
            entes: tuple[str, ...] = UF_ENTES) -> Path:
    shard_dir = data_dir / "raw" / "siconfi" / f"{exercicio}-{periodo}"
    shard_dir.mkdir(parents=True, exist_ok=True)

    fetched = skipped = empty = 0
    for ente in entes:
        shard = shard_dir / f"{ente}.json"
        if shard.exists():
            skipped += 1
            continue
        items = fetch_rreo(ente, exercicio, periodo)
        shard.write_text(json.dumps(items, ensure_ascii=False))
        fetched += 1
        if not items:
            empty += 1
        if fetched % 100 == 0:
            print(f"siconfi sweep: {fetched} fetched, {skipped} cached...")
        time.sleep(REQUEST_INTERVAL)
    print(f"siconfi rreo {exercicio}/{periodo}: {fetched} fetched "
          f"({empty} empty), {skipped} already cached")

    rows: list[dict] = []
    for shard in sorted(shard_dir.glob("*.json")):
        for it in json.loads(shard.read_text()):
            rows.append({
                "exercicio": it["exercicio"], "periodo": it["periodo"],
                "cod_ibge": str(it["cod_ibge"]),
                "instituicao": it["instituicao"], "uf": it["uf"],
                "esfera": it["esfera"], "populacao": it.get("populacao"),
                "anexo": it["anexo"], "rotulo": it.get("rotulo"),
                "coluna": it.get("coluna"), "cod_conta": it.get("cod_conta"),
                "conta": it.get("conta"), "valor": it.get("valor"),
            })

    out_path = data_dir / "siconfi_rreo.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"siconfi_rreo: {len(rows)} rows ({len(list(shard_dir.glob('*.json')))} "
          f"entes) -> {out_path}")
    return out_path
