"""Attribute emendas Pix to the município (or state) that received them.

The Portal da Transparência emendas dump files ~93% of Pix money since 2023
under localidade "MÚLTIPLO", with no município. TransfereGov's planos de
ação (`pix_planos_acao`, one per emenda x beneficiary) name the beneficiary
entity — "MUNICIPIO DE X" with its CNPJ and UF — and, for plans in situação
CIENTE, add up to the Portal's Pix empenhado per year to the cent. This
module resolves each beneficiary to an IBGE code:

- exact: normalized name (accents, "MUNICIPIO DE", punctuation stripped)
  + UF equals an IBGE município name + UF;
- aproximado: the only IBGE município in that UF whose name starts with
  the beneficiary's name (renamed places, e.g. "SAO LUIZ" -> São Luiz do
  Anauá), or a close spelling (difflib ratio >= 0.88, e.g. GOUVEA/Gouveia);
- estado: state governments and the Distrito Federal (cod_ibge = UF code);
- ALIASES covers renamed municípios the rules above cannot reach;
- none: anything else (consórcios, typos below the threshold).

Output `pix_beneficiarios`: one row per plano de ação with the resolved
code, values as numbers and the plan's situação, so consumers can filter
to CIENTE (money committed) and sum by município and emenda year.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA = pa.schema([
    ("id_plano_acao", pa.string()),
    ("ano_emenda", pa.int16()),
    ("codigo_emenda", pa.string()),  # = emendas.codigo_emenda (Portal)
    ("nome_parlamentar", pa.string()),
    ("cnpj_beneficiario", pa.string()),
    ("nome_beneficiario", pa.string()),
    ("uf", pa.string()),
    ("cod_ibge", pa.string()),   # município (7 digits) or UF (2 digits)
    ("esfera", pa.string()),     # municipio | estado | outro
    ("match", pa.string()),      # exact | aproximado | estado | none
    ("situacao", pa.string()),
    ("valor_custeio", pa.float64()),
    ("valor_investimento", pa.float64()),
    ("valor_total", pa.float64()),
])

_PREFIX = re.compile(r"^(?:PREFEITURA MUNICIPAL|PREFEITURA|MUNICIPIO)"
                     r"(?: D[AEO]S?)? ")
_SUFFIX = re.compile(r" PREFEITURA MUNICIPAL$")
_ESTADO = re.compile(r"^(?:GOVERNO DO )?(?:ESTADO D[AEO]S? |DISTRITO FEDERAL)")
_UF_CODE = {"RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15",
            "AP": "16", "TO": "17", "MA": "21", "PI": "22", "CE": "23",
            "RN": "24", "PB": "25", "PE": "26", "AL": "27", "SE": "28",
            "BA": "29", "MG": "31", "ES": "32", "RJ": "33", "SP": "35",
            "PR": "41", "SC": "42", "RS": "43", "MS": "50", "MT": "51",
            "GO": "52", "DF": "53"}


# Former or alternative names still used by TransfereGov beneficiaries
# (normalized beneficiary name, UF) -> current IBGE name.
ALIASES = {
    ("PRESIDENTE JUSCELINO", "RN"): "SERRA CAIADA",
    ("BOA SAUDE", "RN"): "JANUARIO CICCO",
    ("ARES", "RN"): "AREZ",
    ("IGUARACI", "PE"): "IGUARACY",
    ("FORTALEZA DO TABOCAO", "TO"): "TABOCAO",
    ("SAO VALERIO DA NATIVIDADE", "TO"): "SAO VALERIO",
    ("POXOREO", "MT"): "POXOREU",
    ("JAHU", "SP"): "JAU",
    ("PARATI", "RJ"): "PARATY",
}


def norm(name: str | None) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(s.split())


def _clean_municipio(name: str) -> str:
    s = _SUFFIX.sub("", _PREFIX.sub("", name))
    s = s.replace("ESTANCIA TURISTICA DE ", "").replace("ESTANCIA DE ", "")
    return s.replace(" D OESTE", " DOESTE")


def _key(name: str) -> str:
    return name.replace(" D OESTE", " DOESTE")


class Resolver:
    def __init__(self, municipios: list[tuple[str, str, str]]):
        self.exact: dict[tuple[str, str], str] = {}
        self.by_uf: dict[str, list[tuple[str, str]]] = {}
        for cod, nome, uf in municipios:
            k = _key(norm(nome))
            self.exact[(k, uf)] = cod
            self.by_uf.setdefault(uf, []).append((k, cod))

    def resolve(self, nome: str | None, uf: str | None) -> tuple[str | None, str, str]:
        """-> (cod_ibge, esfera, match)"""
        n, uf = norm(nome), (uf or "").upper()
        if _ESTADO.match(n) or (uf == "DF" and "DISTRITO FEDERAL" in n):
            return _UF_CODE.get(uf), "estado", "estado"
        m = _clean_municipio(n)
        m = ALIASES.get((m, uf), m)
        if (m, uf) in self.exact:
            return self.exact[(m, uf)], "municipio", "exact"
        cands = self.by_uf.get(uf, [])
        starts = [c for k, c in cands if k.startswith(m + " ")]
        if len(starts) == 1:
            return starts[0], "municipio", "aproximado"
        names = [k for k, _ in cands]
        close = difflib.get_close_matches(m, names, n=2, cutoff=0.88)
        if len(close) == 1 or (close and difflib.SequenceMatcher(
                None, m, close[0]).ratio() - difflib.SequenceMatcher(
                None, m, close[1]).ratio() > 0.05):
            return dict(cands)[close[0]], "municipio", "aproximado"
        return None, "outro", "none"


def _num(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def resolve(data_dir: Path) -> Path | None:
    src = data_dir / "pix_planos_acao.parquet"
    mun = data_dir / "municipios.parquet"
    if not src.exists() or not mun.exists():
        print("pix: pix_planos_acao or municipios missing; skipped")
        return None
    m = pq.read_table(mun, columns=["codigo_ibge", "nome", "uf"]).to_pylist()
    resolver = Resolver([(r["codigo_ibge"], r["nome"], r["uf"]) for r in m])
    rows, counts, cache = [], {}, {}
    for r in pq.read_table(src).to_pylist():
        key = (r["nome_beneficiario_plano_acao"], r["uf_beneficiario_plano_acao"])
        if key not in cache:
            cache[key] = resolver.resolve(*key)
        cod, esfera, match = cache[key]
        custeio = _num(r["valor_custeio_plano_acao"])
        invest = _num(r["valor_investimento_plano_acao"])
        ano = r["ano_emenda_parlamentar_plano_acao"]
        rows.append({
            "id_plano_acao": r["id_plano_acao"],
            "ano_emenda": int(ano) if ano and str(ano).isdigit() else None,
            "codigo_emenda": r["numero_emenda_parlamentar_plano_acao"],
            "nome_parlamentar": r["nome_parlamentar_emenda_plano_acao"],
            "cnpj_beneficiario": r["cnpj_beneficiario_plano_acao"],
            "nome_beneficiario": r["nome_beneficiario_plano_acao"],
            "uf": r["uf_beneficiario_plano_acao"],
            "cod_ibge": cod, "esfera": esfera, "match": match,
            "situacao": r["situacao_plano_acao"],
            "valor_custeio": custeio, "valor_investimento": invest,
            "valor_total": custeio + invest,
        })
        counts[match] = counts.get(match, 0) + custeio + invest
    out = data_dir / "pix_beneficiarios.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out,
                   compression="zstd")
    total = sum(counts.values()) or 1
    print(f"pix: {len(rows)} planos -> {out.name}; by value: " + ", ".join(
        f"{k} {v / total:.1%}" for k, v in sorted(counts.items())))
    return out
