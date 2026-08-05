"""Harvest emendas-Pix accountability records from TransfereGov.

Since the 2023 rules, transferências especiais (emendas Pix) must register
planos de ação and planos de trabalho in TransfereGov — including the
free-text declared purpose of the money. This is the official answer to what
the federal budget cannot say (every Pix emenda is labeled only "DESPESAS
DIVERSAS" upstream).

API: https://api.transferegov.gestao.gov.br/transferenciasespeciais
(PostgREST: limit/offset pagination, no auth).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API = "https://api.transferegov.gestao.gov.br/transferenciasespeciais"
PAGE = 1000  # PostgREST server-side max-rows cap
REQUEST_INTERVAL = 0.3

# table -> (output name, columns kept)
TABLES = {
    "plano_acao_especial": ("pix_planos_acao", [
        "id_plano_acao", "codigo_plano_acao", "ano_plano_acao",
        "situacao_plano_acao", "cnpj_beneficiario_plano_acao",
        "nome_beneficiario_plano_acao", "uf_beneficiario_plano_acao",
        "codigo_emenda_parlamentar_plano_acao",
        "nome_parlamentar_emenda_plano_acao", "ano_emenda_parlamentar_plano_acao",
        "numero_emenda_parlamentar_plano_acao",
        "valor_custeio_plano_acao", "valor_investimento_plano_acao",
    ]),
    "plano_trabalho_especial": ("pix_planos_trabalho", [
        "id_plano_trabalho", "id_plano_acao", "situacao_plano_trabalho",
        "data_inicio_execucao_plano_trabalho",
        "data_fim_execucao_plano_trabalho",
        "classificacao_orcamentaria_pt",
    ]),
    "finalidade_especial": ("pix_finalidades", [
        "id_executor", "cd_area_politica_publica_tipo_pt",
        "area_politica_publica_tipo_pt", "cd_area_politica_publica_pt",
        "area_politica_publica_pt",
    ]),
    "meta_especial": ("pix_metas", [
        "id_meta", "id_plano_trabalho", "numero_meta", "nome_meta",
        "descricao_meta", "valor_meta",
    ]),
}


def fetch_table(table: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = get(f"{API}/{table}", params={
            "limit": PAGE, "offset": offset,
        }, timeout=120.0).json()
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE
        print(f"transferegov {table}: {offset} rows...")
        time.sleep(REQUEST_INTERVAL)


def harvest(data_dir: Path) -> None:
    for table, (out_name, columns) in TABLES.items():
        raw = fetch_table(table)
        rows = []
        for r in raw:
            row = {}
            for c in columns:
                v = r.get(c)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                elif v is not None and not isinstance(v, str):
                    v = str(v)
                row[c] = v
            rows.append(row)
        schema = pa.schema([(c, pa.string()) for c in columns])
        out = data_dir / f"{out_name}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), out,
                       compression="zstd")
        print(f"transferegov: {table} -> {out_name}.parquet ({len(rows)} rows)")
