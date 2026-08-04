"""Resolve emenda author names against Câmara and Senado parliamentarian ids.

Emendas name authors as bare uppercase strings ("ALICE PORTUGAL") with no
stable id. Deputados match on their parliamentary name; senadores on both
their parliamentary and full names. Remaining unresolved names are mostly
pre-2015 mandates and non-individual authors.
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
    ("casa", pa.string()),  # camara | senado
    ("parlamentar_id", pa.int64()),
    ("match", pa.string()),  # exact | ambiguous | none
])


def normalize_name(name: str) -> str:
    stripped = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return " ".join(stripped.upper().split())


def build_index(data_dir: Path) -> dict[str, set[tuple[str, int]]]:
    index: dict[str, set[tuple[str, int]]] = {}

    for dep in pq.read_table(data_dir / "deputados.parquet").to_pylist():
        index.setdefault(normalize_name(dep["nome"]), set()).add(
            ("camara", dep["id"]))

    for sen in pq.read_table(data_dir / "senadores.parquet").to_pylist():
        for name in (sen["nome_parlamentar"], sen["nome_completo"]):
            if name:
                index.setdefault(normalize_name(name), set()).add(
                    ("senado", sen["codigo"]))
    return index


def resolve(data_dir: Path) -> Path:
    emendas = pq.read_table(data_dir / "emendas.parquet",
                            columns=["nome_autor", "tipo"])
    index = build_index(data_dir)

    authors = sorted({
        row["nome_autor"] for row in emendas.to_pylist()
        if row["nome_autor"] and "Individual" in (row["tipo"] or "")
    })

    rows: list[dict] = []
    stats: Counter = Counter()
    for author in authors:
        norm = normalize_name(author)
        hits = index.get(norm, set())
        if len(hits) == 1:
            casa, pid = next(iter(hits))
            match = "exact"
        else:
            casa, pid = None, None
            match = "ambiguous" if hits else "none"
        stats[match] += 1
        rows.append({
            "nome_autor": author, "nome_normalizado": norm,
            "casa": casa, "parlamentar_id": pid, "match": match,
        })

    out_path = data_dir / "autores_crosswalk.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=CROSSWALK_SCHEMA),
                   out_path, compression="zstd")
    total = sum(stats.values())
    print(f"autores: {total} distinct individual-emenda authors -> {out_path}")
    for key in ("exact", "ambiguous", "none"):
        pct = 100 * stats[key] / total if total else 0
        print(f"  {key}: {stats[key]} ({pct:.1f}%)")
    return out_path
