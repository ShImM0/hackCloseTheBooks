# Análisis OFFLINE (golden): no es un resolver. ar_n_to_m "todas citadas y efectivo == suma de saldos reales" que no
# casamos: ¿nuestro ledger tenía otro saldo? Saldo nuestro = total − lo que NUESTRO envío asignó antes a la partida.
import sys
from collections import Counter, defaultdict
import polars as pl
from bankrec import load, refs
au = pl.read_parquet(sys.argv[1]); sub = pl.read_csv(sys.argv[2], infer_schema_length=0)
t = load.table("train", "bank_transactions"); g = load.table("train", "golden_allocations")
inv = load.table("train", "ar_invoices").select("item_id", "document_number", "total_amount_cents")
doc = dict(zip(inv["item_id"], inv["document_number"])); tot = dict(zip(inv["item_id"], inv["total_amount_cents"]))
bd = dict(zip(t["bank_txn_id"], t["booking_date"]))
import datetime as dt
V0 = dt.date(2025, 1, 1)
def ledger(rows):
    p = defaultdict(list)
    for b, i, a, e in rows:
        if i: p[i].append((bd[b], b, -a if e == "RETURNED" else (0 if e == "DUPLICATE_PAYMENT" else a)))
    return p
gp = ledger(g.select("bank_txn_id", "item_id", "amount_cents", "exception").iter_rows())
s2 = sub.filter(pl.col("item_id") != "").with_columns((pl.col("amount").cast(pl.Float64) * 100).round().cast(pl.Int64).alias("c"))
sp = ledger(s2.select("bank_txn_id", "item_id", "c", "exception").iter_rows())
res = dict(zip(au["bank_txn_id"], au["resolver"]))
gv = g.filter(pl.col("segment") == "ar_n_to_m")
tx = {r["bank_txn_id"]: r for r in t.filter((pl.col("booking_date") >= pl.lit(V0)) & pl.col("bank_txn_id").is_in(gv["bank_txn_id"].unique().implode())).iter_rows(named=True)}
C = Counter(); ex = []
for (b,), grp in gv.group_by("bank_txn_id"):
    if b not in tx or res.get(b) in ("multi_ref", "docs_remit", "subset_sum", "nm_partial"): continue
    txn = tx[b]; d = txn["booking_date"]; items = grp["item_id"].to_list()
    toks = [x for x in refs.tokens(refs.joined(refs.haystack(txn))) if len(x) >= 5]
    if not all(refs.ref_score(doc[i], toks) >= 85 for i in items): continue
    real = {i: tot[i] - sum(a for dd, bb, a in gp[i] if (dd, bb) < (d, b)) for i in items}
    if txn["amount_cents"] != sum(real.values()): continue
    pre = {i: sum(a for dd, bb, a in gp[i] if dd < V0) for i in items}      # lo que reconstruye open_at
    ours = {i: tot[i] - pre[i] - sum(a for dd, bb, a in sp[i] if (dd, bb) < (d, b) and dd >= V0) for i in items}
    real_full = real
    C["casos"] += 1
    C["ledger_igual"] += all(ours[i] == real_full[i] for i in items)
    C["ledger_mayor(pago previo sin casar)"] += any(ours[i] > real_full[i] for i in items)
    C["ledger_menor(otro consumió)"] += any(ours[i] < real_full[i] for i in items)
    if all(ours[i] == real_full[i] for i in items) and len(ex) < 8:
        ex.append((b, res.get(b), txn["amount_cents"], [doc[i] for i in items], txn["text"][:90]))
print(dict(C))
for e in ex: print(e)
