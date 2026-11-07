"""Ejecuta el pipeline de una fase. Dueño: P1.

    uv run python scripts/run_phase.py --phase sample|val|live|final [--disable a,b] [--months N]

Salidas: out/<fase>/submission.csv.gz y out/<fase>/audit.parquet; tiempos por resolver en el log.
"""
import argparse
import logging
import sys

from bankrec import config, pipeline
from bankrec.validate import phase_txn_ids, validate


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=list(config.PHASES))
    ap.add_argument("--disable", default="", help="resolvers a desactivar, separados por comas")
    ap.add_argument("--months", type=int, default=None, help="solo los N primeros meses (pruebas rápidas)")
    ap.add_argument("--no-validate", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    res = pipeline.run(a.phase, {d.strip() for d in a.disable.split(",") if d.strip()}, a.months)
    if not a.no_validate:
        errors, warnings = validate(res["submission"], a.phase, phase_txn_ids(a.phase, a.months))
        for w in warnings:
            print("AVISO:", w)
        for e in errors:
            print("ERROR:", e)
        print("VALIDO" if not errors else "NO VALIDO")
        sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
