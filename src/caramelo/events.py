"""Derive typed events from the diff between two published states.

Event taxonomy v0:
    dataset.updated   any table whose content hash changed
    emenda.created    a codigo_emenda not present in the previous emendas table
    emenda.paid       valor_pago for a codigo_emenda increased
    proposicao.apresentada  a bill id not present before (Câmara or Senado)
    proposicao.tramitou     a bill's situação/tramitação changed

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


PROPOSICAO_TABLES = {
    # table: (casa, status columns, author table, author fk, author fields)
    "proposicoes": ("camara", ("status_situacao", "status_tramitacao"),
                    "proposicoes_autores", "id_proposicao",
                    ("nome_autor", "partido", "uf", "id_deputado")),
    "senado_proposicoes": ("senado", ("situacao_atual",),
                           "senado_proposicoes_autores", "id_processo",
                           ("nome_senador", None, "uf", "codigo_senador")),
}


def _previous_table(prev_manifest: dict, name: str, target) -> list[dict] | None:
    prev_file = (prev_manifest["tables"].get(name) or {}).get("file")
    prev_bytes = target.get(f"latest/{prev_file}") if prev_file else None
    if not prev_bytes:
        return None  # new table: first publish is a baseline, no row events
    with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
        tmp.write(prev_bytes)
        tmp.flush()
        return pq.read_table(tmp.name).to_pylist()


def _proposicao_events(data_dir: Path, name: str, at: str,
                       prev_rows: list[dict]) -> list[dict]:
    casa, status_cols, autores_table, fk, (nome, partido, uf, pid) = \
        PROPOSICAO_TABLES[name]
    old = {r["id"]: r for r in prev_rows}
    first_author: dict[int, dict] = {}
    autores_path = data_dir / f"{autores_table}.parquet"
    if autores_path.exists():
        for a in pq.read_table(autores_path).to_pylist():
            first_author.setdefault(a[fk], a)
    events = []
    for r in pq.read_table(data_dir / f"{name}.parquet").to_pylist():
        a = first_author.get(r["id"], {})
        base = {
            "at": at, "casa": casa, "proposicao_id": r["id"],
            "sigla": f"{r['sigla_tipo']} {r['numero']}/{r['ano']}",
            "ementa": (r.get("ementa") or "")[:300],
            "autor": a.get(nome), "partido": a.get(partido) if partido else None,
            # `uf` = first author's UF, so webhook UF filters work
            "uf": a.get(uf), "parlamentar_id": a.get(pid), "url": r.get("url"),
        }
        prev = old.get(r["id"])
        if prev is None:
            events.append({"type": "proposicao.apresentada", **base,
                           "data_apresentacao": r.get("data_apresentacao")})
        elif any(r.get(c) != prev.get(c) for c in status_cols):
            events.append({"type": "proposicao.tramitou", **base,
                           "situacao": r.get(status_cols[0]),
                           "situacao_anterior": prev.get(status_cols[0]),
                           "tramitacao": (r.get(status_cols[1])
                                          if len(status_cols) > 1 else None)})
    return events


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

    for name in PROPOSICAO_TABLES:
        if prev_manifest and name in changed:
            prev_rows = _previous_table(prev_manifest, name, target)
            if prev_rows is not None:
                events.extend(_proposicao_events(data_dir, name, at, prev_rows))

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
