"""Harvest senators' nominal votes (Senado API, per-senator endpoint).

One call per senator of the legislature returns every roll-call they took
part in, with matéria identification and the vote itself. Secret votes come
through as "Secreto" and are kept (the fact a vote was secret is a datum).
"""

from __future__ import annotations

import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

API = "https://legis.senado.leg.br/dadosabertos"
REQUEST_INTERVAL = 0.4

SCHEMA = pa.schema([
    ("senador_id", pa.int64()),
    ("senador_nome", pa.string()),
    ("data_sessao", pa.string()),
    ("codigo_sessao", pa.string()),
    ("codigo_materia", pa.string()),
    ("materia", pa.string()),
    ("ementa", pa.string()),
    ("descricao_votacao", pa.string()),
    ("voto", pa.string()),
])


def fetch_votes(codigo: int, nome: str) -> list[dict]:
    body = get(f"{API}/senador/{codigo}/votacoes",
               headers={"Accept": "application/json"}).json()
    parl = body.get("VotacaoParlamentar", {}).get("Parlamentar") or {}
    votes = (parl.get("Votacoes") or {}).get("Votacao") or []
    if isinstance(votes, dict):
        votes = [votes]
    out = []
    for v in votes:
        materia = v.get("Materia") or {}
        sessao = v.get("SessaoPlenaria") or {}
        out.append({
            "senador_id": codigo, "senador_nome": nome,
            "data_sessao": sessao.get("DataSessao"),
            "codigo_sessao": sessao.get("CodigoSessao"),
            "codigo_materia": materia.get("Codigo"),
            "materia": materia.get("DescricaoIdentificacao"),
            "ementa": (materia.get("Ementa") or "")[:500] or None,
            "descricao_votacao": v.get("DescricaoVotacao"),
            "voto": v.get("SiglaDescricaoVoto"),
        })
    return out


def harvest(data_dir: Path, legislatura: int = 57) -> Path:
    senadores = pq.read_table(data_dir / "senadores.parquet").to_pylist()
    targets: dict[int, str] = {}
    for s in senadores:
        if s["legislatura"] == legislatura:
            targets.setdefault(s["codigo"], s["nome_parlamentar"])

    rows: list[dict] = []
    for i, (codigo, nome) in enumerate(sorted(targets.items())):
        try:
            rows.extend(fetch_votes(codigo, nome))
        except RuntimeError as exc:
            print(f"senado_votos: skipping {codigo} ({str(exc)[:60]})")
        if (i + 1) % 50 == 0:
            print(f"senado_votos: {i + 1}/{len(targets)} senators...")
        time.sleep(REQUEST_INTERVAL)

    out_path = data_dir / "senado_votos.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out_path,
                   compression="zstd")
    print(f"senado_votos: {len(rows)} votes from {len(targets)} senators "
          f"-> {out_path}")
    return out_path
