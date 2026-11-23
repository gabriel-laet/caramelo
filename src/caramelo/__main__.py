"""Caramelo CLI.

Usage:
    caramelo harvest emendas [--data-dir DATA]
    caramelo harvest deputados [--data-dir DATA]
    caramelo resolve autores [--data-dir DATA]
    caramelo harvest youtube|instagram|debates|manchetes|gdelt
    caramelo archive youtube|instagram|debates   (local network -> NAS)
    caramelo transcribe [--whisper]              (local GPU)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="caramelo")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--archive-dir", type=Path, default=None,
                        help="local media/transcript archive, never "
                             "published (default: $CARAMELO_ARCHIVE_DIR "
                             "or ./archive)")
    sub = parser.add_subparsers(dest="command", required=True)

    harvest = sub.add_parser("harvest", help="download and normalize a source")
    harvest.add_argument("source",
                         choices=["emendas", "deputados", "senadores",
                                  "votacoes", "municipios", "siconfi", "ceap",
                                  "redes", "media", "x", "favorecidos",
                                  "gazetas", "transferegov", "ceaps",
                                  "senado-votos", "tse", "trends", "cnpj",
                                  "indicadores", "financas", "proposicoes",
                                  "senado-proposicoes", "youtube",
                                  "instagram", "debates", "manchetes",
                                  "gdelt", "entrevistas"])
    harvest.add_argument("--years", type=int, nargs="+", default=None,
                         help="years for bulk sources (default: 2023-2026)")
    harvest.add_argument("--exercicio", type=int, default=2025,
                         help="siconfi: fiscal year")
    harvest.add_argument("--periodo", type=int, default=1,
                         help="siconfi: bimonthly period (1-6)")
    harvest.add_argument("--entes", nargs="+", default=None,
                         help="siconfi: IBGE ente codes (default: 27 UFs)")
    harvest.add_argument("--municipios", default=None, metavar="UF|all",
                         help="siconfi: sweep municípios of one UF, or 'all'")
    harvest.add_argument("--budget", type=int, default=None,
                         help="media/trends: max SearchApi credits (25); "
                              "instagram: max Graph API calls (150)")
    harvest.add_argument("--reads-budget", type=int, default=350,
                         help="x: max post-reads this run (US$0.005 each)")
    harvest.add_argument("--backfill", action="store_true",
                         help="x: pull latest page per author instead of "
                              "incremental since_id")
    harvest.add_argument("--engine", default="google_news",
                         help="media: SearchApi engine (google_news, google, "
                              "youtube, ...)")

    harvest.add_argument("--units", type=int, default=None,
                         help="youtube: max Data API units this run "
                              "(default $CARAMELO_YOUTUBE_UNITS or 8000)")
    harvest.add_argument("--stats-all", action="store_true",
                         help="youtube: snapshot stats of every video, not "
                              "just the recent window")
    harvest.add_argument("--pages", type=int, default=1,
                         help="instagram: pages of 50 posts per account")
    harvest.add_argument("--step-days", type=int, default=7,
                         help="manchetes: sample one front page every N days")
    harvest.add_argument("--max-fetches", type=int, default=300,
                         help="manchetes: max new Wayback pages this run")
    harvest.add_argument("--outlets", nargs="+", default=None,
                         help="manchetes: outlet slugs (default: all seeded)")

    arch = sub.add_parser("archive", help="download media/captions into the "
                          "local archive (run on the local network)")
    arch.add_argument("platform", choices=["youtube", "instagram", "debates",
                                           "entrevistas"])
    arch.add_argument("--limit", type=int, default=200,
                      help="max items to fetch this run")
    arch.add_argument("--audio", action="store_true",
                      help="youtube: also download audio (for Whisper)")
    arch.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                      help="only items published on/after this date")
    arch.add_argument("--only", default=None,
                      help="debates: a single debate id")

    disc = sub.add_parser("discover", help="propose recordings for curated "
                          "seeds (yt-dlp search, no API quota)")
    disc.add_argument("what", choices=["debates"])
    disc.add_argument("--min-minutes", type=int, default=60)

    tr = sub.add_parser("transcribe", help="captions -> segments, optional "
                        "Whisper on archived audio, compile transcricoes")
    tr.add_argument("--whisper", action="store_true",
                    help="run faster-whisper on items with audio")
    tr.add_argument("--model", default="large-v3")
    tr.add_argument("--limit", type=int, default=50,
                    help="max items for Whisper this run")
    tr.add_argument("--kinds", nargs="+",
                    default=["debate", "youtube", "instagram"])

    resolve = sub.add_parser("resolve", help="build entity-resolution tables")
    resolve.add_argument("entity", choices=["autores"])

    enrich = sub.add_parser("enrich", help="build derived (enrichment) tables")
    enrich.add_argument("dataset", choices=["categorias", "categorias-llm"])

    pub = sub.add_parser("publish",
                         help="publish tables + manifest + events to a target")
    pub.add_argument("--target", default=os.environ.get(
        "CARAMELO_PUBLISH_TARGET", "local:data/published"),
        help="local:<dir> or r2 (default: $CARAMELO_PUBLISH_TARGET)")

    runall = sub.add_parser(
        "run-all",
        help="harvest every source, resolve, publish (container entrypoint)")
    runall.add_argument("--exercicio", type=int, default=None,
                        help="siconfi fiscal year (default: current year)")
    runall.add_argument("--periodo", type=int, default=None,
                        help="siconfi bimonthly period (default: latest "
                             "plausibly-published one)")

    args = parser.parse_args(argv)

    if args.command == "harvest" and args.source == "emendas":
        from caramelo.harvesters import emendas
        emendas.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "deputados":
        from caramelo.harvesters import camara
        camara.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "senadores":
        from caramelo.harvesters import senado
        senado.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "votacoes":
        from caramelo.harvesters import camara_bulk
        years = tuple(args.years) if args.years else camara_bulk.DEFAULT_YEARS
        camara_bulk.harvest(args.data_dir, years)
    elif args.command == "harvest" and args.source == "municipios":
        from caramelo.harvesters import ibge
        ibge.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "siconfi":
        from caramelo.harvesters import siconfi
        if args.municipios:
            uf = None if args.municipios.lower() == "all" else args.municipios
            entes = siconfi.municipio_entes(args.data_dir, uf)
        elif args.entes:
            entes = tuple(args.entes)
        else:
            entes = siconfi.UF_ENTES
        siconfi.harvest(args.data_dir, args.exercicio, args.periodo, entes)
    elif args.command == "harvest" and args.source == "ceap":
        from caramelo.harvesters import ceap
        years = tuple(args.years) if args.years else ceap.DEFAULT_YEARS
        ceap.harvest(args.data_dir, years)
    elif args.command == "harvest" and args.source == "redes":
        from caramelo.harvesters import redes
        redes.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "media":
        from caramelo import media
        media.harvest(args.data_dir, args.budget or 25, args.engine)
    elif args.command == "harvest" and args.source == "x":
        from caramelo.harvesters import x
        x.harvest(args.data_dir, args.reads_budget, args.backfill)
    elif args.command == "harvest" and args.source == "favorecidos":
        from caramelo.harvesters import favorecidos
        favorecidos.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "gazetas":
        from caramelo.harvesters import gazetas
        gazetas.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "transferegov":
        from caramelo.harvesters import transferegov
        transferegov.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "ceaps":
        from caramelo.harvesters import ceaps
        years = tuple(args.years) if args.years else ceaps.DEFAULT_YEARS
        ceaps.harvest(args.data_dir, years)
    elif args.command == "harvest" and args.source == "senado-votos":
        from caramelo.harvesters import senado_votos
        senado_votos.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "tse":
        from caramelo.harvesters import tse
        tse.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "trends":
        from caramelo import media
        media.harvest_trends(args.data_dir, args.budget or 25)
    elif args.command == "harvest" and args.source == "cnpj":
        from caramelo.harvesters import cnpj
        cnpj.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "indicadores":
        from caramelo.harvesters import indicadores
        indicadores.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "financas":
        from caramelo.harvesters import dca
        dca.harvest(args.data_dir, tuple(args.years) if args.years else None,
                    int(os.environ.get("CARAMELO_DCA_MAX_ENTES", "1500")))
    elif args.command == "harvest" and args.source == "proposicoes":
        from caramelo.harvesters import proposicoes
        proposicoes.harvest(args.data_dir,
                            tuple(args.years) if args.years else None)
    elif args.command == "harvest" and args.source == "senado-proposicoes":
        from caramelo.harvesters import senado_proposicoes
        senado_proposicoes.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "youtube":
        from caramelo.harvesters import youtube
        youtube.harvest(args.data_dir, units=args.units,
                        backfill=args.backfill, stats_all=args.stats_all)
    elif args.command == "harvest" and args.source == "instagram":
        from caramelo.harvesters import instagram
        instagram.harvest(args.data_dir, budget=args.budget or 150,
                          pages=args.pages)
    elif args.command == "harvest" and args.source == "debates":
        from caramelo.harvesters import debates
        debates.harvest(args.data_dir)
    elif args.command == "harvest" and args.source == "manchetes":
        from caramelo import archive as arc
        from caramelo.harvesters import zeitgeist
        zeitgeist.harvest_manchetes(args.data_dir, arc.root(args.archive_dir),
                                    step_days=args.step_days,
                                    max_fetches=args.max_fetches,
                                    outlets=args.outlets)
    elif args.command == "harvest" and args.source == "entrevistas":
        from caramelo.harvesters import programas
        programas.build(args.data_dir)
    elif args.command == "discover":
        import json as _json

        from caramelo.harvesters import debates
        found = debates.discover(min_minutes=args.min_minutes)
        out = args.data_dir / "debates_candidatos.json"
        out.write_text(_json.dumps(found, ensure_ascii=False, indent=1))
        for d in found:
            print(f"\n{d['debate_id']}  (query: {d['query']})")
            for c in d["candidatos"] or [{"title": "-- no full-length result"}]:
                print(f"  [{c.get('score', '')}] {c.get('minutes', '')}min "
                      f"{c.get('channel', '')} | {c['title']} | "
                      f"{c.get('url', '')}")
        print(f"\ncandidates -> {out} (approve into seeds/debates.toml)")
    elif args.command == "harvest" and args.source == "gdelt":
        from caramelo.harvesters import zeitgeist
        zeitgeist.harvest_gdelt(args.data_dir)
    elif args.command == "archive":
        from caramelo import archive as arc
        root = arc.root(args.archive_dir)
        if args.platform == "youtube":
            from caramelo.harvesters import youtube
            youtube.archive(args.data_dir, root, limit=args.limit,
                            audio=args.audio, since=args.since)
        elif args.platform == "entrevistas":
            import pyarrow.parquet as _pq

            from caramelo.harvesters import youtube
            ids = {r["video_id"] for r in _pq.read_table(
                args.data_dir / "entrevistas.parquet",
                columns=["video_id"]).to_pylist()}
            youtube.archive(args.data_dir, root, limit=args.limit, audio=True,
                            since=args.since, video_ids=ids)
        elif args.platform == "instagram":
            from caramelo.harvesters import instagram
            instagram.archive(args.data_dir, root, limit=args.limit,
                              since=args.since)
        else:
            from caramelo.harvesters import debates
            debates.archive(root, only=args.only)
    elif args.command == "transcribe":
        from caramelo import archive as arc
        from caramelo import transcribe
        transcribe.run(arc.root(args.archive_dir), whisper=args.whisper,
                       model=args.model, limit=args.limit,
                       kinds=tuple(args.kinds), data_dir=args.data_dir)
    elif args.command == "resolve" and args.entity == "autores":
        from caramelo.resolution import authors
        authors.resolve(args.data_dir)
    elif args.command == "enrich" and args.dataset == "categorias":
        from caramelo.enrich import categorias
        categorias.enrich(args.data_dir)
    elif args.command == "enrich" and args.dataset == "categorias-llm":
        from caramelo.enrich import categorias, llm
        llm.classify_indefinidos(args.data_dir)
        categorias.enrich(args.data_dir)
    elif args.command == "publish":
        from caramelo.publish import publish
        publish(args.data_dir, args.target)
    elif args.command == "run-all":
        import time as _time

        from caramelo import state
        from caramelo.harvesters import camara, camara_bulk, ceap, emendas, ibge, senado, siconfi
        from caramelo.publish import make_target, publish
        from caramelo.resolution import authors
        target_spec = os.environ.get("CARAMELO_PUBLISH_TARGET",
                                     "local:data/published")
        state.seed(args.data_dir, make_target(target_spec))
        now = _time.gmtime()
        # SICONFI publishes each bimester with ~2 months of lag
        exercicio = args.exercicio or (now.tm_year if now.tm_mon > 3
                                       else now.tm_year - 1)
        periodo = args.periodo or (max(1, (now.tm_mon - 3) // 2 + 1)
                                   if now.tm_mon > 3 else 6)
        ufs_per_run = int(os.environ.get("CARAMELO_SICONFI_UFS_PER_RUN", "4"))

        def _siconfi() -> None:
            siconfi.harvest(args.data_dir, exercicio, periodo)
            if ufs_per_run > 0:
                siconfi.harvest(args.data_dir, exercicio, periodo,
                                siconfi.rotating_municipal_entes(
                                    args.data_dir, ufs_per_run))

        # Core sources: later steps read their tables, so on failure (e.g.
        # Câmara API 504s, Nov 2026) we fall back to the last published
        # version from the lake and carry on. Only a core source with no
        # previous version anywhere aborts the run.
        publish_target = make_target(target_spec)
        stale: list[str] = []
        for name, tables, step in (
                ("emendas", ("emendas",), lambda: emendas.harvest(args.data_dir)),
                ("camara", ("deputados",), lambda: camara.harvest(args.data_dir)),
                ("senado", ("senadores",), lambda: senado.harvest(args.data_dir)),
                ("camara-bulk", ("votacoes", "votos", "orientacoes"),
                 lambda: camara_bulk.harvest(args.data_dir,
                                             camara_bulk.DEFAULT_YEARS)),
                ("ceap", ("ceap",),
                 lambda: ceap.harvest(args.data_dir, ceap.DEFAULT_YEARS)),
                ("ibge", ("municipios",), lambda: ibge.harvest(args.data_dir)),
                ("siconfi", ("siconfi_rreo",), _siconfi)):
            try:
                step()
            except Exception as exc:
                print(f"run-all: core source {name} failed: {exc!r}")
                if not state.restore(args.data_dir, publish_target, tables):
                    raise
                stale.append(name)
        if stale:
            print(f"run-all: core sources reused from the lake (stale): "
                  f"{', '.join(stale)}")
        # Everything below is a secondary source: its previous table was
        # seeded from the lake, so if the upstream is down (e.g. Querido
        # Diário TLS outage, Nov 2026) we log, keep yesterday's rows and
        # still publish.
        from caramelo import media
        from caramelo.harvesters import (ceaps, cnpj, dca, favorecidos,
                                         gazetas, indicadores, proposicoes,
                                         redes, senado_proposicoes,
                                         senado_votos, transferegov)

        def _media() -> None:
            if not os.environ.get("CARAMELO_SEARCHAPI_KEY"):
                return
            media.harvest(args.data_dir,
                          int(os.environ.get("CARAMELO_MEDIA_BUDGET", "25")))
            media.harvest_trends(args.data_dir,
                                 int(os.environ.get("CARAMELO_TRENDS_BUDGET",
                                                    "10")))

        def _x() -> None:
            if not os.environ.get("CARAMELO_X_BEARER"):
                return
            from caramelo.harvesters import x
            x.harvest(args.data_dir,
                      int(os.environ.get("CARAMELO_X_READS", "350")))

        failed: list[str] = []
        for name, step in (
                ("redes", lambda: redes.harvest(args.data_dir)),
                ("media", _media),
                ("x", _x),
                ("gazetas", lambda: gazetas.harvest(args.data_dir)),
                ("favorecidos", lambda: favorecidos.harvest(args.data_dir)),
                ("transferegov", lambda: transferegov.harvest(args.data_dir)),
                ("ceaps", lambda: ceaps.harvest(args.data_dir)),
                ("senado-votos", lambda: senado_votos.harvest(args.data_dir)),
                ("cnpj", lambda: cnpj.harvest(
                    args.data_dir,
                    int(os.environ.get("CARAMELO_CNPJ_LOOKUPS", "300")))),
                ("indicadores", lambda: indicadores.harvest(args.data_dir)),
                ("financas", lambda: dca.harvest(
                    args.data_dir, None,
                    int(os.environ.get("CARAMELO_DCA_MAX_ENTES", "1500")))),
                ("proposicoes", lambda: proposicoes.harvest(args.data_dir)),
                ("senado-proposicoes",
                 lambda: senado_proposicoes.harvest(args.data_dir)),
                ("debates", lambda: _debates(args.data_dir)),
                ("youtube", lambda: _if_env(
                    "CARAMELO_YOUTUBE_KEY", "youtube", args.data_dir)),
                ("instagram", lambda: _if_env(
                    "CARAMELO_IG_TOKEN", "instagram", args.data_dir)),
                ("entrevistas", lambda: _entrevistas(args.data_dir))):
            try:
                step()
            except Exception as exc:  # one source must not sink run-all
                failed.append(name)
                print(f"run-all: {name} failed: {exc!r}")
        if failed:
            print(f"run-all: secondary sources failed (kept previous rows): "
                  f"{', '.join(failed)}")
        authors.resolve(args.data_dir)
        try:  # Pix destination per município (TransfereGov beneficiaries)
            from caramelo.resolution import pix
            pix.resolve(args.data_dir)
        except Exception as exc:
            print(f"run-all: pix resolution failed: {exc!r}")
        try:  # emenda money received by each prefeitura (favorecidos)
            from caramelo.resolution import recebidos
            recebidos.resolve(args.data_dir)
        except Exception as exc:
            print(f"run-all: recebidos resolution failed: {exc!r}")
        from caramelo.enrich import categorias
        # Enrichment is derived data: a failure here must not block publishing
        # the freshly harvested tables (the previous enrichment is carried
        # forward by publish).
        if os.environ.get("CARAMELO_AI_TOKEN"):
            try:
                from caramelo.enrich import llm
                llm.classify_indefinidos(args.data_dir)
            except Exception as exc:
                print(f"run-all: llm enrichment failed: {exc!r}")
        try:
            categorias.enrich(args.data_dir)
        except Exception as exc:
            print(f"run-all: categorias enrichment failed: {exc!r}")
        publish(args.data_dir, target_spec)
        state.push(args.data_dir, make_target(target_spec))


def _debates(data_dir: Path) -> None:
    from caramelo.harvesters import debates
    debates.harvest(data_dir)


def _entrevistas(data_dir: Path) -> None:
    if (data_dir / "youtube_videos.parquet").exists():
        from caramelo.harvesters import programas
        programas.build(data_dir)


def _if_env(var: str, module: str, data_dir: Path) -> None:
    """Metadata-only harvests safe for the cloud cron; media download and
    transcription stay on the local network (`archive`, `transcribe`)."""
    if not os.environ.get(var):
        return
    import importlib
    importlib.import_module(f"caramelo.harvesters.{module}").harvest(data_dir)


if __name__ == "__main__":
    main()
