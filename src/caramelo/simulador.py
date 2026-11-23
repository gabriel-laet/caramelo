"""Simulador de propostas — apply a bill's stated criteria to every município.

Neutral by construction: a proposal is a declarative list of the criteria
its own text states (metric, operator, threshold, source). The simulator
only reports, per município and per criterion, `atende` / `nao_atende` /
`sem_dado`, plus what the bill requires that open data cannot measure. It
never scores, ranks or labels municípios beyond the proposal's own terms,
and every threshold can be overridden by the caller to explore variants.

Missing data is never a fail: a município with any `sem_dado` criterion and
no failed one is `indeterminado`.

Metrics come from one per-município panel (see `painel_sql`):
    indicadores_municipio (pivoted) + latest financas_municipio year +
    derived fiscal ratios.
"""

from __future__ import annotations

# metric id -> (SQL expression over the panel, label, unit)
METRICAS: dict[str, tuple[str, str, str]] = {
    "populacao_censo2022": ("populacao_censo2022", "População (Censo 2022)", "pessoas"),
    "agua_rede_censo2022_pct": ("agua_rede_censo2022_pct",
                                "Água por rede geral (Censo 2022)", "%"),
    "esgoto_rede_censo2022_pct": ("esgoto_rede_censo2022_pct",
                                  "Esgoto por rede (Censo 2022)", "%"),
    "agua_rede_sinisa_pct": ("agua_rede_sinisa_pct",
                             "Atendimento com rede de água (SINISA 2024)", "%"),
    "esgoto_rede_sinisa_pct": ("esgoto_rede_sinisa_pct",
                               "Atendimento com rede de esgoto (SINISA 2024)", "%"),
    "aps_cobertura_pct": ("aps_cobertura_pct",
                          "Cobertura potencial da APS (Min. Saúde)", "%"),
    "ideb_ai_municipal": ("ideb_ai_municipal",
                          "Ideb anos iniciais, rede municipal (2025)", "índice"),
    "ideb_ai_publica": ("ideb_ai_publica",
                        "Ideb anos iniciais, rede pública (2025)", "índice"),
    "favela_pct": ("favela_pct", "População em favelas e comunidades urbanas "
                                 "(Censo 2022)", "%"),
    "impostos_proprios_pct_receita": (
        "100 * (coalesce(iptu,0) + coalesce(iss,0) + coalesce(itbi,0)) "
        "/ nullif(coalesce(receita_corrente,0) + coalesce(receita_capital,0), 0)",
        "IPTU + ISS + ITBI sobre a receita total (DCA)", "%"),
    "fpm_pct_receita": (
        "100 * fpm / nullif(coalesce(receita_corrente,0) "
        "+ coalesce(receita_capital,0), 0)",
        "FPM sobre a receita total (DCA)", "%"),
}

SANEAMENTO_VARIANTES = {
    "censo2022": ("agua_rede_censo2022_pct", "esgoto_rede_censo2022_pct",
                  "IBGE Censo 2022 (declaração dos domicílios)"),
    "sinisa": ("agua_rede_sinisa_pct", "esgoto_rede_sinisa_pct",
               "SINISA 2024 (dados dos prestadores)"),
}

PROPOSTAS: dict[str, dict] = {
    "pl-5870-2026": {
        "id": "pl-5870-2026",
        "titulo": "PL 5.870/2026",
        "ementa": ("Altera a Lei nº 14.133/2021 para estabelecer requisitos "
                   "para a contratação de profissionais do setor artístico "
                   "pela Administração Pública."),
        "casa": "camara", "proposicao_id": 2654327,
        "autoria": "Dep. Kim Kataguiri (MISSÃO/SP)",
        "apresentacao": "2026-11-17",
        "url": "https://www.camara.leg.br/propostas-legislativas/2654327",
        "resultado": ("Município onde a contratação de shows com recursos "
                      "públicos atenderia aos requisitos sociais do projeto"),
        "variantes": {"saneamento": SANEAMENTO_VARIANTES},
        "variante_padrao": {"saneamento": "censo2022"},
        "criterios": [
            {"id": "agua", "metrica": "{saneamento.agua}", "op": ">=",
             "limiar": 90, "texto": "art. 74 §2º-A V a): ≥ 90% da população "
                                    "atendida por abastecimento de água potável"},
            {"id": "esgoto", "metrica": "{saneamento.esgoto}", "op": ">=",
             "limiar": 90, "texto": "art. 74 §2º-A V a): ≥ 90% da população "
                                    "atendida por coleta de esgoto"},
            {"id": "aps", "metrica": "aps_cobertura_pct", "op": ">=",
             "limiar": 80, "texto": "art. 74 §2º-A V b–c): cobertura potencial "
                                    "da Atenção Primária ≥ 80%"},
            {"id": "ideb", "metrica": "ideb_ai_municipal", "op": ">=",
             "limiar": 7.0, "texto": "art. 74 §2º-A V d): Ideb ≥ 7,0 nos anos "
                                     "iniciais da rede pública municipal"},
            {"id": "favelas", "metrica": "favela_pct", "op": "<", "limiar": 5,
             "texto": "art. 74 §2º-A V e): < 5% da população em favelas e "
                      "comunidades urbanas (classificação IBGE)"},
        ],
        "nao_mensuraveis": [
            "Pagamento de precatórios em dia pelo ente contratante",
            "Disponibilidade orçamentária e financeira sem comprometer "
            "despesas obrigatórias",
            "Justificativa de interesse público e publicação prévia de custos",
            "Ausência de filiação partidária ou manifestação política do "
            "artista nos 5 anos anteriores",
        ],
        "notas": [
            "Os critérios são avaliados no município onde o show ocorre.",
            "O texto diz 'rede pública municipal' para o Ideb; usamos a linha "
            "'Municipal' do Inep (a linha 'Pública' também está disponível).",
            "Água e esgoto podem ser medidos pelo Censo 2022 ou pelo SINISA "
            "2024; os resultados diferem (ver variante).",
        ],
    },
    "pec-188-2019-art6": {
        "id": "pec-188-2019-art6",
        "titulo": "PEC 188/2019 — sustentabilidade financeira (art. 6º)",
        "ementa": ("PEC do Pacto Federativo. Municípios de até 5 mil "
                   "habitantes que não comprovem arrecadação própria (IPTU, "
                   "ISS, ITBI) de ao menos 10% da receita seriam incorporados "
                   "a um município vizinho."),
        "casa": "senado", "proposicao_id": None,
        "autoria": "Presidência da República (apresentada no Senado)",
        "apresentacao": "2019-11-05",
        "url": "https://www25.senado.leg.br/web/atividade/materias/-/materia/139704",
        "resultado": ("Município que se enquadraria na regra de incorporação "
                      "(até 5 mil habitantes e arrecadação própria < 10%)"),
        "variantes": {},
        "variante_padrao": {},
        "criterios": [
            {"id": "populacao", "metrica": "populacao_censo2022", "op": "<=",
             "limiar": 5000, "texto": "até 5.000 habitantes"},
            {"id": "arrecadacao", "metrica": "impostos_proprios_pct_receita",
             "op": "<", "limiar": 10,
             "texto": "impostos do art. 156 (IPTU, ISS, ITBI) < 10% da receita"},
        ],
        "nao_mensuraveis": [
            "Os prazos e o procedimento de incorporação (texto de 2019, "
            "não aprovado)",
        ],
        "notas": [
            "Usamos o Censo 2022 para população e o último DCA disponível "
            "para receitas; 'receita' = receita corrente + de capital brutas.",
            "Citada no programa do Partido Missão (2026) como referência "
            "de viabilidade fiscal municipal.",
        ],
    },
}

OPS = {">=", ">", "<=", "<"}


def resolve(proposta: dict, variantes: dict[str, str] | None = None,
            limiares: dict[str, float] | None = None) -> list[dict]:
    """Concrete criteria for one evaluation: variant metrics + overrides."""
    chosen = {**proposta["variante_padrao"], **(variantes or {})}
    out = []
    for c in proposta["criterios"]:
        metrica = c["metrica"]
        if metrica.startswith("{"):
            group, field = metrica.strip("{}").split(".")
            agua, esgoto, _ = proposta["variantes"][group][chosen[group]]
            metrica = {"agua": agua, "esgoto": esgoto}[field]
        limiar = float((limiares or {}).get(c["id"], c["limiar"]))
        out.append({**c, "metrica": metrica, "limiar": limiar,
                    "limiar_original": c["limiar"],
                    "metrica_label": METRICAS[metrica][1],
                    "unidade": METRICAS[metrica][2]})
    return out


# Stand-ins when a table is not published yet: same columns, zero rows, so
# the panel and the simulator degrade to "sem dado" instead of erroring.
EMPTY = {
    "indicadores_municipio": "(SELECT NULL::VARCHAR AS cod_ibge, "
        "NULL::VARCHAR AS indicador, NULL::DOUBLE AS valor WHERE false)",
    "financas_municipio": "(SELECT NULL::VARCHAR AS cod_ibge, "
        "NULL::SMALLINT AS exercicio, " + ", ".join(
            f"NULL::DOUBLE AS {c}" for c in (
                "receita_corrente", "receita_capital", "iptu", "iss", "itbi",
                "fpm", "despesa_cultura", "despesa_difusao_cultural"))
        + " WHERE false)",
}


def painel_sql(table, has=lambda name: True) -> str:
    """One row per município: identity, every indicator, latest DCA year.
    `has(name)` says whether a table is published; missing optional tables
    are swapped for the empty stand-ins above."""
    def src(name: str) -> str:
        return table(name) if name not in EMPTY or has(name) else EMPTY[name]
    pivot_cols = ", ".join(
        f"max(valor) FILTER (WHERE indicador = '{m}') AS {m}"
        for m in METRICAS if not m.endswith("_receita"))
    return f"""
        WITH ind AS (
            SELECT cod_ibge, {pivot_cols}
            FROM {src('indicadores_municipio')} GROUP BY cod_ibge),
        fin AS (
            SELECT * FROM {src('financas_municipio')}
            QUALIFY row_number() OVER (PARTITION BY cod_ibge
                                       ORDER BY exercicio DESC) = 1)
        SELECT m.codigo_ibge AS cod_ibge, m.nome, m.uf,
               m.populacao AS populacao_estimada,
               ind.* EXCLUDE (cod_ibge),
               fin.exercicio AS dca_exercicio, fin.receita_corrente,
               fin.receita_capital, fin.iptu, fin.iss, fin.itbi, fin.fpm,
               fin.despesa_cultura, fin.despesa_difusao_cultural
        FROM {table('municipios')} m
        LEFT JOIN ind ON ind.cod_ibge = m.codigo_ibge
        LEFT JOIN fin ON fin.cod_ibge = m.codigo_ibge"""


def criterio_sql(c: dict) -> str:
    if c["op"] not in OPS:
        raise ValueError(c["op"])
    expr = METRICAS[c["metrica"]][0]
    return (f"CASE WHEN ({expr}) IS NULL THEN 'sem_dado' "
            f"WHEN ({expr}) {c['op']} {c['limiar']!r} THEN 'atende' "
            f"ELSE 'nao_atende' END")
