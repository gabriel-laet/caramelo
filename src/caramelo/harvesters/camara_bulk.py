"""Harvest Câmara roll-call voting data from the yearly bulk files.

Files: https://dadosabertos.camara.leg.br/arquivos/{dataset}/csv/{dataset}-{year}.csv
UTF-8 with BOM, semicolon-delimited. Three datasets travel together:
votacoes (sessions), votacoesVotos (individual roll-call votes) and
votacoesOrientacoes (party/bloc orientations).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import download

BULK_BASE = "https://dadosabertos.camara.leg.br/arquivos"

DEFAULT_YEARS = (2023, 2024, 2025, 2026)

VOTACOES_SCHEMA = pa.schema([
    ("id", pa.string()),
    ("ano", pa.int16()),
    ("data", pa.string()),
    ("data_hora_registro", pa.string()),
    ("id_orgao", pa.int64()),
    ("sigla_orgao", pa.string()),
    ("aprovacao", pa.int8()),
    ("votos_sim", pa.int32()),
    ("votos_nao", pa.int32()),
    ("votos_outros", pa.int32()),
    ("descricao", pa.string()),
    ("proposicao_id", pa.int64()),
])

VOTOS_SCHEMA = pa.schema([
    ("id_votacao", pa.string()),
    ("ano", pa.int16()),
    ("data_hora_voto", pa.string()),
    ("voto", pa.string()),
    ("deputado_id", pa.int64()),
    ("partido", pa.string()),
    ("uf", pa.string()),
])

ORIENTACOES_SCHEMA = pa.schema([
    ("id_votacao", pa.string()),
    ("ano", pa.int16()),
    ("sigla_orgao", pa.string()),
    ("bancada", pa.string()),
    ("orientacao", pa.string()),
])


def _to_int(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    return int(float(raw)) if raw else None


def _read_bulk_csv(data_dir: Path, dataset: str, year: int) -> list[dict[str, str]]:
    url = f"{BULK_BASE}/{dataset}/csv/{dataset}-{year}.csv"
    raw = data_dir / "raw" / f"{dataset}-{year}.csv"
    download(url, raw)
    with open(raw, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def harvest(data_dir: Path, years: tuple[int, ...] = DEFAULT_YEARS) -> None:
    votacoes: list[dict] = []
    votos: list[dict] = []
    orientacoes: list[dict] = []

    for year in years:
        for row in _read_bulk_csv(data_dir, "votacoes", year):
            votacoes.append({
                "id": row["id"], "ano": year,
                "data": row["data"] or None,
                "data_hora_registro": row["dataHoraRegistro"] or None,
                "id_orgao": _to_int(row["idOrgao"]),
                "sigla_orgao": row["siglaOrgao"] or None,
                "aprovacao": _to_int(row["aprovacao"]),
                "votos_sim": _to_int(row["votosSim"]),
                "votos_nao": _to_int(row["votosNao"]),
                "votos_outros": _to_int(row["votosOutros"]),
                "descricao": row["descricao"] or None,
                "proposicao_id": _to_int(
                    row.get("ultimaApresentacaoProposicao_idProposicao")),
            })
        for row in _read_bulk_csv(data_dir, "votacoesVotos", year):
            votos.append({
                "id_votacao": row["idVotacao"], "ano": year,
                "data_hora_voto": row["dataHoraVoto"] or None,
                "voto": row["voto"] or None,
                "deputado_id": _to_int(row["deputado_id"]),
                "partido": row["deputado_siglaPartido"] or None,
                "uf": row["deputado_siglaUf"] or None,
            })
        for row in _read_bulk_csv(data_dir, "votacoesOrientacoes", year):
            orientacoes.append({
                "id_votacao": row["idVotacao"], "ano": year,
                "sigla_orgao": row["siglaOrgao"] or None,
                "bancada": row["siglaBancada"] or None,
                "orientacao": row["orientacao"] or None,
            })
        print(f"camara bulk {year}: ok")

    for name, rows, schema in (
        ("votacoes", votacoes, VOTACOES_SCHEMA),
        ("votos", votos, VOTOS_SCHEMA),
        ("orientacoes", orientacoes, ORIENTACOES_SCHEMA),
    ):
        out = data_dir / f"{name}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), out,
                       compression="zstd")
        print(f"{name}: {len(rows)} rows -> {out}")
