"""Politicians' Instagram posts (metadata) + Reels audio for transcription.

Discovery uses Meta's Instagram Graph API *Business Discovery*: a
professional (business/creator) account you own can read public metadata of
other professional accounts — which most politicians' accounts are. Needs:

    CARAMELO_IG_TOKEN     long-lived token of a Meta app with
                          instagram_basic + pages_read_engagement
    CARAMELO_IG_USER_ID   the IG business account id that token belongs to

One call per account per page of up to 50 posts; Graph API rate limits are
per-app (~200 calls/hour/user), so runs are capped with --budget.

Personal (non-professional) accounts are invisible to Business Discovery —
those rows end up in `instagram_contas` with status=not_found.

Tables (public-safe, metadata only):
    instagram_contas   parlamentar -> username, followers, media_count
    instagram_posts    one row per post (append-only, deduped by id)

`archive` downloads audio of video posts (Reels) with yt-dlp into the NAS
archive. Instagram has no captions, so these always go through Whisper.
yt-dlp generally needs logged-in cookies for Instagram:
CARAMELO_YTDLP_COOKIES=<cookies.txt exported from your browser>.
"""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.harvesters.youtube import SEEDS
from caramelo.http import get

GRAPH = "https://graph.facebook.com/v21.0"
MEDIA_FIELDS = ("id,caption,media_type,media_product_type,timestamp,"
                "permalink,like_count,comments_count")

CONTAS_SCHEMA = pa.schema([
    ("casa", pa.string()),
    ("parlamentar_id", pa.int64()),
    ("nome", pa.string()),
    ("username", pa.string()),
    ("ig_id", pa.string()),
    ("followers", pa.int64()),
    ("media_count", pa.int64()),
    ("status", pa.string()),        # ok | not_found
    ("source", pa.string()),        # declared | override
    ("checked_at", pa.string()),
])

POSTS_SCHEMA = pa.schema([
    ("post_id", pa.string()),
    ("username", pa.string()),
    ("casa", pa.string()),
    ("parlamentar_id", pa.int64()),
    ("published_at", pa.string()),
    ("media_type", pa.string()),         # IMAGE | VIDEO | CAROUSEL_ALBUM
    ("product_type", pa.string()),       # FEED | REELS | STORY ...
    ("caption", pa.string()),
    ("permalink", pa.string()),
    ("likes", pa.int64()),               # snapshot at first_seen
    ("comments", pa.int64()),
    ("first_seen", pa.string()),
])


def _targets(data_dir: Path, seeds: Path) -> list[dict]:
    targets: dict[tuple, dict] = {}
    path = data_dir / "redes_sociais.parquet"
    if path.exists():
        for r in pq.read_table(path).to_pylist():
            if r["rede"] == "instagram" and r["handle"]:
                targets.setdefault((r["casa"], r["parlamentar_id"]), {
                    "casa": r["casa"], "parlamentar_id": r["parlamentar_id"],
                    "nome": r["nome"], "username": r["handle"],
                    "source": "declared"})
    ov = seeds / "instagram_contas.toml"
    if ov.exists():
        for c in tomllib.loads(ov.read_text()).get("conta", []):
            targets[(c["casa"], c["parlamentar_id"])] = {
                "casa": c["casa"], "parlamentar_id": c["parlamentar_id"],
                "nome": c.get("nome"), "username": c["username"].lstrip("@"),
                "source": "override"}
    return list(targets.values())


def business_discovery(username: str, after: str | None = None) -> dict | None:
    media = f"media.limit(50){{{MEDIA_FIELDS}}}"
    if after:
        media = f"media.after({after}).limit(50){{{MEDIA_FIELDS}}}"
    fields = (f"business_discovery.username({username})"
              f"{{id,username,followers_count,media_count,{media}}}")
    try:
        resp = get(f"{GRAPH}/{os.environ['CARAMELO_IG_USER_ID']}", params={
            "fields": fields, "access_token": os.environ["CARAMELO_IG_TOKEN"],
        }, retries=1)
    except RuntimeError:
        return None  # non-professional / renamed / private: Graph 400s
    return resp.json().get("business_discovery")


def harvest(data_dir: Path, *, budget: int = 150, pages: int = 1,
            seeds: Path = SEEDS) -> None:
    """pages=1 is the daily incremental (latest 50 posts per account);
    raise it for a one-off backfill."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    posts_path = data_dir / "instagram_posts.parquet"
    posts = {r["post_id"]: r for r in (pq.read_table(posts_path).to_pylist()
                                       if posts_path.exists() else [])}
    contas, calls, before = [], 0, len(posts)
    for t in _targets(data_dir, seeds):
        if calls >= budget:
            break
        after = None
        bd = None
        for page in range(pages):
            if calls >= budget:
                break
            calls += 1
            res = business_discovery(t["username"], after)
            if res is None:
                break
            bd = bd or res
            media = res.get("media") or {}
            for m in media.get("data") or []:
                posts.setdefault(m["id"], {
                    "post_id": m["id"], "username": t["username"],
                    "casa": t["casa"], "parlamentar_id": t["parlamentar_id"],
                    "published_at": m.get("timestamp"),
                    "media_type": m.get("media_type"),
                    "product_type": m.get("media_product_type"),
                    "caption": m.get("caption"),
                    "permalink": m.get("permalink"),
                    "likes": m.get("like_count"),
                    "comments": m.get("comments_count"), "first_seen": now})
            after = (media.get("paging") or {}).get("cursors", {}).get("after")
            if not after:
                break
        contas.append({**{k: t[k] for k in ("casa", "parlamentar_id", "nome",
                                            "username", "source")},
                       "ig_id": (bd or {}).get("id"),
                       "followers": (bd or {}).get("followers_count"),
                       "media_count": (bd or {}).get("media_count"),
                       "status": "ok" if bd else "not_found",
                       "checked_at": now})

    pq.write_table(pa.Table.from_pylist(contas, schema=CONTAS_SCHEMA),
                   data_dir / "instagram_contas.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(list(posts.values()),
                                        schema=POSTS_SCHEMA),
                   posts_path, compression="zstd")
    ok = sum(1 for c in contas if c["status"] == "ok")
    print(f"instagram: {ok}/{len(contas)} accounts visible, "
          f"+{len(posts) - before} posts ({len(posts)} total), {calls} calls")


def archive(data_dir: Path, archive_root: Path, *, limit: int = 100,
            since: str | None = None) -> None:
    """Audio of video posts (Reels) -> NAS, newest first."""
    from caramelo import archive as arc

    posts = [p for p in pq.read_table(
        data_dir / "instagram_posts.parquet").to_pylist()
        if p["media_type"] == "VIDEO" and p["permalink"]]
    posts.sort(key=lambda p: p["published_at"] or "", reverse=True)
    done = failed = 0
    for p in posts:
        if done + failed >= limit:
            break
        if since and (p["published_at"] or "") < since:
            continue
        shortcode = p["permalink"].rstrip("/").rsplit("/", 1)[-1]
        dest = arc.platform_dir(archive_root, "instagram", p["username"],
                                shortcode)
        if any(dest.glob("audio.*")):
            continue
        res = arc.fetch_media(p["permalink"], dest, audio=True)
        if res["ok"]:
            done += 1
        else:
            failed += 1
            print(f"instagram-archive: {shortcode}: {res['error']}")
    print(f"instagram-archive: {done} fetched, {failed} failed -> "
          f"{archive_root / 'instagram'}")
