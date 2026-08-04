"""Publish harvested tables to a target (Cloudflare R2 or a local directory).

Layout in the target:
    latest/<table>.parquet        current version of every table
    latest/manifest.json          current manifest
    harvests/<ts>/manifest.json   manifest history (one per publish with changes)
    events/<ts>.jsonl             events derived from the diff vs previous state

Only tables whose sha256 changed are uploaded. Events are derived at publish
time in Python (where the data tooling lives) so downstream consumers — the
Cloudflare Worker that fans out webhooks included — only read events.jsonl,
never the Parquet.

Targets:
    local:<dir>   filesystem directory (dev / testing)
    r2            Cloudflare R2 via S3 API (needs boto3 and env vars:
                  CARAMELO_R2_ENDPOINT, CARAMELO_R2_BUCKET,
                  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from caramelo.events import derive_events
from caramelo.manifest import build_manifest


class LocalTarget:
    def __init__(self, root: Path):
        self.root = root

    def get(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.exists() else None

    def put_file(self, key: str, src: Path) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def put_bytes(self, key: str, data: bytes) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


class R2Target:
    def __init__(self) -> None:
        import boto3  # optional dependency: pip install caramelo-br[publish]

        endpoint = os.environ["CARAMELO_R2_ENDPOINT"]
        self.bucket = os.environ["CARAMELO_R2_BUCKET"]
        self.client = boto3.client("s3", endpoint_url=endpoint,
                                   region_name="auto")

    def get(self, key: str) -> bytes | None:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None

    def put_file(self, key: str, src: Path) -> None:
        self.client.upload_file(str(src), self.bucket, key)

    def put_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)


def make_target(spec: str):
    if spec.startswith("local:"):
        return LocalTarget(Path(spec.removeprefix("local:")))
    if spec == "r2":
        return R2Target()
    raise ValueError(f"unknown publish target: {spec!r} (use local:<dir> or r2)")


def publish(data_dir: Path, target_spec: str) -> None:
    target = make_target(target_spec)
    manifest = build_manifest(data_dir)
    if not manifest["tables"]:
        print("publish: no tables in data dir, nothing to do")
        return

    prev_raw = target.get("latest/manifest.json")
    prev = json.loads(prev_raw) if prev_raw else None
    prev_tables = (prev or {}).get("tables", {})

    changed = [name for name, meta in manifest["tables"].items()
               if prev_tables.get(name, {}).get("sha256") != meta["sha256"]]
    if not changed:
        print("publish: no changes since last publish")
        return

    events = derive_events(data_dir, changed, manifest, prev, target)

    ts = manifest["generated_at"].replace(":", "").replace("-", "")
    for name in changed:
        meta = manifest["tables"][name]
        target.put_file(f"latest/{meta['file']}", data_dir / meta["file"])
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode()
    target.put_bytes("latest/manifest.json", manifest_bytes)
    target.put_bytes(f"harvests/{ts}/manifest.json", manifest_bytes)
    if events:
        lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
        target.put_bytes(f"events/{ts}.jsonl", lines.encode())

    print(f"publish: {len(changed)} table(s) uploaded "
          f"({', '.join(changed)}), {len(events)} event(s)")
