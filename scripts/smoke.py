"""Prueba de humo (< 2 min): pipeline sobre sample + validador + scorer oficial. Dueño: P1.

    uv run python scripts/smoke.py

Debe estar en verde antes de cada push a main.
"""
import logging
import subprocess
import sys
import time
from pathlib import Path

from bankrec import config, pipeline
from bankrec.validate import phase_txn_ids, validate


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    t0 = time.perf_counter()
    res = pipeline.run("sample")
    errors, warnings = validate(res["submission"], "sample", phase_txn_ids("sample"))
    for w in warnings:
        print("AVISO:", w)
    if errors:
        for e in errors:
            print("ERROR:", e)
        print("SMOKE ROJO: submission no válida")
        sys.exit(1)
    if res["errors"]:
        print("AVISO: errores en resolvers:", res["errors"])
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("eval.py")), "--phase", "sample"], capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.splitlines()[0] if r.returncode == 0 and r.stdout else r.stdout + r.stderr)
    dt = time.perf_counter() - t0
    print(f"SMOKE {'VERDE' if r.returncode == 0 else 'ROJO'} en {dt:.0f}s ({res['txns']} movimientos, {res['rows']} filas)")
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
