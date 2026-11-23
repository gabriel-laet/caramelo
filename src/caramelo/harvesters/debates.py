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
