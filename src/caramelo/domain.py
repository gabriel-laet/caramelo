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
        self._has: dict[str, tuple[float, bool]] = {}
        if self.base.startswith("http"):
            self.con.execute("INSTALL httpfs; LOAD httpfs;")

    def table(self, name: str) -> str:
        return f"'{self.base}/{name}.parquet'"

    def has(self, name: str) -> bool:
        """Is `name` published? Cached for 10 minutes (the lake changes
        once a day) so a newly published table shows up soon after."""
        import time
        hit = self._has.get(name)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        try:
            self.con.cursor().execute(
                f"SELECT 1 FROM {self.table(name)} LIMIT 0")
            ok = True
        except duckdb.Error:
            ok = False
        self._has[name] = (time.monotonic(), ok)
        return ok

    def emendas(self) -> str:
        """`emendas` with each Pix emenda split across the entities that
        received it, per TransfereGov's planos de ação (see
        caramelo.resolution.pix): the Portal files ~93% of Pix money under
        "MÚLTIPLO". Values are prorated from the Portal's own totals by each
        plan's share, so sums by author or year are unchanged. Use this for
        anything grouped or filtered by município; `fonte_destino` says
        which source placed the row. Plain `emendas` until
        pix_beneficiarios is published."""
        if not self.has("pix_beneficiarios"):
            return f"(SELECT *, 'portal' AS fonte_destino FROM {self.table('emendas')})"
        e, b, m = (self.table(t) for t in
                   ("emendas", "pix_beneficiarios", "municipios"))
        share = "b.share"
        vals = ", ".join(f"e.{c} * {share} AS {c}" for c in (
            "valor_empenhado", "valor_liquidado", "valor_pago",
            "valor_rap_inscritos", "valor_rap_cancelados", "valor_rap_pagos"))
        return f"""(
            WITH b AS (
                SELECT codigo_emenda, cod_ibge, esfera, uf AS b_uf,
                       nome_beneficiario,
                       valor_total / nullif(sum(valor_total) OVER (
                           PARTITION BY codigo_emenda), 0) AS share
                FROM {b} WHERE situacao = 'CIENTE' AND valor_total > 0)
            SELECT *, 'portal' AS fonte_destino FROM {e}
            WHERE NOT (is_transferencia_especial
                       AND codigo_emenda IN (SELECT codigo_emenda FROM b))
            UNION ALL BY NAME
            SELECT e.* REPLACE (
                       CASE WHEN b.esfera = 'municipio' THEN b.cod_ibge END
                         AS codigo_ibge_municipio,
                       mm.nome AS municipio, b.b_uf AS uf,
                       coalesce(mm.nome, b.nome_beneficiario) AS localidade,
                       {vals}),
                   'transferegov' AS fonte_destino
            FROM {e} e JOIN b USING (codigo_emenda)
            LEFT JOIN {m} mm
              ON b.esfera = 'municipio' AND mm.codigo_ibge = b.cod_ibge
            WHERE e.is_transferencia_especial)"""

    def autoria(self, source: str | None = None) -> str:
        """`source` (default: plain emendas) plus who wrote each emenda:
        `casa` is 'camara' or 'senado' for individual emendas (the house
        the author sat in when the budget was voted, the year before `ano`;
        people who moved between houses are split correctly), or
        'bancada' / 'comissao' / 'relator' for collective ones, with the
        author's `deputado_id` / `senador_id` from autores_crosswalk."""
        src = source or self.table("emendas")
        c, s, d = (self.table(t) for t in
                   ("autores_crosswalk", "senadores", "deputados"))
        # legislatura sitting in the year the LOA was voted (ano - 1):
        # 56 = 2019-2022, 57 = 2023-2026, ...
        leg = "(CAST(floor((e.ano - 1 - 2019) / 4) AS INTEGER) + 56)"
        return f"""(
            SELECT e.*, c.deputado_id, c.senador_id, c.uf AS autor_uf,
                   CASE
                     WHEN e.tipo ILIKE '%bancada%' THEN 'bancada'
                     WHEN e.tipo ILIKE '%comiss%' THEN 'comissao'
                     WHEN e.tipo ILIKE '%relator%' THEN 'relator'
                     WHEN c.senador_id IN (SELECT codigo FROM {s}
                                           WHERE legislatura = {leg})
                       THEN 'senado'
                     WHEN c.deputado_id IN (SELECT id FROM {d}
                                            WHERE legislatura = {leg})
                       THEN 'camara'
                     WHEN c.senador_id IS NOT NULL AND c.deputado_id IS NULL
                       THEN 'senado'
                     WHEN c.deputado_id IS NOT NULL THEN 'camara'
                   END AS casa
            FROM {src} e
            LEFT JOIN {c} c USING (nome_autor))"""

    def q(self, sql: str, params: list | None = None) -> list[dict]:
        # One cursor per query: the API serves requests from a thread pool
        # and a DuckDB connection refuses concurrent calls.
        cur = self.con.cursor().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@functools.lru_cache(maxsize=1)
def default_lake() -> Lake:
    return Lake()


# ---------------------------------------------------------------- emendas

def emendas_por_autor(lake: Lake, *, ano_min: int = 2023,
                      apenas_pix: bool = False, casa: str | None = None,
                      limit: int = 25) -> list[dict]:
    """Ranking of emenda authors by money moved. `casa` filters to
    'camara', 'senado' (individual emendas by house) or 'coletivas'
    (bancada, comissão, relator); each row says its `casa`."""
    where = "ano >= ? AND nome_autor IS NOT NULL"
    params: list = [ano_min]
    if apenas_pix:
        where += " AND is_transferencia_especial"
    if casa == "coletivas":
        where += " AND casa IN ('bancada', 'comissao', 'relator')"
    elif casa:
        where += " AND casa = ?"
        params.append(casa)
    return lake.q(f"""
        SELECT nome_autor,
               arg_max(casa, ano) AS casa,
               any_value(autor_uf) AS uf,
               any_value(deputado_id) AS deputado_id,
               any_value(senador_id) AS senador_id,
               count(*) AS emendas,
               round(sum(valor_empenhado), 2) AS empenhado,
               round(sum(valor_pago), 2) AS pago
        FROM {lake.autoria()}
        WHERE {where}
        GROUP BY 1 ORDER BY pago DESC LIMIT ?""", [*params, limit])


def emendas_por_municipio(lake: Lake, codigo_ibge: str,
                          ano_min: int = 2023) -> list[dict]:
    """Every emenda that sent money to one município."""
    return lake.q(f"""
        SELECT ano, nome_autor, tipo, is_transferencia_especial AS pix,
               nome_funcao, round(valor_empenhado, 2) AS empenhado,
               round(valor_pago, 2) AS pago
        FROM {lake.emendas()}
        WHERE codigo_ibge_municipio = ? AND ano >= ?
        ORDER BY valor_empenhado DESC""", [codigo_ibge, ano_min])


def pix_per_capita(lake: Lake, *, ano_min: int = 2023,
                   limit: int = 25) -> list[dict]:
    """Municípios ranked by emendas-Pix money per inhabitant."""
    return lake.q(f"""
        SELECT m.nome, m.uf, m.populacao,
               round(sum(e.valor_empenhado), 2) AS empenhado,
               round(sum(e.valor_empenhado) / m.populacao, 2) AS per_capita
        FROM {lake.emendas()} e
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
    profile["casa"] = "camara"
    profile["senador_id"] = (lake.q(f"""
        SELECT senador_id FROM {lake.table('autores_crosswalk')}
        WHERE deputado_id = ? AND senador_id IS NOT NULL LIMIT 1""",
        [deputado_id]) or [{}])[0].get("senador_id")
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
        FROM {lake.autoria()} e
        WHERE e.deputado_id = ? AND e.casa = 'camara'
        GROUP BY 1 ORDER BY 1""", [deputado_id])
    # Destination: the Portal's município, or for Pix the beneficiary from
    # TransfereGov (Lake.emendas); what neither resolves stays as the
    # Portal's localidade ("MÚLTIPLO", a whole UF).
    profile["destinos"] = lake.q(f"""
        SELECT e.codigo_ibge_municipio AS codigo_ibge,
               coalesce(any_value(e.municipio), any_value(e.localidade))
                 AS nome, any_value(e.uf) AS uf,
               count(*) AS emendas,
               round(sum(e.valor_empenhado), 2) AS empenhado,
               round(sum(CASE WHEN e.is_transferencia_especial
                              THEN e.valor_empenhado ELSE 0 END), 2) AS pix
        FROM {lake.autoria(lake.emendas())} e
        WHERE e.deputado_id = ? AND e.casa = 'camara' AND e.ano >= 2023
        GROUP BY e.codigo_ibge_municipio,
                 CASE WHEN e.codigo_ibge_municipio IS NULL THEN e.localidade END
        ORDER BY empenhado DESC LIMIT 15""", [deputado_id])
    gov = lake.q(f"""
        WITH gov AS (
            SELECT id_votacao, orientacao FROM {lake.table('orientacoes')}
            WHERE bancada = 'Governo' AND orientacao IN ('Sim', 'Não'))
        SELECT count(*) AS votos,
               round(avg(CASE WHEN v.voto = gov.orientacao THEN 1 ELSE 0 END),
                     4) AS governismo
        FROM {lake.table('votos')} v JOIN gov USING (id_votacao)
        WHERE v.deputado_id = ? AND v.voto IN ('Sim', 'Não')""",
        [deputado_id])
    profile["governismo"] = gov[0] if gov and gov[0]["votos"] else None
    try:
        profile["proposicoes_resumo"] = {
            "por_tipo": lake.q(f"""
                SELECT p.sigla_tipo, count(*) AS n
                FROM {lake.table('proposicoes_autores')} a
                JOIN {lake.table('proposicoes')} p ON p.id = a.id_proposicao
                WHERE a.id_deputado = ? GROUP BY 1 ORDER BY 2 DESC""",
                [deputado_id]),
            "temas": lake.q(f"""
                SELECT t.tema, count(*) AS n
                FROM {lake.table('proposicoes_autores')} a
                JOIN {lake.table('proposicoes_temas')} t
                  ON t.id_proposicao = a.id_proposicao
                WHERE a.id_deputado = ? GROUP BY 1 ORDER BY 2 DESC
                LIMIT 8""", [deputado_id]),
        }
    except Exception:  # proposições not published yet
        profile["proposicoes_resumo"] = {"por_tipo": [], "temas": []}
    return profile


def _ceaps_por_senador(lake: Lake) -> str:
    """CEAPS rows with the senator's código (the file only has names)."""
    return f"""(
        SELECT x.*, s.codigo AS senador_id
        FROM {lake.table('ceaps')} x
        LEFT JOIN (SELECT codigo,
                          any_value(upper(strip_accents(nome_parlamentar))) AS np,
                          any_value(upper(strip_accents(nome_completo))) AS nc
                   FROM {lake.table('senadores')} GROUP BY 1) s
          ON upper(strip_accents(x.senador_nome)) IN (s.np, s.nc))"""


def senador(lake: Lake, senador_id: int) -> dict:
    """One senator's profile: identity, emendas written as senator,
    destinations, CEAPS by year, Senate bills and roll-call votes."""
    base = lake.q(f"""
        SELECT codigo AS senador_id, any_value(nome_parlamentar) AS nome,
               any_value(nome_completo) AS nome_completo,
               max_by(uf, legislatura) AS uf,
               list(DISTINCT legislatura ORDER BY legislatura) AS legislaturas
        FROM {lake.table('senadores')} WHERE codigo = ? GROUP BY 1""",
        [senador_id])
    if not base:
        return {}
    p = base[0]
    p["casa"] = "senado"
    p["deputado_id"] = (lake.q(f"""
        SELECT deputado_id FROM {lake.table('autores_crosswalk')}
        WHERE senador_id = ? AND deputado_id IS NOT NULL LIMIT 1""",
        [senador_id]) or [{}])[0].get("deputado_id")
    p["emendas"] = lake.q(f"""
        SELECT ano, count(*) AS emendas, round(sum(valor_pago), 2) AS pago
        FROM {lake.autoria()} WHERE senador_id = ? AND casa = 'senado'
        GROUP BY 1 ORDER BY 1""", [senador_id])
    p["destinos"] = lake.q(f"""
        SELECT e.codigo_ibge_municipio AS codigo_ibge,
               coalesce(any_value(e.municipio), any_value(e.localidade))
                 AS nome, any_value(e.uf) AS uf, count(*) AS emendas,
               round(sum(e.valor_empenhado), 2) AS empenhado,
               round(sum(CASE WHEN e.is_transferencia_especial
                              THEN e.valor_empenhado ELSE 0 END), 2) AS pix
        FROM {lake.autoria(lake.emendas())} e
        WHERE e.senador_id = ? AND e.casa = 'senado' AND e.ano >= 2023
        GROUP BY e.codigo_ibge_municipio,
                 CASE WHEN e.codigo_ibge_municipio IS NULL THEN e.localidade END
        ORDER BY empenhado DESC LIMIT 15""", [senador_id])
    try:
        p["ceaps"] = lake.q(f"""
            SELECT ano, round(sum(valor_reembolsado), 2) AS gasto
            FROM {_ceaps_por_senador(lake)} WHERE senador_id = ?
            GROUP BY 1 ORDER BY 1""", [senador_id])
    except Exception:
        p["ceaps"] = []
    try:
        p["votos"] = lake.q(f"""
            SELECT CASE WHEN voto IN ('Sim', 'Não') THEN voto
                        WHEN voto IN ('Votou', 'Abstenção') THEN 'Outro voto'
                        WHEN voto LIKE 'P-%' OR voto LIKE 'Presidente%'
                          THEN 'Presente sem votar'
                        ELSE 'Ausente ou licenciado' END AS tipo,
                   count(*) AS n
            FROM {lake.table('senado_votos')}
            WHERE senador_id = ? AND data_sessao >= '2023-02-01'
            GROUP BY 1 ORDER BY 2 DESC""", [senador_id])
    except Exception:
        p["votos"] = []
    try:
        p["proposicoes_resumo"] = {"por_tipo": lake.q(f"""
            SELECT sp.sigla_tipo, count(*) AS n
            FROM {lake.table('senado_proposicoes_autores')} a
            JOIN {lake.table('senado_proposicoes')} sp ON sp.id = a.id_processo
            WHERE a.codigo_senador = ? AND sp.ano >= 2023
            GROUP BY 1 ORDER BY 2 DESC""", [senador_id])}
        p["proposicoes"] = lake.q(f"""
            SELECT sp.id, sp.identificacao AS sigla, sp.ementa,
                   sp.data_apresentacao, sp.situacao_atual, sp.tramitando,
                   sp.url
            FROM {lake.table('senado_proposicoes_autores')} a
            JOIN {lake.table('senado_proposicoes')} sp ON sp.id = a.id_processo
            WHERE a.codigo_senador = ?
            ORDER BY sp.data_apresentacao DESC NULLS LAST LIMIT 12""",
            [senador_id])
    except Exception:
        p["proposicoes_resumo"], p["proposicoes"] = {"por_tipo": []}, []
    return p


def cota(lake: Lake, *, casa: str = "camara", ano_min: int = 2023,
         limit: int = 30) -> list[dict]:
    """Parliamentarians ranked by expense-quota spending: CEAP for
    deputados, CEAPS for senadores (amounts reimbursed/net)."""
    if casa == "senado":
        return lake.q(f"""
            SELECT senador_id, any_value(senador_nome) AS nome,
                   NULL AS partido,
                   (SELECT max_by(uf, legislatura) FROM {lake.table('senadores')}
                    WHERE codigo = x.senador_id) AS uf,
                   round(sum(valor_reembolsado), 2) AS gasto,
                   count(DISTINCT ano::INT * 100 + mes) AS meses
            FROM {_ceaps_por_senador(lake)} x
            WHERE ano >= ? AND senador_id IS NOT NULL
            GROUP BY 1 ORDER BY gasto DESC LIMIT ?""", [ano_min, limit])
    return lake.q(f"""
        SELECT deputado_id, any_value(nome_parlamentar) AS nome,
               max_by(partido, ano::INT * 100 + mes) AS partido,
               max_by(uf, ano::INT * 100 + mes) AS uf,
               round(sum(valor_liquido), 2) AS gasto,
               count(DISTINCT ano::INT * 100 + mes) AS meses
        FROM {lake.table('ceap')}
        WHERE ano >= ? AND deputado_id IS NOT NULL
        GROUP BY 1 ORDER BY gasto DESC LIMIT ?""", [ano_min, limit])


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
        FROM {lake.emendas()}
        WHERE codigo_ibge_municipio = ? GROUP BY ano ORDER BY ano""",
        [codigo_ibge])
    try:
        out["categorias"] = lake.q(f"""
            SELECT c.categoria, round(sum(e.valor_empenhado), 2) AS empenhado
            FROM {lake.emendas()} e
            JOIN {lake.table('emendas_categorias')} c
              ON e.codigo_emenda = c.codigo_emenda
             AND e.codigo_acao IS NOT DISTINCT FROM c.codigo_acao
             AND e.codigo_plano_orcamentario IS NOT DISTINCT FROM c.codigo_plano_orcamentario
            WHERE e.codigo_ibge_municipio = ?
            GROUP BY 1 ORDER BY 2 DESC""", [codigo_ibge])
        out["categorias_fonte"] = "emendas_categorias"
    except Exception:  # enrichment not published: fall back to the
        # budget function each emenda already declares
        out["categorias"] = lake.q(f"""
            SELECT coalesce(nome_funcao, 'Não informada') AS categoria,
                   round(sum(valor_empenhado), 2) AS empenhado
            FROM {lake.emendas()}
            WHERE codigo_ibge_municipio = ?
            GROUP BY 1 ORDER BY 2 DESC""", [codigo_ibge])
        out["categorias_fonte"] = "emendas.nome_funcao"
    try:
        out["gazetas_shows"] = lake.q(f"""
            SELECT term, data, url FROM {lake.table('gazetas')}
            WHERE codigo_ibge = ? AND (term LIKE '%show%' OR term LIKE '%art%')
            ORDER BY data DESC LIMIT 20""", [codigo_ibge])
    except Exception:
        out["gazetas_shows"] = []
    out["autores"] = lake.q(f"""
        SELECT e.nome_autor, arg_max(e.casa, e.ano) AS casa,
               any_value(e.deputado_id) AS deputado_id,
               any_value(e.senador_id) AS senador_id,
               any_value(e.autor_uf) AS uf, count(*) AS emendas,
               round(sum(e.valor_empenhado), 2) AS empenhado,
               round(sum(CASE WHEN e.is_transferencia_especial
                              THEN e.valor_empenhado ELSE 0 END), 2) AS pix
        FROM {lake.autoria(lake.emendas())} e
        WHERE e.codigo_ibge_municipio = ? AND e.ano >= 2023
          AND e.nome_autor IS NOT NULL
        GROUP BY 1 ORDER BY empenhado DESC LIMIT 15""", [codigo_ibge])
    for key, sql in (
            ("indicadores", f"""
                SELECT indicador, valor, unidade, referencia, fonte, url
                FROM {lake.table('indicadores_municipio')}
                WHERE cod_ibge = ? ORDER BY indicador"""),
            ("financas", f"""
                SELECT * EXCLUDE (cod_ibge, instituicao, uf, fonte)
                FROM {lake.table('financas_municipio')}
                WHERE cod_ibge = ? ORDER BY exercicio""")):
        try:
            out[key] = lake.q(sql, [codigo_ibge])
        except Exception:  # table not published yet
            out[key] = []
    return out


def show_detector(lake: Lake, *, ano_min: int = 2023, pop_max: int = 100000,
                  limit: int = 50) -> list[dict]:
    """Municípios that received emendas Pix AND published show/artist
    contracts in their gazettes — the lead detector. Co-occurrence, not
    proof of financing source. Empty until gazetas is published."""
    try:
        lake.q(f"SELECT 1 FROM {lake.table('gazetas')} LIMIT 1")
    except Exception:
        return []
    return lake.q(f"""
        WITH pix AS (
            SELECT codigo_ibge_municipio AS codigo_ibge,
                   sum(valor_empenhado) AS pix_rs,
                   any_value(nome_autor) AS um_autor
            FROM {lake.emendas()}
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
# table -> (group, description in Portuguese); also the browsable allow-list
TABLE_CATALOG: dict[str, tuple[str, str]] = {
    # emendas
    "emendas": ("Emendas parlamentares", "Todas as emendas ao Orçamento desde 2014: autor, tipo, função, local e valores empenhado/liquidado/pago (Portal da Transparência)."),
    "emendas_categorias": ("Emendas parlamentares", "Categoria prática de cada emenda (saúde, pavimentação, eventos…), por regras e IA."),
    "favorecidos": ("Emendas parlamentares", "Quem recebeu o dinheiro de cada emenda, por mês: CNPJ/CPF, natureza jurídica, UF e município do recebedor."),
    "emendas_recebidas_municipio": ("Emendas parlamentares", "Derivada: valor pago à prefeitura e aos fundos municipais de cada município, por ano e tipo de emenda."),
    "pix_beneficiarios": ("Emendas parlamentares", "Derivada: destino de cada emenda Pix (beneficiário do plano de ação), com código IBGE."),
    "pix_planos_acao": ("Emendas parlamentares", "Planos de ação das emendas Pix no TransfereGov (um por emenda × beneficiário)."),
    "pix_planos_trabalho": ("Emendas parlamentares", "Planos de trabalho das emendas Pix (TransfereGov)."),
    "pix_finalidades": ("Emendas parlamentares", "Finalidades declaradas das emendas Pix (TransfereGov)."),
    "pix_metas": ("Emendas parlamentares", "Metas físicas declaradas das emendas Pix (TransfereGov)."),
    "autores_crosswalk": ("Emendas parlamentares", "Derivada: liga o nome do autor da emenda ao deputado e/ou senador."),
    # Câmara
    "deputados": ("Câmara dos Deputados", "Deputados das legislaturas 55 a 57: nome, partido, UF."),
    "votacoes": ("Câmara dos Deputados", "Votações nominais da Câmara desde 2023."),
    "votos": ("Câmara dos Deputados", "Voto de cada deputado em cada votação nominal."),
    "orientacoes": ("Câmara dos Deputados", "Orientação de cada bancada (partidos, Governo, Oposição) em cada votação."),
    "ceap": ("Câmara dos Deputados", "Cota parlamentar (CEAP) dos deputados: cada despesa, fornecedor e valor, desde 2023."),
    "proposicoes": ("Câmara dos Deputados", "Projetos apresentados na Câmara desde 2023 (PL, PLP, PEC, PDL, MPV…) e situação atual."),
    "proposicoes_autores": ("Câmara dos Deputados", "Autores de cada proposição da Câmara."),
    "proposicoes_temas": ("Câmara dos Deputados", "Temas de cada proposição da Câmara."),
    "redes_sociais": ("Câmara dos Deputados", "Redes sociais declaradas nos perfis da Câmara."),
    # Senado
    "senadores": ("Senado Federal", "Senadores das legislaturas 55 a 57: nome, UF."),
    "senado_votos": ("Senado Federal", "Voto de cada senador nas votações nominais do plenário."),
    "ceaps": ("Senado Federal", "Cota parlamentar dos senadores (CEAPS): cada despesa reembolsada, desde 2023."),
    "senado_proposicoes": ("Senado Federal", "Projetos de senadores da 57ª legislatura (PL, PLP, PEC, PDL, PRS) e situação."),
    "senado_proposicoes_autores": ("Senado Federal", "Autores (senadores) de cada projeto do Senado."),
    "senadores_x": ("Senado Federal", "Contas no X (Twitter) dos senadores."),
    # municípios
    "municipios": ("Municípios", "Os 5.571 municípios (IBGE) com UF, região e população."),
    "indicadores_municipio": ("Municípios", "Indicadores por município em formato longo: água, esgoto, favelas (Censo 2022), APS, Ideb, SINISA — cada linha com fonte e período."),
    "financas_municipio": ("Municípios", "Contas anuais (DCA/SICONFI) por município: receitas, FPM, IPTU/ISS/ITBI, despesa por função."),
    "siconfi_rreo": ("Municípios", "Relatório Resumido da Execução Orçamentária (RREO) dos estados."),
    "gazetas": ("Municípios", "Menções a termos (show, contratação artística…) nos diários oficiais municipais (Querido Diário)."),
    # eleições e empresas
    "tse_candidatos": ("Eleições e empresas", "Candidaturas (TSE)."),
    "tse_receitas": ("Eleições e empresas", "Receitas de campanha declaradas ao TSE."),
    "cnpjs": ("Eleições e empresas", "Cadastro (Receita Federal) das empresas que receberam emendas."),
    "socios": ("Eleições e empresas", "Quadro societário dessas empresas."),
    # mídia
    "media": ("Mídia e redes", "Notícias que citam parlamentares (snapshots diários)."),
    "trends": ("Mídia e redes", "Buscas em alta (Google Trends) sobre política."),
    "posts": ("Mídia e redes", "Posts de parlamentares no X."),
    "x_users": ("Mídia e redes", "Perfis no X de parlamentares."),
    "youtube_canais": ("Mídia e redes", "Canais de YouTube de deputados e senadores."),
    "youtube_videos": ("Mídia e redes", "Vídeos desses canais: título, duração, data."),
    "youtube_stats": ("Mídia e redes", "Visualizações, curtidas e comentários de cada vídeo ao longo do tempo."),
    "entrevistas": ("Mídia e redes", "Entrevistas (ex.: Roda Viva) com políticos."),
    "debates": ("Mídia e redes", "Debates presidenciais e eleitorais gravados."),
    "transcricoes": ("Mídia e redes", "Transcrição em texto de debates e entrevistas (Whisper)."),
}
BROWSABLE_TABLES = tuple(TABLE_CATALOG)

_TABLES_CACHE: dict[str, tuple[float, list]] = {}


def list_tables(lake: Lake) -> list[dict]:
    """Every published table with group, description, row count and
    column names/types. Cached for an hour (the lake changes daily)."""
    import time
    hit = _TABLES_CACHE.get(lake.base)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        name, (grupo, descricao) = item
        try:  # Parquet footers only: row count and schema, no data scan
            cur = lake.con.cursor()
            n = cur.execute(
                f"SELECT count(*) FROM {lake.table(name)}").fetchone()[0]
            cols = cur.execute(
                f"DESCRIBE SELECT * FROM {lake.table(name)}").fetchall()
        except Exception:
            return None
        return {"tabela": name, "grupo": grupo, "descricao": descricao,
                "linhas": n, "colunas": [c[0] for c in cols],
                "tipos": [c[1] for c in cols]}

    with ThreadPoolExecutor(max_workers=12) as pool:
        out = [r for r in pool.map(one, TABLE_CATALOG.items()) if r]
    _TABLES_CACHE[lake.base] = (time.monotonic(), out)
    return out


def sample_table(lake: Lake, name: str, limit: int = 50) -> dict:
    if name not in BROWSABLE_TABLES:
        return {}
    rows = lake.q(f"SELECT * FROM {lake.table(name)} LIMIT ?", [limit])
    grupo, descricao = TABLE_CATALOG[name]
    return {"tabela": name, "grupo": grupo, "descricao": descricao,
            "amostra": rows}


def busca(lake: Lake, q: str, limit: int = 10) -> dict:
    """Free-text lookup across politicians and municípios."""
    pattern = f"%{q.upper()}%"
    return {
        "politicos": lake.q(f"""
            SELECT * FROM (
                SELECT id AS deputado_id, NULL::BIGINT AS senador_id,
                       max_by(nome, legislatura) AS nome,
                       max_by(partido, legislatura) AS partido,
                       max_by(uf, legislatura) AS uf, 'camara' AS casa,
                       max(legislatura) AS ultima_legislatura
                FROM {lake.table('deputados')}
                WHERE upper(strip_accents(nome)) LIKE strip_accents(?)
                GROUP BY id
                UNION ALL
                SELECT NULL, codigo, max_by(nome_parlamentar, legislatura),
                       NULL, max_by(uf, legislatura), 'senado',
                       max(legislatura)
                FROM {lake.table('senadores')}
                WHERE upper(strip_accents(nome_parlamentar)) LIKE strip_accents(?)
                   OR upper(strip_accents(nome_completo)) LIKE strip_accents(?)
                GROUP BY codigo)
            ORDER BY ultima_legislatura DESC, nome
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
                               "apresentacao", "url", "resultado",
                               "proposicao_id")}
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
        WITH painel AS ({simulador.painel_sql(lake.table, lake.has)}),
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


PAINEL_METRICAS: dict[str, tuple[str, str]] = {
    # column -> (label, unit); the client builds map layers from these
    "populacao": ("População (Censo 2022)", "pessoas"),
    "pix_per_capita": ("Emendas Pix por habitante (2023+)", "R$/hab"),
    "emendas_per_capita": ("Todas as emendas por habitante (2023+)", "R$/hab"),
    "recebido_prefeitura_per_capita": (
        "Emendas recebidas pela prefeitura por habitante (2023+)",
        "R$/hab"),
    "fpm_pct_receita": ("FPM sobre a receita total (DCA)", "%"),
    "impostos_proprios_pct_receita": ("IPTU + ISS + ITBI sobre a receita (DCA)", "%"),
    "cultura_per_capita": ("Despesa com Cultura por habitante (DCA)", "R$/hab"),
    "agua_rede_censo2022_pct": ("Água por rede geral (Censo 2022)", "%"),
    "esgoto_rede_censo2022_pct": ("Esgoto por rede (Censo 2022)", "%"),
    "aps_cobertura_pct": ("Cobertura potencial da APS", "%"),
    "ideb_ai_municipal": ("Ideb anos iniciais, rede municipal (2025)", "índice"),
    "favela_pct": ("População em favelas (Censo 2022)", "%"),
    "shows_diario": ("Menções a shows no diário oficial", "menções"),
}


_PAINEL_CACHE: dict[str, tuple[float, dict]] = {}
PAINEL_TTL = 3600.0


def painel(lake: Lake) -> dict:
    """Every município in one compact, columnar payload: the indicators,
    latest DCA ratios, emendas per capita and gazette show mentions.
    Built for maps and client-side comparison (one request, ~5.6k rows).
    Cached per lake for an hour: the lake changes once a day."""
    import time
    hit = _PAINEL_CACHE.get(lake.base)
    if hit and time.monotonic() - hit[0] < PAINEL_TTL:
        return hit[1]
    out = _painel(lake)
    # A degraded panel (tables not published yet) is cached for 10 minutes
    # only, so the full one appears soon after the next publish.
    degraded = out["faltando"] and out["faltando"] != ["gazetas"]
    ttl_start = time.monotonic() - (PAINEL_TTL - 600 if degraded else 0)
    _PAINEL_CACHE[lake.base] = (ttl_start, out)
    return out


def _painel(lake: Lake) -> dict:
    from caramelo import simulador
    if lake.has("gazetas"):
        shows = f"""(SELECT codigo_ibge AS cod_ibge, count(*) AS n
                     FROM {lake.table('gazetas')}
                     WHERE term LIKE '%show%' OR term LIKE '%art%'
                     GROUP BY 1)"""
    else:  # gazetas not published yet
        shows = "(SELECT NULL::VARCHAR AS cod_ibge, NULL::BIGINT AS n WHERE false)"
    if lake.has("emendas_recebidas_municipio"):
        recebido = f"""(SELECT cod_ibge, sum(valor_recebido) AS valor
                        FROM {lake.table('emendas_recebidas_municipio')}
                        WHERE ano >= 2023 GROUP BY 1)"""
    else:  # not published yet: the column stays null
        recebido = "(SELECT NULL::VARCHAR AS cod_ibge, NULL::DOUBLE AS valor WHERE false)"
    fpm = simulador.METRICAS["fpm_pct_receita"][0]
    imp = simulador.METRICAS["impostos_proprios_pct_receita"][0]
    rows = lake.con.cursor().execute(f"""
        WITH p AS ({simulador.painel_sql(lake.table, lake.has)}),
        em AS (
            SELECT codigo_ibge_municipio AS cod_ibge,
                   sum(valor_empenhado) AS emendas,
                   sum(CASE WHEN is_transferencia_especial
                            THEN valor_empenhado ELSE 0 END) AS pix
            FROM {lake.emendas()}
            WHERE ano >= 2023 AND codigo_ibge_municipio IS NOT NULL
            GROUP BY 1),
        rc AS {recebido},
        sh AS {shows}
        SELECT p.cod_ibge, p.nome, p.uf,
               pop::BIGINT AS populacao,
               round(coalesce(em.pix, 0) / nullif(pop, 0), 2),
               round(coalesce(em.emendas, 0) / nullif(pop, 0), 2),
               round(rc.valor / nullif(pop, 0), 2),
               round(({fpm})::DOUBLE, 1), round(({imp})::DOUBLE, 1),
               round(p.despesa_cultura / nullif(pop, 0), 2),
               round(p.agua_rede_censo2022_pct, 1),
               round(p.esgoto_rede_censo2022_pct, 1),
               round(p.aps_cobertura_pct, 1), p.ideb_ai_municipal,
               round(p.favela_pct, 2), coalesce(sh.n, 0)::BIGINT,
               p.dca_exercicio
        FROM (SELECT *, coalesce(populacao_censo2022, populacao_estimada) AS pop
              FROM p) p
        LEFT JOIN em USING (cod_ibge) LEFT JOIN rc USING (cod_ibge)
        LEFT JOIN sh USING (cod_ibge)
        ORDER BY p.cod_ibge""").fetchall()
    cols = ["cod_ibge", "nome", "uf", *PAINEL_METRICAS, "dca_exercicio"]
    faltando = [t for t in ("indicadores_municipio", "financas_municipio",
                            "emendas_recebidas_municipio", "gazetas")
                if not lake.has(t)]
    return {
        "faltando": faltando,
        # where Pix money is placed: TransfereGov beneficiaries, or only the
        # Portal's município field (most Pix then has no município)
        "pix_atribuicao": ("transferegov" if lake.has("pix_beneficiarios")
                           else "portal"),
        "colunas": cols,
        "linhas": [list(r) for r in rows],
        "metricas": {k: {"label": lbl, "unidade": u}
                     for k, (lbl, u) in PAINEL_METRICAS.items()},
        "notas": [
            "Emendas: empenhado 2023+, Portal da Transparência; per capita "
            "sobre a população do Censo 2022.",
            "Emendas Pix: o Portal registra a maior parte como “MÚLTIPLO”; "
            "o destino vem dos planos de ação do TransfereGov (beneficiário "
            "declarado), com o valor do Portal rateado entre eles.",
            "Emendas pagas à prefeitura: valores efetivamente recebidos "
            "(pagos) desde 2023 pela prefeitura e seus fundos municipais "
            "(saúde, assistência etc.), de todos os tipos de emenda, pelo "
            "arquivo de favorecidos do Portal da Transparência. Exclui "
            "repasses a empresas, entidades e governos estaduais.",
            "FPM, impostos próprios e cultura: último DCA (SICONFI) "
            "disponível por município; cobertura cresce a cada dia.",
            "Shows no diário: menções a contratação de show/artística no "
            "Querido Diário (co-ocorrência, não prova).",
        ],
    }


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
    if not lake.has("proposicoes"):
        return []
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


def senado_proposicoes(lake: Lake, *, senador_id: int | None = None,
                       q: str | None = None, sigla_tipo: str | None = None,
                       limit: int = 50) -> list[dict]:
    """Senate bills by senators of the current legislature, filterable by
    author, type or ementa text (same shape as `proposicoes` where it can
    be: `sigla`, `ementa`, `autores`, `url`)."""
    if not lake.has("senado_proposicoes"):
        return []
    a = lake.table("senado_proposicoes_autores")
    where, params = ["true"], []
    if senador_id is not None:
        where.append(f"p.id IN (SELECT id_processo FROM {a} WHERE codigo_senador = ?)")
        params.append(senador_id)
    if q:
        where.append("strip_accents(lower(coalesce(p.ementa, ''))) "
                     "LIKE strip_accents(lower(?))")
        params.append(f"%{q}%")
    if sigla_tipo:
        where.append("p.sigla_tipo = ?")
        params.append(sigla_tipo.upper())
    params.append(limit)
    return lake.q(f"""
        SELECT p.id, p.identificacao AS sigla, p.sigla_tipo, p.ementa,
               p.data_apresentacao, p.situacao_atual AS status_situacao,
               p.tramitando, p.url,
               (SELECT list(DISTINCT x.nome_senador) FROM {a} x
                WHERE x.id_processo = p.id) AS autores,
               (SELECT list(DISTINCT x.codigo_senador) FROM {a} x
                WHERE x.id_processo = p.id) AS autores_ids
        FROM {lake.table('senado_proposicoes')} p
        WHERE {' AND '.join(where)}
        ORDER BY p.data_apresentacao DESC NULLS LAST LIMIT ?""", params)


# ---------------------------------------------------------------- falas
# Transcripts (`transcricoes`): debate recordings are keyed
# "<debate_id>/<n>" (n = index into debates.fontes), YouTube videos by
# video id. Text only; machine transcription (Whisper / YouTube captions),
# no speaker attribution yet.

def _debate_meta(lake: Lake) -> dict[str, dict]:
    if not lake.has("debates"):
        return {}
    return {r["debate_id"]: r for r in lake.q(f"""
        SELECT debate_id, ano, turno, cargo, uf, emissora, data,
               participantes, fontes, status FROM {lake.table('debates')}""")}


def debates(lake: Lake) -> list[dict]:
    """Every debate we know of, with how much of it is transcribed."""
    meta = _debate_meta(lake)
    stats = {}
    if lake.has("transcricoes"):
        for r in lake.q(f"""
            SELECT split_part(source_id, '/', 1) AS debate_id,
                   count(DISTINCT source_id) AS gravacoes,
                   count(*) AS segmentos,
                   round(sum(end_s - start_s) / 60) AS minutos
            FROM {lake.table('transcricoes')} WHERE source_kind = 'debate'
            GROUP BY 1"""):
            stats[r["debate_id"]] = r
    out = []
    for did in sorted(set(meta) | set(stats), reverse=True):
        m = meta.get(did) or {"debate_id": did, "ano": int(did[:4]),
                              "turno": None, "cargo": did.split("-")[1],
                              "uf": None, "emissora": None, "data": None,
                              "participantes": [], "fontes": [],
                              "status": None}
        st = stats.get(did, {})
        out.append({**m, "transcrito": bool(st),
                    "segmentos": st.get("segmentos", 0),
                    "minutos": st.get("minutos", 0)})
    out.sort(key=lambda r: (r["data"] or str(r["ano"]), r["debate_id"]),
             reverse=True)
    return out


def debate(lake: Lake, debate_id: str) -> dict:
    """One debate: metadata and the transcript of each recording."""
    meta = _debate_meta(lake).get(debate_id)
    segs = lake.q(f"""
        SELECT split_part(source_id, '/', 2)::INT AS gravacao, seg_idx AS i,
               round(start_s, 1) AS ini, round(end_s, 1) AS fim, text,
               speaker, method
        FROM {lake.table('transcricoes')}
        WHERE source_kind = 'debate' AND split_part(source_id, '/', 1) = ?
        ORDER BY gravacao, seg_idx""", [debate_id]) if lake.has("transcricoes") else []
    if not meta and not segs:
        return {}
    meta = meta or {"debate_id": debate_id, "ano": int(debate_id[:4]),
                    "fontes": [], "participantes": []}
    grav: dict[int, list] = {}
    for s in segs:
        grav.setdefault(s["gravacao"], []).append(
            {k: s[k] for k in ("i", "ini", "fim", "text", "speaker")})
    fontes = meta.get("fontes") or []
    return {**meta, "metodo": segs[0]["method"] if segs else None,
            "gravacoes": [{"n": n, "url": fontes[n] if n < len(fontes) else None,
                           "segmentos": v} for n, v in sorted(grav.items())]}


def entrevistas(lake: Lake, *, q: str | None = None,
                limit: int = 200) -> list[dict]:
    """Long-form interviews (e.g. Roda Viva), newest first."""
    if not lake.has("entrevistas"):
        return []
    where, params = "true", []
    if q:
        where = ("strip_accents(lower(coalesce(convidado, '') || ' ' || "
                 "coalesce(titulo, ''))) LIKE strip_accents(lower(?))")
        params.append(f"%{q}%")
    tr = (f"video_id IN (SELECT source_id FROM {lake.table('transcricoes')} "
          "WHERE source_kind = 'youtube')") if lake.has("transcricoes") else "false"
    return lake.q(f"""
        SELECT programa, video_id, convidado, titulo,
               coalesce(data, substr(published_at, 1, 10)) AS data,
               round(duration_s / 60) AS minutos, casa, parlamentar_id,
               {tr} AS transcrito
        FROM {lake.table('entrevistas')} WHERE {where}
        ORDER BY data DESC NULLS LAST LIMIT ?""", [*params, limit])


def video_fala(lake: Lake, video_id: str) -> dict:
    """Transcript of one YouTube video (interview or parliamentarian
    channel), with whatever metadata we hold for it."""
    if not lake.has("transcricoes"):
        return {}
    segs = lake.q(f"""
        SELECT seg_idx AS i, round(start_s, 1) AS ini, round(end_s, 1) AS fim,
               text, speaker, method
        FROM {lake.table('transcricoes')}
        WHERE source_kind = 'youtube' AND source_id = ? ORDER BY seg_idx""",
        [video_id])
    if not segs:
        return {}
    meta: dict = {"video_id": video_id}
    for t, sql in (("entrevistas", "SELECT titulo, convidado, programa, coalesce(data, substr(published_at, 1, 10)) AS data, casa, parlamentar_id FROM {} WHERE video_id = ?"),
                   ("youtube_videos", "SELECT title AS titulo, substr(published_at, 1, 10) AS data, casa, parlamentar_id FROM {} WHERE video_id = ?")):
        if lake.has(t):
            hit = lake.q(sql.format(lake.table(t)), [video_id])
            if hit:
                meta.update({k: v for k, v in hit[0].items() if v is not None})
                break
    return {**meta, "url": f"https://www.youtube.com/watch?v={video_id}",
            "metodo": segs[0]["method"],
            "segmentos": [{k: s[k] for k in ("i", "ini", "fim", "text", "speaker")} for s in segs]}


def busca_falas(lake: Lake, q: str, *, tipo: str | None = None,
                limit: int = 60) -> list[dict]:
    """Transcript segments containing `q` (accents and case ignored), with
    where they come from and the second they start."""
    if not lake.has("transcricoes") or len(q.strip()) < 3:
        return []
    where, params = ["strip_accents(lower(text)) LIKE strip_accents(lower(?))"], [f"%{q.strip()}%"]
    if tipo in ("debate", "youtube"):
        where.append("source_kind = ?")
        params.append(tipo)
    rows = lake.q(f"""
        SELECT source_kind AS tipo, source_id, seg_idx AS i,
               round(start_s, 1) AS ini, text
        FROM {lake.table('transcricoes')} WHERE {' AND '.join(where)}
        ORDER BY source_kind, source_id DESC, seg_idx LIMIT ?""",
        [*params, limit])
    meta = _debate_meta(lake)
    titles: dict[str, str] = {}
    vids = [r["source_id"] for r in rows if r["tipo"] == "youtube"]
    if vids:
        for t, col in (("entrevistas", "titulo"), ("youtube_videos", "title")):
            if lake.has(t):
                for r in lake.q(f"SELECT video_id, {col} AS t FROM {lake.table(t)} "
                                f"WHERE video_id IN ({','.join('?' * len(vids))})", vids):
                    titles.setdefault(r["video_id"], r["t"])
    for r in rows:
        if r["tipo"] == "debate":
            did, n = r["source_id"].split("/")
            m = meta.get(did, {})
            r["debate_id"], r["gravacao"] = did, int(n)
            r["titulo"] = (f"Debate {m.get('cargo', 'presidente')} {did[:4]}"
                           f"{' · ' + str(m['turno']) + 'º turno' if m.get('turno') else ''}"
                           f"{' · ' + m['emissora'] if m.get('emissora') else ''}")
        else:
            r["titulo"] = titles.get(r["source_id"], "Vídeo " + r["source_id"])
    return rows


def proposicao(lake: Lake, proposicao_id: int) -> dict:
    """One Câmara bill: text, authors, themes, attached bills, roll calls."""
    if not lake.has("proposicoes"):
        return {}
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
