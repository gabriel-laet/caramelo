"""Refresh APS coverage in the published `indicadores_municipio` from here.

The Health Ministry's relatorioaps endpoint answers from a residential
Brazilian connection but fails from the cloud harvester, whose `indicadores`
step then carries the last published APS rows forward. This command runs
the same `_aps` fetch locally against the *published* table (downloaded by
deploy/local/refresh-aps.sh) and, when the rows changed, rewrites the table
and patches its manifest entry so the script can upload both. The next cloud
run seeds the uploaded table (state.SEED_TABLES) and keeps it.

Expects in data_dir: indicadores_municipio.parquet, municipios.parquet,
manifest.json. Returns 0 changed, 3 unchanged, 1 fetch failed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.harvesters.indicadores import SCHEMA, _aps

TABLE = "indicadores_municipio"
IND = "aps_cobertura_pct"


def refresh(data_dir: Path) -> int:
    path = data_dir / f"{TABLE}.parquet"
    cod6_to_7 = {r["codigo_ibge"][:6]: r["codigo_ibge"] for r in pq.read_table(
        data_dir / "municipios.parquet", columns=["codigo_ibge"]).to_pylist()}
    rows = pq.read_table(path).to_pylist()
    fresh = _aps(cod6_to_7)
    if not fresh:
        print("refresh-aps: APS fetch returned nothing; table untouched")
        return 1

    def key(r):
        return (r["cod_ibge"], r["indicador"], r["referencia"], r["valor"])

    old = sorted(key(r) for r in rows if r["indicador"] == IND)
    if old == sorted(key(r) for r in fresh):
        print(f"refresh-aps: unchanged ({len(fresh)} rows, "
              f"ref {fresh[0]['referencia']})")
        return 3

    out = [r for r in rows if r["indicador"] != IND] + fresh
    out.sort(key=lambda r: (r["cod_ibge"], r["indicador"]))
    pq.write_table(pa.Table.from_pylist(out, schema=SCHEMA), path,
                   compression="zstd")

    mpath = data_dir / "manifest.json"
    manifest = json.loads(mpath.read_text())
    blob = path.read_bytes()
    manifest["tables"][TABLE] = {
        **manifest["tables"].get(TABLE, {"file": path.name}),
        "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
        "rows": len(out)}
    mpath.write_text(json.dumps(manifest, ensure_ascii=False))
    refs = sorted({r["referencia"] for r in rows if r["indicador"] == IND})
    print(f"refresh-aps: changed {refs} -> {fresh[0]['referencia']}, "
          f"{len(old)} -> {len(fresh)} APS rows; {TABLE} {len(out)} rows")
    return 0
