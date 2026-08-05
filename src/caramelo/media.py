"""Budget-aware SearchApi.io client + media harvesting.

Every SearchApi call costs credits, so spending is a first-class concern:

- A per-run budget caps how many calls a harvest may make; the run stops
  cleanly when the budget is exhausted (never mid-write).
- Every call is appended to a JSONL ledger (engine, query, credits) so total
  spend is observable and auditable after the fact.
- Target selection is prioritized (who matters most this run) instead of
  sweeping everyone, so a small budget still covers the interesting slice.

Engines share one key: google_news, google, youtube, google_maps_reviews, etc.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"

MEDIA_SCHEMA = pa.schema([
    ("fetched_at", pa.string()),
    ("engine", pa.string()),
    ("query", pa.string()),
    ("nome_autor", pa.string()),
    ("position", pa.int32()),
    ("title", pa.string()),
    ("link", pa.string()),
    ("source", pa.string()),
    ("date_raw", pa.string()),
    ("snippet", pa.string()),
])


class BudgetExhausted(Exception):
    pass


class SearchApiClient:
    def __init__(self, data_dir: Path, budget: int,
                 api_key: str | None = None):
        self.api_key = api_key or os.environ["CARAMELO_SEARCHAPI_KEY"]
        self.budget = budget
        self.spent = 0
        self.ledger = data_dir / "searchapi_ledger.jsonl"
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    def search(self, engine: str, **params) -> dict:
        if self.spent >= self.budget:
            raise BudgetExhausted(
                f"budget of {self.budget} credits spent this run")
        resp = get(SEARCHAPI_URL, params={
            "engine": engine, "api_key": self.api_key, **params,
        }, timeout=60.0).json()
        self.spent += 1
        with open(self.ledger, "a") as fh:
            fh.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "engine": engine,
                "q": params.get("q"),
                "credits": 1,
            }, ensure_ascii=False) + "\n")
        return resp

    def ledger_total(self) -> int:
        if not self.ledger.exists():
            return 0
        return sum(json.loads(l).get("credits", 1)
                   for l in self.ledger.read_text().splitlines() if l)


def priority_targets(data_dir: Path, limit: int) -> list[str]:
    """Author names ranked by emendas-Pix money moved since 2023 —
    the politicians whose media trail matters most right now."""
    sums: dict[str, float] = {}
    for r in pq.read_table(
            data_dir / "emendas.parquet",
            columns=["nome_autor", "ano", "is_transferencia_especial",
                     "valor_empenhado"]).to_pylist():
        if (r["is_transferencia_especial"] and r["nome_autor"]
                and (r["ano"] or 0) >= 2023):
            sums[r["nome_autor"]] = (sums.get(r["nome_autor"], 0)
                                     + (r["valor_empenhado"] or 0))
    return [name for name, _ in
            sorted(sums.items(), key=lambda kv: -kv[1])[:limit]]


def extract_results(engine: str, resp: dict) -> list[dict]:
    """Normalize each engine's result shape to title/link/source/date/snippet."""
    out: list[dict] = []
    if engine == "youtube":
        for r in resp.get("videos", []):
            out.append({
                "title": r.get("title"), "link": r.get("link"),
                "source": (r.get("channel") or {}).get("title"),
                "date_raw": r.get("published_time"),
                "snippet": r.get("description"),
            })
        return out
    for r in resp.get("organic_results", []):
        source = r.get("source")
        out.append({
            "title": r.get("title"), "link": r.get("link"),
            "source": (source.get("name") if isinstance(source, dict)
                       else source),
            "date_raw": r.get("date"), "snippet": r.get("snippet"),
        })
    return out


def harvest(data_dir: Path, budget: int = 25,
            engine: str = "google_news") -> Path:
    client = SearchApiClient(data_dir, budget)
    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    cargo_by_nome: dict[str, str] = {}
    crosswalk = data_dir / "autores_crosswalk.parquet"
    if crosswalk.exists():
        for r in pq.read_table(crosswalk).to_pylist():
            cargo_by_nome[r["nome_autor"]] = (
                "senador" if r["senador_id"] else "deputado")

    rows: list[dict] = []
    targets = priority_targets(data_dir, limit=budget)
    covered = 0
    for nome in targets:
        hl = "pt" if engine == "youtube" else "pt-br"
        # qualify the query with the mandate to cut homonym noise
        q = f"{cargo_by_nome.get(nome, '')} {nome}".strip()
        try:
            resp = client.search(engine, q=q, gl="br", hl=hl)
        except BudgetExhausted:
            break
        covered += 1
        for i, r in enumerate(extract_results(engine, resp), 1):
            rows.append({"fetched_at": fetched_at, "engine": engine,
                         "query": nome, "nome_autor": nome, "position": i,
                         **r})

    out_path = data_dir / "media.parquet"
    table = pa.Table.from_pylist(rows, schema=MEDIA_SCHEMA)
    if out_path.exists():
        table = pa.concat_tables([pq.read_table(out_path), table])
    pq.write_table(table, out_path, compression="zstd")
    print(f"media[{engine}]: {covered} targets, {len(rows)} results this run "
          f"(spent {client.spent}/{budget} credits, "
          f"ledger total {client.ledger_total()}) -> {out_path}")
    return out_path
