"""Harvest Câmara bills: proposicoes, proposicoes_autores, proposicoes_temas.

Bulk files (Câmara dados abertos, regenerated daily, no auth):
    https://dadosabertos.camara.leg.br/arquivos/proposicoes/csv/proposicoes-{ano}.csv
    .../proposicoesAutores/csv/proposicoesAutores-{ano}.csv
    .../proposicoesTemas/csv/proposicoesTemas-{ano}.csv

Gotcha: a year's file lists bills with *activity* that year, not bills
filed that year — a 2005 bill voted in 2026 sits in the 2026 file. Rows are
deduplicated by id with the most recent file winning, so ultimoStatus_* is
current.

Only legislative types are kept (PL, PLP, PEC, PDL, MPV, PLV, PRC, PLN):
requerimentos and indicações are ~90% of the rows and are procedural, not
"what a parlamentar proposes". Authors include non-deputies (Senado,
Executivo, comissões); `id_deputado` is null for those.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import download

BASE = "https://dadosabertos.camara.leg.br/arquivos"
TIPOS = {"PL", "PLP", "PEC", "PDL", "MPV", "PLV", "PRC", "PLN"}


def default_years() -> tuple[int, ...]:
    y = time.gmtime().tm_year
    return tuple(range(2023, y + 1))  # 57th legislature onwards


PROPOSICOES = pa.schema([
    ("id", pa.int64()), ("sigla_tipo", pa.string()), ("numero", pa.int32()),
    ("ano", pa.int16()), ("descricao_tipo", pa.string()),
    ("ementa", pa.string()), ("ementa_detalhada", pa.string()),
    ("keywords", pa.string()), ("data_apresentacao", pa.string()),
    ("id_principal", pa.int64()), ("url_inteiro_teor", pa.string()),
    ("status_data", pa.string()), ("status_orgao", pa.string()),
    ("status_regime", pa.string()), ("status_tramitacao", pa.string()),
    ("status_situacao", pa.string()), ("status_despacho", pa.string()),
    ("status_apreciacao", pa.string()), ("status_relator_id", pa.int64()),
    ("url", pa.string()),
])

AUTORES = pa.schema([
    ("id_proposicao", pa.int64()), ("id_deputado", pa.int64()),
    ("tipo_autor", pa.string()), ("nome_autor", pa.string()),
    ("partido", pa.string()), ("uf", pa.string()),
    ("ordem_assinatura", pa.int16()), ("proponente", pa.bool_()),
])

TEMAS = pa.schema([
    ("id_proposicao", pa.int64()), ("cod_tema", pa.int32()),
    ("tema", pa.string()), ("relevancia", pa.int16()),
])


def _int(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def _tail_id(uri: str | None) -> int | None:
    return _int((uri or "").rstrip("/").rsplit("/", 1)[-1]) if uri else None


def _read(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh, delimiter=";")


def harvest(data_dir: Path, years: tuple[int, ...] | None = None) -> list[Path]:
    years = tuple(sorted(years or default_years()))
    raw = data_dir / "raw" / "camara_proposicoes"

    props: dict[int, dict] = {}
    for year in years:  # ascending: later files overwrite earlier status
        f = download(f"{BASE}/proposicoes/csv/proposicoes-{year}.csv",
                     raw / f"proposicoes-{year}.csv", skip_if_exists=False)
        n = 0
        for r in _read(f):
            if r["siglaTipo"] not in TIPOS:
                continue
            pid = int(r["id"])
            props[pid] = {
                "id": pid, "sigla_tipo": r["siglaTipo"],
                "numero": _int(r["numero"]), "ano": _int(r["ano"]),
                "descricao_tipo": r["descricaoTipo"] or None,
                "ementa": r["ementa"] or None,
                "ementa_detalhada": r["ementaDetalhada"] or None,
                "keywords": r["keywords"] or None,
                "data_apresentacao": r["dataApresentacao"] or None,
                "id_principal": _tail_id(r["uriPropPrincipal"]),
                "url_inteiro_teor": r["urlInteiroTeor"] or None,
                "status_data": r["ultimoStatus_dataHora"] or None,
                "status_orgao": r["ultimoStatus_siglaOrgao"] or None,
                "status_regime": r["ultimoStatus_regime"] or None,
                "status_tramitacao": r["ultimoStatus_descricaoTramitacao"] or None,
                "status_situacao": r["ultimoStatus_descricaoSituacao"] or None,
                "status_despacho": r["ultimoStatus_despacho"] or None,
                "status_apreciacao": r["ultimoStatus_apreciacao"] or None,
                "status_relator_id": _tail_id(r["ultimoStatus_uriRelator"]),
                "url": f"https://www.camara.leg.br/propostas-legislativas/{pid}",
            }
            n += 1
        print(f"proposicoes {year}: {n} legislative rows")

    autores: dict[tuple[int, str, int | None], dict] = {}
    temas: dict[tuple[int, int], dict] = {}
    for year in years:
        f = download(f"{BASE}/proposicoesAutores/csv/proposicoesAutores-{year}.csv",
                     raw / f"proposicoesAutores-{year}.csv", skip_if_exists=False)
        for r in _read(f):
            pid = _int(r["idProposicao"])
            if pid not in props:
                continue
            key = (pid, r["nomeAutor"], _int(r["ordemAssinatura"]))
            autores[key] = {
                "id_proposicao": pid, "id_deputado": _int(r["idDeputadoAutor"]),
                "tipo_autor": r["tipoAutor"] or None,
                "nome_autor": r["nomeAutor"] or None,
                "partido": r["siglaPartidoAutor"] or None,
                "uf": r["siglaUFAutor"] or None,
                "ordem_assinatura": _int(r["ordemAssinatura"]),
                "proponente": r["proponente"] == "1",
            }
        f = download(f"{BASE}/proposicoesTemas/csv/proposicoesTemas-{year}.csv",
                     raw / f"proposicoesTemas-{year}.csv", skip_if_exists=False)
        for r in _read(f):
            pid = _tail_id(r["uriProposicao"])
            if pid not in props or _int(r["codTema"]) is None:
                continue
            temas[(pid, _int(r["codTema"]))] = {
                "id_proposicao": pid, "cod_tema": _int(r["codTema"]),
                "tema": r["tema"] or None, "relevancia": _int(r["relevancia"]),
            }

    outs = []
    for name, rows, schema in (
            ("proposicoes", sorted(props.values(), key=lambda r: r["id"]),
             PROPOSICOES),
            ("proposicoes_autores", sorted(
                autores.values(),
                key=lambda r: (r["id_proposicao"], r["ordem_assinatura"] or 0)),
             AUTORES),
            ("proposicoes_temas", sorted(
                temas.values(), key=lambda r: (r["id_proposicao"], r["cod_tema"])),
             TEMAS)):
        out = data_dir / f"{name}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), out,
                       compression="zstd")
        print(f"{name}: {len(rows)} rows -> {out}")
        outs.append(out)
    return outs
