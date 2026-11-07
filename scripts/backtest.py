"""Validación temporal reproducible sobre TRAIN, con salidas aisladas de out/val.

Uso: uv run python scripts/backtest.py --start 2024-07-01 --end 2024-08-01
El corte de entrenamiento es --start. Conserva las limitaciones de reconstrucción de saldos de phase.build(val).
"""
import argparse
import datetime as dt
import logging
import os
from pathlib import Path
import runpy
import subprocess
import sys

if __name__ == "__main__" and os.environ.get("PYTHONHASHSEED") != "0":
    sys.exit(subprocess.call([sys.executable, *sys.argv], env={**os.environ, "PYTHONHASHSEED": "0"}))

from bankrec import config, load, pipeline
from bankrec.validate import phase_txn_ids, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--end", required=True, type=dt.date.fromisoformat, help="fecha final exclusiva")
    parser.add_argument("--out", type=Path, help="carpeta raíz de resultados (se crea un subdirectorio val)")
    parser.add_argument("--disable", default="")
    args = parser.parse_args()
    dates = load.table("train", "bank_transactions")["booking_date"]
    if not dates.min() < args.start < args.end <= dates.max() + dt.timedelta(days=1):
        parser.error("la ventana debe estar dentro de TRAIN y disponer de histórico anterior")
    config.PHASES["val"] = ("train", str(args.start), str(args.end))
    config.OUT_DIR = args.out or config.OUT_DIR / "backtests" / f"{args.start}_{args.end}"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = pipeline.run("val", set(filter(None, args.disable.split(","))))
    errors, warnings = validate(result["submission"], "val", phase_txn_ids("val"))
    for warning in warnings:
        print("AVISO:", warning)
    if errors or result["errors"]:
        raise RuntimeError(f"backtest falló: {errors}, resolvers: {result['errors']}")
    sys.argv = ["eval.py", "--phase", "val"]
    runpy.run_path(str(Path(__file__).with_name("eval.py")), run_name="__main__")


if __name__ == "__main__":
    main()
