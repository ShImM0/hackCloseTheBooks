"""Auditoría offline de nonitem, sin entrenar ni cambiar reglas. Dueño: P4.

    uv run python scripts/p4_category_audit.py --years 2023 2024

POLICIES §4 define las categorías. Se evalúa el resolver sobre TODOS los
movimientos, incluyendo partidas, para contar categorías que interceptan pagos.
El golden solo se une después de resolver. No es un scorer del pipeline completo.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from bankrec import config, load
from bankrec.resolvers import nonitem


def summarize(frame):
    """Precisión de propuestas y recall sobre movimientos sin partida."""
    predicted = frame.filter(pl.col('predicted') != '').height
    actual = frame.filter(pl.col('category') != '').height
    correct = frame.filter((pl.col('predicted') != '') & (pl.col('predicted') == pl.col('category'))).height
    intercepted = frame.filter((pl.col('predicted') != '') & (pl.col('category') == '')).height
    return dict(transactions=frame.height, predicted=predicted, actual_nonitem=actual,
                correct=correct, false_categories=predicted-correct, intercepted_item_payments=intercepted,
                precision=correct/predicted if predicted else None,
                recall=correct/actual if actual else None)


def breakdown(frame, keys):
    """Agregados por dimensiones sin volcar movimientos ni textos."""
    return [{**dict(zip(keys, values)), **summarize(group)}
            for values, group in frame.group_by(keys, maintain_order=True)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=int, nargs='+', default=[2023, 2024])
    a = ap.parse_args()
    if any(year >= 2025 for year in a.years):
        ap.error('Este análisis histórico usa años anteriores a la validación 2025')
    txns = load.table('train', 'bank_transactions')
    if txns is None:
        raise FileNotFoundError('Falta TRAIN/bank_transactions.csv.gz')
    txns = txns.filter(pl.col('booking_date').dt.year().is_in(a.years)).sort('booking_date', 'bank_txn_id')
    ctx = SimpleNamespace(txns=txns, shared={})
    nonitem.prepare(ctx)
    rows = []
    for t in txns.iter_rows(named=True):
        proposal = nonitem.resolve(t, ctx, None)
        rows.append((t['bank_txn_id'], proposal.category if proposal else ''))
    predicted = pl.DataFrame(rows, schema=['bank_txn_id', 'predicted'], orient='row')
    # El resolver ya terminó: a partir de aquí solo se calculan métricas offline.
    gold = load.table('train', 'golden_allocations')
    labels = gold.group_by('bank_txn_id').agg(pl.col('category').fill_null('').first())
    frame = (txns.select('bank_txn_id', 'bank', 'entity', 'statement_format',
                         pl.col('booking_date').dt.year().alias('year'),
                         pl.col('booking_date').dt.strftime('%Y-%m').alias('month'))
             .join(predicted, on='bank_txn_id').join(labels, on='bank_txn_id', how='left'))
    if frame['category'].null_count():
        raise ValueError('Hay movimientos sin etiqueta: no publicar métricas incompletas')
    report = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'years': a.years,
        'resolver_sha256': hashlib.sha256(Path(nonitem.__file__).read_bytes()).hexdigest(),
        'overall': summarize(frame),
        'by_year': breakdown(frame, ['year']),
        'by_bank_format': breakdown(frame, ['bank', 'statement_format']),
        'by_month': breakdown(frame, ['month']),
        'by_entity': breakdown(frame, ['entity']),
        'by_category': [],
    }
    cats = sorted(set(frame['predicted']) | set(frame['category']))
    for cat in cats:
        if not cat:
            continue
        p = frame.filter(pl.col('predicted') == cat).height
        g = frame.filter(pl.col('category') == cat).height
        tp = frame.filter((pl.col('predicted') == cat) & (pl.col('category') == cat)).height
        report['by_category'].append(dict(category=cat, predicted=p, actual=g, correct=tp,
            precision=tp/p if p else None, recall=tp/g if g else None))
    out = config.OUT_DIR / 'qa'
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'p4_category_history.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'overall': report['overall'], 'by_year': report['by_year']}, ensure_ascii=False, indent=2))
    print(path)


if __name__ == '__main__':
    main()
