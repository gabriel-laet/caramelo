"""Harvest emenda payments by final recipient (PorFavorecido bulk file).

Same zip as the emendas harvester — no extra download. Each row is money
actually received by a favorecido (CNPJ/CPF), with the upstream-provided
legal nature ("Município", "Associação Privada", "Empresário (Individual)"…)
— the execution side of every emenda, streamed to Parquet in batches.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.harvesters.emendas import BULK_URL, MISSING, parse_money
from caramelo.http import download

CSV_NAME = "EmendasParlamentares_PorFavorecido.csv"
BATCH = 200_000

SCHEMA = pa.schema([
    ("codigo_emenda", pa.string()),
    ("nome_autor", pa.string()),
    ("tipo", pa.string()),
    ("ano_mes", pa.string()),
    ("favorecido_codigo", pa.string()),
    ("favorecido", pa.string()),
    ("natureza_juridica", pa.string()),
    ("tipo_favorecido", pa.string()),
    ("uf", pa.string()),
    ("municipio", pa.string()),
    ("valor_recebido", pa.float64()),
])


def _clean(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    return None if raw in MISSING else raw


def harvest(data_dir: Path) -> Path:
    raw_zip = data_dir / "raw" / "emendas.zip"
    download(BULK_URL, raw_zip)
    out_path = data_dir / "favorecidos.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    writer = pq.ParquetWriter(out_path, SCHEMA, compression="zstd")
    total = 0
    try:
        with zipfile.ZipFile(raw_zip) as zf, zf.open(CSV_NAME) as fh:
            text = io.TextIOWrapper(fh, encoding="latin1")
            batch: list[dict] = []
            for r in csv.DictReader(text, delimiter=";"):
                batch.append({
                    "codigo_emenda": _clean(r["Código da Emenda"]),
                    "nome_autor": _clean(r["Nome do Autor da Emenda"]),
                    "tipo": _clean(r["Tipo de Emenda"]),
                    "ano_mes": _clean(r["Ano/Mês"]),
                    "favorecido_codigo": _clean(r["Código do Favorecido"]),
                    "favorecido": _clean(r["Favorecido"]),
                    "natureza_juridica": _clean(r["Natureza Jurídica"]),
                    "tipo_favorecido": _clean(r["Tipo Favorecido"]),
                    "uf": _clean(r["UF Favorecido"]),
                    "municipio": _clean(r["Município Favorecido"]),
                    "valor_recebido": parse_money(
                        (r["Valor Recebido"] or "").strip()),
                })
                if len(batch) >= BATCH:
                    writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
                    total += len(batch)
                    batch = []
                    print(f"favorecidos: {total} rows...")
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
                total += len(batch)
    finally:
        writer.close()
    print(f"favorecidos: {total} rows -> {out_path}")
    return out_path
