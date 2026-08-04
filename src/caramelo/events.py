"""Derive typed events from the diff between two published states.

Event taxonomy v0:
    dataset.updated   any table whose content hash changed
    emenda.created    a codigo_emenda not present in the previous emendas table
    emenda.paid       valor_pago for a codigo_emenda increased

Row-level diffing needs the previous Parquet, which is fetched from the
publish target. The first publish (no previous manifest) is a baseline:
only dataset.updated events are emitted, no row-level noise.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pyarrow.parquet as pq

EMENDA_KEY_COLUMNS = ["codigo_emenda", "ano", "nome_autor", "municipio", "uf",
                      "valor_empenhado", "valor_pago"]


def _emendas_by_codigo(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in pq.read_table(path, columns=EMENDA_KEY_COLUMNS).to_pylist():
        code = r["codigo_emenda"]
        if not code or code == "Sem informação":
            continue
        entry = out.setdefault(code, {
            "ano": r["ano"], "nome_autor": r["nome_autor"],
            "municipio": r["municipio"], "uf": r["uf"],
            "valor_empenhado": 0.0, "valor_pago": 0.0,
        })
        entry["valor_empenhado"] += r["valor_empenhado"] or 0
        entry["valor_pago"] += r["valor_pago"] or 0
    return out


def derive_events(data_dir: Path, changed: list[str], manifest: dict,
                  prev_manifest: dict | None, target) -> list[dict]:
    at = manifest["generated_at"]
    events: list[dict] = []

    for name in changed:
        events.append({
            "type": "dataset.updated", "at": at, "dataset": name,
            "rows": manifest["tables"][name]["rows"],
            "prev_rows": ((prev_manifest or {}).get("tables", {})
                          .get(name, {}).get("rows")),
        })

    if prev_manifest and "redes_sociais" in changed:
        prev_file = (prev_manifest["tables"].get("redes_sociais") or {}).get("file")
        prev_bytes = target.get(f"latest/{prev_file}") if prev_file else None
        if prev_bytes:
            def account_keys(path: Path) -> dict[tuple, dict]:
                return {
                    (r["parlamentar_id"], r["rede"], r["handle"]): r
                    for r in pq.read_table(path).to_pylist()
                    if r["handle"]
                }
            with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
                tmp.write(prev_bytes)
                tmp.flush()
                old_accounts = account_keys(Path(tmp.name))
            new_accounts = account_keys(data_dir / "redes_sociais.parquet")
            for key, r in new_accounts.items():
                if key not in old_accounts:
                    events.append({
                        "type": "rede.added", "at": at, "casa": r["casa"],
                        "parlamentar_id": r["parlamentar_id"],
                        "nome": r["nome"], "rede": r["rede"],
                        "handle": r["handle"], "url": r["url"],
                    })
            for key, r in old_accounts.items():
                if key not in new_accounts:
                    events.append({
                        "type": "rede.removed", "at": at, "casa": r["casa"],
                        "parlamentar_id": r["parlamentar_id"],
                        "nome": r["nome"], "rede": r["rede"],
                        "handle": r["handle"],
                    })

    if prev_manifest and "emendas" in changed:
        prev_file = (prev_manifest["tables"].get("emendas") or {}).get("file")
        prev_bytes = target.get(f"latest/{prev_file}") if prev_file else None
        if prev_bytes:
            with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
                tmp.write(prev_bytes)
                tmp.flush()
                old = _emendas_by_codigo(Path(tmp.name))
            new = _emendas_by_codigo(data_dir / "emendas.parquet")
            for code, entry in new.items():
                if code not in old:
                    events.append({
                        "type": "emenda.created", "at": at, "codigo": code,
                        "ano": entry["ano"], "autor": entry["nome_autor"],
                        "municipio": entry["municipio"], "uf": entry["uf"],
                        "valor_empenhado": round(entry["valor_empenhado"], 2),
                    })
                elif entry["valor_pago"] > old[code]["valor_pago"] + 0.005:
                    events.append({
                        "type": "emenda.paid", "at": at, "codigo": code,
                        "autor": entry["nome_autor"],
                        "municipio": entry["municipio"], "uf": entry["uf"],
                        "valor_pago": round(entry["valor_pago"], 2),
                        "delta": round(
                            entry["valor_pago"] - old[code]["valor_pago"], 2),
                    })
    return events
