"""Discover senators' X accounts (the Senado API doesn't publish socials).

Method (documented provenance, confidence-scored):
1. Current senators from the official lista-atual endpoint.
2. SearchApi Google query per senator restricted to x.com -> candidate handle.
3. X API users/by batch lookup verifies the candidate exists and its display
   name matches the senator's name (token overlap).

Output: data/senadores_x.parquet (codigo, nome, handle, confidence, evidence).
Costs: ~81 SearchApi credits + X user lookups (not billed as post reads).
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from caramelo.http import get  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"

SCHEMA = pa.schema([
    ("senador_id", pa.int64()),
    ("nome", pa.string()),
    ("handle", pa.string()),
    ("x_display_name", pa.string()),
    ("followers", pa.int64()),
    ("confidence", pa.string()),  # verified | weak | none
    ("evidence", pa.string()),
])

BLOCKLIST = {"senadofederal", "search", "hashtag", "i", "intent", "share",
             "home", "login", "explore", "status"}


def norm_tokens(name: str) -> set[str]:
    stripped = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return {t for t in re.split(r"\W+", stripped.lower()) if len(t) > 2}


def current_senators() -> list[dict]:
    resp = get("https://legis.senado.leg.br/dadosabertos/senador/lista/atual",
               headers={"Accept": "application/json"}).json()
    parls = (resp["ListaParlamentarEmExercicio"]["Parlamentares"]
             ["Parlamentar"])
    return [{
        "codigo": int(p["IdentificacaoParlamentar"]["CodigoParlamentar"]),
        "nome": p["IdentificacaoParlamentar"]["NomeParlamentar"],
        "nome_completo": p["IdentificacaoParlamentar"]
                          .get("NomeCompletoParlamentar", ""),
    } for p in parls]


def find_candidate(nome: str) -> tuple[str | None, str]:
    resp = get("https://www.searchapi.io/api/v1/search", params={
        "engine": "google", "q": f'"{nome}" senador site:x.com',
        "gl": "br", "hl": "pt-br",
        "api_key": os.environ["CARAMELO_SEARCHAPI_KEY"],
    }, timeout=60.0).json()
    for r in resp.get("organic_results", []):
        link = r.get("link", "")
        m = re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/?$",
                     link)
        if m and m.group(1).lower() not in BLOCKLIST:
            return m.group(1), link
    return None, ""


def verify(handles: dict[str, dict]) -> dict[str, dict]:
    """Batch lookup on X, bisecting failing batches so one poison handle
    can't sink the rest. Returns handle -> {name, followers}."""
    bearer = os.environ["CARAMELO_X_BEARER"]
    out: dict[str, dict] = {}
    stack: list[list[str]] = [sorted(handles)[i:i + 50]
                              for i in range(0, len(handles), 50)]
    while stack:
        batch = stack.pop()
        try:
            resp = get("https://api.twitter.com/2/users/by", params={
                "usernames": ",".join(batch),
                "user.fields": "public_metrics",
            }, headers={"Authorization": f"Bearer {bearer}"},
                retries=1).json()
        except RuntimeError:
            if len(batch) > 1:
                mid = len(batch) // 2
                stack.extend([batch[:mid], batch[mid:]])
            else:
                print(f"  verify failed for @{batch[0]} — left unverified")
            continue
        for u in resp.get("data", []):
            out[u["username"].lower()] = {
                "name": u["name"],
                "followers": u.get("public_metrics", {})
                              .get("followers_count", 0),
            }
        time.sleep(0.5)
    return out


def main() -> None:
    senators = current_senators()
    print(f"{len(senators)} senators em exercício")
    candidates: dict[str, dict] = {}
    rows: list[dict] = []
    for s in senators:
        handle, evidence = find_candidate(s["nome"])
        if handle:
            candidates[handle.lower()] = {**s, "evidence": evidence,
                                          "handle": handle}
        else:
            rows.append({"senador_id": s["codigo"], "nome": s["nome"],
                         "handle": None, "x_display_name": None,
                         "followers": None, "confidence": "none",
                         "evidence": ""})
        time.sleep(0.2)

    verified = verify(candidates)
    for handle, s in candidates.items():
        info = verified.get(handle)
        if not info:
            # keep the discovered handle: search evidence still stands
            confidence, display, followers = "unverified", None, None
        else:
            display, followers = info["name"], info["followers"]
            overlap = norm_tokens(s["nome"]) & norm_tokens(display)
            confidence = "verified" if overlap else "weak"
        rows.append({"senador_id": s["codigo"], "nome": s["nome"],
                     "handle": handle,
                     "x_display_name": display, "followers": followers,
                     "confidence": confidence, "evidence": s["evidence"]})

    out = DATA / "senadores_x.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out,
                   compression="zstd")
    counts = {}
    for r in rows:
        counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1
    print(f"-> {out} | {counts}")
    for r in sorted(rows, key=lambda x: -(x["followers"] or 0))[:8]:
        if r["handle"]:
            print(f"  [{r['confidence']:8}] @{r['handle']:<18} {r['nome']} "
                  f"({r['followers']:,} seguidores)")


if __name__ == "__main__":
    main()
