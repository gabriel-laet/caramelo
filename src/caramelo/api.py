"""Caramelo REST API — a thin FastAPI adapter over caramelo.domain.

Run locally:  uvicorn caramelo.api:app
The MCP server is generated from this same app (see caramelo.mcp_server),
so REST and MCP stay in lockstep by construction.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from caramelo import domain

app = FastAPI(
    title="Caramelo",
    version="0.1.0",
    description=(
        "Open, politically-neutral data layer for Brazilian public data. "
        "All endpoints are read-only queries over the public Parquet lake."),
)


def lake() -> domain.Lake:
    return domain.default_lake()


@app.get("/emendas/autores", operation_id="emendas_autores",
         summary="Ranking of emenda authors by money moved")
def emendas_autores(ano_min: int = 2023, apenas_pix: bool = False,
                    limit: int = Query(25, le=200)) -> list[dict]:
    return domain.emendas_por_autor(lake(), ano_min=ano_min,
                                    apenas_pix=apenas_pix, limit=limit)


@app.get("/emendas/municipio/{codigo_ibge}", operation_id="emendas_municipio",
         summary="Every emenda sent to one município")
def emendas_municipio(codigo_ibge: str, ano_min: int = 2023) -> list[dict]:
    return domain.emendas_por_municipio(lake(), codigo_ibge, ano_min=ano_min)


@app.get("/rankings/pix-per-capita", operation_id="pix_per_capita",
         summary="Municípios ranked by emendas-Pix money per inhabitant")
def ranking_pix_per_capita(ano_min: int = 2023,
                           limit: int = Query(25, le=200)) -> list[dict]:
    return domain.pix_per_capita(lake(), ano_min=ano_min, limit=limit)


@app.get("/politicos/{deputado_id}", operation_id="politico",
         summary="Deputy profile: identity, socials, emendas, expenses")
def politico(deputado_id: int) -> dict:
    profile = domain.politico(lake(), deputado_id)
    if not profile:
        raise HTTPException(404, "deputado não encontrado")
    return profile


@app.get("/indices/governismo", operation_id="governismo",
         summary="Deputies ranked by agreement with the Governo orientation")
def indice_governismo(min_votos: int = 200,
                      limit: int = Query(513, le=600)) -> list[dict]:
    return domain.governismo(lake(), min_votos=min_votos, limit=limit)


@app.get("/municipios/{codigo_ibge}", operation_id="municipio",
         summary="Município overview with emendas totals by year")
def municipio(codigo_ibge: str) -> dict:
    result = domain.municipio(lake(), codigo_ibge)
    if not result:
        raise HTTPException(404, "município não encontrado")
    return result


@app.get("/busca", operation_id="busca",
         summary="Free-text search across politicians and municípios")
def busca(q: str, limit: int = Query(10, le=50)) -> dict:
    return domain.busca(lake(), q, limit=limit)
