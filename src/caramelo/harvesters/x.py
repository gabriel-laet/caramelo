"""Harvest deputies' X (Twitter) timelines — budget-aware, pay-per-use.

X bills per post READ (US$0.005 each, 2026 pay-per-use model), so the budget
unit here is post-reads, not requests. Every call is logged to a JSONL ledger
with its read count; a run stops cleanly when its read budget is spent.

Targets come from the declared handles in redes_sociais.parquet. Handle ->
user-id resolution is batched (100 per call) and cached in x_users.parquet.
Incremental runs use since_id per author (a request returning nothing new
bills ~nothing), and a rotating cursor spreads a small daily budget fairly
across all authors. Backfill mode pulls the most recent page (up to 100
posts) per author, subject to the same budget.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API = "https://api.twitter.com/2"

USERS_SCHEMA = pa.schema([
    ("user_id", pa.string()),
    ("handle", pa.string()),
    ("casa", pa.string()),  # camara | senado
    ("parlamentar_id", pa.int64()),
    ("nome", pa.string()),
    ("followers", pa.int64()),
    ("tweets_total", pa.int64()),
])

POSTS_SCHEMA = pa.schema([
    ("post_id", pa.string()),
    ("user_id", pa.string()),
    ("handle", pa.string()),
    ("casa", pa.string()),  # camara | senado
    ("parlamentar_id", pa.int64()),
    ("created_at", pa.string()),
    ("text", pa.string()),
    ("likes", pa.int64()),
    ("retweets", pa.int64()),
    ("replies", pa.int64()),
    ("quotes", pa.int64()),
    ("ref_type", pa.string()),  # retweeted | quoted | replied_to | null
    ("fetched_at", pa.string()),
])


def _bearer() -> str:
    return os.environ["CARAMELO_X_BEARER"]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ReadLedger:
    def __init__(self, data_dir: Path, budget_reads: int):
        self.path = data_dir / "x_ledger.jsonl"
        self.budget = budget_reads
        self.spent = 0

    def exhausted(self) -> bool:
        return self.spent >= self.budget

    def log(self, endpoint: str, reads: int, note: str = "") -> None:
        self.spent += reads
        with open(self.path, "a") as fh:
            fh.write(json.dumps({
                "at": _now(), "endpoint": endpoint, "reads": reads,
                "note": note,
            }) + "\n")


def resolve_users(data_dir: Path, ledger: ReadLedger) -> list[dict]:
    """Resolve declared twitter handles to user ids (cached)."""
    cache = data_dir / "x_users.parquet"
    if cache.exists():
        return pq.read_table(cache).to_pylist()

    declared = [
        r for r in pq.read_table(data_dir / "redes_sociais.parquet").to_pylist()
        if r["rede"] == "twitter" and r["handle"]
        and re.fullmatch(r"[A-Za-z0-9_]{1,15}", r["handle"])
    ]
    by_handle = {r["handle"].lower(): r for r in declared}
    handles = sorted(by_handle)
    users: list[dict] = []
    for i in range(0, len(handles), 100):
        batch = handles[i:i + 100]
        resp = get(f"{API}/users/by", params={
            "usernames": ",".join(batch),
            "user.fields": "public_metrics",
        }, headers={"Authorization": f"Bearer {_bearer()}"}).json()
        ledger.log("users/by", 0, f"{len(batch)} handles")
        for u in resp.get("data", []):
            src = by_handle.get(u["username"].lower())
            if not src:
                continue
            metrics = u.get("public_metrics", {})
            users.append({
                "user_id": u["id"], "handle": u["username"].lower(),
                "casa": "camara",
                "parlamentar_id": src["parlamentar_id"], "nome": src["nome"],
                "followers": metrics.get("followers_count"),
                "tweets_total": metrics.get("tweet_count"),
            })
        errors = resp.get("errors", [])
        if errors:
            print(f"x: {len(errors)} handle(s) not found in batch {i // 100}")
        time.sleep(0.5)

    pq.write_table(pa.Table.from_pylist(users, schema=USERS_SCHEMA), cache,
                   compression="zstd")
    print(f"x: resolved {len(users)}/{len(handles)} handles -> {cache}")
    return users


def _existing_posts(data_dir: Path):
    path = data_dir / "posts.parquet"
    if not path.exists():
        return None, {}, set()
    table = pq.read_table(path)
    if "casa" not in table.column_names:  # upgrade pre-senate files
        table = table.add_column(
            3, "casa", pa.array(["camara"] * table.num_rows))
    since: dict[str, str] = {}
    ids: set[str] = set()
    for r in table.select(["user_id", "post_id"]).to_pylist():
        ids.add(r["post_id"])
        cur = since.get(r["user_id"])
        if cur is None or int(r["post_id"]) > int(cur):
            since[r["user_id"]] = r["post_id"]
    return table, since, ids


def harvest(data_dir: Path, reads_budget: int = 350,
            backfill: bool = False) -> Path:
    ledger = ReadLedger(data_dir, reads_budget)
    users = resolve_users(data_dir, ledger)
    existing, since_by_user, seen_ids = _existing_posts(data_dir)

    state_path = data_dir / "x_cursor.json"
    start = 0
    if state_path.exists() and not backfill:
        start = json.loads(state_path.read_text()).get("index", 0) % len(users)
    order = users[start:] + users[:start]

    out_path = data_dir / "posts.parquet"

    def flush() -> None:
        nonlocal existing, rows
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=POSTS_SCHEMA)
        if existing is not None:
            table = pa.concat_tables([existing, table])
        pq.write_table(table, out_path, compression="zstd")
        existing, rows = table, []

    rows: list[dict] = []
    fetched_at = _now()
    covered = 0
    for offset, user in enumerate(order):
        if ledger.exhausted():
            break
        if backfill and user["user_id"] in since_by_user:
            continue  # resume: this author's backfill page already stored
        params: dict = {
            "max_results": 100,
            "tweet.fields": "created_at,public_metrics,referenced_tweets",
        }
        since = since_by_user.get(user["user_id"])
        if since and not backfill:
            params["since_id"] = since
        try:
            resp = get(f"{API}/users/{user['user_id']}/tweets", params=params,
                       headers={"Authorization": f"Bearer {_bearer()}"}).json()
        except RuntimeError as exc:
            cause = str(exc.__cause__ or exc)
            if "402" in cause:
                # X pay-per-use balance exhausted: stop cleanly, keep data
                print("x: 402 Payment Required — X credit exhausted, "
                      "stopping run and keeping partial data")
                break
            if any(code in cause for code in ("401", "403", "404")):
                # protected/suspended/deleted account: skip this author
                print(f"x: skipping @{user['handle']} ({cause[:60]})")
                covered += 1
                continue
            raise
        posts = resp.get("data", [])
        ledger.log("users/:id/tweets", len(posts), user["handle"])
        covered += 1
        for p in posts:
            if p["id"] in seen_ids:
                continue
            metrics = p.get("public_metrics", {})
            refs = p.get("referenced_tweets") or []
            rows.append({
                "post_id": p["id"], "user_id": user["user_id"],
                "handle": user["handle"],
                "casa": user.get("casa") or "camara",
                "parlamentar_id": user["parlamentar_id"],
                "created_at": p.get("created_at"), "text": p.get("text"),
                "likes": metrics.get("like_count"),
                "retweets": metrics.get("retweet_count"),
                "replies": metrics.get("reply_count"),
                "quotes": metrics.get("quote_count"),
                "ref_type": refs[0]["type"] if refs else None,
                "fetched_at": fetched_at,
            })
        if covered % 25 == 0:
            flush()  # paid reads must never be lost to a crash
        time.sleep(0.3)

    if not backfill:
        state_path.write_text(json.dumps(
            {"index": (start + covered) % len(users)}))

    flush()
    total = pq.read_metadata(out_path).num_rows if out_path.exists() else 0
    est = ledger.spent * 0.005
    print(f"x: {covered}/{len(users)} authors visited "
          f"({ledger.spent} reads ≈ US$ {est:.2f}) -> {out_path} "
          f"({total} posts total)")
    return out_path
