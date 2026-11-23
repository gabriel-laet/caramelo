"""Caramelo REST API — a thin FastAPI adapter over caramelo.domain.

Run locally:  uvicorn caramelo.api:app
The MCP server is generated from this same app (see caramelo.mcp_server),
so REST and MCP stay in lockstep by construction.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from caramelo import domain

app = FastAPI(
    title="Caramelo",
    version="0.1.0",
    description=(
        "Open, politically-neutral data layer for Brazilian public data. "
        "All endpoints are read-only queries over the public Parquet lake."),
)

app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
    allow_headers=["*"],
)


def lake() -> domain.Lake:
    return domain.default_lake()


@app.get("/emendas/autores", operation_id="emendas_autores",
         summary="Ranking of emenda authors by money moved")
def emendas_autores(ano_min: int = 2023, apenas_pix: bool = False,
                    casa: str | None = Query(
                        None, pattern="^(camara|senado|coletivas)$",
                        description="camara | senado (individual emendas "
                                    "by house) | coletivas (bancada, "
                                    "comissão, relator)"),
                    limit: int = Query(25, le=200)) -> list[dict]:
    return domain.emendas_por_autor(lake(), ano_min=ano_min,
                                    apenas_pix=apenas_pix, casa=casa,
                                    limit=limit)


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


@app.get("/senadores/{senador_id}", operation_id="senador",
         summary="Senator profile: identity, emendas, CEAPS, bills, votes")
def senador(senador_id: int) -> dict:
    profile = domain.senador(lake(), senador_id)
    if not profile:
        raise HTTPException(404, "senador não encontrado")
    return profile


@app.get("/rankings/cota", operation_id="cota",
         summary="Parliamentarians ranked by expense-quota spending "
                 "(CEAP for deputados, CEAPS for senadores)")
def ranking_cota(casa: str = Query("camara", pattern="^(camara|senado)$"),
                 ano_min: int = 2023,
                 limit: int = Query(30, le=600)) -> list[dict]:
    return domain.cota(lake(), casa=casa, ano_min=ano_min, limit=limit)


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


@app.get("/detector/shows", operation_id="detector_shows",
         summary="Municípios with both Pix money and gazette show contracts")
def detector_shows(ano_min: int = 2023, pop_max: int = Query(100000, le=1000000),
                   limit: int = Query(50, le=200)) -> list[dict]:
    return domain.show_detector(lake(), ano_min=ano_min, pop_max=pop_max,
                                limit=limit)


@app.get("/emendas/categorias", operation_id="emendas_categorias",
         summary="Emenda money by practical category (enrichment layer)")
def emendas_categorias(ano_min: int = 2021,
                       apenas_pix: bool = False) -> list[dict]:
    return domain.categorias_resumo(lake(), ano_min=ano_min,
                                    apenas_pix=apenas_pix)


@app.get("/indices/partidos", operation_id="partidos",
         summary="Per-party size and mean governismo")
def partidos(min_votos: int = 100) -> list[dict]:
    return domain.partidos(lake(), min_votos=min_votos)


@app.get("/comparar", operation_id="comparar",
         summary="Compare two deputies side by side")
def comparar(a: int, b: int) -> dict:
    return domain.comparar(lake(), a, b)


@app.get("/favorecidos", operation_id="favorecidos",
         summary="Largest final recipients of emenda money, with CNAE")
def favorecidos(limit: int = Query(30, le=200),
                apenas_empresas: bool = True) -> list[dict]:
    return domain.top_favorecidos(lake(), limit=limit,
                                  apenas_empresas=apenas_empresas)


@app.get("/tabelas", operation_id="tabelas",
         summary="List all browsable lake tables with row counts and columns")
def tabelas() -> list[dict]:
    return domain.list_tables(lake())


@app.get("/tabela/{name}", operation_id="tabela",
         summary="Sample rows from any lake table")
def tabela(name: str, limit: int = Query(50, le=200)) -> dict:
    result = domain.sample_table(lake(), name, limit=limit)
    if not result:
        raise HTTPException(404, "tabela não disponível")
    return result


@app.get("/busca", operation_id="busca",
         summary="Free-text search across politicians and municípios")
def busca(q: str, limit: int = Query(10, le=50)) -> dict:
    return domain.busca(lake(), q, limit=limit)


def _limiares(raw: str | None) -> dict[str, float]:
    """'agua=85,ideb=6.5' -> {'agua': 85.0, 'ideb': 6.5}"""
    out: dict[str, float] = {}
    for part in filter(None, (raw or "").split(",")):
        key, _, val = part.partition("=")
        try:
            out[key.strip()] = float(val)
        except ValueError:
            raise HTTPException(422, f"limiar inválido: {part}")
    return out


@app.get("/painel", operation_id="painel",
         summary="Every município in one columnar payload (indicators, "
                 "fiscal ratios, emendas per capita) for maps")
def painel(response: Response) -> dict:
    out = domain.painel(lake())
    # a partial panel (tables still unpublished) must not stick in caches
    degraded = out["faltando"] and out["faltando"] != ["gazetas"]
    response.headers["Cache-Control"] = (
        f"public, max-age={600 if degraded else 3600}")
    return out


@app.get("/propostas", operation_id="propostas",
         summary="Bills whose stated criteria can be simulated per município")
def propostas() -> list[dict]:
    return domain.propostas()


@app.get("/simulador/{proposta_id}", operation_id="simulador",
         summary="Apply a bill's criteria to every município (neutral: "
                 "atende / não atende / sem dado per criterion)")
def simulador(proposta_id: str,
              saneamento: str | None = Query(
                  None, description="saneamento source: censo2022 | sinisa"),
              limiares: str | None = Query(
                  None, description="threshold overrides, e.g. agua=85,ideb=6.5"),
              uf: str | None = None,
              municipios: bool = Query(
                  True, description="include one row per município")) -> dict:
    variantes = {"saneamento": saneamento} if saneamento else None
    try:
        result = domain.simular(lake(), proposta_id, variantes=variantes,
                                limiares=_limiares(limiares), uf=uf,
                                incluir_municipios=municipios)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not result:
        raise HTTPException(404, "proposta não encontrada")
    return result


@app.get("/municipios/{codigo_ibge}/propostas",
         operation_id="municipio_propostas",
         summary="Every simulable bill evaluated for one município")
def municipio_propostas(codigo_ibge: str) -> list[dict]:
    return domain.simulacoes_municipio(lake(), codigo_ibge)


@app.get("/proposicoes", operation_id="proposicoes",
         summary="Câmara bills by author, theme, type or text")
def proposicoes(deputado_id: int | None = None, tema: str | None = None,
                q: str | None = None, sigla_tipo: str | None = None,
                limit: int = Query(50, le=200)) -> list[dict]:
    return domain.proposicoes(lake(), deputado_id=deputado_id, tema=tema,
                              q=q, sigla_tipo=sigla_tipo, limit=limit)


@app.get("/senado/proposicoes", operation_id="senado_proposicoes",
         summary="Senate bills by senator, type or text")
def senado_proposicoes(senador_id: int | None = None, q: str | None = None,
                       sigla_tipo: str | None = None,
                       limit: int = Query(50, le=200)) -> list[dict]:
    return domain.senado_proposicoes(lake(), senador_id=senador_id, q=q,
                                     sigla_tipo=sigla_tipo, limit=limit)


@app.get("/proposicoes/{proposicao_id}", operation_id="proposicao",
         summary="One Câmara bill: authors, themes, attached bills, votes")
def proposicao(proposicao_id: int) -> dict:
    result = domain.proposicao(lake(), proposicao_id)
    if not result:
        raise HTTPException(404, "proposição não encontrada")
    return result
