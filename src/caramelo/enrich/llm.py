"""LLM classification pass via Cloudflare AI Gateway (Workers AI models).

Classifies the emenda action/plan strings the keyword rules can't reach,
using a closed taxonomy and batched JSON prompts. Results are cached in
llm_categorias.parquet keyed by (codigo_acao, codigo_plano_orcamentario) so
each distinct string is paid for exactly once; every call is logged to
ai_ledger.jsonl. Billing comes from Cloudflare credits through the gateway
(no provider keys anywhere).

Env: CARAMELO_AI_TOKEN (AI Gateway + Workers AI token),
     CARAMELO_AI_GATEWAY (default: "default"),
     CARAMELO_CF_ACCOUNT (account id).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get  # noqa: F401  (kept for symmetry)
import httpx

MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
BATCH = 15

CATEGORIAS = ("saude", "saneamento", "educacao", "infraestrutura",
              "agropecuaria", "esporte", "cultura", "eventos_shows",
              "assistencia_social", "seguranca", "turismo", "publicidade",
              "administracao", "outros")

CACHE_SCHEMA = pa.schema([
    ("codigo_acao", pa.string()),
    ("codigo_plano_orcamentario", pa.string()),
    ("categoria", pa.string()),
    ("modelo", pa.string()),
])


def _endpoint() -> tuple[str, dict]:
    acct = os.environ.get("CARAMELO_CF_ACCOUNT",
                          "2cdbde3608c5914c43e4433fd39ba6b2")
    gateway = os.environ.get("CARAMELO_AI_GATEWAY", "default")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run"
    headers = {
        "Authorization": f"Bearer {os.environ['CARAMELO_AI_TOKEN']}",
        "cf-aig-gateway-id": gateway,
        "Content-Type": "application/json",
    }
    return url, headers


def _classify_batch(items: list[dict], ledger: Path) -> dict[int, str]:
    url, headers = _endpoint()
    lines = "\n".join(
        f'{i}: {it["texto"][:220]}' for i, it in enumerate(items))
    prompt = (
        "Classifique cada item de orçamento público brasileiro em UMA "
        f"categoria desta lista fechada: {', '.join(CATEGORIAS)}.\n"
        "Responda APENAS um array JSON, sem explicação, no formato "
        '[{"i": 0, "categoria": "..."}].\n\nItens:\n' + lines)
    resp = httpx.post(url, headers=headers, json={
        "model": MODEL,
        "input": {"messages": [{"role": "user", "content": prompt}]},
    }, timeout=180.0, verify=os.environ.get("AWS_CA_BUNDLE", True))
    resp.raise_for_status()
    content = (resp.json().get("result", {}).get("choices") or
               [{}])[0].get("message", {}).get("content", "")
    with open(ledger, "a") as fh:
        fh.write(json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": MODEL, "items": len(items),
        }) + "\n")
    start, end = content.find("["), content.rfind("]")
    out: dict[int, str] = {}
    if start != -1 and end != -1:
        try:
            for entry in json.loads(content[start:end + 1]):
                cat = entry.get("categoria")
                if cat in CATEGORIAS:
                    out[int(entry["i"])] = cat
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
    return out


def classify_indefinidos(data_dir: Path, max_calls: int = 60) -> Path:
    import duckdb
    con = duckdb.connect()
    pending = con.execute(f"""
        SELECT DISTINCT e.codigo_acao, e.codigo_plano_orcamentario,
               coalesce(e.nome_plano_orcamentario, '') || ' | ' ||
               coalesce(e.nome_acao, '') || ' | ' ||
               coalesce(e.nome_subfuncao, '') AS texto
        FROM '{data_dir}/emendas.parquet' e
        JOIN '{data_dir}/emendas_categorias.parquet' c
          ON e.codigo_emenda = c.codigo_emenda
         AND e.codigo_acao IS NOT DISTINCT FROM c.codigo_acao
         AND e.codigo_plano_orcamentario IS NOT DISTINCT FROM c.codigo_plano_orcamentario
        WHERE c.categoria = 'indefinido'""").fetchall()
    items = [{"codigo_acao": a, "codigo_plano_orcamentario": p, "texto": t}
             for a, p, t in pending]

    cache_path = data_dir / "llm_categorias.parquet"
    cached: set[tuple] = set()
    rows: list[dict] = []
    if cache_path.exists():
        rows = pq.read_table(cache_path).to_pylist()
        cached = {(r["codigo_acao"], r["codigo_plano_orcamentario"])
                  for r in rows}
    todo = [it for it in items
            if (it["codigo_acao"], it["codigo_plano_orcamentario"]) not in cached]
    print(f"llm: {len(items)} distinct indefinidos, {len(todo)} uncached")

    ledger = data_dir / "ai_ledger.jsonl"
    calls = 0
    for i in range(0, len(todo), BATCH):
        if calls >= max_calls:
            print("llm: max_calls reached, stopping cleanly")
            break
        batch = todo[i:i + BATCH]
        result = _classify_batch(batch, ledger)
        calls += 1
        for j, it in enumerate(batch):
            cat = result.get(j)
            if cat:
                rows.append({
                    "codigo_acao": it["codigo_acao"],
                    "codigo_plano_orcamentario": it["codigo_plano_orcamentario"],
                    "categoria": cat, "modelo": MODEL,
                })
        if calls % 10 == 0:
            print(f"llm: {calls} calls, {len(rows)} classifications...")
        time.sleep(0.3)

    pq.write_table(pa.Table.from_pylist(rows, schema=CACHE_SCHEMA),
                   cache_path, compression="zstd")
    print(f"llm: {calls} calls this run -> {cache_path} ({len(rows)} cached)")
    return cache_path
