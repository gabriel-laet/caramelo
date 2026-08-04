"""Caramelo CLI.

Usage:
    caramelo harvest emendas [--data-dir DATA]
    caramelo harvest deputados [--data-dir DATA]
    caramelo resolve autores [--data-dir DATA]
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="caramelo")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sub = parser.add_subparsers(dest="command", required=True)

    harvest = sub.add_parser("harvest", help="download and normalize a source")
    harvest.add_argument("source",
                         choices=["emendas", "deputados", "senadores", "votacoes"])
    harvest.add_argument("--years", type=int, nargs="+", default=None,
                         help="years for bulk sources (default: 2023-2026)")

    resolve = sub.add_parser("resolve", help="build entity-resolution tables")
    resolve.add_argument("entity", choices=["autores"])

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
    elif args.command == "resolve" and args.entity == "autores":
        from caramelo.resolution import authors
        authors.resolve(args.data_dir)


if __name__ == "__main__":
    main()
