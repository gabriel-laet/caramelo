"""Harvest declared social-media accounts of deputados (Câmara API).

Each deputy's detail endpoint exposes a `redeSocial` URL list. The Senado API
does not publish socials, so senators enter via a manual overrides file later.
Output is the person -> platform handle crosswalk consumed by the media
harvesters (X, YouTube, Instagram via SearchApi).
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API_BASE = "https://dadosabertos.camara.leg.br/api/v2"
REQUEST_INTERVAL = 0.1

SCHEMA = pa.schema([
    ("casa", pa.string()),
    ("parlamentar_id", pa.int64()),
    ("nome", pa.string()),
    ("rede", pa.string()),   # twitter | instagram | facebook | youtube | tiktok | site
    ("handle", pa.string()),
    ("url", pa.string()),
])

PLATFORMS = {
    "twitter.com": "twitter", "x.com": "twitter",
    "instagram.com": "instagram",
    "facebook.com": "facebook", "fb.com": "facebook",
    "youtube.com": "youtube", "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}


def classify(url: str) -> tuple[str, str | None]:
    """-> (platform, handle)"""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return "site", None
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    platform = PLATFORMS.get(host, "site")
    parts = [p for p in parsed.path.split("/") if p]
    handle = None
    if platform in ("twitter", "instagram", "facebook", "tiktok") and parts:
        handle = parts[0].lstrip("@").lower() or None
    elif platform == "youtube" and parts:
        if parts[0].startswith("@"):
            handle = parts[0].lstrip("@")
        elif parts[0] in ("channel", "c", "user") and len(parts) > 1:
            handle = parts[1]
    return platform, handle


def harvest(data_dir: Path, legislatura: int = 57) -> Path:
    deputados = [
        d for d in pq.read_table(data_dir / "deputados.parquet").to_pylist()
        if d["legislatura"] == legislatura
    ]
    seen: set[int] = set()
    rows: list[dict] = []
    with_network = 0
    for i, dep in enumerate(deputados):
        if dep["id"] in seen:
            continue
        seen.add(dep["id"])
        body = get(f"{API_BASE}/deputados/{dep['id']}",
                   headers={"Accept": "application/json"}).json()["dados"]
        urls = list(body.get("redeSocial") or [])
        if body.get("urlWebsite"):
            urls.append(body["urlWebsite"])
        if urls:
            with_network += 1
        for url in urls:
            rede, handle = classify(url)
            rows.append({
                "casa": "camara", "parlamentar_id": dep["id"],
                "nome": dep["nome"], "rede": rede,
                "handle": handle, "url": url,
            })
        if (i + 1) % 100 == 0:
            print(f"redes: {i + 1}/{len(deputados)} deputados...")
        time.sleep(REQUEST_INTERVAL)

    out_path = data_dir / "redes_sociais.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"redes: {len(rows)} accounts from {len(seen)} deputados "
          f"({with_network} with at least one) -> {out_path}")
    return out_path
