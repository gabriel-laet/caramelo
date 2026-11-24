"""Interview shows (Roda Viva, ...) -> `entrevistas`.

The shows' channels are harvested by the YouTube harvester (seeds/
programas.toml adds them as casa="programa"). This module derives one row
per full episode from `youtube_videos`: guest and air date parsed from the
title with the show's regex, and the guest matched by name to a deputy or
senator when unambiguous. Media and transcripts follow the normal path:
`caramelo archive entrevistas` -> `caramelo transcribe --whisper`.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.harvesters.youtube import SEEDS

SCHEMA = pa.schema([
    ("programa", pa.string()),
    ("video_id", pa.string()),
    ("convidado", pa.string()),
    ("data", pa.string()),          # air date when the title has one
    ("ano", pa.int16()),
    ("duration_s", pa.int32()),
    ("published_at", pa.string()),  # upload date (re-uploads of old shows!)
    ("titulo", pa.string()),
    ("casa", pa.string()),          # matched parliamentarian, if any
    ("parlamentar_id", pa.int64()),
])


def load_programas(seeds: Path = SEEDS) -> list[dict]:
    path = seeds / "programas.toml"
    if not path.exists():
        return []
    return tomllib.loads(path.read_text()).get("programa", [])


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z ]", " ", s.lower()).split())


def parse_date(raw: str | None, uploaded: str | None
               ) -> tuple[str | None, int | None]:
    """Title date -> (iso date | None, year | None). Day/month order varies
    between uploads (11/16/2026 vs 16/11/2026); when both readings are
    valid, take the one closest to — and not after — the upload date."""
    if not raw:
        return None, None
    if re.fullmatch(r"\d{4}", raw):
        return None, int(raw)
    a, b, y = (int(x) for x in re.split(r"[/.-]", raw))
    if y < 100:
        y += 1900 if y >= 80 else 2000
    cands = []
    for m, d in ((a, b), (b, a)):
        try:
            cands.append(date(y, m, d))
        except ValueError:
            pass
    if not cands:
        return None, y
    if uploaded and len(cands) == 2:
        up = date.fromisoformat(uploaded[:10])
        cands.sort(key=lambda c: (c > up, abs((up - c).days)))
    return cands[0].isoformat(), y


def _people(data_dir: Path) -> dict[str, list[tuple[str, int]]]:
    """normalized name -> [(casa, id)] for deputies and senators."""
    idx: dict[str, set] = {}
    dep = data_dir / "deputados.parquet"
    if dep.exists():
        for r in pq.read_table(dep, columns=["id", "nome"]).to_pylist():
            idx.setdefault(norm(r["nome"]), set()).add(("camara", r["id"]))
    sen = data_dir / "senadores.parquet"
    if sen.exists():
        for r in pq.read_table(sen, columns=["codigo", "nome_parlamentar",
                                              "nome_completo"]).to_pylist():
            for n in (r["nome_parlamentar"], r["nome_completo"]):
                if n:
                    idx.setdefault(norm(n), set()).add(("senado", r["codigo"]))
    return {k: sorted(v) for k, v in idx.items()}


def build(data_dir: Path, seeds: Path = SEEDS) -> Path:
    programas = load_programas(seeds)
    canais = {r["channel_id"]: r["nome"] for r in pq.read_table(
        data_dir / "youtube_canais.parquet").to_pylist()
        if r["casa"] == "programa"}
    people = _people(data_dir)
    by_slug = {p["slug"]: p for p in programas}
    rx = {p["slug"]: [re.compile(t, re.I)
                      for t in p.get("titulos") or [p["titulo"]]]
          for p in programas}

    rows, seen_titles, matched, dupes = [], 0, 0, 0
    seen_keys: set[tuple] = set()
    for v in pq.read_table(data_dir / "youtube_videos.parquet").to_pylist():
        slug = canais.get(v["channel_id"])
        if slug not in by_slug:
            continue
        seen_titles += 1
        title = v["title"] or ""
        if title in ("Deleted video", "Private video"):
            continue
        m = next((m for r in rx[slug] if (m := r.match(title))), None)
        if not m:
            continue
        p = by_slug[slug]
        if v["duration_s"] is not None and v["duration_s"] < 60 * p.get(
                "min_minutos", 0):
            continue
        guest = " ".join(m.group("convidado").split())
        data, ano = parse_date(m.groupdict().get("data"), v["published_at"])
        hits = people.get(norm(guest), [])
        casa, pid = hits[0] if len(hits) == 1 else (None, None)
        matched += pid is not None
        # "Retrô" re-runs and Libras versions repeat an interview already
        # on the channel: one row per (guest, date or year)
        key = (slug, norm(guest), data or ano)
        if key in seen_keys:
            dupes += 1
            continue
        seen_keys.add(key)
        rows.append({"programa": slug, "video_id": v["video_id"],
                     "convidado": guest.title() if guest.isupper() else guest,
                     "data": data, "ano": ano or (int(data[:4]) if data else None),
                     "duration_s": v["duration_s"],
                     "published_at": v["published_at"], "titulo": v["title"],
                     "casa": casa, "parlamentar_id": pid})
    # a year-only row ("Retrô | Guest | 1995") is a re-run of a dated
    # original from that year when one exists
    dated = {(r["programa"], norm(r["convidado"]), r["ano"])
             for r in rows if r["data"]}
    before = len(rows)
    rows = [r for r in rows if r["data"] or (
        r["programa"], norm(r["convidado"]), r["ano"]) not in dated]
    dupes += before - len(rows)
    rows.sort(key=lambda r: (r["programa"], r["data"] or str(r["ano"] or "")))
    out = data_dir / "entrevistas.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out,
                   compression="zstd")
    print(f"entrevistas: {len(rows)} episodes from {seen_titles} show videos "
          f"({dupes} re-runs dropped, {matched} guests matched to "
          f"parliamentarians) -> {out}")
    return out


def archive_order(rows: list[dict]) -> list[str]:
    """Download order for an interview archive: one episode per year in
    turn (oldest year first), so a partial run already spans every decade.
    Within a year, interviews with a sitting parliamentarian come first,
    then the rest by date."""
    by_year: dict[int, list[dict]] = {}
    for r in rows:
        by_year.setdefault(r["ano"] or 0, []).append(r)
    for eps in by_year.values():
        eps.sort(key=lambda r: (r["parlamentar_id"] is None, r["data"] or ""))
    order, rnd = [], 0
    while True:
        batch = [eps[rnd]["video_id"] for _, eps in sorted(by_year.items())
                 if rnd < len(eps)]
        if not batch:
            return order
        order += batch
        rnd += 1
