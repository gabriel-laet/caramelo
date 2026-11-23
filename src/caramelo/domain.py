"""Caramelo domain: canonical queries over the Parquet lake.

One definition, many surfaces: the CLI, the REST API, and the MCP server all
call these functions. Each function takes plain typed arguments and returns
JSON-serializable dicts/lists — adapters stay thin.

The lake location is either a local data dir (harvest output) or any
HTTP(S)/S3 base holding the published `latest/` tables — DuckDB reads both.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import duckdb

DEFAULT_BASE = os.environ.get(
    "CARAMELO_LAKE",
    "https://pub-b18c9a8d60c74f5080b0a1abd4045f2b.r2.dev/latest")


class Lake:
    def __init__(self, base: str | None = None):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.con = duckdb.connect()
        if self.base.startswith("http"):
            self.con.execute("INSTALL httpfs; LOAD httpfs;")

    def table(self, name: str) -> str:
        return f"'{self.base}/{name}.parquet'"

    def q(self, sql: str, params: list | None = None) -> list[dict]:
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@functools.lru_cache(maxsize=1)
def default_lake() -> Lake:
    return Lake()


# ---------------------------------------------------------------- emendas

def emendas_por_autor(lake: Lake, *, ano_min: int = 2023,
                      apenas_pix: bool = False, limit: int = 25) -> list[dict]:
    """Ranking of emenda authors by money moved."""
    where = "ano >= ? AND nome_autor IS NOT NULL"
    if apenas_pix:
        where += " AND is_transferencia_especial"
    return lake.q(f"""
        SELECT e.nome_autor,
               any_value(c.uf) AS uf,
               any_value(c.deputado_id) AS deputado_id,
               any_value(c.senador_id) AS senador_id,
               count(*) AS emendas,
               round(sum(e.valor_empenhado), 2) AS empenhado,
               round(sum(e.valor_pago), 2) AS pago
        FROM {lake.table('emendas')} e
        LEFT JOIN {lake.table('autores_crosswalk')} c USING (nome_autor)
        WHERE {where}
        GROUP BY 1 ORDER BY pago DESC LIMIT ?""", [ano_min, limit])


def emendas_por_municipio(lake: Lake, codigo_ibge: str,
                          ano_min: int = 2023) -> list[dict]:
    """Every emenda that sent money to one município."""
    return lake.q(f"""
        SELECT ano, nome_autor, tipo, is_transferencia_especial AS pix,
               nome_funcao, round(valor_empenhado, 2) AS empenhado,
               round(valor_pago, 2) AS pago
        FROM {lake.table('emendas')}
        WHERE codigo_ibge_municipio = ? AND ano >= ?
        ORDER BY valor_empenhado DESC""", [codigo_ibge, ano_min])


def pix_per_capita(lake: Lake, *, ano_min: int = 2023,
                   limit: int = 25) -> list[dict]:
    """Municípios ranked by emendas-Pix money per inhabitant."""
    return lake.q(f"""
        SELECT m.nome, m.uf, m.populacao,
               round(sum(e.valor_empenhado), 2) AS empenhado,
               round(sum(e.valor_empenhado) / m.populacao, 2) AS per_capita
        FROM {lake.table('emendas')} e
        JOIN {lake.table('municipios')} m
          ON e.codigo_ibge_municipio = m.codigo_ibge
        WHERE e.is_transferencia_especial AND e.ano >= ?
          AND m.populacao > 0
        GROUP BY 1, 2, 3 ORDER BY per_capita DESC LIMIT ?""",
        [ano_min, limit])


# ------------------------------------------------------------ politicians

def politico(lake: Lake, deputado_id: int) -> dict:
    """One deputy's profile: identity, socials, emendas, CEAP totals."""
    base = lake.q(f"""
        SELECT id AS deputado_id, any_value(nome) AS nome,
               max_by(partido, legislatura) AS partido,
               max_by(uf, legislatura) AS uf,
               list(DISTINCT legislatura) AS legislaturas
        FROM {lake.table('deputados')} WHERE id = ? GROUP BY id""",
        [deputado_id])
    if not base:
        return {}
    profile = base[0]
    profile["redes"] = lake.q(f"""
        SELECT rede, handle, url FROM {lake.table('redes_sociais')}
        WHERE parlamentar_id = ? AND casa = 'camara'""", [deputado_id])
    profile["ceap"] = lake.q(f"""
        SELECT ano, round(sum(valor_liquido), 2) AS gasto
        FROM {lake.table('ceap')} WHERE deputado_id = ?
        GROUP BY ano ORDER BY ano""", [deputado_id])
    profile["emendas"] = lake.q(f"""
        SELECT e.ano, count(*) AS emendas,
               round(sum(e.valor_pago), 2) AS pago
        FROM {lake.table('emendas')} e
        JOIN {lake.table('autores_crosswalk')} c USING (nome_autor)
        WHERE c.deputado_id = ? GROUP BY 1 ORDER BY 1""", [deputado_id])
    return profile


def governismo(lake: Lake, *, min_votos: int = 200,
               limit: int = 513) -> list[dict]:
    """Deputies ranked by agreement with the Governo bancada orientation."""
    return lake.q(f"""
        WITH gov AS (
            SELECT id_votacao, orientacao FROM {lake.table('orientacoes')}
            WHERE bancada = 'Governo' AND orientacao IN ('Sim', 'Não')
        )
        SELECT v.deputado_id, any_value(d.nome) AS nome,
               max_by(v.partido, v.data_hora_voto) AS partido,
               any_value(v.uf) AS uf,
               count(*) AS votos,
               round(avg(CASE WHEN v.voto = gov.orientacao THEN 1 ELSE 0 END), 4)
                 AS governismo
        FROM {lake.table('votos')} v
        JOIN gov USING (id_votacao)
        JOIN {lake.table('deputados')} d ON d.id = v.deputado_id
        WHERE v.voto IN ('Sim', 'Não')
        GROUP BY 1 HAVING count(*) >= ?
        ORDER BY governismo DESC LIMIT ?""", [min_votos, limit])


# ------------------------------------------------------------- municípios

def municipio(lake: Lake, codigo_ibge: str) -> dict:
    """Município overview: identity, population, emendas, categories, gazettes."""
    base = lake.q(f"""
        SELECT codigo_ibge, nome, uf, regiao, populacao
        FROM {lake.table('municipios')} WHERE codigo_ibge = ?""",
        [codigo_ibge])
    if not base:
        return {}
    out = base[0]
    out["emendas_por_ano"] = lake.q(f"""
        SELECT ano, count(*) AS emendas,
               round(sum(valor_empenhado), 2) AS empenhado,
               round(sum(valor_pago), 2) AS pago,
               round(sum(CASE WHEN is_transferencia_especial
                              THEN valor_empenhado ELSE 0 END), 2) AS pix
        FROM {lake.table('emendas')}
        WHERE codigo_ibge_municipio = ? GROUP BY ano ORDER BY ano""",
        [codigo_ibge])
    out["categorias"] = lake.q(f"""
        SELECT c.categoria, round(sum(e.valor_empenhado), 2) AS empenhado
        FROM {lake.table('emendas')} e
        JOIN {lake.table('emendas_categorias')} c
          ON e.codigo_emenda = c.codigo_emenda
         AND e.codigo_acao IS NOT DISTINCT FROM c.codigo_acao
         AND e.codigo_plano_orcamentario IS NOT DISTINCT FROM c.codigo_plano_orcamentario
        WHERE e.codigo_ibge_municipio = ?
        GROUP BY 1 ORDER BY 2 DESC""", [codigo_ibge])
    try:
        out["gazetas_shows"] = lake.q(f"""
            SELECT term, data, url FROM {lake.table('gazetas')}
            WHERE codigo_ibge = ? AND (term LIKE '%show%' OR term LIKE '%art%')
            ORDER BY data DESC LIMIT 20""", [codigo_ibge])
    except Exception:
        out["gazetas_shows"] = []
    return out


def show_detector(lake: Lake, *, ano_min: int = 2023, pop_max: int = 100000,
                  limit: int = 50) -> list[dict]:
    """Municípios that received emendas Pix AND published show/artist
    contracts in their gazettes — the lead detector. Co-occurrence, not
    proof of financing source."""
    return lake.q(f"""
        WITH pix AS (
            SELECT codigo_ibge_municipio AS codigo_ibge,
                   sum(valor_empenhado) AS pix_rs,
                   any_value(nome_autor) AS um_autor
            FROM {lake.table('emendas')}
            WHERE is_transferencia_especial AND ano >= ?
              AND codigo_ibge_municipio IS NOT NULL
            GROUP BY 1
        ), shows AS (
            SELECT codigo_ibge, count(*) AS mencoes, max(data) AS ultima
            FROM {lake.table('gazetas')}
            WHERE term LIKE '%show%' OR term LIKE '%art%'
            GROUP BY 1
        )
        SELECT m.codigo_ibge, m.nome, m.uf, m.populacao,
               round(p.pix_rs, 2) AS pix,
               round(p.pix_rs / m.populacao, 2) AS per_capita,
               s.mencoes, s.ultima, p.um_autor
        FROM pix p JOIN shows s USING (codigo_ibge)
        JOIN {lake.table('municipios')} m USING (codigo_ibge)
        WHERE m.populacao > 0 AND m.populacao < ?
        ORDER BY per_capita DESC LIMIT ?""", [ano_min, pop_max, limit])


def categorias_resumo(lake: Lake, *, ano_min: int = 2021,
                      apenas_pix: bool = False) -> list[dict]:
    """Emenda money by practical category (from the enrichment layer)."""
    where = "e.ano >= ?"
    if apenas_pix:
        where += " AND e.is_transferencia_especial"
    return lake.q(f"""
        SELECT c.categoria, c.confianca, count(*) AS emendas,
               round(sum(e.valor_empenhado), 2) AS empenhado
        FROM {lake.table('emendas')} e
        JOIN {lake.table('emendas_categorias')} c
          ON e.codigo_emenda = c.codigo_emenda
         AND e.codigo_acao IS NOT DISTINCT FROM c.codigo_acao
         AND e.codigo_plano_orcamentario IS NOT DISTINCT FROM c.codigo_plano_orcamentario
        WHERE {where}
        GROUP BY 1, 2 ORDER BY empenhado DESC""", [ano_min])


def partidos(lake: Lake, *, min_votos: int = 100) -> list[dict]:
    """Per-party aggregates: size, mean governismo, mean party discipline,
    total emendas — the material for cross-party comparison charts."""
    gov = lake.q(f"""
        WITH g AS (
            SELECT id_votacao, orientacao FROM {lake.table('orientacoes')}
            WHERE bancada = 'Governo' AND orientacao IN ('Sim','Não')
        ), dep AS (
            SELECT v.deputado_id,
                   max_by(v.partido, v.data_hora_voto) AS partido,
                   avg(CASE WHEN v.voto = g.orientacao THEN 1.0 ELSE 0 END) AS governismo,
                   count(*) AS votos
            FROM {lake.table('votos')} v JOIN g USING (id_votacao)
            WHERE v.voto IN ('Sim','Não')
            GROUP BY 1 HAVING count(*) >= ?
        )
        SELECT partido, count(*) AS deputados,
               round(avg(governismo), 4) AS governismo_medio
        FROM dep WHERE partido IS NOT NULL AND partido <> ''
        GROUP BY 1 ORDER BY deputados DESC""", [min_votos])
    return gov


def comparar(lake: Lake, id_a: int, id_b: int) -> dict:
    """Two deputies side by side for a comparison view."""
    return {"a": politico(lake, id_a), "b": politico(lake, id_b)}


def top_favorecidos(lake: Lake, *, limit: int = 30,
                    apenas_empresas: bool = True) -> list[dict]:
    """Largest final recipients of emenda money, with registry activity
    (CNAE) when known — the execution-side red-flag surface."""
    where = "f.favorecido_codigo IS NOT NULL AND length(f.favorecido_codigo) = 14"
    if apenas_empresas:
        where += (" AND f.natureza_juridica NOT LIKE '%Município%'"
                  " AND f.natureza_juridica NOT LIKE '%Órgão Público%'"
                  " AND f.natureza_juridica NOT LIKE '%Fundo Público%'")
    return lake.q(f"""
        SELECT f.favorecido_codigo AS cnpj, any_value(f.favorecido) AS nome,
               any_value(f.natureza_juridica) AS natureza,
               cj.cnae_descricao AS atividade,
               round(sum(f.valor_recebido), 2) AS recebido,
               count(DISTINCT f.nome_autor) AS n_autores
        FROM {lake.table('favorecidos')} f
        LEFT JOIN {lake.table('cnpjs')} cj ON cj.cnpj = f.favorecido_codigo
        WHERE {where}
        GROUP BY 1, cj.cnae_descricao
        ORDER BY recebido DESC LIMIT ?""", [limit])


def trends_politico(lake: Lake, nome_autor: str) -> list[dict]:
    try:
        return lake.q(f"""
            SELECT date, interesse FROM {lake.table('trends')}
            WHERE nome_autor = ? ORDER BY date""", [nome_autor])
    except Exception:
        return []


# Tables safe to expose row-sampled through /tabela/{name}
BROWSABLE_TABLES = (
    "emendas", "favorecidos", "deputados", "senadores", "votacoes", "votos",
    "orientacoes", "ceap", "ceaps", "municipios", "siconfi_rreo",
    "redes_sociais", "media", "trends", "posts", "x_users", "senadores_x",
    "gazetas", "tse_candidatos", "tse_receitas", "cnpjs", "socios",
    "pix_planos_acao", "pix_planos_trabalho", "pix_finalidades", "pix_metas",
    "emendas_categorias", "autores_crosswalk",
    "indicadores_municipio", "financas_municipio", "proposicoes",
    "proposicoes_autores", "proposicoes_temas", "senado_proposicoes",
    "senado_proposicoes_autores",
)


def list_tables(lake: Lake) -> list[dict]:
    out = []
    for name in BROWSABLE_TABLES:
        try:
            n = lake.q(f"SELECT count(*) AS n FROM {lake.table(name)}")[0]["n"]
            cols = lake.q(f"SELECT * FROM {lake.table(name)} LIMIT 0")
            out.append({"tabela": name, "linhas": n,
                        "colunas": list(cols[0].keys()) if cols else []})
        except Exception:
            continue
    return out


def sample_table(lake: Lake, name: str, limit: int = 50) -> dict:
    if name not in BROWSABLE_TABLES:
        return {}
    rows = lake.q(f"SELECT * FROM {lake.table(name)} LIMIT ?", [limit])
    return {"tabela": name, "amostra": rows}


def busca(lake: Lake, q: str, limit: int = 10) -> dict:
    """Free-text lookup across politicians and municípios."""
    pattern = f"%{q.upper()}%"
    return {
        "politicos": lake.q(f"""
            SELECT id AS deputado_id, NULL AS senador_id, nome,
                   max_by(partido, legislatura) AS partido,
                   max_by(uf, legislatura) AS uf, 'camara' AS casa
            FROM {lake.table('deputados')}
            WHERE upper(strip_accents(nome)) LIKE strip_accents(?)
            GROUP BY 1, 2, 3
            UNION ALL
            SELECT NULL, codigo, any_value(nome_parlamentar),
                   NULL, max_by(uf, legislatura), 'senado'
            FROM {lake.table('senadores')}
            WHERE upper(strip_accents(nome_parlamentar)) LIKE strip_accents(?)
               OR upper(strip_accents(nome_completo)) LIKE strip_accents(?)
            GROUP BY 2
            LIMIT ?""", [pattern, pattern, pattern, limit]),
        "municipios": lake.q(f"""
            SELECT codigo_ibge, nome, uf, populacao
            FROM {lake.table('municipios')}
            WHERE upper(strip_accents(nome)) LIKE strip_accents(?)
            LIMIT ?""", [pattern, limit]),
    }


# ------------------------------------------------------------- simulador

def propostas() -> list[dict]:
    """Bills whose stated criteria can be simulated against open data."""
    from caramelo import simulador
    return [{k: p[k] for k in ("id", "titulo", "ementa", "casa", "autoria",
                               "apresentacao", "url", "resultado")}
            | {"criterios": [c["texto"] for c in p["criterios"]],
               "variantes": {g: list(v) for g, v in p["variantes"].items()}}
            for p in simulador.PROPOSTAS.values()]


def simular(lake: Lake, proposta_id: str, *,
            variantes: dict[str, str] | None = None,
            limiares: dict[str, float] | None = None,
            uf: str | None = None, incluir_municipios: bool = True) -> dict:
    """Apply one bill's criteria to every município.

    Returns the resolved criteria (with any threshold overrides), aggregate
    counts (municípios and population per outcome, per criterion) and,
    optionally, one row per município with each criterion's status and the
    measured value. Outcomes: `atende` (all criteria met), `nao_atende`
    (at least one failed), `indeterminado` (none failed, some lack data).
    """
    from caramelo import simulador
    p = simulador.PROPOSTAS.get(proposta_id)
    if not p:
        return {}
    for group, choice in (variantes or {}).items():
        if choice not in p["variantes"].get(group, {}):
            raise ValueError(f"variante inválida: {group}={choice}")
    criterios = simulador.resolve(p, variantes, limiares)
    cols = ",\n".join(
        f"{simulador.criterio_sql(c)} AS \"s_{c['id']}\", "
        f"round(({simulador.METRICAS[c['metrica']][0]})::DOUBLE, 2) "
        f"AS \"v_{c['id']}\""
        for c in criterios)
    status = [f"\"s_{c['id']}\"" for c in criterios]
    where = "WHERE uf = ?" if uf else ""
    rows = lake.q(f"""
        WITH painel AS ({simulador.painel_sql(lake.table)}),
        av AS (SELECT cod_ibge, nome, uf, populacao_censo2022 AS populacao,
                      {cols} FROM painel {where})
        SELECT *,
               CASE WHEN list_contains([{', '.join(status)}], 'nao_atende')
                      THEN 'nao_atende'
                    WHEN list_contains([{', '.join(status)}], 'sem_dado')
                      THEN 'indeterminado'
                    ELSE 'atende' END AS resultado
        FROM av ORDER BY uf, nome""", [uf.upper()] if uf else [])

    def tally(key: str) -> dict:
        out: dict[str, dict] = {}
        for r in rows:
            t = out.setdefault(r[key], {"municipios": 0, "populacao": 0})
            t["municipios"] += 1
            t["populacao"] += int(r["populacao"] or 0)
        return out

    variante = {**p["variante_padrao"], **(variantes or {})}
    result = {
        "proposta": {k: p[k] for k in ("id", "titulo", "ementa", "autoria",
                                       "url", "resultado", "nao_mensuraveis",
                                       "notas")},
        "variante": variante,
        "uf": uf.upper() if uf else None,
        "criterios": [{k: c[k] for k in ("id", "texto", "metrica",
                                         "metrica_label", "unidade", "op",
                                         "limiar", "limiar_original")}
                      | {"resumo": tally(f"s_{c['id']}")}
                      for c in criterios],
        "resumo": tally("resultado"),
        "fontes": "indicadores_municipio, financas_municipio, municipios "
                  "(https://caramelo.dev.br/docs/data.md)",
    }
    if incluir_municipios:
        result["municipios"] = [
            {"cod_ibge": r["cod_ibge"], "nome": r["nome"], "uf": r["uf"],
             "populacao": r["populacao"], "resultado": r["resultado"],
             "criterios": {c["id"]: {"status": r[f"s_{c['id']}"],
                                     "valor": r[f"v_{c['id']}"]}
                           for c in criterios}}
            for r in rows]
    return result


def simulacoes_municipio(lake: Lake, codigo_ibge: str) -> list[dict]:
    """Every simulable bill evaluated for one município."""
    from caramelo import simulador
    out = []
    for pid in simulador.PROPOSTAS:
        r = simular(lake, pid)
        m = next((x for x in r["municipios"] if x["cod_ibge"] == codigo_ibge),
                 None)
        if m:
            out.append({"proposta": r["proposta"]["titulo"], "id": pid,
                        "resultado": m["resultado"],
                        "criterios": m["criterios"],
                        "variante": r["variante"]})
    return out


# ------------------------------------------------------------ proposições

def proposicoes(lake: Lake, *, deputado_id: int | None = None,
                tema: str | None = None, q: str | None = None,
                sigla_tipo: str | None = None, limit: int = 50) -> list[dict]:
    """Câmara bills, filterable by author, theme, type or ementa text."""
    where, params = ["true"], []
    if deputado_id is not None:
        where.append(f"""p.id IN (SELECT id_proposicao FROM
            {lake.table('proposicoes_autores')} WHERE id_deputado = ?)""")
        params.append(deputado_id)
    if tema:
        where.append(f"""p.id IN (SELECT id_proposicao FROM
            {lake.table('proposicoes_temas')}
            WHERE strip_accents(lower(tema)) LIKE strip_accents(lower(?)))""")
        params.append(f"%{tema}%")
    if q:
        where.append("strip_accents(lower(coalesce(p.ementa,'') || ' ' || "
                     "coalesce(p.keywords,''))) LIKE strip_accents(lower(?))")
        params.append(f"%{q}%")
    if sigla_tipo:
        where.append("p.sigla_tipo = ?")
        params.append(sigla_tipo.upper())
    params.append(limit)
    return lake.q(f"""
        SELECT p.id, p.sigla_tipo || ' ' || p.numero || '/' || p.ano AS sigla,
               p.ementa, p.data_apresentacao, p.status_situacao,
               p.status_orgao, p.status_data, p.url,
               (SELECT list(a.nome_autor ORDER BY a.ordem_assinatura)
                FROM {lake.table('proposicoes_autores')} a
                WHERE a.id_proposicao = p.id) AS autores,
               (SELECT count(*) FROM {lake.table('votacoes')} v
                WHERE v.proposicao_id = p.id) AS votacoes
        FROM {lake.table('proposicoes')} p
        WHERE {' AND '.join(where)}
        ORDER BY p.data_apresentacao DESC NULLS LAST LIMIT ?""", params)


def proposicao(lake: Lake, proposicao_id: int) -> dict:
    """One Câmara bill: text, authors, themes, attached bills, roll calls."""
    base = lake.q(f"SELECT * FROM {lake.table('proposicoes')} WHERE id = ?",
                  [proposicao_id])
    if not base:
        return {}
    return {
        **base[0],
        "autores": lake.q(f"""
            SELECT nome_autor, partido, uf, id_deputado, tipo_autor,
                   proponente FROM {lake.table('proposicoes_autores')}
            WHERE id_proposicao = ? ORDER BY ordem_assinatura""",
            [proposicao_id]),
        "temas": [r["tema"] for r in lake.q(f"""
            SELECT tema FROM {lake.table('proposicoes_temas')}
            WHERE id_proposicao = ? ORDER BY relevancia DESC""",
            [proposicao_id])],
        "apensadas": lake.q(f"""
            SELECT id, sigla_tipo || ' ' || numero || '/' || ano AS sigla,
                   ementa FROM {lake.table('proposicoes')}
            WHERE id_principal = ? ORDER BY data_apresentacao""",
            [proposicao_id]),
        "votacoes": lake.q(f"""
            SELECT id, data, sigla_orgao, descricao, aprovacao,
                   votos_sim, votos_nao, votos_outros
            FROM {lake.table('votacoes')} WHERE proposicao_id = ?
            ORDER BY data DESC""", [proposicao_id]),
    }
