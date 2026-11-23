"""Emenda money paid to each município's own government, from `favorecidos`.

The Portal da Transparência emendas dump files ~95% of non-Pix money since
2023 under localidade "MÚLTIPLO" (no município). The Portal's per-recipient
file (`favorecidos`: one row per emenda x recipient x month, with the
recipient's UF and município) does name the place for every payment. This
module keeps only recipients that ARE the município's public administration
(the prefeitura itself, its fundos municipais — saúde, assistência,
educação — and municipal autarquias/fundações), so the location is the
place that received the money, not a company's head office. Values are
amounts actually received (paid), by payment month.

Output `emendas_recebidas_municipio`: one row per município x year x
emenda type x legal nature, with the amount received and payment count.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.resolution.pix import Resolver

SCHEMA = pa.schema([
    ("cod_ibge", pa.string()),
    ("ano", pa.int16()),              # year of the payment (ano_mes)
    ("tipo", pa.string()),            # emenda type (Pix = Transferências Especiais)
    ("natureza_juridica", pa.string()),
    ("valor_recebido", pa.float64()),
    ("pagamentos", pa.int64()),
])

MUNICIPAL = ("Município",
             "Fundo Público da Administração Direta Municipal",
             "Órgão Público do Poder Executivo Municipal",
             "Autarquia Municipal",
             "Fundação Pública de Direito Público Municipal",
             "Fundação Pública de Direito Privado Municipal")


def resolve(data_dir: Path) -> Path | None:
    src = data_dir / "favorecidos.parquet"
    mun = data_dir / "municipios.parquet"
    if not src.exists() or not mun.exists():
        print("recebidos: favorecidos or municipios missing; skipped")
        return None
    m = pq.read_table(mun, columns=["codigo_ibge", "nome", "uf"]).to_pylist()
    resolver = Resolver([(r["codigo_ibge"], r["nome"], r["uf"]) for r in m])
    t = pq.read_table(src, columns=["tipo", "ano_mes", "natureza_juridica",
                                    "uf", "municipio", "valor_recebido"])
    agg: dict[tuple, list] = {}
    cache: dict[tuple, str | None] = {}
    unmatched = 0.0
    total = 0.0
    for r in t.to_pylist():
        if r["natureza_juridica"] not in MUNICIPAL:
            continue
        v = r["valor_recebido"] or 0.0
        total += v
        key = (r["municipio"], r["uf"])
        if key not in cache:
            cod, esfera, _ = resolver.resolve(*key)
            cache[key] = cod if esfera == "municipio" else None
        cod = cache[key]
        ano = str(r["ano_mes"] or "")[:4]
        if cod is None or not ano.isdigit():
            unmatched += v
            continue
        k = (cod, int(ano), r["tipo"], r["natureza_juridica"])
        cell = agg.setdefault(k, [0.0, 0])
        cell[0] += v
        cell[1] += 1
    rows = [{"cod_ibge": k[0], "ano": k[1], "tipo": k[2],
             "natureza_juridica": k[3], "valor_recebido": round(v[0], 2),
             "pagamentos": v[1]} for k, v in sorted(agg.items())]
    out = data_dir / "emendas_recebidas_municipio.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out,
                   compression="zstd")
    print(f"recebidos: {len(rows)} rows, "
          f"{len({r['cod_ibge'] for r in rows})} municípios -> {out.name}; "
          f"unmatched {unmatched / (total or 1):.2%} of value")
    return out
