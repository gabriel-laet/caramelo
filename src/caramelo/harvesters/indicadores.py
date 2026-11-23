"""Harvest municipal social indicators -> indicadores_municipio (long format).

One row per (cod_ibge, indicador). Every value carries its source, reference
period and URL, so any downstream comparison (e.g. a proposal simulator) can
say exactly which number it used. Sources (all official, auth-free):

  IBGE Censo 2022 via SIDRA API
    populacao_censo2022        t/4709  v/93
    agua_rede_censo2022_pct    t/6894  % residents whose main supply is the
                               rede geral (c1821=72144)
    esgoto_rede_censo2022_pct  t/7555  % residents on rede geral/pluvial or
                               fossa ligada à rede (c11558=46290)
    favela_populacao           t/9884  v/9612 (residents of favelas e
                               comunidades urbanas); municípios absent from
                               the table have none -> 0
    favela_pct                 favela_populacao / populacao_censo2022

  SINISA 2025 (ref. 2024; successor of SNIS) — Ministério das Cidades
    agua_rede_sinisa_pct       IAG0001 atendimento da população total com
                               rede de água
    esgoto_rede_sinisa_pct     IES0001 atendimento da população total com
                               rede coletora de esgoto (only municípios whose
                               provider reported; absence != 0)

  Ministério da Saúde — relatorioaps (cobertura potencial da APS)
    aps_cobertura_pct          latest published month; NOT capped at 100

  Inep — Ideb 2025, anos iniciais do ensino fundamental
    ideb_ai_municipal          rede Municipal
    ideb_ai_publica            rede Pública (municipal + estadual)

SIDRA "-" means a true zero; "..", "...", "X" mean unavailable/suppressed
and produce no row.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo import xlsx
from caramelo.http import download, get, ssl_with_intermediate

SIDRA = "https://apisidra.ibge.gov.br/values"
SIDRA_TABLES = {
    "populacao_censo2022": ("t/4709/n6/all/v/93/p/2022", "pessoas"),
    "agua_rede_censo2022_pct": (
        "t/6894/n6/all/v/1000382/p/2022/c1821/72144/c58/95253/c86/95251", "%"),
    "esgoto_rede_censo2022_pct": (
        "t/7555/n6/all/v/1000382/p/2022/c11558/46290/c58/95253/c86/95251", "%"),
    "favela_populacao": (
        "t/9884/n6/all/v/9612/p/2022/c86/95251/c2/6794/c58/95253", "pessoas"),
}

SINISA_BASE = ("https://www.gov.br/cidades/pt-br/acesso-a-informacao/"
               "acoes-e-programas/saneamento/sinisa/resultados-sinisa/")
SINISA = {
    "agua_rede_sinisa_pct": (
        SINISA_BASE + "SINISA_Resultados_Ref2024.zip",
        "Água - Base Municipal/SINISA_AGUA_Indicadores_Base Municipal_2024_Retificação.xlsx",
        "Indicadores de Gestão", "IAG0001"),
    "esgoto_rede_sinisa_pct": (
        SINISA_BASE + "SINISA_ESGOTO_Planilhas_2024.zip",
        "Esgoto - Base Municipal/SINISA_ESGOTO_Indicadores_Base Municipal_2024.xlsx",
        "Indicadores - Esgoto", "IES0001"),
}

APS_URL = "https://relatorioaps-prd.saude.gov.br/cobertura/aps"

IDEB_URL = ("https://download.inep.gov.br/ideb/resultados/"
            "divulgacao_anos_iniciais_municipios_2025.zip")
# download.inep.gov.br omits its intermediate; this is the AIA issuer URL
# from its certificate (GlobalSign-operated RNP CA).
INEP_ISSUER = "http://secure.globalsign.com/cacert/rnpicpedugr46ovtlsca2025.crt"

SCHEMA = pa.schema([
    ("cod_ibge", pa.string()),
    ("indicador", pa.string()),
    ("valor", pa.float64()),
    ("unidade", pa.string()),
    ("referencia", pa.string()),
    ("fonte", pa.string()),
    ("url", pa.string()),
])


def _num(raw: str) -> float | None:
    raw = (raw or "").strip()
    if raw == "-":
        return 0.0
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def _sidra() -> list[dict]:
    rows: list[dict] = []
    values: dict[str, dict[str, float]] = {}
    for ind, (path, unit) in SIDRA_TABLES.items():
        url = f"{SIDRA}/{path}"
        data = get(url, timeout=180.0).json()
        values[ind] = {}
        for r in data[1:]:  # first element is the header row
            v = _num(r.get("V", ""))
            if v is None:
                continue
            values[ind][r["D1C"]] = v
            rows.append({"cod_ibge": r["D1C"], "indicador": ind, "valor": v,
                         "unidade": unit, "referencia": "2022",
                         "fonte": "IBGE Censo 2022 (SIDRA)", "url": url})
        print(f"indicadores: {ind} {len(values[ind])} municípios")
        time.sleep(1)

    pop = values["populacao_censo2022"]
    fav = values["favela_populacao"]
    fav_url = f"{SIDRA}/{SIDRA_TABLES['favela_populacao'][0]}"
    for cod, p in pop.items():
        if cod not in fav:  # not in table 9884 = no favelas mapped
            rows.append({"cod_ibge": cod, "indicador": "favela_populacao",
                         "valor": 0.0, "unidade": "pessoas", "referencia": "2022",
                         "fonte": "IBGE Censo 2022 (SIDRA)", "url": fav_url})
        rows.append({"cod_ibge": cod, "indicador": "favela_pct",
                     "valor": round(100 * fav.get(cod, 0.0) / p, 3) if p else None,
                     "unidade": "%", "referencia": "2022",
                     "fonte": "IBGE Censo 2022 (SIDRA t/9884 ÷ t/4709)",
                     "url": fav_url})
    return rows


def _sinisa(raw_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for ind, (url, inner, sheet, code) in SINISA.items():
        zpath = download(url, raw_dir / url.rsplit("/", 1)[1])
        with zipfile.ZipFile(zpath) as zf:
            # inner paths sometimes carry a top-level folder; match by suffix
            name = next(n for n in zf.namelist() if n.endswith(inner))
            with zipfile.ZipFile(zf.open(name)) as book:
                n = 0
                for r in xlsx.table(book, code, sheet):
                    cod = (r.get("cod_IBGE") or "").strip()
                    v = _num(r.get(code, ""))
                    if not cod.isdigit() or v is None:
                        continue  # unit row, "Não calculado", blanks
                    rows.append({"cod_ibge": cod, "indicador": ind, "valor": v,
                                 "unidade": "%", "referencia": "2024",
                                 "fonte": f"SINISA 2025 ({code})", "url": url})
                    n += 1
        print(f"indicadores: {ind} {n} municípios")
    return rows


def _aps(cod6_to_7: dict[str, str]) -> list[dict]:
    now = time.gmtime()
    y, m = now.tm_year, now.tm_mon
    errors: list[str] = []
    for _ in range(8):  # newest published month, usually ~3 months back
        comp = f"{y}{m:02d}"
        try:  # one bad month (the server 500s on some) must not end the search
            data = get(APS_URL, params={"unidadeGeografica": "MUNICIPIO",
                                        "nuCompInicio": comp, "nuCompFim": comp},
                       timeout=180.0).json()
        except Exception as exc:
            errors.append(f"{comp}: {exc!r}"[:200])
            data = None
        if data:
            break
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    else:
        print("indicadores: APS — no published month found"
              + (f"; errors: {'; '.join(errors)}" if errors else ""))
        return []
    rows = []
    for r in data:
        cod = cod6_to_7.get(str(r.get("coMunicipioIbge", "")).zfill(6))
        if cod is None or r.get("qtCobertura") is None:
            continue
        rows.append({"cod_ibge": cod, "indicador": "aps_cobertura_pct",
                     "valor": float(r["qtCobertura"]), "unidade": "%",
                     "referencia": f"{comp[:4]}-{comp[4:]}",
                     "fonte": "Ministério da Saúde — cobertura potencial APS",
                     "url": f"{APS_URL}?unidadeGeografica=MUNICIPIO"
                            f"&nuCompInicio={comp}&nuCompFim={comp}"})
    print(f"indicadores: aps_cobertura_pct {len(rows)} municípios ({comp})")
    return rows


def _ideb(raw_dir: Path) -> list[dict]:
    zpath = download(IDEB_URL, raw_dir / IDEB_URL.rsplit("/", 1)[1],
                     verify=ssl_with_intermediate(INEP_ISSUER))
    rows = []
    redes = {"Municipal": "ideb_ai_municipal", "Pública": "ideb_ai_publica"}
    with zipfile.ZipFile(zpath) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".xlsx"))
        with zipfile.ZipFile(zf.open(name)) as book:
            for r in xlsx.table(book, "CO_MUNICIPIO"):
                ind = redes.get(r.get("REDE", ""))
                cod = r.get("CO_MUNICIPIO", "")
                v = _num(r.get("VL_OBSERVADO_2025", ""))
                # "-" here means not published (too few SAEB takers), not 0
                if not ind or not cod.isdigit() or v is None or \
                        r.get("VL_OBSERVADO_2025", "").strip() == "-":
                    continue
                rows.append({"cod_ibge": cod, "indicador": ind, "valor": v,
                             "unidade": "índice", "referencia": "2025",
                             "fonte": "Inep — Ideb 2025, anos iniciais",
                             "url": IDEB_URL})
    print(f"indicadores: ideb {len(rows)} rows")
    return rows


def harvest(data_dir: Path) -> Path:
    raw_dir = data_dir / "raw" / "indicadores"
    cod6_to_7 = {r["codigo_ibge"][:6]: r["codigo_ibge"] for r in pq.read_table(
        data_dir / "municipios.parquet", columns=["codigo_ibge"]).to_pylist()}

    out_path = data_dir / "indicadores_municipio.parquet"
    previous = (pq.read_table(out_path).to_pylist()
                if out_path.exists() else [])

    rows: list[dict] = []
    steps = (
        ("sidra", _sidra, ("populacao_censo2022", "agua_rede_censo2022_pct",
                           "esgoto_rede_censo2022_pct", "favela_populacao",
                           "favela_pct")),
        ("sinisa", lambda: _sinisa(raw_dir), tuple(SINISA)),
        ("aps", lambda: _aps(cod6_to_7), ("aps_cobertura_pct",)),
        ("ideb", lambda: _ideb(raw_dir), ("ideb_ai_municipal", "ideb_ai_publica")),
    )
    for name, step, indicators in steps:
        try:
            got = step()
            if not got:
                raise RuntimeError("no rows")
            rows.extend(got)
        except Exception as exc:
            # A source being down must not erase its indicators from the
            # lake: carry the last published values forward (their
            # `referencia` still says how old they are).
            kept = [r for r in previous if r["indicador"] in indicators]
            rows.extend(kept)
            print(f"indicadores: {name} failed ({exc}); kept {len(kept)} "
                  f"previous rows")

    rows.sort(key=lambda r: (r["cod_ibge"], r["indicador"]))
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"indicadores_municipio: {len(rows)} rows -> {out_path}")
    return out_path
