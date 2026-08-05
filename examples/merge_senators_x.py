"""Merge verified senator X accounts into x_users.parquet (casa='senado').

Reads data/senadores_x.parquet (from discover_senators_x.py), takes the
'verified' rows, resolves their user ids, and appends them to the harvester's
user table so `caramelo harvest x --backfill` picks them up (authors already
stored are skipped, so only senators get pulled).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from caramelo.http import get  # noqa: E402
from caramelo.harvesters.x import USERS_SCHEMA  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    senators = [r for r in pq.read_table(DATA / "senadores_x.parquet").to_pylist()
                if r["confidence"] == "verified" and r["handle"]]
    print(f"{len(senators)} verified senator accounts to merge")

    users_table = pq.read_table(DATA / "x_users.parquet")
    if "casa" not in users_table.column_names:
        users_table = users_table.add_column(
            2, "casa", pa.array(["camara"] * users_table.num_rows))
    existing_ids = {r["user_id"] for r in users_table.to_pylist()}
    existing_handles = {r["handle"] for r in users_table.to_pylist()}

    bearer = os.environ["CARAMELO_X_BEARER"]
    rows: list[dict] = []
    handles = [s["handle"].lower() for s in senators
               if s["handle"].lower() not in existing_handles]
    by_handle = {s["handle"].lower(): s for s in senators}
    for i in range(0, len(handles), 100):
        batch = handles[i:i + 100]
        resp = get("https://api.twitter.com/2/users/by", params={
            "usernames": ",".join(batch),
            "user.fields": "public_metrics",
        }, headers={"Authorization": f"Bearer {bearer}"}).json()
        for u in resp.get("data", []):
            if u["id"] in existing_ids:
                continue
            s = by_handle[u["username"].lower()]
            metrics = u.get("public_metrics", {})
            rows.append({
                "user_id": u["id"], "handle": u["username"].lower(),
                "casa": "senado", "parlamentar_id": s["senador_id"],
                "nome": s["nome"],
                "followers": metrics.get("followers_count"),
                "tweets_total": metrics.get("tweet_count"),
            })
        time.sleep(0.5)

    merged = pa.concat_tables([
        users_table.cast(USERS_SCHEMA),
        pa.Table.from_pylist(rows, schema=USERS_SCHEMA),
    ])
    pq.write_table(merged, DATA / "x_users.parquet", compression="zstd")
    print(f"merged {len(rows)} senators -> x_users.parquet "
          f"({merged.num_rows} accounts total)")


if __name__ == "__main__":
    main()
