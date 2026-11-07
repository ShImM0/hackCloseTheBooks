"""Evalúa una submission contra el golden con el scorer oficial. Dueño: P4 (versión inicial de P1).

    uv run python scripts/eval.py --phase val|sample [--sub RUTA] [--months N] [--segment X] [--dump N]

- Imprime métricas, P/R/F1 por clase de excepción (conteo por fila del scorer), la confusión predicho→golden de
  las filas sin partida y la precisión por segmento (golden.segment). Todo queda en out/<fase>/metrics.json.
- --dump N vuelca N movimientos fallados del segmento (--segment) o de todos a out/<fase>/failures_<segmento>.md:
  movimiento, golden, predicción y resolver/razón del audit.
- Para val escribe antes un golden y una lista de movimientos filtrados en out/val/ (el golden completo de
  TRAIN son 5,4 M filas y el scorer lo carga en dicts de Python).
"""
import argparse
import json
import sys
from pathlib import Path

import polars as pl

from bankrec import config, phase as phase_mod
from bankrec.validate import categories, scorer, submission_rows


def golden_files(phase: str, months):
    """Golden y lista de movimientos de la fase/ventana, en out/<fase>/ (csv.gz para el scorer oficial)."""
    folder = config.PHASES[phase][0]
    start, end = phase_mod.window(phase, months)
    from bankrec import load
    t = load.table(folder, "bank_transactions")
    if t is None:
        raise FileNotFoundError(f"No hay bank_transactions en {config.folder_dir(folder)}")
    t = t.filter((pl.col("booking_date") >= start) & (pl.col("booking_date") < end))
    g = load.table(folder, "golden_allocations").filter(pl.col("bank_txn_id").is_in(t["bank_txn_id"].implode()))
    out = config.out_dir(phase)
    gp, tp = out / "golden.csv.gz", out / "txns.csv.gz"
    import gzip
    with gzip.open(gp, "wb") as fh:
        g.select("bank_txn_id", "item_id", "amount", "category", "exception", "segment", "difficulty").write_csv(fh)
    with gzip.open(tp, "wb") as fh:
        t.select("bank_txn_id").write_csv(fh)
    return gp, tp, t, g


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="val", choices=["val", "sample"])
    ap.add_argument("--sub", default=None)
    ap.add_argument("--months", type=int, default=None)
    ap.add_argument("--segment", default=None)
    ap.add_argument("--dump", type=int, default=0)
    a = ap.parse_args()
    S = scorer()
    sub = a.sub or str(config.out_dir(a.phase) / "submission.csv.gz")
    gp, tp, txns, gold = golden_files(a.phase, a.months)
    G = S.load_golden(str(gp), str(tp), config.EXCEPTIONS, categories(a.phase))
    res = S.score_submission(G, Path(sub).read_bytes(), sub)
    if not res["ok"]:
        print(json.dumps(res["errors"], indent=1, ensure_ascii=False))
        sys.exit(1)
    m = res["metrics"]
    rows = submission_rows(Path(sub).read_bytes())
    excs = exception_report(G, rows)
    conf = nonitem_confusion(G, rows)
    report = config.out_dir(a.phase) / 'metrics.json'
    report.write_text(json.dumps({'phase': a.phase, 'months': a.months, 'submission': sub, **res,
                                  'exceptions': excs, 'nonitem_confusion': conf},
                                 ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"SCORE {res['score']:.2f} | allocF1 {m['allocation_f1']:.4f} (P {m['precision']:.4f} R {m['recall']:.4f}) | "
          f"txnAcc {m['txn_accuracy']:.4f} | excF1 {m['exception_macro_f1']:.4f} | amtAcc {m['amount_weighted_accuracy']:.4f}")
    print(f"{'excepción':20s} {'gold':>7s} {'pred':>7s} {'tp':>7s} {'P':>6s} {'R':>6s} {'F1':>6s}  (por fila, como el scorer)")
    for e in sorted(excs, key=lambda e: e['f1'] if e['f1'] is not None else 2):
        f1 = f"{e['f1']:6.3f}" if e['f1'] is not None else "    — "
        print(f"{e['exception']:20s} {e['gold']:7d} {e['pred']:7d} {e['tp']:7d} {e['precision']:6.3f} {e['recall']:6.3f} {f1}")
    print("sin partida, predicho -> golden (ITEM = el golden trae partidas), mayores errores:")
    for c in conf[:12]:
        print(f"  {c['pred']:18s} -> {c['gold']:18s} {c['n']:7d}")
    segs = sorted(res["segments"].items(), key=lambda x: -x[1]["n"] * (1 - x[1]["accuracy"]))
    print(f"{'segmento':32s} {'n':>7s} {'acc':>6s} {'fallos':>7s}")
    for k, v in segs:
        if a.segment and k != a.segment:
            continue
        print(f"{k:32s} {v['n']:7d} {v['accuracy']:6.3f} {round(v['n'] * (1 - v['accuracy'])):7d}")
    if a.dump:
        dump(a, sub, txns, gold, G, S)


def exception_report(G, rows) -> list[dict]:
    """P/R/F1 por clase de excepción contadas por fila igual que starter/bankrec_score.py.

    Fila con partida: acierto si su excepción coincide con la del golden para ese (txn, item), sin mirar importe.
    Fila sin partida: acierto solo si el golden no trae partidas y su excepción coincide.
    Solo entran en la media las clases con algún caso en el golden (las demás salen con F1 None).
    """
    tp, fp, gold = {}, {}, {}
    for gi, _gcat, gexc, _seg in G.txns.values():
        for _amt, ge in (gi.values() if gi else ()):
            if ge:
                gold[ge] = gold.get(ge, 0) + 1
        if not gi and gexc:
            gold[gexc] = gold.get(gexc, 0) + 1
    for r in rows:
        exc = r['exception']
        if not exc or r['bank_txn_id'] not in G.txns:
            continue
        gi, _gcat, gexc, _seg = G.txns[r['bank_txn_id']]
        if r['item_id']:
            hit = gi.get(r['item_id'], (None, None))[1] == exc
        else:
            hit = not gi and gexc == exc
        (tp if hit else fp)[exc] = (tp if hit else fp).get(exc, 0) + 1
    out = []
    for c in config.EXCEPTIONS:
        if not c:
            continue
        t, f, g = tp.get(c, 0), fp.get(c, 0), gold.get(c, 0)
        p_ = t / (t + f) if t + f else 0.0
        r_ = t / g if g else 0.0
        f1 = (2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0.0) if g else None
        out.append({'exception': c, 'gold': g, 'pred': t + f, 'tp': t, 'precision': p_, 'recall': r_, 'f1': f1})
    return out


def nonitem_confusion(G, rows) -> list[dict]:
    """Pares (predicho, golden) distintos cuando alguno de los dos no trae partidas; ITEM = trae partidas."""
    pred = {}
    for r in rows:
        pred.setdefault(r['bank_txn_id'], 'ITEM' if r['item_id'] else (r['category'] or ''))
    cells = {}
    for t, (gi, gcat, _gexc, _seg) in G.txns.items():
        g, p = ('ITEM' if gi else gcat), pred.get(t, '(falta)')
        if g != p:
            cells[(p, g)] = cells.get((p, g), 0) + 1
    return [{'pred': p, 'gold': g, 'n': n} for (p, g), n in sorted(cells.items(), key=lambda x: -x[1])]


def _rows_by_txn(df: pl.DataFrame) -> dict:
    out = {}
    for r in df.iter_rows(named=True):
        out.setdefault(r["bank_txn_id"], []).append(r)
    return out


def dump(a, sub, txns, gold, G, S):
    """Vuelca fallos: compara (item, importe±tol, excepción) / (categoría, excepción) como el scorer."""
    seg = a.segment
    pred = pl.DataFrame(submission_rows(Path(sub).read_bytes()), schema={k: pl.String for k in config.HEADER})
    if seg:
        ids = gold.filter(pl.col("segment") == seg)["bank_txn_id"].unique()
        pred = pred.filter(pl.col("bank_txn_id").is_in(ids.implode()))
        gold = gold.filter(pl.col("bank_txn_id").is_in(ids.implode()))
    P, Gr = _rows_by_txn(pred), _rows_by_txn(gold)
    audit_p = config.out_dir(a.phase) / "audit.parquet"
    audit = {r["bank_txn_id"]: r for r in pl.read_parquet(audit_p).iter_rows(named=True)} if audit_p.exists() else {}
    tx = {r["bank_txn_id"]: r for r in txns.filter(pl.col("bank_txn_id").is_in(list(Gr))).iter_rows(named=True)}

    def ok(t):
        gi, gcat, gexc, _ = G.txns[t]
        ps = P.get(t, [])
        if not gi:
            return len(ps) == 1 and not ps[0]["item_id"] and (ps[0]["category"] or "") == gcat and (ps[0]["exception"] or "") == gexc
        pi = {r["item_id"]: r for r in ps if r["item_id"]}
        return len(pi) == len(gi) == len(ps) and all(
            i in pi and abs(float(pi[i]["amount"].replace(',', '.')) - ga) <= S._tol(ga) and (pi[i]["exception"] or "") == ge for i, (ga, ge) in gi.items())

    fails = [t for t in sorted(Gr) if not ok(t)][: a.dump]
    # Solo se construyen partidas si se solicitaron fallos; no usa el golden para ordenarlas.
    items = phase_mod.build(a.phase, a.months).items if fails else pl.DataFrame()
    path = config.out_dir(a.phase) / f"failures_{seg or 'all'}.md"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# Fallos {a.phase} · segmento {seg or 'todos'} ({len(fails)} mostrados)\n\n")
        for t in fails:
            x = tx.get(t, {})
            fh.write(f"## {t} · {x.get('booking_date')} · {x.get('account_id')} · {x.get('amount')} {x.get('currency')}\n")
            fh.write(f"- text: `{x.get('text')}`\n- ref1/ref2: `{x.get('ref1')}` / `{x.get('ref2')}` · cpty: `{x.get('counterparty_name')}` "
                     f"`{x.get('counterparty_account')}` · rmt: `{x.get('remittance_info')}` · e2e: `{x.get('end_to_end_id')}`\n")
            au = audit.get(t, {})
            fh.write(f"- resolver: {au.get('resolver')} ({au.get('confidence')}) {au.get('reason') or ''}\n")
            fh.write("- golden: " + "; ".join(f"{r['item_id'] or r['category']} {r['amount'] or ''} {r['exception'] or ''} [{r['segment']}]" for r in Gr[t]) + "\n")
            fh.write("- pred:   " + "; ".join(f"{r['item_id'] or r['category']} {r['amount'] or ''} {r['exception'] or ''}" for r in P.get(t, [])) + "\n\n")
            fh.write('- candidatas (heurística por referencia o importe; no implica asignación):\n')
            for item in candidates(x, items):
                fh.write(f"  - {item['item_id']} · {item['document_number']} · "
                         f"total={item['total_cents']} céntimos · saldo inicial={item['open_cents']} céntimos · "
                         f"{item['currency']} · referencia={item['ref_match']} · Δ={item['gap']} céntimos\n")
            fh.write('\n')
    print(f"{len(fails)} fallos -> {path}")


def candidates(txn, items, limit=5):
    """Diagnóstico acotado: referencias o importe cercano, sin leer etiquetas."""
    from bankrec.refs import norm
    if items.is_empty() or not txn:
        return []
    text = norm(' '.join(txn.get(k) or '' for k in ('text', 'ref1', 'ref2', 'remittance_info', 'end_to_end_id')))
    amount = abs(txn['amount_cents'])
    pool = items.filter((pl.col('entity') == txn['entity']) &
                        (pl.col('currency') == txn['currency']) &
                        (pl.col('issue_date') <= txn['booking_date']))
    found = []
    for r in pool.iter_rows(named=True):
        ref = norm(r.get('document_number'))
        match = len(ref) >= 6 and ref in text
        gap = min(abs(amount - (r.get(k) or 0)) for k in ('total_cents', 'open_cents'))
        if match or gap <= max(1, amount // 200):
            found.append({**r, 'ref_match': match, 'gap': gap})
    return sorted(found, key=lambda r: (not r['ref_match'], r['gap'], r['issue_date'], r['item_id']))[:limit]


if __name__ == "__main__":
    main()
