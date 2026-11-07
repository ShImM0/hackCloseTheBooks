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
import importlib.util
import io
import re
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
    if t is None:
        raise FileNotFoundError(f"No hay bank_transactions para {phase} en {config.folder_dir(config.PHASES[phase][0])}")
    return t.filter((pl.col("booking_date") >= start) & (pl.col("booking_date") < end))["bank_txn_id"].to_list()


def categories(phase: str) -> list[str]:
    return load.master("categories", phase)["category"].to_list()


def submission_rows(data: bytes) -> list[dict[str, str]]:
    """Lee las variantes aceptadas por el scorer; llamar después de validar."""
    raw = scorer()._inflate(data) if data[:2] == b"\x1f\x8b" else data
    stream = io.StringIO(raw.decode("utf-8-sig"), newline="")
    first = stream.readline()
    delim = ";" if first.count(";") >= 4 and first.count(",") < 4 else ","
    header = [h.strip().lower() for h in next(csv.reader([first], delimiter=delim))]
    return [{k: (row[i] if i < len(row) else "").strip() for i, k in enumerate(header)}
            for row in csv.reader(stream, delimiter=delim) if row and any(c.strip() for c in row)]


def unknown_item_ids(phase: str, submitted: set[str]) -> set[str]:
    """Busca solo ids enviados en tablas de partidas actuales y anteriores, sin golden."""
    remaining = set(submitted)
    folders = [config.PHASES[phase][0], *config.PRIOR_FOLDERS.get(phase, [])]
    for folder in dict.fromkeys(folders):
        for name in ('open_items_at_start', 'ar_invoices', 'ap_invoices', 'credit_notes'):
            if not remaining:
                return remaining
            table = load.table(folder, name)
            if table is not None:
                found = table.filter(pl.col('item_id').is_in(list(remaining)))['item_id']
                remaining.difference_update(found.to_list())
    return remaining


def validate(path: Path, phase: str, txn_ids: list[str] | None = None, known_items: set[str] | None = None,
             *, check_known_items: bool = False) -> tuple[list[str], list[str]]:
    S = scorer()
    g = S.Golden()
    g.exceptions = list(config.EXCEPTIONS)
    g.categories = categories(phase)
    ids = txn_ids if txn_ids is not None else phase_txn_ids(phase)
    g.txns = {t: ({}, "", "", "unlabelled") for t in ids}
    g.weight = {t: 1.0 for t in ids}
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return [f"No se pudo leer {path}: {exc}"], []
    res = S.score_submission(g, data, str(path))
    errors = [f"fila {e['row']}: {e['message']}" for e in res["errors"]]
    warnings: list[str] = []
    if res["ok"]:
        m = res["metrics"]
        if m["txns_covered"] != m["txns_total"]:
            errors.append(f"cubre {m['txns_covered']} de {m['txns_total']} movimientos")
        rd = submission_rows(data)
        submitted = {r['item_id'] for r in rd if r['item_id']}
        missing = (submitted - known_items if known_items is not None else
                   unknown_item_ids(phase, submitted) if check_known_items else set())
        bad_dec, unknown = 0, 0
        for r in rd:
            a = r["amount"]
            if a and not re.fullmatch(r"\d+[.,]\d{2}", a):
                bad_dec += 1
            if r["item_id"] in missing:
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
    errors, warnings = validate(Path(a.submission), a.phase, phase_txn_ids(a.phase, a.months), check_known_items=True)
    for w in warnings:
        print("AVISO:", w)
    for e in errors:
        print("ERROR:", e)
    print("VALIDO" if not errors else "NO VALIDO")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
