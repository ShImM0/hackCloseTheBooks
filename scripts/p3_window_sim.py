# Análisis OFFLINE (lee golden): no es un resolver. Uso: uv run python scripts/p3_window_sim.py ar_1toN_credit_note
"""Simulación offline (sample): ventana contigua de facturas del cliente por fecha, netas de sus abonos ligados.
Usa el golden SOLO para conocer el cliente y excluir partidas cobradas por otros movimientos (análisis, no resolver)."""
import sys, duckdb
from collections import defaultdict
D = 'bankrec-docs-and-masters/sample/'
con = duckdb.connect()
g = con.sql(f"select * from '{D}golden_allocations.csv.gz'").pl()
inv = con.sql(f"select item_id, customer_id, document_number, issue_date, round(total_amount*100)::bigint c from '{D}ar_invoices.csv.gz'").pl()
cn = con.sql(f"select item_id, party_id, related_document, issue_date, round(total_amount*100)::bigint c from '{D}credit_notes.csv.gz' where side='AR'").pl()
bt = con.sql(f"select bank_txn_id, booking_date, round(amount*100)::bigint a from '{D}bank_transactions.csv.gz'").pl()
seg = sys.argv[1] if len(sys.argv) > 1 else 'ar_1toN_credit_note'
amt = dict(zip(bt['bank_txn_id'], bt['a'])); bdate = dict(zip(bt['bank_txn_id'], bt['booking_date']))
paid_by = defaultdict(set)
for t, i in zip(g['bank_txn_id'], g['item_id']):
    if i: paid_by[i].add(t)
cust_of = dict(zip(inv['item_id'], inv['customer_id']))
inv_by_c = defaultdict(list)
for r in inv.iter_rows(named=True): inv_by_c[r['customer_id']].append(r)
cn_by_doc = defaultdict(list)
for r in cn.iter_rows(named=True): cn_by_doc[r['related_document']].append(r)
gs = g.filter(g['segment'] == seg)
res = defaultdict(int); status = {}
for t, grp in gs.group_by('bank_txn_id'):
    t = t[0]; gold = set(grp['item_id'])
    custs = {cust_of[i] for i in gold if i in cust_of}
    if len(custs) != 1: res['cliente_no_unico'] += 1; continue
    c = custs.pop()
    # "abiertas": del cliente, emitidas antes del cobro, no cobradas por OTRO movimiento
    op = sorted([r for r in inv_by_c[c] if r['issue_date'] <= bdate[t] and not (paid_by[r['item_id']] - {t})],
                key=lambda r: (r['issue_date'], r['item_id']))
    net = []
    for r in op:
        arcs = [x for x in cn_by_doc[r['document_number']] if not (paid_by[x['item_id']] - {t})]
        net.append((r['c'] - sum(x['c'] for x in arcs), {r['item_id']} | {x['item_id'] for x in arcs}))
    A = amt[t]; sols = []
    for i in range(len(net)):
        s = 0
        for j in range(i, len(net)):
            s += net[j][0]
            if s == A: sols.append((i, j))
            if s > A + 10**9: break
    pre = [x for x in sols if x[0] == 0]
    if pre and len(pre) == 1:
        pp = set().union(*[net[k][1] for k in range(0, pre[0][1] + 1)]); res['prefijo_unico_' + ('ok' if pp == gold else 'mal')] += 1
    status[t] = 'sin_ventana' if not sols else ('unica' if len(sols) == 1 else 'varias')
    if not sols: res['sin_ventana'] += 1; continue
    if len(sols) > 1: res['varias_ventanas'] += 1
    i, j = sols[0]
    pred = set().union(*[net[k][1] for k in range(i, j + 1)])
    res['unica_ok' if len(sols) == 1 and pred == gold else ('unica_mal' if len(sols) == 1 else 'varias')] += 1
    res['empieza_en_la_mas_antigua'] += (i == 0)
print(seg, dict(res))

import json; json.dump(status, open(f'out/sample/p3_window_{seg}.json', 'w', encoding='utf-8'))
