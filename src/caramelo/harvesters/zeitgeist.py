""""Spirit of the time": front-page headlines and news-attention timelines.

manchetes — front pages of major outlets sampled from the Wayback Machine.
    Raw HTML goes to the NAS archive (web/<outlet>/<yyyymmdd>.html.gz); the
    public table holds only headline text + link + position, re-extracted
    from the archive on every run so a better extractor can be applied to
    all history at once. An archived page is never fetched twice.

gdelt — GDELT DOC 2.0 timelines (volume share + average tone) for a seed
    list of themes in Brazilian sources, 2017+. Aggregates only.

Neither publishes article bodies: headlines and aggregate signals are
the safe, durable slice of copyrighted news.
"""

from __future__ import annotations

import gzip
import re
import time
import tomllib
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.harvesters.youtube import SEEDS
from caramelo.http import get

CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK = "https://web.archive.org/web/{ts}id_/{url}"  # id_ = raw, no toolbar
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
WAYBACK_INTERVAL = 4.0   # seconds between page fetches; IA asks for restraint

MANCHETES_SCHEMA = pa.schema([
    ("veiculo", pa.string()),
    ("data", pa.string()),          # capture day (yyyy-mm-dd)
    ("capturado_em", pa.string()),  # wayback timestamp
    ("posicao", pa.int32()),        # order on the page
    ("destaque", pa.bool_()),       # inside an h1-h3
    ("titulo", pa.string()),
    ("url", pa.string()),
])

GDELT_SCHEMA = pa.schema([
    ("consulta", pa.string()),
    ("medida", pa.string()),        # volume | tom
    ("data", pa.string()),
    ("valor", pa.float64()),
    ("fetched_at", pa.string()),
])


def _seed(seeds: Path) -> dict:
    return tomllib.loads((seeds / "zeitgeist.toml").read_text())


# --- headline extraction ---------------------------------------------------

_NAV = re.compile(r"^(assine|entrar|login|menu|busca|newsletter|veja mais|"
                  r"leia mais|saiba mais|mais not[ií]cias|publicidade)\b", re.I)


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[tuple[str, str, bool]] = []
        self._href: str | None = None
        self._buf: list[str] = []
        self._heading = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer"):
            self._skip += 1
        elif tag in ("h1", "h2", "h3"):
            self._heading += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("h1", "h2", "h3"):
            self._heading = max(0, self._heading - 1)
        elif tag == "a" and self._href is not None:
            if not self._skip:
                self.out.append((" ".join("".join(self._buf).split()),
                                 self._href, self._heading > 0))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)


def decode_html(raw: bytes) -> str:
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + [
            "utf-8", "cp1252"]:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def extract_headlines(html: str, base_url: str, limit: int = 150) -> list[dict]:
    """Generic, layout-agnostic: anchor texts that look like headlines
    (4+ words, 25-220 chars, not navigation). Works across 25 years of
    redesigns at the cost of some noise; per-outlet extractors can replace
    it later and re-run over the whole archive."""
    p = _Anchors()
    try:
        p.feed(html)
    except Exception:  # malformed 1990s HTML: keep whatever parsed
        pass
    seen, out = set(), []
    for text, href, heading in p.out:
        if not (25 <= len(text) <= 220) or len(text.split()) < 4:
            continue
        if _NAV.match(text) or text in seen or not href:
            continue
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        seen.add(text)
        url = urljoin(base_url, href)
        # unwrap wayback-rewritten links back to the original
        url = re.sub(r"^https?://web\.archive\.org/web/\d+(?:id_)?/", "", url)
        out.append({"titulo": text, "url": url, "destaque": heading})
        if len(out) >= limit:
            break
    return out


# --- wayback sampling --------------------------------------------------------

def daily_captures(url: str, year: int) -> dict[str, str]:
    """-> {yyyymmdd: wayback timestamp}, one 200-status capture per day."""
    resp = get(CDX, params={
        "url": url, "from": f"{year}0101", "to": f"{year}1231",
        "output": "json", "fl": "timestamp,statuscode",
        "filter": "statuscode:200", "collapse": "timestamp:8",
    }, timeout=120.0)
    rows = resp.json() if resp.text.strip() else []
    return {ts[:8]: ts for ts, _ in rows[1:]}


def _sample_days(start: date, end: date, step: int) -> list[str]:
    days, d = [], start
    while d <= end:
        days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=step)
    return days


def harvest_manchetes(data_dir: Path, archive_root: Path, *,
                      step_days: int = 7, max_fetches: int = 300,
                      outlets: list[str] | None = None,
                      seeds: Path = SEEDS) -> Path:
    """Sample each outlet's front page every `step_days` (nearest capture
    within the following step window), newest years first, at most
    `max_fetches` new pages per run. Then rebuild `manchetes` from the
    whole archive."""
    fetched = 0
    today = date.today()
    for v in _seed(seeds).get("veiculo", []):
        if outlets and v["slug"] not in outlets:
            continue
        web = archive_root / "web" / v["slug"]
        web.mkdir(parents=True, exist_ok=True)
        have = {p.name[:8] for p in web.glob("*.html.gz")}
        for year in range(today.year, v["desde"] - 1, -1):
            if fetched >= max_fetches:
                break
            caps = None
            for day in _sample_days(date(year, 1, 1),
                                    min(date(year, 12, 31), today), step_days):
                if fetched >= max_fetches:
                    break
                window = _sample_days(
                    date(int(day[:4]), int(day[4:6]), int(day[6:])),
                    date(int(day[:4]), int(day[4:6]), int(day[6:]))
                    + timedelta(days=step_days - 1), 1)
                if any(w in have for w in window):
                    continue
                if caps is None:
                    caps = daily_captures(v["url"], year)
                    time.sleep(WAYBACK_INTERVAL)
                hit = next((w for w in window if w in caps), None)
                if not hit:
                    continue
                try:
                    raw = get(WAYBACK.format(ts=caps[hit], url=v["url"]),
                              retries=2, timeout=90.0).content
                except RuntimeError as exc:
                    print(f"manchetes: {v['slug']} {hit}: {exc}")
                    continue
                (web / f"{hit}.{caps[hit]}.html.gz").write_bytes(
                    gzip.compress(raw))
                have.add(hit)
                fetched += 1
                time.sleep(WAYBACK_INTERVAL)
    print(f"manchetes: {fetched} new front pages archived")
    return build_manchetes(data_dir, archive_root)


def build_manchetes(data_dir: Path, archive_root: Path) -> Path:
    rows: list[dict] = []
    pages = 0
    for page in sorted((archive_root / "web").glob("*/*.html.gz")):
        day, ts = page.name.split(".")[:2]
        slug = page.parent.name
        html = decode_html(gzip.decompress(page.read_bytes()))
        pages += 1
        for i, h in enumerate(extract_headlines(
                html, f"https://{slug}/"), 1):
            rows.append({"veiculo": slug,
                         "data": f"{day[:4]}-{day[4:6]}-{day[6:]}",
                         "capturado_em": ts, "posicao": i, **h})
    out = data_dir / "manchetes.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=MANCHETES_SCHEMA), out,
                   compression="zstd")
    print(f"manchetes: {len(rows)} headlines from {pages} pages -> {out}")
    return out


# --- GDELT -------------------------------------------------------------------

def harvest_gdelt(data_dir: Path, seeds: Path = SEEDS,
                  start: str = "20170101000000") -> Path:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    end = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    rows: list[dict] = []
    for q in _seed(seeds).get("gdelt", {}).get("consultas", []):
        for mode, medida in (("timelinevol", "volume"), ("timelinetone", "tom")):
            try:
                resp = get(GDELT_DOC, params={
                    "query": f'"{q}" sourcecountry:BR' if " " in q
                             else f"{q} sourcecountry:BR",
                    "mode": mode, "format": "json",
                    "startdatetime": start, "enddatetime": end,
                    "timelinesmooth": 0}, retries=2, timeout=120.0)
                series = resp.json().get("timeline") or []
            except (RuntimeError, ValueError) as exc:
                print(f"gdelt: {q} [{mode}]: {exc}")
                continue
            for s in series[:1]:
                for p in s.get("data") or []:
                    d = p["date"]
                    rows.append({"consulta": q, "medida": medida,
                                 "data": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                                 "valor": p.get("value"), "fetched_at": now})
            time.sleep(5)  # GDELT asks for ~1 request / 5s
    out = data_dir / "gdelt_timeline.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=GDELT_SCHEMA), out,
                   compression="zstd")
    print(f"gdelt: {len(rows)} points -> {out}")
    return out
