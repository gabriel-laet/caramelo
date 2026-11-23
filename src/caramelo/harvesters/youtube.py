"""Politicians' own YouTube channels: channels -> videos -> stats.

Three public-safe tables (metadata only; media and transcripts go to the
local archive, see caramelo.archive):

    youtube_canais   parlamentar -> channel id (declared | override)
    youtube_videos   one row per upload (append-only, deduped by video_id)
    youtube_stats    view/like/comment snapshots (append-only time series)

Quota (YouTube Data API v3, free 10k units/day, key in
$CARAMELO_YOUTUBE_KEY):
    channels.list        1 unit / call        (handle -> channel id)
    playlistItems.list   1 unit / 50 videos   (uploads backfill)
    videos.list          1 unit / 50 videos   (details + stats)
Daily discovery uses the channel Atom feed (last ~15 uploads) which costs
nothing; the uploads playlist is only walked on first sight of a channel
or with --backfill. search.list (100 units) is never used.

Sources of channels:
    redes_sociais.parquet   deputies' declared accounts (Câmara redeSocial)
    seeds/youtube_canais.toml  hand-curated overrides — senators (the Senado
                            API publishes no socials) and fixes
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API = "https://www.googleapis.com/youtube/v3"
FEED = "https://www.youtube.com/feeds/videos.xml"
_REPO_SEEDS = Path(__file__).resolve().parents[3] / "seeds"
# repo checkout (editable install) -> ./seeds (container WORKDIR) -> override
SEEDS = Path(os.environ.get("CARAMELO_SEEDS_DIR")
             or (_REPO_SEEDS if _REPO_SEEDS.exists() else "seeds"))
STATS_WINDOW_DAYS = 60  # daily stats for recent uploads; older via --stats-all

CANAIS_SCHEMA = pa.schema([
    ("casa", pa.string()),
    ("parlamentar_id", pa.int64()),
    ("nome", pa.string()),
    ("channel_id", pa.string()),
    ("handle", pa.string()),
    ("title", pa.string()),
    ("uploads_playlist", pa.string()),
    ("subscribers", pa.int64()),
    ("video_count", pa.int64()),
    ("source", pa.string()),        # declared | override
    ("resolved_at", pa.string()),
])

VIDEOS_SCHEMA = pa.schema([
    ("video_id", pa.string()),
    ("channel_id", pa.string()),
    ("casa", pa.string()),
    ("parlamentar_id", pa.int64()),
    ("published_at", pa.string()),
    ("title", pa.string()),
    ("description", pa.string()),
    ("duration_s", pa.int32()),
    ("is_short", pa.bool_()),       # heuristic: duration <= 180s
    ("is_live", pa.bool_()),        # was a livestream
    ("tags", pa.list_(pa.string())),
    ("first_seen", pa.string()),
])

STATS_SCHEMA = pa.schema([
    ("video_id", pa.string()),
    ("fetched_at", pa.string()),
    ("views", pa.int64()),
    ("likes", pa.int64()),
    ("comments", pa.int64()),
])

ATOM = {"a": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/"}


class QuotaExhausted(Exception):
    pass


class YouTubeClient:
    def __init__(self, units: int, key: str | None = None):
        self.key = key or os.environ["CARAMELO_YOUTUBE_KEY"]
        self.units = units
        self.spent = 0

    def call(self, resource: str, **params) -> dict:
        if self.spent >= self.units:
            raise QuotaExhausted(f"{self.units} units spent this run")
        self.spent += 1  # every list call we use costs exactly 1 unit
        try:
            return get(f"{API}/{resource}",
                       params={"key": self.key, **params}).json()
        except RuntimeError as exc:
            # never let the key reach logs: httpx errors embed the full URL
            raise RuntimeError(f"YouTube API {resource}: "
                               f"{_api_error(exc)}") from None


def _api_error(exc: Exception) -> str:
    """Google's own error reason (keyInvalid, quotaExceeded, ...) from the
    response, without the request URL."""
    resp = getattr(exc.__cause__, "response", None)
    if resp is None:
        return type(exc.__cause__ or exc).__name__
    try:
        err = resp.json().get("error", {})
        reasons = ",".join(e.get("reason", "") for e in err.get("errors", []))
        return f"HTTP {resp.status_code} {reasons}: {err.get('message', '')}"
    except ValueError:
        return f"HTTP {resp.status_code}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_duration(iso: str | None) -> int | None:
    """ISO-8601 duration (PT1H2M3S, P1DT2H) -> seconds."""
    if not iso:
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _int(v) -> int | None:
    return int(v) if v not in (None, "") else None


# --- channels ---------------------------------------------------------------

def _declared_targets(data_dir: Path) -> list[dict]:
    path = data_dir / "redes_sociais.parquet"
    if not path.exists():
        return []
    out = []
    for r in pq.read_table(path).to_pylist():
        if r["rede"] == "youtube":
            out.append({"casa": r["casa"], "parlamentar_id": r["parlamentar_id"],
                        "nome": r["nome"], "ref": r["handle"], "url": r["url"],
                        "source": "declared"})
    return out


def _override_targets(seeds: Path) -> list[dict]:
    path = seeds / "youtube_canais.toml"
    if not path.exists():
        return []
    doc = tomllib.loads(path.read_text())
    return [{"casa": c["casa"], "parlamentar_id": c["parlamentar_id"],
             "nome": c.get("nome"), "ref": c["canal"], "url": c["canal"],
             "source": "override"} for c in doc.get("canal", [])]


def classify_ref(ref: str | None, url: str | None) -> tuple[str, str] | None:
    """-> (kind, value) where kind is id | handle | username | page."""
    for cand in (ref, url):
        if not cand:
            continue
        if re.fullmatch(r"UC[\w-]{22}", cand):
            return "id", cand
        m = re.search(r"/channel/(UC[\w-]{22})", cand)
        if m:
            return "id", m.group(1)
        m = re.search(r"youtube\.com/@([\w.\-]+)", cand)
        if m or cand.startswith("@"):
            return "handle", (m.group(1) if m else cand[1:])
        m = re.search(r"youtube\.com/user/([\w.\-]+)", cand)
        if m:
            return "username", m.group(1)
    # /c/<custom> and other legacy urls: only the page itself knows the id
    if url and "youtube.com" in url:
        return "page", url if "://" in url else f"https://{url}"
    if ref:
        return "handle", ref
    return None


_JUNK_SLUGS = {"featured", "videos", "watch", "youtube.com", "www.youtube.com",
               "about", "playlists", "shorts", "live"}


def _channel_id_from_page(url: str) -> str | None:
    try:
        html = get(url, retries=1).text
    except RuntimeError:
        return None
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/'
                   r'channel/(UC[\w-]{22})"', html)
         or re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', html))
    return m.group(1) if m else None


def resolve_channels(data_dir: Path, client: YouTubeClient,
                     seeds: Path = SEEDS) -> Path:
    """Targets -> youtube_canais. Already-resolved channels are carried over
    and only refreshed (stats) in a batch; new refs cost 1 unit each."""
    out_path = data_dir / "youtube_canais.parquet"
    old = {(r["casa"], r["parlamentar_id"], r["channel_id"]): r
           for r in (pq.read_table(out_path).to_pylist()
                     if out_path.exists() else [])}
    known_refs = {(r["casa"], r["parlamentar_id"]) for r in old.values()}

    # overrides win over declared for the same person
    targets: dict[tuple, dict] = {}
    for t in _declared_targets(data_dir) + _override_targets(seeds):
        key = (t["casa"], t["parlamentar_id"])
        if t["source"] == "override" or key not in targets:
            targets[key] = t

    resolved: dict[tuple, dict] = {}
    unresolved = 0
    for key, t in targets.items():
        if key in known_refs and t["source"] == "declared":
            continue  # resolved in a previous run; refreshed below
        kind_val = classify_ref(t["ref"], t["url"])
        if not kind_val:
            unresolved += 1
            continue
        kind, val = kind_val
        if kind == "page":
            cid = _channel_id_from_page(val)
            slug = val.rstrip("/").rsplit("/", 1)[-1]
            if not cid and slug not in _JUNK_SLUGS:
                # bare youtube.com/<name> is often a handle nowadays
                cid = _channel_id_from_page(f"https://www.youtube.com/@{slug}")
            if not cid:
                unresolved += 1
                continue
            kind, val = "id", cid
        param = {"id": "id", "handle": "forHandle",
                 "username": "forUsername"}[kind]
        try:
            resp = client.call("channels",
                               part="snippet,contentDetails,statistics",
                               **{param: val})
        except QuotaExhausted:
            break
        items = resp.get("items") or []
        if not items:
            unresolved += 1
            continue
        ch = items[0]
        resolved[(t["casa"], t["parlamentar_id"], ch["id"])] = _canal_row(
            t, ch)

    # refresh carried-over channels in batches of 50 ids (1 unit each)
    redone = {(k[0], k[1]) for k in resolved}  # e.g. an override replaced it
    carry = {k: v for k, v in old.items() if (k[0], k[1]) not in redone}
    ids = sorted({k[2] for k in carry})
    fresh: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        try:
            resp = client.call("channels", part="snippet,contentDetails,"
                               "statistics", id=",".join(ids[i:i + 50]),
                               maxResults=50)
        except QuotaExhausted:
            break
        for ch in resp.get("items") or []:
            fresh[ch["id"]] = ch
    for k, r in carry.items():
        ch = fresh.get(k[2])
        resolved[k] = _canal_row(r, ch) if ch else r

    rows = list(resolved.values())
    pq.write_table(pa.Table.from_pylist(rows, schema=CANAIS_SCHEMA), out_path,
                   compression="zstd")
    print(f"youtube: {len(rows)} channels ({unresolved} refs unresolved), "
          f"{client.spent} units -> {out_path}")
    return out_path


def _canal_row(t: dict, ch: dict) -> dict:
    sn, st = ch.get("snippet", {}), ch.get("statistics", {})
    return {
        "casa": t["casa"], "parlamentar_id": t["parlamentar_id"],
        "nome": t["nome"], "channel_id": ch["id"],
        "handle": (sn.get("customUrl") or "").lstrip("@") or None,
        "title": sn.get("title"),
        "uploads_playlist": ch.get("contentDetails", {})
                              .get("relatedPlaylists", {}).get("uploads"),
        "subscribers": _int(st.get("subscriberCount")),
        "video_count": _int(st.get("videoCount")),
        "source": t["source"], "resolved_at": _now(),
    }


# --- videos -----------------------------------------------------------------

def feed_items(channel_id: str) -> list[dict]:
    """Latest ~15 uploads from the free Atom feed (no key, no quota)."""
    xml = get(FEED, params={"channel_id": channel_id}, retries=2).text
    return parse_feed(xml)


def parse_feed(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", ATOM):
        out.append({
            "video_id": e.findtext("yt:videoId", namespaces=ATOM),
            "published_at": e.findtext("a:published", namespaces=ATOM),
            "title": e.findtext("a:title", namespaces=ATOM),
            "description": e.findtext("media:group/media:description",
                                      namespaces=ATOM),
        })
    return out


def playlist_page(client: YouTubeClient, playlist_id: str,
                  token: str | None) -> tuple[list[dict], str | None]:
    """One page (50) of a channel's uploads -> (items, next token). snippet
    rides along for free, so a video row exists even before details."""
    params = {"part": "snippet,contentDetails", "playlistId": playlist_id,
              "maxResults": 50}
    if token:
        params["pageToken"] = token
    resp = client.call("playlistItems", **params)
    items = []
    for it in resp.get("items") or []:
        sn, cd = it.get("snippet", {}), it.get("contentDetails", {})
        items.append({"video_id": cd.get("videoId"),
                      "published_at": cd.get("videoPublishedAt")
                                      or sn.get("publishedAt"),
                      "title": sn.get("title"),
                      "description": sn.get("description")})
    return items, resp.get("nextPageToken")


def video_details(client: YouTubeClient, ids: list[str]):
    """Yields video resources; stops cleanly (keeping what it yielded) when
    the quota runs out."""
    for i in range(0, len(ids), 50):
        resp = client.call("videos", part="snippet,contentDetails,statistics,"
                           "liveStreamingDetails", id=",".join(ids[i:i + 50]),
                           maxResults=50)
        yield from resp.get("items") or []


DETAILS_SHARE = 0.3  # of the run's units kept back for details + stats


def harvest(data_dir: Path, *, units: int | None = None,
            backfill: bool = False, stats_all: bool = False,
            seeds: Path = SEEDS) -> None:
    client = YouTubeClient(units or int(os.environ.get(
        "CARAMELO_YOUTUBE_UNITS", "8000")))
    resolve_channels(data_dir, client, seeds)
    canais = pq.read_table(data_dir / "youtube_canais.parquet").to_pylist()

    videos_path = data_dir / "youtube_videos.parquet"
    videos = {r["video_id"]: r for r in (pq.read_table(videos_path).to_pylist()
                                         if videos_path.exists() else [])}
    # per-channel backfill cursor: resumes a long uploads walk across runs
    cursor_path = data_dir / "youtube_backfill.json"
    cursor = (json.loads(cursor_path.read_text())
              if cursor_path.exists() else {})
    if backfill:
        cursor = {}
    now = _now()

    def add(c: dict, item: dict) -> bool:
        vid = item["video_id"]
        if not vid or vid in videos:
            return False
        videos[vid] = {
            "video_id": vid, "channel_id": c["channel_id"], "casa": c["casa"],
            "parlamentar_id": c["parlamentar_id"],
            "published_at": item["published_at"], "title": item["title"],
            "description": item["description"], "duration_s": None,
            "is_short": None, "is_live": None, "tags": None,
            "first_seen": now}
        return True

    discovery_cap = client.units - int(client.units * DETAILS_SHARE)
    added = feed_errors = 0
    pending_backfill = 0
    for c in canais:
        cid, cur = c["channel_id"], cursor.get(c["channel_id"], {})
        if not cur.get("done") and c["uploads_playlist"]:
            token, done = cur.get("token"), False
            try:
                while client.spent < discovery_cap:
                    items, token = playlist_page(client,
                                                 c["uploads_playlist"], token)
                    added += sum(add(c, it) for it in items)
                    if not token:
                        done = True
                        break
            except QuotaExhausted:
                pass
            cursor[cid] = {"token": token, "done": done}
            if not done:
                pending_backfill += 1
                continue  # out of discovery budget: resume here next run
        # daily path (and channels already backfilled): free RSS
        try:
            added += sum(add(c, it) for it in feed_items(cid))
        except (RuntimeError, ET.ParseError):
            feed_errors += 1
    cursor_path.write_text(json.dumps(cursor))

    # details: videos never detailed (duration unknown), then stats for the
    # recent window (or everything with --stats-all)
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - STATS_WINDOW_DAYS * 86400))
    undetailed = [v for v, r in videos.items() if r["duration_s"] is None]
    recent = [v for v, r in videos.items() if r["duration_s"] is not None
              and (stats_all or (r["published_at"] or "") >= cutoff)]
    stats_rows: list[dict] = []
    try:
        for it in video_details(client, sorted(undetailed) + sorted(recent)):
            vid, sn, st = it["id"], it.get("snippet", {}), it.get("statistics", {})
            r = videos.get(vid)
            if r is not None and r["duration_s"] is None:
                dur = parse_duration(it.get("contentDetails", {})
                                     .get("duration"))
                r.update(duration_s=dur,
                         is_short=dur is not None and dur <= 180,
                         is_live="liveStreamingDetails" in it,
                         tags=sn.get("tags"),
                         published_at=sn.get("publishedAt") or r["published_at"],
                         description=sn.get("description") or r["description"])
            stats_rows.append({"video_id": vid, "fetched_at": now,
                               "views": _int(st.get("viewCount")),
                               "likes": _int(st.get("likeCount")),
                               "comments": _int(st.get("commentCount"))})
    except QuotaExhausted:
        pass
    still = sum(1 for r in videos.values() if r["duration_s"] is None)

    pq.write_table(pa.Table.from_pylist(list(videos.values()),
                                        schema=VIDEOS_SCHEMA),
                   videos_path, compression="zstd")
    stats_path = data_dir / "youtube_stats.parquet"
    table = pa.Table.from_pylist(stats_rows, schema=STATS_SCHEMA)
    if stats_path.exists():
        table = pa.concat_tables([pq.read_table(stats_path), table])
    pq.write_table(table, stats_path, compression="zstd")
    print(f"youtube: +{added} videos ({len(videos)} total, {still} awaiting "
          f"details), {len(stats_rows)} stat snapshots, {pending_backfill} "
          f"channels mid-backfill, {feed_errors} feed errors, "
          f"{client.spent}/{client.units} units")


# --- archive ----------------------------------------------------------------

def archive(data_dir: Path, archive_root: Path, *, limit: int = 200,
            audio: bool = False, since: str | None = None) -> None:
    """Pull captions (+ audio with --audio) for videos not yet archived,
    newest first. Runs on the local network (YouTube bot-checks datacenter
    IPs), writing into the NAS archive."""
    from caramelo import archive as arc

    videos = pq.read_table(data_dir / "youtube_videos.parquet").to_pylist()
    videos.sort(key=lambda v: v["published_at"] or "", reverse=True)
    done = failed = 0
    for v in videos:
        if done + failed >= limit:
            break
        if since and (v["published_at"] or "") < since:
            continue
        dest = arc.youtube_dir(archive_root, v["channel_id"], v["video_id"])
        if (dest / "media.info.json").exists() and (
                not audio or any(dest.glob("audio.*"))):
            continue
        res = arc.fetch_media(
            f"https://www.youtube.com/watch?v={v['video_id']}", dest,
            audio=audio)
        if res["ok"]:
            done += 1
        else:
            failed += 1
            print(f"youtube-archive: {v['video_id']}: {res['error']}")
    print(f"youtube-archive: {done} fetched, {failed} failed -> "
          f"{archive_root / 'youtube'}")
