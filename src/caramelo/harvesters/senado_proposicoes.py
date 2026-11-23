"""Harvest Senado bills authored by senators of the 57th legislature.

Endpoint (Senado dados abertos, JSON via Accept, no auth, no pagination):
    https://legis.senado.leg.br/dadosabertos/processo
        ?codigoParlamentarAutor={codigo}&dataInicioApresentacao=2023-02-01

(The older /senador/{cod}/autorias endpoint was deprecated 2025-03-18.)

Outputs, mirroring the Câmara tables:
    senado_proposicoes          one row per processo (deduplicated)
    senado_proposicoes_autores  (processo id, senador codigo) pairs — the
                                author list as the Senado indexes it, so
                                co-authored bills appear once per senator

Only legislative types are kept (PL, PLP, PEC, PDL, PRS).
"""

from __future__ import annotations

import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from caramelo.http import get

URL = "https://legis.senado.leg.br/dadosabertos/processo"
DESDE = "2023-02-01"
LEGISLATURA = 57
TIPOS = {"PL", "PLP", "PEC", "PDL", "PRS"}

PROPOSICOES = pa.schema([
    ("id", pa.int64()), ("codigo_materia", pa.int64()),
    ("identificacao", pa.string()), ("sigla_tipo", pa.string()),
    ("numero", pa.int32()), ("ano", pa.int16()),
    ("tipo_documento", pa.string()), ("tipo_conteudo", pa.string()),
    ("ementa", pa.string()), ("autoria", pa.string()),
    ("data_apresentacao", pa.string()), ("tramitando", pa.bool_()),
    ("situacao_atual", pa.string()), ("data_situacao_atual", pa.string()),
    ("deliberacao", pa.string()), ("data_deliberacao", pa.string()),
    ("casa_identificadora", pa.string()), ("url_documento", pa.string()),
    ("url", pa.string()),
])

AUTORES = pa.schema([
    ("id_processo", pa.int64()), ("codigo_senador", pa.int64()),
    ("nome_senador", pa.string()), ("uf", pa.string()),
])


def _parse_identificacao(ident: str) -> tuple[str, int | None, int | None]:
    # "PL 2338/2023" -> ("PL", 2338, 2023)
    sigla, _, rest = (ident or "").partition(" ")
    num, _, ano = rest.partition("/")
    return (sigla, int(num) if num.isdigit() else None,
            int(ano) if ano.isdigit() else None)


def harvest(data_dir: Path) -> list[Path]:
    senadores: dict[int, dict] = {}
    for r in pq.read_table(data_dir / "senadores.parquet").to_pylist():
        if r["legislatura"] == LEGISLATURA:
            senadores[r["codigo"]] = r

    props: dict[int, dict] = {}
    autores: set[tuple[int, int]] = set()
    for i, (cod, sen) in enumerate(sorted(senadores.items()), 1):
        try:
            items = get(URL, params={"codigoParlamentarAutor": cod,
                                     "dataInicioApresentacao": DESDE},
                        headers={"Accept": "application/json"},
                        timeout=120.0).json()
        except RuntimeError as exc:
            print(f"senado_proposicoes: {cod} failed: {exc}")
            continue
        for it in items or []:
            sigla, numero, ano = _parse_identificacao(it.get("identificacao"))
            if sigla not in TIPOS:
                continue
            pid = it["id"]
            autores.add((pid, cod))
            props[pid] = {
                "id": pid, "codigo_materia": it.get("codigoMateria"),
                "identificacao": it.get("identificacao"),
                "sigla_tipo": sigla, "numero": numero, "ano": ano,
                "tipo_documento": it.get("tipoDocumento"),
                "tipo_conteudo": it.get("tipoConteudo"),
                "ementa": it.get("ementa"), "autoria": it.get("autoria"),
                "data_apresentacao": it.get("dataApresentacao"),
                "tramitando": it.get("tramitando") == "Sim",
                "situacao_atual": it.get("situacaoAtual"),
                "data_situacao_atual": it.get("dataSituacaoAtual"),
                "deliberacao": it.get("siglaTipoDeliberacao"),
                "data_deliberacao": it.get("dataDeliberacao"),
                "casa_identificadora": it.get("casaIdentificadora"),
                "url_documento": it.get("urlDocumento"),
                "url": ("https://www25.senado.leg.br/web/atividade/materias/-/"
                        f"materia/{it['codigoMateria']}"
                        if it.get("codigoMateria") else None),
            }
        if i % 20 == 0:
            print(f"senado_proposicoes: {i}/{len(senadores)} senadores...")
        time.sleep(0.3)

    outs = []
    p_out = data_dir / "senado_proposicoes.parquet"
    pq.write_table(pa.Table.from_pylist(
        sorted(props.values(), key=lambda r: r["id"]), schema=PROPOSICOES),
        p_out, compression="zstd")
    a_rows = [{"id_processo": pid, "codigo_senador": cod,
               "nome_senador": senadores[cod]["nome_parlamentar"],
               "uf": senadores[cod]["uf"]} for pid, cod in sorted(autores)]
    a_out = data_dir / "senado_proposicoes_autores.parquet"
    pq.write_table(pa.Table.from_pylist(a_rows, schema=AUTORES), a_out,
                   compression="zstd")
    print(f"senado_proposicoes: {len(props)} processos, {len(a_rows)} "
          f"autorias ({len(senadores)} senadores) -> {p_out}")
    outs += [p_out, a_out]
    return outs
