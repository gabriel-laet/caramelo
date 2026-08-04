"""Demo: rankings straight from the harvested Parquet tables.

Run after:
    caramelo harvest emendas deputados senadores votacoes  (each once)
    caramelo resolve autores

Produces:
1. Top authors of emendas Pix (transferências especiais), 2023-2026.
2. Deputy alignment indexes over plenary roll-calls, 2023-2026:
   - governismo: agreement with the "Governo" bancada orientation
   - disciplina: agreement with the deputy's own party/federation orientation

Blocks ("Bl ...") are not matched to parties; federations ("Fdr A-B") are.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

DATA = Path(__file__).resolve().parent.parent / "data"

MIN_VOTES = 200


def money(v: float) -> str:
    return f"R$ {v:,.0f}".replace(",", ".")


def top_pix_authors(n: int = 15) -> None:
    crosswalk = {r["nome_autor"]: r for r in
                 pq.read_table(DATA / "autores_crosswalk.parquet").to_pylist()}
    partido_by_dep = {}
    for dep in pq.read_table(DATA / "deputados.parquet").to_pylist():
        partido_by_dep[dep["id"]] = dep["partido"]  # last write = latest leg

    sums: dict[str, dict[str, float]] = defaultdict(lambda: Counter())
    for r in pq.read_table(
            DATA / "emendas.parquet",
            columns=["nome_autor", "ano", "is_transferencia_especial",
                     "valor_empenhado", "valor_pago"]).to_pylist():
        if (r["is_transferencia_especial"] and r["nome_autor"]
                and (r["ano"] or 0) >= 2023):
            sums[r["nome_autor"]]["empenhado"] += r["valor_empenhado"] or 0
            sums[r["nome_autor"]]["pago"] += r["valor_pago"] or 0

    print(f"\n=== Top {n} autores de emendas Pix (2023-2026, por valor pago) ===")
    ranked = sorted(sums.items(), key=lambda kv: -kv[1]["pago"])[:n]
    for i, (author, v) in enumerate(ranked, 1):
        # 2023+ emendas: whoever holds a Senado id authored them as senator
        cw = crosswalk.get(author, {})
        casa = ("senador" if cw.get("senador_id") else
                "deputado" if cw.get("deputado_id") else "?")
        partido = (partido_by_dep.get(cw.get("deputado_id"), "")
                   if casa == "deputado" else "")
        tag = " ".join(t for t in (partido, cw.get("uf")) if t)
        print(f"{i:2d}. {author:<30} {casa:<9} {tag:<12} "
              f"pago {money(v['pago']):>18}  empenhado {money(v['empenhado']):>18}")


def pix_per_capita(n: int = 15) -> None:
    municipios = {m["codigo_ibge"]: m for m in
                  pq.read_table(DATA / "municipios.parquet").to_pylist()}

    sums: dict[str, dict] = {}
    for r in pq.read_table(
            DATA / "emendas.parquet",
            columns=["codigo_ibge_municipio", "nome_autor", "ano",
                     "is_transferencia_especial", "valor_empenhado"]).to_pylist():
        code = r["codigo_ibge_municipio"]
        if (r["is_transferencia_especial"] and (r["ano"] or 0) >= 2023
                and code in municipios):
            entry = sums.setdefault(code, {"total": 0.0, "autores": Counter()})
            entry["total"] += r["valor_empenhado"] or 0
            if r["nome_autor"]:
                entry["autores"][r["nome_autor"]] += r["valor_empenhado"] or 0

    ranked = sorted(
        ((code, e) for code, e in sums.items()
         if municipios[code]["populacao"]),
        key=lambda kv: -kv[1]["total"] / municipios[kv[0]]["populacao"])

    print(f"\n=== Top {n} municípios por emenda Pix per capita "
          f"(2023-2026, empenhado) ===")
    for i, (code, e) in enumerate(ranked[:n], 1):
        m = municipios[code]
        pc = e["total"] / m["populacao"]
        autor = e["autores"].most_common(1)[0][0] if e["autores"] else "?"
        print(f"{i:2d}. {m['nome']:<24} {m['uf']}  pop {m['populacao']:>9,}  "
              f"{money(e['total']):>16}  = {money(pc):>10}/hab  "
              f"maior autor: {autor}")


def party_tokens(bancada: str) -> set[str]:
    if bancada.startswith("Fdr "):
        return {t.upper() for t in bancada[4:].split("-")}
    if bancada.startswith("Bl "):
        return set()
    return {bancada.upper()}


def alignment_indexes() -> None:
    orientations: dict[str, dict[str, str]] = defaultdict(dict)
    gov: dict[str, str] = {}
    for o in pq.read_table(DATA / "orientacoes.parquet").to_pylist():
        if o["orientacao"] not in ("Sim", "Não"):
            continue
        if o["bancada"] == "Governo":
            gov[o["id_votacao"]] = o["orientacao"]
        else:
            for token in party_tokens(o["bancada"] or ""):
                orientations[o["id_votacao"]][token] = o["orientacao"]

    names: dict[int, str] = {}
    stats: dict[int, Counter] = defaultdict(Counter)
    partido_of: dict[int, str] = {}
    for v in pq.read_table(
            DATA / "votos.parquet",
            columns=["id_votacao", "voto", "deputado_id", "partido"]).to_pylist():
        if v["voto"] not in ("Sim", "Não"):
            continue
        dep, vid = v["deputado_id"], v["id_votacao"]
        partido_of[dep] = (v["partido"] or "").upper()
        s = stats[dep]
        if vid in gov:
            s["gov_total"] += 1
            s["gov_agree"] += v["voto"] == gov[vid]
        own = orientations.get(vid, {}).get((v["partido"] or "").upper())
        if own:
            s["own_total"] += 1
            s["own_agree"] += v["voto"] == own

    for dep in pq.read_table(DATA / "deputados.parquet").to_pylist():
        names[dep["id"]] = dep["nome"]

    rows = []
    for dep, s in stats.items():
        if s["gov_total"] >= MIN_VOTES:
            rows.append({
                "dep": dep, "nome": names.get(dep, str(dep)),
                "partido": partido_of.get(dep, ""),
                "governismo": s["gov_agree"] / s["gov_total"],
                "n_gov": s["gov_total"],
                "disciplina": (s["own_agree"] / s["own_total"]
                               if s["own_total"] >= MIN_VOTES else None),
            })

    rows.sort(key=lambda r: -r["governismo"])
    print(f"\n=== Governismo (concordância com orientação do Governo, "
          f"plenário 2023-2026, min {MIN_VOTES} votos) ===")
    print("Mais governistas:")
    for r in rows[:10]:
        print(f"  {r['governismo']:6.1%}  {r['nome']:<32} {r['partido']:<14} "
              f"({r['n_gov']} votos)")
    print("Menos governistas:")
    for r in rows[-10:]:
        print(f"  {r['governismo']:6.1%}  {r['nome']:<32} {r['partido']:<14} "
              f"({r['n_gov']} votos)")

    with_disc = [r for r in rows if r["disciplina"] is not None]
    with_disc.sort(key=lambda r: r["disciplina"])
    print(f"\n=== Disciplina partidária (concordância com a própria bancada) ===")
    print("Menos disciplinados:")
    for r in with_disc[:10]:
        print(f"  {r['disciplina']:6.1%}  {r['nome']:<32} {r['partido']:<14}")

    by_party: dict[str, list[float]] = defaultdict(list)
    for r in with_disc:
        by_party[r["partido"]].append(r["disciplina"])
    print("Disciplina média por partido (>= 10 deputados medidos):")
    ranked = sorted(((p, sum(v) / len(v), len(v)) for p, v in by_party.items()
                     if len(v) >= 10), key=lambda t: -t[1])
    for p, avg, count in ranked:
        print(f"  {avg:6.1%}  {p:<14} ({count} deputados)")


if __name__ == "__main__":
    top_pix_authors()
    pix_per_capita()
    alignment_indexes()
