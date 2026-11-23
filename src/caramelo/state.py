"""Seed and persist harvest state across ephemeral container runs.

Append-only tables (posts, media, ...) and rotation cursors live on the
publish target between runs: run-all seeds them into the local data dir
before harvesting and pushes cursors back after publishing. Without this,
an ephemeral container would overwrite accumulated history in the lake
with one run's sample.
"""

from __future__ import annotations

from pathlib import Path

SEED_TABLES = ("posts", "x_users", "media", "senadores_x", "gazetas",
               "tse_candidatos", "tse_receitas", "trends", "cnpjs", "socios",
               # incremental (financas fills gaps per run) / carry-forward
               "financas_municipio", "indicadores_municipio")
STATE_FILES = ("x_cursor.json", "siconfi_cursor.json")


def seed(data_dir: Path, target) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in SEED_TABLES:
        dest = data_dir / f"{name}.parquet"
        if dest.exists():
            continue
        blob = target.get(f"latest/{name}.parquet")
        if blob:
            dest.write_bytes(blob)
            print(f"state: seeded {name}.parquet ({len(blob) // 1024}KB)")
    for name in STATE_FILES:
        dest = data_dir / name
        if dest.exists():
            continue
        blob = target.get(f"state/{name}")
        if blob:
            dest.write_bytes(blob)


def push(data_dir: Path, target) -> None:
    for name in STATE_FILES:
        src = data_dir / name
        if src.exists():
            target.put_bytes(f"state/{name}", src.read_bytes())


def restore(data_dir: Path, target, tables: tuple[str, ...]) -> bool:
    """Fall back to the last published version of `tables` after their
    source failed. Keeps any table already present locally (e.g. written
    before the failure). True only if every table is available afterwards."""
    data_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for name in tables:
        dest = data_dir / f"{name}.parquet"
        if dest.exists():
            continue
        blob = target.get(f"latest/{name}.parquet")
        if blob:
            dest.write_bytes(blob)
            print(f"state: restored {name}.parquet from the lake "
                  f"({len(blob) // 1024}KB, stale)")
        else:
            ok = False
    return ok
