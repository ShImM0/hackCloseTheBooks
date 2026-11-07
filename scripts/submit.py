"""Preparación de envíos. Dueño: P4. SOLO P4 envía, y A MANO.

Este script NO llama a la API de la plataforma (Claude Code lo tiene denegado en .claude/settings.json).
Qué hace: valida el fichero, imprime tamaño, filas, hash y la checklist de envío.

    uv run python scripts/submit.py out/live/submission.csv.gz --phase live

TODO(P4): checklist FINAL (validar, copia con hora en out/final/, registrar en docs/SCOREBOARD.md).
"""
import argparse
import gzip
import hashlib
import sys
from pathlib import Path

from bankrec.validate import phase_txn_ids, validate


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--phase", required=True, choices=["live", "final"])
    a = ap.parse_args()
    p = Path(a.submission)
    errors, warnings = validate(p, a.phase, phase_txn_ids(a.phase))
    raw = p.read_bytes()
    size = len(gzip.decompress(raw)) if raw[:2] == b"\x1f\x8b" else len(raw)
    print(f"{p} · {size / 1e6:.1f} MB sin comprimir · sha256 {hashlib.sha256(raw).hexdigest()[:16]}")
    for w in warnings:
        print("AVISO:", w)
    for e in errors:
        print("ERROR:", e)
    print("LISTO PARA SUBIR A MANO" if not errors else "NO SUBIR")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
