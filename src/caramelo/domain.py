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
