"""Election debates: curated seed -> `debates` table -> local archive.

The seed (seeds/debates.toml) is hand-curated: there is no API listing
debates, and recordings are scattered across broadcaster channels and fan
re-uploads. `harvest` publishes only the public-safe event metadata;
`archive` pulls audio + any captions for each recording into the NAS
archive, where `transcribe` (Whisper) picks them up.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.harvesters.youtube import SEEDS

SCHEMA = pa.schema([
    ("debate_id", pa.string()),
    ("ano", pa.int16()),
    ("turno", pa.int8()),
    ("cargo", pa.string()),
    ("uf", pa.string()),
    ("emissora", pa.string()),
    ("data", pa.string()),
    ("participantes", pa.list_(pa.string())),
    ("fontes", pa.list_(pa.string())),
    ("status", pa.string()),
])


def load_seed(seeds: Path = SEEDS) -> list[dict]:
    doc = tomllib.loads((seeds / "debates.toml").read_text())
    rows, seen = [], set()
    for d in doc.get("debate", []):
        if d["id"] in seen:
            raise ValueError(f"debates.toml: duplicate id {d['id']}")
        seen.add(d["id"])
        rows.append({
            "debate_id": d["id"], "ano": d["ano"], "turno": d.get("turno"),
            "cargo": d["cargo"], "uf": d.get("uf"),
            "emissora": d.get("emissora"), "data": d.get("data"),
            "participantes": d.get("participantes", []),
            "fontes": d.get("fontes", []),
            "status": d.get("status", "verificar"),
        })
    return rows


def harvest(data_dir: Path, seeds: Path = SEEDS) -> Path:
    rows = load_seed(seeds)
    out = data_dir / "debates.parquet"
    data_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out,
                   compression="zstd")
    with_src = sum(1 for r in rows if r["fontes"])
    print(f"debates: {len(rows)} debates ({with_src} with recordings) -> {out}")
    return out


def archive(archive_root: Path, seeds: Path = SEEDS,
            only: str | None = None) -> None:
    """Fetch audio + captions for every recording of every debate.
    Each fonte gets its own dir (<debate_id>/<n>) — mirrors can differ in
    cut and length, so they are transcribed independently."""
    from caramelo import archive as arc

    ok = failed = 0
    for d in load_seed(seeds):
        if only and d["debate_id"] != only:
            continue
        for n, url in enumerate(d["fontes"]):
            res = arc.fetch_media(url, arc.debate_dir(
                archive_root, d["debate_id"], n), audio=True)
            if res["ok"]:
                ok += 1
            else:
                failed += 1
                print(f"debates-archive: {d['debate_id']}[{n}]: {res['error']}")
    print(f"debates-archive: {ok} recordings ok, {failed} failed -> "
          f"{archive_root / 'debates'}")


# --- link discovery ---------------------------------------------------------

def _search(query: str, n: int) -> list[dict]:
    """YouTube search through yt-dlp: no API key, no quota."""
    import json
    import subprocess

    from caramelo.archive import _ytdlp_base
    proc = subprocess.run(
        _ytdlp_base() + ["--flat-playlist", "--dump-json",
                         f"ytsearch{n}:{query}"],
        capture_output=True, text=True)
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def _score(d: dict, r: dict) -> int:
    import unicodedata

    def norm(s: str) -> str:
        return unicodedata.normalize("NFKD", s or "").encode(
            "ascii", "ignore").decode().lower()
    title, channel = norm(r.get("title")), norm(r.get("channel"))
    broadcaster = norm(d["emissora"] or "").split(" ")[0].strip("(")
    day, month = ((d["data"][8:10], d["data"][5:7]) if d["data"]
                  else ("", ""))
    score = 0
    score += 3 * (str(d["ano"]) in title)
    score += 2 * bool(broadcaster and (broadcaster in title
                                      or broadcaster in channel))
    score += 2 * bool(d["turno"] and (f"{d['turno']}o turno" in title
                                      or f"{d['turno']}º turno" in (r.get("title") or "").lower()
                                      or ("segundo" if d["turno"] == 2 else "primeiro") in title))
    score += 2 * any(w in title for w in ("completo", "integra", "na integra"))
    score += 1 * bool(day and f"{day}/{month}" in title)
    score -= 3 * any(w in title for w in ("melhores momentos", "resumo",
                                          "analise", "trecho", "corte"))
    return score


def discover(seeds: Path = SEEDS, per_query: int = 10, min_minutes: int = 60,
             keep: int = 3) -> list[dict]:
    """Propose recordings for debates that have no `fontes` yet. Only
    full-length results (>= min_minutes) are kept; candidates are ranked
    for a human to approve into debates.toml — never written there."""
    out = []
    for d in load_seed(seeds):
        if d["fontes"]:
            continue
        turno = f"{d['turno']}º turno" if d["turno"] else ""
        broadcaster = (d["emissora"] or "").split("(")[0].strip()
        query = (f"debate {d['cargo']} {d['ano']} {turno} {broadcaster} "
                 f"completo").replace("  ", " ")
        seen, cands = set(), []
        for r in _search(query, per_query):
            vid = r.get("id")
            if vid in seen or (r.get("duration") or 0) < 60 * min_minutes:
                continue
            seen.add(vid)
            cands.append({"url": f"https://www.youtube.com/watch?v={vid}",
                          "title": r.get("title"), "channel": r.get("channel"),
                          "minutes": round((r.get("duration") or 0) / 60),
                          "score": _score(d, r)})
        cands.sort(key=lambda c: -c["score"])
        out.append({"debate_id": d["debate_id"], "query": query,
                    "candidatos": cands[:keep]})
    return out
