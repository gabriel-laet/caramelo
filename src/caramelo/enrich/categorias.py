"""Derived layer: practical category per emenda (rule-based v0).

Maps each emenda to a citizen-meaningful category from the government's own
classification strings (função, subfunção, ação, plano orçamentário). This is
mechanical derivation with published rules — every row records which rule
fired (`evidencia`) and a confidence marker. Rows no rule reaches stay
`indefinido` and are the queue for the LLM pass (v1).

Output: emendas_categorias.parquet — one row per emenda row in the source
table, keyed by (codigo_emenda, ano, codigo_acao, codigo_plano_orcamentario).
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

RULES_VERSION = "v0-2026-08"

SCHEMA = pa.schema([
    ("codigo_emenda", pa.string()),
    ("ano", pa.int16()),
    ("codigo_acao", pa.string()),
    ("codigo_plano_orcamentario", pa.string()),
    ("categoria", pa.string()),
    ("confianca", pa.string()),   # regra | indefinido
    ("evidencia", pa.string()),   # the matched keyword and field
    ("regras_versao", pa.string()),
])

# Ordered: first match wins. Keywords are accent-stripped uppercase substrings
# matched against ação + plano orçamentário + subfunção text.
KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("eventos_shows", ("FESTIVIDADE", "FESTA", "FESTIVAL", "SHOW",
                       "EVENTOS CULTURAIS", "APOIO A EVENTOS", "CARNAVAL",
                       "SAO JOAO", "VAQUEJADA", "RODEIO", "EXPOSICAO AGROPEC",
                       "APOIO A PROJETOS CULTURAIS")),
    ("publicidade", ("PUBLICIDADE", "DIVULGACAO", "COMUNICACAO INSTITUCIONAL")),
    ("saude", ("SAUDE", "SUS", "ATENCAO BASICA", "ATENCAO ESPECIALIZADA",
               "HOSPITAL", "UPA", "FARMAC", "VIGILANCIA EPIDEMI",
               "SANITARIA")),
    ("saneamento", ("SANEAMENTO", "ESGOTO", "RESIDUOS SOLIDOS",
                    "ABASTECIMENTO DE AGUA", "DRENAGEM")),
    ("educacao", ("EDUCACAO", "ESCOLA", "CRECHE", "ENSINO", "UNIVERSID",
                  "ALFABETIZ", "MERENDA")),
    ("infraestrutura", ("PAVIMENTACAO", "INFRAESTRUTURA URBANA", "ESTRADA",
                        "RODOVIA", "PONTE", "MOBILIDADE", "ILUMINACAO",
                        "INFRAESTRUTURA TURISTICA", "OBRAS")),
    ("agropecuaria", ("AGRICULTURA", "AGROPECUARI", "PRODUTOR RURAL",
                      "PECUARI", "PESCA", "ABASTECIMENTO AGROALIMENTAR")),
    ("esporte", ("ESPORTE", "QUADRA", "GINASIO", "ATLETA")),
    ("cultura", ("CULTURA", "PATRIMONIO HISTORICO", "BIBLIOTECA", "MUSEU")),
    ("assistencia_social", ("ASSISTENCIA SOCIAL", "PROTECAO SOCIAL", "SUAS",
                            "CRIANCA E ADOLESCENTE", "IDOSO")),
    ("seguranca", ("SEGURANCA PUBLICA", "POLICIAMENTO", "DEFESA CIVIL",
                   "VIDEOMONITORAMENTO")),
    ("turismo", ("TURISMO",)),
]


def _norm(text: str | None) -> str:
    stripped = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in stripped if not unicodedata.combining(c)).upper()


def classify(texts: dict[str, str | None]) -> tuple[str, str, str]:
    """-> (categoria, confianca, evidencia)"""
    for field, raw in texts.items():
        text = _norm(raw)
        if not text:
            continue
        for categoria, keywords in KEYWORD_RULES:
            for kw in keywords:
                if kw in text:
                    return categoria, "regra", f"{field}~{kw}"
    return "indefinido", "indefinido", ""


def enrich(data_dir: Path) -> Path:
    llm_cache: dict[tuple, str] = {}
    cache_path = data_dir / "llm_categorias.parquet"
    if cache_path.exists():
        for r in pq.read_table(cache_path).to_pylist():
            llm_cache[(r["codigo_acao"], r["codigo_plano_orcamentario"])] = \
                r["categoria"]

    rows: list[dict] = []
    stats: Counter = Counter()
    for r in pq.read_table(
            data_dir / "emendas.parquet",
            columns=["codigo_emenda", "ano", "codigo_acao",
                     "codigo_plano_orcamentario", "nome_acao",
                     "nome_plano_orcamentario", "nome_subfuncao",
                     "nome_funcao"]).to_pylist():
        categoria, confianca, evidencia = classify({
            "plano_orcamentario": r["nome_plano_orcamentario"],
            "acao": r["nome_acao"],
            "subfuncao": r["nome_subfuncao"],
            "funcao": r["nome_funcao"],
        })
        if categoria == "indefinido":
            llm_cat = llm_cache.get(
                (r["codigo_acao"], r["codigo_plano_orcamentario"]))
            if llm_cat:
                categoria, confianca, evidencia = llm_cat, "llm", "workers-ai"
        stats[categoria] += 1
        rows.append({
            "codigo_emenda": r["codigo_emenda"], "ano": r["ano"],
            "codigo_acao": r["codigo_acao"],
            "codigo_plano_orcamentario": r["codigo_plano_orcamentario"],
            "categoria": categoria, "confianca": confianca,
            "evidencia": evidencia, "regras_versao": RULES_VERSION,
        })

    out = data_dir / "emendas_categorias.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out,
                   compression="zstd")
    total = sum(stats.values())
    print(f"categorias: {total} rows classified -> {out}")
    for cat, n in stats.most_common():
        print(f"  {cat}: {n} ({100 * n / total:.1f}%)")
    return out
