"""Ejecuta el pipeline con módulos de P4 añadidos al final de la cascada, sin tocar registry.py. Dueño: P4.

    uv run python scripts/p4_run.py --phase val [--months N] [--add bankrec.resolvers.nonitem_unmatched]

Sirve para medir un módulo nuevo antes de que P1 lo registre. Mismas salidas que scripts/run_phase.py.
"""
import argparse
import logging
import sys

from bankrec import config, pipeline, registry
from bankrec.validate import phase_txn_ids, validate


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=list(config.PHASES))
    ap.add_argument("--months", type=int, default=None)
    ap.add_argument("--disable", default="")
    ap.add_argument("--add", default="bankrec.resolvers.nonitem_unmatched",
                    help="módulos a añadir al final de la cascada, separados por comas")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for path in (p.strip() for p in a.add.split(",") if p.strip()):
        if path not in registry.CASCADE:
            registry.CASCADE.append(path)
    res = pipeline.run(a.phase, {d.strip() for d in a.disable.split(",") if d.strip()}, a.months)
    errors, warnings = validate(res["submission"], a.phase, phase_txn_ids(a.phase, a.months))
    for w in warnings:
        print("AVISO:", w)
    for e in errors:
        print("ERROR:", e)
    print("VALIDO" if not errors else "NO VALIDO")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
