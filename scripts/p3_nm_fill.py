# Análisis OFFLINE (lee golden): no es un resolver. Uso: uv run python scripts/p3_nm_fill.py
"""Cobros donde text_ref casa UNA factura y el importe la supera: ¿el resto va FIFO a las demás abiertas del cliente?
Saldo de cada factura = total − lo cobrado por movimientos anteriores (golden). Sample: saldo inicial = total."""
from collections import defaultdict
import duckdb
D = 'bankrec-docs-and-masters/sample/'
con = duckdb.connect()
g = con.sql(f"select bank_txn_id, item_id, round(amount*100)::bigint, segment from '{D}golden_allocations.csv.gz' where item_id is not null").fetchall()
inv = {r[0]: r[1:] for r in con.sql(f"select item_id, customer_id, issue_date, round(total_amount*100)::bigint from '{D}ar_invoices.csv.gz'").fetchall()}
bt = {r[0]: r[1:] for r in con.sql(f"select bank_txn_id, booking_date, round(amount*100)::bigint from '{D}bank_transactions.csv.gz'").fetchall()}
pred1 = con.sql("select p.bank_txn_id, p.item_id, round(p.amount*100)::bigint from 'out/sample/submission.csv.gz' p join 'out/sample/audit.parquet' a using(bank_txn_id) where a.resolver='text_ref' and p.item_id like 'ARI%'").fetchall()
by_c = defaultdict(list)
for i, (c, d, tot) in inv.items(): by_c[c].append(i)
gold = defaultdict(dict); seg = {}; paid = defaultdict(list)
for t, i, a, s in g: gold[t][i] = a; seg[t] = s; paid[i].append((bt[t][0], t, a))
n1 = defaultdict(list)
for t, i, a in pred1: n1[t].append((i, a))
tol = lambda x, y: abs(x - y) <= max(1, y // 200)
res = defaultdict(lambda: defaultdict(int))
for t, lst in n1.items():
    if len(lst) != 1: continue
    i0, a0 = lst[0]
    if i0 not in inv: continue
    d, A = bt[t]; c = inv[i0][0]
    bal = lambda i: inv[i][2] - sum(a for (dd, tt, a) in paid[i] if (dd, tt) < (d, t))
    open0 = bal(i0)
    if A <= open0: continue                        # solo cobros que superan la factura citada
    rest = A - open0; pred = {i0: open0}
    for i in sorted((i for i in by_c[c] if i != i0 and inv[i][1] <= d and bal(i) > 0), key=lambda i: (inv[i][1], i)):
        if rest <= 0: break
        x = min(rest, bal(i)); pred[i] = x; rest -= x
    s = seg.get(t, 'sin_golden')
    ok = pred.keys() == gold[t].keys() and all(tol(pred[i], gold[t][i]) for i in pred)
    r = res[s]; r['txns'] += 1; r['ok'] += ok; r['pares_pred'] += len(pred)
    r['pares_ok'] += sum(1 for i in pred if i in gold[t] and tol(pred[i], gold[t][i])); r['pares_gold'] += len(gold[t])
    r['hoy_ok'] += (gold[t].keys() == {i0})
for s, r in sorted(res.items(), key=lambda kv: -kv[1]['txns']): print(f"{s:28s}", dict(r))
