"""Caramelo CLI.

Usage:
    caramelo harvest emendas [--data-dir DATA]
    caramelo harvest deputados [--data-dir DATA]
    caramelo resolve autores [--data-dir DATA]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="caramelo")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sub = parser.add_subparsers(dest="command", required=True)

    harvest = sub.add_parser("harvest", help="download and normalize a source")
    harvest.add_argument("source",
                         choices=["emendas", "deputados", "senadores",
                                  "votacoes", "municipios", "siconfi", "ceap"])
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

    resolve = sub.add_parser("resolve", help="build entity-resolution tables")
    resolve.add_argument("entity", choices=["autores"])

    pub = sub.add_parser("publish",
                         help="publish tables + manifest + events to a target")
    pub.add_argument("--target", default=os.environ.get(
        "CARAMELO_PUBLISH_TARGET", "local:data/published"),
        help="local:<dir> or r2 (default: $CARAMELO_PUBLISH_TARGET)")

    sub.add_parser("run-all",
                   help="harvest every source, resolve, publish (container entrypoint)")

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
    elif args.command == "resolve" and args.entity == "autores":
        from caramelo.resolution import authors
        authors.resolve(args.data_dir)
    elif args.command == "publish":
        from caramelo.publish import publish
        publish(args.data_dir, args.target)
    elif args.command == "run-all":
        from caramelo.harvesters import camara, camara_bulk, ceap, emendas, ibge, senado, siconfi
        from caramelo.publish import publish
        from caramelo.resolution import authors
        emendas.harvest(args.data_dir)
        camara.harvest(args.data_dir)
        senado.harvest(args.data_dir)
        camara_bulk.harvest(args.data_dir, camara_bulk.DEFAULT_YEARS)
        ceap.harvest(args.data_dir, ceap.DEFAULT_YEARS)
        ibge.harvest(args.data_dir)
        siconfi.harvest(args.data_dir, args.exercicio, args.periodo)
        authors.resolve(args.data_dir)
        publish(args.data_dir, os.environ.get(
            "CARAMELO_PUBLISH_TARGET", "local:data/published"))


if __name__ == "__main__":
    main()
