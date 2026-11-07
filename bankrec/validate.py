"""Validador local que replica el del servidor. Dueño: P4 (versión inicial de P1).

Usa el scorer oficial (starter/bankrec_score.py) con un golden vacío: sus errores son exactamente los del servidor.
Añade comprobaciones propias:
- ERROR: no cubre el 100 % de los movimientos de la fase (lo que falta puntúa como fallo);
- ERROR: tamaño > 60 MB o > 3 M filas (ya lo hace el scorer);
- AVISO: item_id que no existe en las partidas conocidas (el servidor no lo rechaza, pero es un FP seguro);
- AVISO: importes sin exactamente 2 decimales (estilo de la casa).

Uso: uv run python -m bankrec.validate out/live/submission.csv.gz --phase live
"""
from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import io
import sys
from pathlib import Path

import polars as pl

from bankrec import config, load


def scorer():
    """Módulo oficial starter/bankrec_score.py (no se copia: se importa por ruta)."""
    spec = importlib.util.spec_from_file_location("bankrec_score", config.STARTER_DIR / "bankrec_score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def phase_txn_ids(phase: str, months: int | None = None) -> list[str]:
    from bankrec.phase import window
    start, end = window(phase, months)
    t = load.table(config.PHASES[phase][0], "bank_transactions")
    return t.filter((pl.col("booking_date") >= start) & (pl.col("booking_date") < end))["bank_txn_id"].to_list()


def categories(phase: str) -> list[str]:
    return load.master("categories", phase)["category"].to_list()


def validate(path: Path, phase: str, txn_ids: list[str] | None = None, known_items: set[str] | None = None) -> tuple[list[str], list[str]]:
    S = scorer()
    g = S.Golden()
    g.exceptions = list(config.EXCEPTIONS)
    g.categories = categories(phase)
    ids = txn_ids if txn_ids is not None else phase_txn_ids(phase)
    g.txns = {t: ({}, "", "", "unlabelled") for t in ids}
    g.weight = {t: 1.0 for t in ids}
    data = Path(path).read_bytes()
    res = S.score_submission(g, data, str(path))
    errors = [f"fila {e['row']}: {e['message']}" for e in res["errors"]]
    warnings: list[str] = []
    if res["ok"]:
        m = res["metrics"]
        if m["txns_covered"] != m["txns_total"]:
            errors.append(f"cubre {m['txns_covered']} de {m['txns_total']} movimientos")
        raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        rd = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        bad_dec, unknown = 0, 0
        for r in rd:
            a = r["amount"]
            if a and (len(a) < 4 or a[-3] != "."):
                bad_dec += 1
            if known_items is not None and r["item_id"] and r["item_id"] not in known_items:
                unknown += 1
        if bad_dec:
            warnings.append(f"{bad_dec} importes sin 2 decimales")
        if unknown:
            warnings.append(f"{unknown} item_id desconocidos (FP seguros)")
    return errors, warnings


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--phase", required=True, choices=list(config.PHASES))
    ap.add_argument("--months", type=int, default=None)
    a = ap.parse_args()
    errors, warnings = validate(Path(a.submission), a.phase, phase_txn_ids(a.phase, a.months))
    for w in warnings:
        print("AVISO:", w)
    for e in errors:
        print("ERROR:", e)
    print("VALIDO" if not errors else "NO VALIDO")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
