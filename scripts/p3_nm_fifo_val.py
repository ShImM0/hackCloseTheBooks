# Análisis OFFLINE (golden): no es un resolver. ¿FIFO con la última parcial acierta en los cobros SIN casar de val?
# Uso: uv run python scripts/p3_nm_fifo_val.py <audit.parquet>
import re, sys
from collections import Counter, defaultdict
import polars as pl
from bankrec import load
au = pl.read_parquet(sys.argv[1])
t = load.table("train", "bank_transactions")
g = load.table("train", "golden_allocations").select("bank_txn_id", "item_id", "amount_cents", "exception", "segment")
inv = load.table("train", "ar_invoices").select("item_id", "customer_id", "issue_date", "total_amount_cents")
cust = dict(zip(inv["item_id"], inv["customer_id"]))
by_c = defaultdict(list)
for i, c, d, tot in inv.iter_rows(): by_c[c].append((d, i, tot))
for c in by_c: by_c[c].sort()
tot = dict(zip(inv["item_id"], inv["total_amount_cents"]))
bd = dict(zip(t["bank_txn_id"], t["booking_date"]))
paid = defaultdict(list)   # item -> [(fecha, txn, importe con signo)]
gold = defaultdict(dict); seg = {}
for b, i, a, e, s in g.iter_rows():
    seg[b] = s
    if i:
        gold[b][i] = a
        paid[i].append((bd[b], b, -a if e == "RETURNED" else (0 if e == "DUPLICATE_PAYMENT" else a)))
un = au.filter(pl.col("resolver") == "nonitem_unmatched")
amt = dict(zip(t["bank_txn_id"], t["amount_cents"]))
C = Counter(); CS = defaultdict(Counter)
for b, r in zip(un["bank_txn_id"], un["reason"]):
    A = amt[b]
    if A <= 0: continue
    m = re.search(r"\((C[A-Z]{2}\d{6,})", r or "") or re.search(r"(ARI-\d{4}-\d+)", r or "")
    if not m: C["sin_tercero"] += 1; continue
    c = m[1] if m[1].startswith("C") else cust.get(m[1])
    d = bd[b]
    rest, pred = A, {}
    for di, i, tt in by_c.get(c, ()):
        if di > d or rest <= 0: continue
        o = tt - sum(a for (dd, bb, a) in paid[i] if (dd, bb) < (d, b))
        if o <= 0: continue
        x = min(o, rest); pred[i] = x; rest -= x
    if rest > 0 or len(pred) < 2: C["no_aplica"] += 1; continue     # sobra dinero o es 1:1
    s = seg.get(b, "?"); G = gold.get(b, {})
    ok = pred.keys() == G.keys() and all(abs(pred[i] - G[i]) <= max(1, G[i] // 200) for i in pred)
    po = sum(1 for i in pred if i in G and abs(pred[i] - G[i]) <= max(1, G[i] // 200))
    CS[s]["txns"] += 1; CS[s]["ok"] += ok; CS[s]["pares_pred"] += len(pred); CS[s]["pares_ok"] += po; CS[s]["pares_gold"] += len(G)
    C["aplica"] += 1; C["ok"] += ok; C["pp"] += len(pred); C["po"] += po
print(dict(C), "precisión pares", round(C["po"] / max(1, C["pp"]), 3))
for s, v in sorted(CS.items(), key=lambda kv: -kv[1]["txns"])[:12]: print(f"{s:28s}", dict(v))
