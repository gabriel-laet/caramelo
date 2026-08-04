"""Harvest CEAP (cota parlamentar) expenses from the Câmara yearly bulk files.

Files: https://www.camara.leg.br/cotas/Ano-{year}.csv.zip
UTF-8 with BOM, semicolon-delimited, dot-decimal values. `ideCadastro` is the
same deputado id used by the Dados Abertos API, so expenses join the
deputados table directly. Leadership/bloc entries have no id and keep it null.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import download

BULK_URL = "https://www.camara.leg.br/cotas/Ano-{year}.csv.zip"

DEFAULT_YEARS = (2023, 2024, 2025, 2026)

SCHEMA = pa.schema([
    ("ano", pa.int16()),
    ("mes", pa.int8()),
    ("nome_parlamentar", pa.string()),
    ("deputado_id", pa.int64()),
    ("uf", pa.string()),
    ("partido", pa.string()),
    ("legislatura", pa.int16()),
    ("subcota_codigo", pa.int32()),
    ("subcota", pa.string()),
    ("fornecedor", pa.string()),
    ("fornecedor_cnpj_cpf", pa.string()),
    ("data_emissao", pa.string()),
    ("valor_documento", pa.float64()),
    ("valor_glosa", pa.float64()),
    ("valor_liquido", pa.float64()),
    ("ide_documento", pa.int64()),
    ("url_documento", pa.string()),
])


def _to_int(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    return int(float(raw)) if raw else None


def _to_float(raw: str | None) -> float | None:
    raw = (raw or "").strip()
    return float(raw.replace(",", ".")) if raw else None


def _rows_for_year(data_dir: Path, year: int):
    raw = data_dir / "raw" / f"ceap-{year}.csv.zip"
    download(BULK_URL.format(year=year), raw)
    with zipfile.ZipFile(raw) as zf:
        with zf.open(f"Ano-{year}.csv") as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig")
            for r in csv.DictReader(text, delimiter=";"):
                yield {
                    "ano": _to_int(r["numAno"]) or year,
                    "mes": _to_int(r["numMes"]),
                    "nome_parlamentar": r["txNomeParlamentar"] or None,
                    "deputado_id": _to_int(r["ideCadastro"]),
                    "uf": r["sgUF"] if r["sgUF"] not in ("", "NA") else None,
                    "partido": r["sgPartido"] or None,
                    "legislatura": _to_int(r["codLegislatura"]),
                    "subcota_codigo": _to_int(r["numSubCota"]),
                    "subcota": r["txtDescricao"] or None,
                    "fornecedor": r["txtFornecedor"] or None,
                    "fornecedor_cnpj_cpf": r["txtCNPJCPF"] or None,
                    "data_emissao": r["datEmissao"] or None,
                    "valor_documento": _to_float(r["vlrDocumento"]),
                    "valor_glosa": _to_float(r["vlrGlosa"]),
                    "valor_liquido": _to_float(r["vlrLiquido"]),
                    "ide_documento": _to_int(r["ideDocumento"]),
                    "url_documento": r["urlDocumento"] or None,
                }


def harvest(data_dir: Path, years: tuple[int, ...] = DEFAULT_YEARS) -> Path:
    out_path = data_dir / "ceap.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    writer = pq.ParquetWriter(out_path, SCHEMA, compression="zstd")
    try:
        for year in years:
            rows = list(_rows_for_year(data_dir, year))
            writer.write_table(pa.Table.from_pylist(rows, schema=SCHEMA))
            total += len(rows)
            print(f"ceap {year}: {len(rows)} rows")
    finally:
        writer.close()
    print(f"ceap: {total} rows -> {out_path}")
    return out_path
