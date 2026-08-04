"""Harvest emendas parlamentares from Portal da Transparência bulk download.

Source (no auth required):
    https://portaldatransparencia.gov.br/download-de-dados/emendas-parlamentares/UNICO

The zip ships three latin1, semicolon-delimited CSVs; this harvester
normalizes the main one (EmendasParlamentares.csv) into a typed Parquet file.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import download

BULK_URL = "https://portaldatransparencia.gov.br/download-de-dados/emendas-parlamentares/UNICO"
MAIN_CSV = "EmendasParlamentares.csv"

TIPO_TRANSFERENCIA_ESPECIAL = "Emenda Individual - Transferências Especiais"

MISSING = {"", "Sem informação", "S/I"}

SCHEMA = pa.schema([
    ("codigo_emenda", pa.string()),
    ("ano", pa.int16()),
    ("tipo", pa.string()),
    ("is_transferencia_especial", pa.bool_()),
    ("codigo_autor", pa.string()),
    ("nome_autor", pa.string()),
    ("numero", pa.string()),
    ("localidade", pa.string()),
    ("codigo_ibge_municipio", pa.string()),
    ("municipio", pa.string()),
    ("codigo_ibge_uf", pa.string()),
    ("uf", pa.string()),
    ("regiao", pa.string()),
    ("codigo_funcao", pa.string()),
    ("nome_funcao", pa.string()),
    ("codigo_subfuncao", pa.string()),
    ("nome_subfuncao", pa.string()),
    ("codigo_programa", pa.string()),
    ("nome_programa", pa.string()),
    ("codigo_acao", pa.string()),
    ("nome_acao", pa.string()),
    ("codigo_plano_orcamentario", pa.string()),
    ("nome_plano_orcamentario", pa.string()),
    ("valor_empenhado", pa.float64()),
    ("valor_liquidado", pa.float64()),
    ("valor_pago", pa.float64()),
    ("valor_rap_inscritos", pa.float64()),
    ("valor_rap_cancelados", pa.float64()),
    ("valor_rap_pagos", pa.float64()),
])

COLUMN_MAP = {
    "Código da Emenda": "codigo_emenda",
    "Ano da Emenda": "ano",
    "Tipo de Emenda": "tipo",
    "Código do Autor da Emenda": "codigo_autor",
    "Nome do Autor da Emenda": "nome_autor",
    "Número da emenda": "numero",
    "Localidade de aplicação do recurso": "localidade",
    "Código Município IBGE": "codigo_ibge_municipio",
    "Município": "municipio",
    "Código UF IBGE": "codigo_ibge_uf",
    "UF": "uf",
    "Região": "regiao",
    "Código Função": "codigo_funcao",
    "Nome Função": "nome_funcao",
    "Código Subfunção": "codigo_subfuncao",
    "Nome Subfunção": "nome_subfuncao",
    "Código Programa": "codigo_programa",
    "Nome Programa": "nome_programa",
    "Código Ação": "codigo_acao",
    "Nome Ação": "nome_acao",
    "Código Plano Orçamentário": "codigo_plano_orcamentario",
    "Nome Plano Orçamentário": "nome_plano_orcamentario",
    "Valor Empenhado": "valor_empenhado",
    "Valor Liquidado": "valor_liquidado",
    "Valor Pago": "valor_pago",
    "Valor Restos A Pagar Inscritos": "valor_rap_inscritos",
    "Valor Restos A Pagar Cancelados": "valor_rap_cancelados",
    "Valor Restos A Pagar Pagos": "valor_rap_pagos",
}

MONEY_FIELDS = {
    "valor_empenhado", "valor_liquidado", "valor_pago",
    "valor_rap_inscritos", "valor_rap_cancelados", "valor_rap_pagos",
}


def parse_money(raw: str) -> float | None:
    if raw in MISSING:
        return None
    return float(raw.replace(".", "").replace(",", "."))


def normalize_row(row: dict[str, str]) -> dict:
    out: dict = {}
    for src, dst in COLUMN_MAP.items():
        raw = (row.get(src) or "").strip()
        if dst in MONEY_FIELDS:
            out[dst] = parse_money(raw)
        elif dst == "ano":
            out[dst] = int(raw) if raw not in MISSING else None
        else:
            out[dst] = None if raw in MISSING else raw
    out["is_transferencia_especial"] = out["tipo"] == TIPO_TRANSFERENCIA_ESPECIAL
    return out


def harvest(data_dir: Path) -> Path:
    raw_zip = data_dir / "raw" / "emendas.zip"
    out_path = data_dir / "emendas.parquet"
    download(BULK_URL, raw_zip)

    rows: list[dict] = []
    with zipfile.ZipFile(raw_zip) as zf:
        with zf.open(MAIN_CSV) as fh:
            text = io.TextIOWrapper(fh, encoding="latin1")
            for row in csv.DictReader(text, delimiter=";"):
                rows.append(normalize_row(row))

    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="zstd")
    print(f"emendas: {table.num_rows} rows -> {out_path}")
    return out_path
