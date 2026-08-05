"""Harvest CEAPS — senators' expense quota (Senado transparency CSVs).

Files: https://www.senado.leg.br/transparencia/LAI/verba/despesa_ceaps_{year}.csv
latin1, semicolon, first line is an update timestamp (skipped). Senators are
named by parliamentary name (no id) — joined downstream via the senadores
table like emenda authors.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import download

URL = "https://www.senado.leg.br/transparencia/LAI/verba/despesa_ceaps_{year}.csv"
DEFAULT_YEARS = (2023, 2024, 2025, 2026)

SCHEMA = pa.schema([
    ("ano", pa.int16()),
    ("mes", pa.int8()),
    ("senador_nome", pa.string()),
    ("tipo_despesa", pa.string()),
    ("fornecedor_cnpj_cpf", pa.string()),
    ("fornecedor", pa.string()),
    ("documento", pa.string()),
    ("data", pa.string()),
    ("detalhamento", pa.string()),
    ("valor_reembolsado", pa.float64()),
    ("cod_documento", pa.string()),
])


def _money(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return float(raw.replace(".", "").replace(",", "."))


def harvest(data_dir: Path, years: tuple[int, ...] = DEFAULT_YEARS) -> Path:
    out_path = data_dir / "ceaps.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(out_path, SCHEMA, compression="zstd")
    total = 0
    try:
        for year in years:
            raw = data_dir / "raw" / f"ceaps-{year}.csv"
            download(URL.format(year=year), raw, skip_if_exists=False)
            with open(raw, encoding="latin1", newline="") as fh:
                fh.readline()  # "ULTIMA ATUALIZACAO";"..."
                rows = []
                for r in csv.DictReader(fh, delimiter=";"):
                    rows.append({
                        "ano": int(r["ANO"]), "mes": int(r["MES"]),
                        "senador_nome": r["SENADOR"] or None,
                        "tipo_despesa": r["TIPO_DESPESA"] or None,
                        "fornecedor_cnpj_cpf": r["CNPJ_CPF"] or None,
                        "fornecedor": r["FORNECEDOR"] or None,
                        "documento": r["DOCUMENTO"] or None,
                        "data": r["DATA"] or None,
                        "detalhamento": r["DETALHAMENTO"] or None,
                        "valor_reembolsado": _money(r["VALOR_REEMBOLSADO"]),
                        "cod_documento": r["COD_DOCUMENTO"] or None,
                    })
            writer.write_table(pa.Table.from_pylist(rows, schema=SCHEMA))
            total += len(rows)
            print(f"ceaps {year}: {len(rows)} rows")
    finally:
        writer.close()
    print(f"ceaps: {total} rows -> {out_path}")
    return out_path
