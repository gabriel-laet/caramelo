"""Harvest municipal gazette mentions via Querido Diário (OKBr).

The federal money trail for emendas Pix ends at the prefeitura; what the
money bought surfaces in municipal official gazettes — show contracts,
inexigibilidade notices, MP recommendations. This harvester runs watched-term
phrase searches and appends matches keyed by IBGE territory id, joining the
rest of the lake at the município level.

API: https://api.queridodiario.ok.org.br (no auth; be polite).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API = "https://api.queridodiario.ok.org.br/gazettes"

WATCHED_TERMS = (
    "contratação de show",
    "cachê artístico",
    "contratação artística",
    "show artístico",
    "transferência especial",
    "emenda pix",
)

DEFAULT_SINCE = "2023-01-01"
PAGE_SIZE = 100

SCHEMA = pa.schema([
    ("mention_id", pa.string()),
    ("term", pa.string()),
    ("codigo_ibge", pa.string()),
    ("municipio", pa.string()),
    ("uf", pa.string()),
    ("data", pa.string()),
    ("url", pa.string()),
    ("excerpt", pa.string()),
    ("fetched_at", pa.string()),
])


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def harvest(data_dir: Path, max_pages_per_term: int = 5) -> Path:
    out_path = data_dir / "gazetas.parquet"
    existing = None
    seen: set[str] = set()
    since_by_term: dict[str, str] = {}
    if out_path.exists():
        existing = pq.read_table(out_path)
        for r in existing.select(["mention_id", "term", "data"]).to_pylist():
            seen.add(r["mention_id"])
            cur = since_by_term.get(r["term"])
            if cur is None or r["data"] > cur:
                since_by_term[r["term"]] = r["data"]

    fetched_at = _now()
    rows: list[dict] = []
    for term in WATCHED_TERMS:
        since = since_by_term.get(term, DEFAULT_SINCE)
        total = None
        for page in range(max_pages_per_term):
            body = get(API, params={
                "querystring": f'"{term}"',
                "published_since": since,
                "size": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
                "sort_by": "descending_date",
            }, timeout=60.0).json()
            total = body.get("total_gazettes", 0)
            gazettes = body.get("gazettes", [])
            if not gazettes:
                break
            for g in gazettes:
                excerpt = " […] ".join(g.get("excerpts") or [])[:2000]
                digest = hashlib.sha256(
                    f"{term}|{g.get('url')}|{excerpt}".encode()).hexdigest()[:20]
                if digest in seen:
                    continue
                seen.add(digest)
                rows.append({
                    "mention_id": digest, "term": term,
                    "codigo_ibge": str(g.get("territory_id") or ""),
                    "municipio": g.get("territory_name"),
                    "uf": g.get("state_code"),
                    "data": g.get("date"), "url": g.get("url"),
                    "excerpt": excerpt, "fetched_at": fetched_at,
                })
            time.sleep(0.5)
        print(f"gazetas[{term}]: {total} matching since {since}, "
              f"harvested new: {sum(1 for r in rows if r['term'] == term)}")

    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    if existing is not None:
        table = pa.concat_tables([existing, table])
    pq.write_table(table, out_path, compression="zstd")
    print(f"gazetas: +{len(rows)} mentions -> {out_path} ({table.num_rows} total)")
    return out_path
