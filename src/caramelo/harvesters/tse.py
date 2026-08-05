"""Harvest TSE data: 2022 federal candidacies and campaign donations.

Sources (TSE CDN, no auth):
- consulta_cand_2022.zip: every candidacy (TSE sequential id, name, party,
  UF) — the TSE leg of the person crosswalk.
- prestacao_de_contas_eleitorais_candidatos_2022.zip: itemized campaign
  receipts with donor CPF/CNPJ — the donor <-> emenda-beneficiary join.

Only federal races are kept (Deputado Federal / Senador): those are the
mandates the rest of the lake tracks.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import download

CAND_URL = ("https://cdn.tse.jus.br/estatistica/sead/odsele/consulta_cand/"
            "consulta_cand_2022.zip")
RECEITAS_URL = ("https://cdn.tse.jus.br/estatistica/sead/odsele/"
                "prestacao_contas/"
                "prestacao_de_contas_eleitorais_candidatos_2022.zip")

FEDERAL_CARGOS = {"DEPUTADO FEDERAL", "SENADOR"}

CAND_SCHEMA = pa.schema([
    ("sq_candidato", pa.string()),
    ("nome", pa.string()),
    ("nome_urna", pa.string()),
    ("cpf", pa.string()),
    ("cargo", pa.string()),
    ("uf", pa.string()),
    ("partido", pa.string()),
    ("situacao_turno", pa.string()),
])

RECEITAS_SCHEMA = pa.schema([
    ("sq_candidato", pa.string()),
    ("candidato", pa.string()),
    ("cargo", pa.string()),
    ("uf", pa.string()),
    ("partido", pa.string()),
    ("doador_cpf_cnpj", pa.string()),
    ("doador_nome", pa.string()),
    ("origem_receita", pa.string()),
    ("fonte_receita", pa.string()),
    ("data_receita", pa.string()),
    ("valor_receita", pa.float64()),
])


def _money(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return float(raw.replace(".", "").replace(",", "."))


def _rows_from_zip(zip_path: Path, member_marker: str):
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist()
                   if member_marker in m and m.endswith(".csv")]
        # prefer the consolidated BRASIL file when present
        brasil = [m for m in members if "BRASIL" in m.upper()]
        for member in (brasil or members):
            print(f"tse: parsing {member}")
            with zf.open(member) as fh:
                text = io.TextIOWrapper(fh, encoding="latin1")
                yield from csv.DictReader(text, delimiter=";")
            if brasil:
                return


def harvest(data_dir: Path) -> None:
    raw_dir = data_dir / "raw"

    cand_zip = download(CAND_URL, raw_dir / "tse_cand_2022.zip")
    cand_rows = []
    for r in _rows_from_zip(cand_zip, "consulta_cand_2022"):
        if (r.get("DS_CARGO") or "").upper() not in FEDERAL_CARGOS:
            continue
        cand_rows.append({
            "sq_candidato": r.get("SQ_CANDIDATO"),
            "nome": r.get("NM_CANDIDATO"),
            "nome_urna": r.get("NM_URNA_CANDIDATO"),
            "cpf": r.get("NR_CPF_CANDIDATO"),
            "cargo": r.get("DS_CARGO"),
            "uf": r.get("SG_UF"),
            "partido": r.get("SG_PARTIDO"),
            "situacao_turno": r.get("DS_SIT_TOT_TURNO"),
        })
    out = data_dir / "tse_candidatos.parquet"
    pq.write_table(pa.Table.from_pylist(cand_rows, schema=CAND_SCHEMA), out,
                   compression="zstd")
    print(f"tse: {len(cand_rows)} federal candidacies -> {out}")

    receitas_zip = download(RECEITAS_URL, raw_dir / "tse_receitas_2022.zip")
    rec_rows = []
    for r in _rows_from_zip(receitas_zip, "receitas_candidatos_2022"):
        if (r.get("DS_CARGO") or "").upper() not in FEDERAL_CARGOS:
            continue
        rec_rows.append({
            "sq_candidato": r.get("SQ_CANDIDATO"),
            "candidato": r.get("NM_CANDIDATO"),
            "cargo": r.get("DS_CARGO"),
            "uf": r.get("SG_UF"),
            "partido": r.get("SG_PARTIDO"),
            "doador_cpf_cnpj": r.get("NR_CPF_CNPJ_DOADOR"),
            "doador_nome": r.get("NM_DOADOR_RFB") or r.get("NM_DOADOR"),
            "origem_receita": r.get("DS_ORIGEM_RECEITA"),
            "fonte_receita": r.get("DS_FONTE_RECEITA"),
            "data_receita": r.get("DT_RECEITA"),
            "valor_receita": _money(r.get("VR_RECEITA")),
        })
    out = data_dir / "tse_receitas.parquet"
    pq.write_table(pa.Table.from_pylist(rec_rows, schema=RECEITAS_SCHEMA), out,
                   compression="zstd")
    print(f"tse: {len(rec_rows)} federal campaign receipts -> {out}")
