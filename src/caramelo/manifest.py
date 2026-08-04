"""Harvest manifest: content hashes and row counts for every published table.

The manifest is the diff anchor of the event layer: two manifests tell you
which datasets changed between harvests without touching the data itself.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pyarrow.parquet as pq

MANIFEST_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(data_dir: Path) -> dict:
    tables = {}
    for path in sorted(data_dir.glob("*.parquet")):
        tables[path.stem] = {
            "file": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "rows": pq.read_metadata(path).num_rows,
        }
    return {
        "version": MANIFEST_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tables": tables,
    }
