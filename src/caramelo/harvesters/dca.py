"""Harvest municipal annual accounts (SICONFI DCA) -> financas_municipio.

Source: Tesouro Nacional SICONFI data lake (ORDS, no auth)
    https://apidatalake.tesouro.gov.br/ords/siconfi/tt/dca
        ?an_exercicio=YYYY&no_anexo=DCA-Anexo I-C&id_ente=<IBGE 7 digits>

Two annexes per município-year:
    I-C  receitas orçamentárias -> receita corrente/capital, IPTU, ISS, ITBI,
         FPM (all "Receitas Brutas Realizadas", excluding intra-orçamentárias)
    I-E  despesas por função    -> total, Cultura (13), Difusão Cultural
         (13.392), Saúde (10), Educação (12), Saneamento (17), Urbanismo (15)
         ("Despesas Liquidadas")

One wide row per (cod_ibge, exercicio). Values are reported, not derived:
ratios such as PEC 188/2019's "impostos próprios / receita" are computed
downstream (simulador), where the formula is stated next to the result.

5,570 municípios x 2 annexes x N years is too many calls for one daily run,
so the table is incremental: run-all seeds the previous financas_municipio
from the lake, and each run fills at most `max_entes` missing
(município, ano) pairs (4 concurrent workers). Coverage converges in about
a week; a município
that has not filed its DCA yet is retried on later runs.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

URL = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt/dca"
REQUEST_INTERVAL = 0.5
WORKERS = 4

# DCA is filed by 30 April for the previous year; the year before that is
# the last one with near-complete coverage.
def default_years() -> tuple[int, ...]:
    now = time.gmtime()
    last = now.tm_year - 1 if now.tm_mon >= 6 else now.tm_year - 2
    return (last - 1, last)


RECEITAS = {  # column -> cod_conta (I-C, Receitas Brutas Realizadas)
    "receita_corrente": "RO1.0.0.0.00.0.0",
    "receita_capital": "RO2.0.0.0.00.0.0",
    "impostos": "RO1.1.1.0.00.0.0",
    "iptu": "RO1.1.1.2.50.0.0",
    "itbi": "RO1.1.1.2.53.0.0",
    "iss": "RO1.1.1.4.51.0.0",
    "fpm": "RO1.7.1.1.51.0.0",
    "transferencias_correntes": "RO1.7.0.0.00.0.0",
}

DESPESAS = {  # column -> conta label prefix (I-E, Despesas Liquidadas)
    "despesa_total": "Despesas Exceto Intraorçamentárias",
    "despesa_saude": "10 - ",
    "despesa_educacao": "12 - ",
    "despesa_cultura": "13 - ",
    "despesa_difusao_cultural": "13.392 - ",
    "despesa_urbanismo": "15 - ",
    "despesa_saneamento": "17 - ",
}

SCHEMA = pa.schema(
    [("cod_ibge", pa.string()), ("exercicio", pa.int16()),
     ("instituicao", pa.string()), ("uf", pa.string()),
     ("populacao", pa.int64())]
    + [(c, pa.float64()) for c in RECEITAS]
    + [(c, pa.float64()) for c in DESPESAS]
    + [("fonte", pa.string())]
)


def _fetch(ente: str, ano: int, anexo: str) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        body = get(URL, params={"an_exercicio": ano, "no_anexo": anexo,
                                "id_ente": ente, "offset": offset},
                   timeout=120.0).json()
        items.extend(body.get("items", []))
        if not body.get("hasMore"):
            return items
        offset += body.get("limit", 5000)


def _parent_or_children(by_code: dict[str, float], code: str) -> float | None:
    """Value of an account; if a município reported only the sub-accounts
    (e.g. ISS split by .1/.2), sum the immediate children instead."""
    if code in by_code:
        return by_code[code]
    stem = code[: -len(".0.0")]
    kids = [v for k, v in by_code.items()
            if k.startswith(stem + ".") and k.endswith(".0") and k != code]
    return sum(kids) if kids else None


def fetch_ente_year(ente: str, ano: int) -> dict | None:
    receitas = _fetch(ente, ano, "DCA-Anexo I-C")
    time.sleep(REQUEST_INTERVAL)
    if not receitas:
        return None  # not filed (yet)
    despesas = _fetch(ente, ano, "DCA-Anexo I-E")
    time.sleep(REQUEST_INTERVAL)

    rec = {r["cod_conta"]: r["valor"] for r in receitas
           if r.get("coluna") == "Receitas Brutas Realizadas"}
    des: dict[str, float] = {}
    for r in despesas:
        if r.get("coluna") != "Despesas Liquidadas":
            continue
        conta = r.get("conta") or ""
        for col, prefix in DESPESAS.items():
            if conta.startswith(prefix) and col not in des:
                des[col] = r["valor"]
    first = receitas[0]
    row = {
        "cod_ibge": str(first["cod_ibge"]), "exercicio": ano,
        "instituicao": first.get("instituicao"), "uf": first.get("uf"),
        "populacao": first.get("populacao"),
        "fonte": f"SICONFI DCA {ano} (Anexos I-C, I-E)",
    }
    row.update({c: _parent_or_children(rec, code) for c, code in RECEITAS.items()})
    # A function the município did not spend on is a true zero, not missing.
    row.update({c: des.get(c, 0.0) for c in DESPESAS})
    return row


def harvest(data_dir: Path, years: tuple[int, ...] | None = None,
            max_entes: int = 1500) -> Path:
    years = years or default_years()
    out_path = data_dir / "financas_municipio.parquet"
    existing: dict[tuple[str, int], dict] = {}
    if out_path.exists():
        for r in pq.read_table(out_path).to_pylist():
            existing[(r["cod_ibge"], r["exercicio"])] = r

    entes = [r["codigo_ibge"] for r in pq.read_table(
        data_dir / "municipios.parquet", columns=["codigo_ibge"]).to_pylist()]
    todo = [(e, y) for y in sorted(years, reverse=True) for e in entes
            if (e, y) not in existing]
    print(f"dca: {len(existing)} município-years cached, {len(todo)} missing; "
          f"fetching up to {max_entes}")

    fetched = unfiled = 0

    def one(pair: tuple[str, int]) -> tuple[tuple[str, int], dict | None, str | None]:
        try:
            return pair, fetch_ente_year(*pair), None
        except RuntimeError as exc:  # one flaky ente must not sink the run
            return pair, None, str(exc)

    # The API is latency-bound (~2s per município-year); a few concurrent
    # workers cut wall time without hammering it.
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for (ente, ano), row, err in pool.map(one, todo[:max_entes]):
            if err:
                print(f"dca: {ente}/{ano} failed: {err}")
                continue
            fetched += 1
            if row is None:
                unfiled += 1
            else:
                existing[(ente, ano)] = row
            if fetched % 200 == 0:
                print(f"dca: {fetched} fetched ({unfiled} not filed)...")

    rows = sorted(existing.values(), key=lambda r: (r["cod_ibge"], r["exercicio"]))
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"financas_municipio: {len(rows)} município-years "
          f"({fetched} fetched, {unfiled} not filed) -> {out_path}")
    return out_path
