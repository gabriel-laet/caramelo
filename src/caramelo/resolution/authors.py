"""Resolve emenda author names to parliamentarians (person-level).

Emendas name authors as bare uppercase strings ("ALICE PORTUGAL") with no
stable id. Resolution targets a *person*, who may hold a Câmara id, a Senado
id, or both (many authors served in both chambers). Disambiguation rules:

- Candidates sharing name + UF across chambers are the same person: both ids
  are kept on one row.
- Duplicate ids within a chamber (same name + UF, re-registrations) collapse
  to the id from the most recent legislature.
- True homonyms (same name, different UFs) are split by the author's modal
  destination UF — individual emendas overwhelmingly target the author's own
  constituency.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

CROSSWALK_SCHEMA = pa.schema([
    ("nome_autor", pa.string()),
    ("nome_normalizado", pa.string()),
    ("uf", pa.string()),
    ("deputado_id", pa.int64()),
    ("senador_id", pa.int64()),
    ("match", pa.string()),  # exact | uf | ambiguous | none
])


def normalize_name(name: str) -> str:
    stripped = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return " ".join(stripped.upper().split())


class Candidate:
    __slots__ = ("casa", "pid", "ufs", "max_leg")

    def __init__(self, casa: str, pid: int):
        self.casa, self.pid = casa, pid
        self.ufs: set[str] = set()
        self.max_leg = 0


def build_index(data_dir: Path) -> dict[str, dict[tuple[str, int], Candidate]]:
    index: dict[str, dict[tuple[str, int], Candidate]] = {}

    def add(name: str, casa: str, pid: int, uf: str | None, leg: int) -> None:
        cands = index.setdefault(normalize_name(name), {})
        cand = cands.setdefault((casa, pid), Candidate(casa, pid))
        if uf:
            cand.ufs.add(uf)
        cand.max_leg = max(cand.max_leg, leg)

    for dep in pq.read_table(data_dir / "deputados.parquet").to_pylist():
        add(dep["nome"], "camara", dep["id"], dep["uf"], dep["legislatura"])

    for sen in pq.read_table(data_dir / "senadores.parquet").to_pylist():
        for name in (sen["nome_parlamentar"], sen["nome_completo"]):
            if name:
                add(name, "senado", sen["codigo"], sen["uf"],
                    sen["legislatura"])
    return index


def _pick(candidates: list[Candidate]) -> tuple[int | None, int | None, str | None]:
    """Collapse candidates assumed to be one person into (dep_id, sen_id, uf)."""
    dep = max((c for c in candidates if c.casa == "camara"),
              key=lambda c: c.max_leg, default=None)
    sen = max((c for c in candidates if c.casa == "senado"),
              key=lambda c: c.max_leg, default=None)
    ufs = set().union(*(c.ufs for c in candidates)) if candidates else set()
    uf = next(iter(ufs)) if len(ufs) == 1 else None
    return (dep.pid if dep else None), (sen.pid if sen else None), uf


def resolve(data_dir: Path) -> Path:
    emendas = pq.read_table(data_dir / "emendas.parquet",
                            columns=["nome_autor", "tipo", "uf"]).to_pylist()
    index = build_index(data_dir)

    dest_ufs: dict[str, Counter] = {}
    for row in emendas:
        if row["nome_autor"] and "Individual" in (row["tipo"] or ""):
            counter = dest_ufs.setdefault(row["nome_autor"], Counter())
            if row["uf"]:
                counter[row["uf"]] += 1

    rows: list[dict] = []
    stats: Counter = Counter()
    for author in sorted(dest_ufs):
        candidates = list(index.get(normalize_name(author), {}).values())
        author_ufs = {uf for c in candidates for uf in c.ufs}
        dep_id = sen_id = uf = None

        if not candidates:
            match = "none"
        elif len(author_ufs) <= 1:
            # single constituency: one person, possibly ids in both chambers
            match = "exact"
            dep_id, sen_id, uf = _pick(candidates)
        else:
            # true homonyms: split by the author's modal destination UF
            match = "ambiguous"
            modal = dest_ufs[author].most_common(1)
            if modal:
                in_uf = [c for c in candidates if modal[0][0] in c.ufs]
                if in_uf and len({uf for c in in_uf for uf in c.ufs}) == 1:
                    match = "uf"
                    dep_id, sen_id, uf = _pick(in_uf)

        stats[match] += 1
        rows.append({
            "nome_autor": author, "nome_normalizado": normalize_name(author),
            "uf": uf, "deputado_id": dep_id, "senador_id": sen_id,
            "match": match,
        })

    out_path = data_dir / "autores_crosswalk.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=CROSSWALK_SCHEMA),
                   out_path, compression="zstd")
    total = sum(stats.values())
    resolved = stats["exact"] + stats["uf"]
    print(f"autores: {total} distinct individual-emenda authors "
          f"({resolved} resolved) -> {out_path}")
    for key in ("exact", "uf", "ambiguous", "none"):
        pct = 100 * stats[key] / total if total else 0
        print(f"  {key}: {stats[key]} ({pct:.1f}%)")
    return out_path
