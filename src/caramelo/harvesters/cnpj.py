"""Enrich emenda-receiving companies with registry data (minhareceita.org).

Targeted lookups instead of the multi-GB Receita dump: every distinct CNPJ
that received meaningful emenda money gets its CNAE (economic activity) and
QSA (partners, with semi-masked CPFs) fetched once and cached. This powers
two red-flag surfaces:
- activity checks (event-production companies receiving 'infrastructure' money)
- the donor-owner join (campaign donor CPFs vs company partners).

API: GET https://minhareceita.org/{cnpj} (free, public; be polite).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API = "https://minhareceita.org"
REQUEST_INTERVAL = 0.15

VALUE_FLOOR = 1_000_000       # companies above this total received
ASSOC_FLOOR = 100_000         # associações (NGOs) above this

CNPJ_SCHEMA = pa.schema([
    ("cnpj", pa.string()),
    ("razao_social", pa.string()),
    ("cnae_codigo", pa.string()),
    ("cnae_descricao", pa.string()),
    ("natureza_juridica", pa.string()),
    ("municipio", pa.string()),
    ("uf", pa.string()),
    ("data_abertura", pa.string()),
    ("status", pa.string()),  # ok | not_found | error
])

SOCIOS_SCHEMA = pa.schema([
    ("cnpj", pa.string()),
    ("nome_socio", pa.string()),
    ("cpf_meio", pa.string()),  # visible middle digits of the masked CPF
    ("qualificacao", pa.string()),
])


def targets(data_dir: Path) -> list[str]:
    import duckdb
    rows = duckdb.connect().execute(f"""
        SELECT favorecido_codigo, sum(valor_recebido) AS total,
               any_value(natureza_juridica) AS nat
        FROM '{data_dir}/favorecidos.parquet'
        WHERE favorecido_codigo IS NOT NULL
          AND len(favorecido_codigo) = 14
          AND natureza_juridica NOT LIKE '%Município%'
          AND natureza_juridica NOT LIKE '%Órgão Público%'
          AND natureza_juridica NOT LIKE '%Fundo Público%'
          AND natureza_juridica NOT LIKE '%Autarquia%'
        GROUP BY 1
        HAVING total >= {ASSOC_FLOOR}
        ORDER BY total DESC""").fetchall()
    out = []
    for cnpj, total, nat in rows:
        is_assoc = "Associação" in (nat or "")
        if total >= VALUE_FLOOR or (is_assoc and total >= ASSOC_FLOOR):
            out.append(cnpj)
    return out


def harvest(data_dir: Path, max_lookups: int = 1500) -> None:
    cnpj_path = data_dir / "cnpjs.parquet"
    socios_path = data_dir / "socios.parquet"
    cnpj_rows = (pq.read_table(cnpj_path).to_pylist()
                 if cnpj_path.exists() else [])
    socio_rows = (pq.read_table(socios_path).to_pylist()
                  if socios_path.exists() else [])
    done = {r["cnpj"] for r in cnpj_rows}

    todo = [c for c in targets(data_dir) if c not in done]
    print(f"cnpj: {len(todo)} targets uncached (cap {max_lookups})")

    fetched = 0
    for cnpj in todo[:max_lookups]:
        try:
            resp = get(f"{API}/{cnpj}", retries=1, timeout=30.0)
            d = resp.json()
            cnpj_rows.append({
                "cnpj": cnpj, "razao_social": d.get("razao_social"),
                "cnae_codigo": str(d.get("cnae_fiscal") or ""),
                "cnae_descricao": d.get("cnae_fiscal_descricao"),
                "natureza_juridica": d.get("natureza_juridica"),
                "municipio": d.get("municipio"), "uf": d.get("uf"),
                "data_abertura": d.get("data_inicio_atividade"),
                "status": "ok",
            })
            for s in d.get("qsa") or []:
                mask = s.get("cnpj_cpf_do_socio") or ""
                meio = "".join(re.findall(r"\d", mask))
                socio_rows.append({
                    "cnpj": cnpj, "nome_socio": s.get("nome_socio"),
                    "cpf_meio": meio or None,
                    "qualificacao": s.get("qualificacao_socio"),
                })
        except RuntimeError as exc:
            status = ("not_found" if "404" in str(exc.__cause__ or exc)
                      else "error")
            cnpj_rows.append({"cnpj": cnpj, "razao_social": None,
                              "cnae_codigo": None, "cnae_descricao": None,
                              "natureza_juridica": None, "municipio": None,
                              "uf": None, "data_abertura": None,
                              "status": status})
        fetched += 1
        if fetched % 200 == 0:
            print(f"cnpj: {fetched} lookups...")
            pq.write_table(pa.Table.from_pylist(cnpj_rows, schema=CNPJ_SCHEMA),
                           cnpj_path, compression="zstd")
            pq.write_table(pa.Table.from_pylist(socio_rows,
                                                schema=SOCIOS_SCHEMA),
                           socios_path, compression="zstd")
        time.sleep(REQUEST_INTERVAL)

    pq.write_table(pa.Table.from_pylist(cnpj_rows, schema=CNPJ_SCHEMA),
                   cnpj_path, compression="zstd")
    pq.write_table(pa.Table.from_pylist(socio_rows, schema=SOCIOS_SCHEMA),
                   socios_path, compression="zstd")
    print(f"cnpj: {fetched} lookups this run -> {len(cnpj_rows)} companies, "
          f"{len(socio_rows)} partner records")
