"""Valida y archiva para envío MANUAL; nunca llama a la plataforma. Dueño: P4.

    uv run python scripts/submit.py out/live/submission.csv.gz --phase live

Solo un fichero válido y con cobertura completa crea copia, manifiesto y registro
PREPARADO (no enviado) en docs/SCOREBOARD.md. FINAL admite un único envío puntuado.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path

from bankrec import config
from bankrec.validate import phase_txn_ids, scorer, submission_rows, validate


def prepare_submission(path: Path, phase: str) -> dict:
    """Prepara un artefacto local verificable; los errores no generan archivos."""
    ids = phase_txn_ids(phase)
    errors, warnings = validate(path, phase, ids)
    if errors:
        return {"errors": errors, "warnings": warnings}
    raw = path.read_bytes()
    # Comprueba que el origen no cambió tras la primera validación.
    result = scorer().score_submission(_empty_golden(ids, phase), raw, str(path))
    if not result['ok'] or result['metrics']['txns_covered'] != len(ids):
        return {"errors": ["El origen cambió o dejó de ser válido; vuelve a preparar"], "warnings": warnings}
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(raw).hexdigest()
    suffix = '.csv.gz' if raw[:2] == b'\x1f\x8b' else '.csv'
    archive = config.out_dir(phase) / f"prepared_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{digest[:12]}{suffix}"
    with archive.open('xb') as fh:
        fh.write(raw)
    unpacked = scorer()._inflate(raw) if suffix.endswith('.gz') else raw
    manifest = {
        'phase': phase, 'status': 'PREPARADO_NO_ENVIADO', 'prepared_utc': now.isoformat(),
        'source': str(path.resolve()), 'archive': str(archive.resolve()), 'sha256': digest,
        'bytes': len(raw), 'uncompressed_bytes': len(unpacked),
        'rows': len(submission_rows(raw)), 'transactions': len(ids), 'warnings': warnings,
    }
    archive.with_suffix(archive.suffix + '.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    scoreboard = config.REPO_ROOT / 'docs' / 'SCOREBOARD.md'
    with scoreboard.open('a', encoding='utf-8', newline='\n') as fh:
        fh.write(f"\n- {now.isoformat()} · P4 · {phase.upper()} PREPARADO, NO ENVIADO: "
                 f"`{archive.relative_to(config.REPO_ROOT)}` · SHA-256 `{digest}` · "
                 f"{len(ids)} movimientos. Nota pendiente de envío manual.\n")
    return {'errors': [], 'warnings': warnings, **manifest}


def _empty_golden(ids, phase):
    """Golden vacío para validar exactamente los bytes que se archivan."""
    from bankrec.validate import categories
    g = scorer().Golden()
    g.exceptions = list(config.EXCEPTIONS)
    g.categories = categories(phase)
    g.txns = {t: ({}, '', '', 'unlabelled') for t in ids}
    g.weight = {t: 1.0 for t in ids}
    return g


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('submission', type=Path)
    ap.add_argument('--phase', required=True, choices=['live', 'final'])
    a = ap.parse_args()
    try:
        result = prepare_submission(a.submission, a.phase)
    except (OSError, ValueError) as exc:
        print(f'NO SUBIR: {exc}')
        return 1
    for w in result['warnings']:
        print('AVISO:', w)
    for e in result['errors']:
        print('ERROR:', e)
    if result['errors']:
        print('NO SUBIR')
        return 1
    print(f"Copia para subir: {result['archive']}")
    print(f"{result['rows']} filas · {result['transactions']} movimientos · "
          f"{result['uncompressed_bytes'] / 1e6:.1f} MB sin comprimir")
    print(f"SHA-256: {result['sha256']}")
    print('LISTO PARA SUBIR A MANO; todavía NO enviado.')
    print('1. Verificar fase y equipo en la plataforma; subir exactamente la copia indicada.')
    if a.phase == 'final':
        print('2. FINAL: un único envío puntuado; confirmar la elección del equipo antes de subir.')
        print('   Un rechazo de validación no consume el envío. Nota oculta hasta las 20:30.')
    else:
        print('2. LIVE: respetar 20 envíos/día y 3 minutos entre envíos; pedir upload-url consume hueco.')
    print('3. Registrar aceptación, hora, identificador y nota real en docs/SCOREBOARD.md.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
