# Análisis OFFLINE (lee golden): no es un resolver. Uso: uv run python scripts/p3_nm_sim.py [segmento]
"""ar_n_to_m: ¿el golden es FIFO secuencial por cliente (facturas por issue_date, la última parcial)?
Ledger por cliente en orden de booking_date; saldo inicial = total (sample no trae saldos)."""
import sys
from collections import defaultdict
import duckdb
D = 'bankrec-docs-and-masters/sample/'
seg = sys.argv[1] if len(sys.argv) > 1 else 'ar_n_to_m'
con = duckdb.connect()
g = con.sql(f"select bank_txn_id, item_id, round(amount*100)::bigint a, exception, segment from '{D}golden_allocations.csv.gz'").fetchall()
inv = con.sql(f"select item_id, customer_id, issue_date, round(total_amount*100)::bigint c from '{D}ar_invoices.csv.gz'").fetchall()
bt = {r[0]: r[1:] for r in con.sql(f"select bank_txn_id, booking_date, round(amount*100)::bigint from '{D}bank_transactions.csv.gz'").fetchall()}
cust = {i: c for i, c, _, _ in inv}
info = {i: (d, c) for i, _, d, c in inv}
by_c = defaultdict(list)
for i, c, d, tot in inv: by_c[c].append(i)
gold = defaultdict(dict); segof = {}
for t, i, a, e, s in g:
    if i: gold[t][i] = a
    segof[t] = s
# cobros del segmento, por cliente, en orden
txc = defaultdict(list)
for t in gold:
    if segof[t] != seg: continue
    cs = {cust.get(i) for i in gold[t]}
    if len(cs) == 1: txc[cs.pop()].append(t)
# saldo: total menos lo cobrado por movimientos de OTROS segmentos (aproximación del ledger real)
paid_other = defaultdict(int)
for t in gold:
    if segof[t] != seg:
        for i, a in gold[t].items(): paid_other[i] += a
res = defaultdict(int); bad = []
for c, ts in txc.items():
    open_ = {i: info[i][1] - paid_other[i] for i in by_c[c]}
    for t in sorted(ts, key=lambda t: (bt[t][0], t)):
        A, d = bt[t][1], bt[t][0]
        pend = sorted([i for i in open_ if open_[i] > 0 and info[i][0] <= d], key=lambda i: (info[i][0], i))
        pred, rest = {}, A
        for i in pend:
            if rest <= 0: break
            x = min(rest, open_[i]); pred[i] = x; rest -= x
        ok = pred.keys() == gold[t].keys() and all(abs(pred[i] - gold[t][i]) <= max(1, gold[t][i] // 200) for i in pred)
        res['ok' if ok else 'mal'] += 1
        res['pares_ok'] += sum(1 for i in pred if i in gold[t] and abs(pred[i] - gold[t][i]) <= max(1, gold[t][i] // 200))
        res['pares_pred'] += len(pred); res['pares_gold'] += len(gold[t])
        if not ok: bad.append(t)
        for i, a in gold[t].items(): open_[i] = open_.get(i, 0) - a   # sigue con el golden para no arrastrar errores
print(seg, dict(res)); print('fallos FIFO:', bad[:15])
