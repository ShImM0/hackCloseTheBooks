# Análisis OFFLINE (golden): no es un resolver. ar_n_to_m de val con alguna factura citada que hoy no se casan entero.
# Uso: uv run python scripts/p3_nm_cited.py <audit.parquet> <submission.csv.gz>
import sys
from collections import Counter, defaultdict
import polars as pl
from bankrec import load, refs
au = pl.read_parquet(sys.argv[1]); sub = pl.read_csv(sys.argv[2], infer_schema_length=0)
t = load.table("train", "bank_transactions")
g = load.table("train", "golden_allocations")
inv = load.table("train", "ar_invoices").select("item_id", "customer_id", "document_number", "issue_date", "total_amount_cents")
info = {r[0]: r[1:] for r in inv.iter_rows()}
by_c = defaultdict(list)
for i, (c, dn, d, tt) in info.items(): by_c[c].append((d, i))
for c in by_c: by_c[c].sort()
bd = dict(zip(t["bank_txn_id"], t["booking_date"]))
paid = defaultdict(list)
for b, i, a, e in g.select("bank_txn_id", "item_id", "amount_cents", "exception").iter_rows():
    if i: paid[i].append((bd[b], b, -a if e == "RETURNED" else (0 if e == "DUPLICATE_PAYMENT" else a)))
def open_at(i, d, b): return info[i][3] - sum(a for (dd, bb, a) in paid[i] if (dd, bb) < (d, b))
res = dict(zip(au["bank_txn_id"], au["resolver"]))
npred = sub.filter(pl.col("item_id") != "").group_by("bank_txn_id").len()
npred = dict(zip(npred["bank_txn_id"], npred["len"]))
gv = g.filter((pl.col("segment") == "ar_n_to_m") & pl.col("bank_txn_id").is_in(t.filter(pl.col("booking_date") >= pl.date(2025, 1, 1))["bank_txn_id"].implode()))
tx = {r["bank_txn_id"]: r for r in t.filter(pl.col("bank_txn_id").is_in(gv["bank_txn_id"].unique().implode())).iter_rows(named=True)}
C = Counter(); ex = defaultdict(list)
for (b,), grp in gv.group_by("bank_txn_id"):
    r = res.get(b)
    if r not in ("nonitem_unmatched", "text_ref", "ranker", "amount_match", "short_cash", "multi_ref_fill"): continue
    txn = tx[b]; d = txn["booking_date"]; cash = txn["amount_cents"]
    toks = [x for x in refs.tokens(refs.joined(refs.haystack(txn))) if len(x) >= 5]
    items = grp["item_id"].to_list(); exc = dict(zip(items, grp["exception"].to_list()))
    cited = [i for i in items if refs.ref_score(info[i][1], toks) >= 85]
    if not cited: continue
    unc = [i for i in items if i not in cited]
    c = info[items[0]][0]
    # abiertas del cliente a la fecha, en FIFO, sin las del cobro
    openc = [i for (di, i) in by_c[c] if di <= d and open_at(i, d, b) > 0]
    ord_ = {i: k for k, i in enumerate(openc)}
    k = "todas" if not unc else "algunas"
    C[(r, k)] += 1
    if unc:
        # ¿las no citadas son las siguientes en FIFO tras excluir citadas?
        rest = [i for i in openc if i not in cited]
        C[(r, "no_citadas=primeras_FIFO")] += set(unc) == set(rest[:len(unc)])
        C[(r, "no_citadas_mas_antiguas_que_citadas")] += all(ord_.get(u, 9e9) < min(ord_.get(x, 9e9) for x in cited) for u in unc)
        C[(r, "parcial_en_no_citada")] += any(exc[u] == "PARTIAL" for u in unc)
    else:
        so = sum(open_at(i, d, b) for i in cited)
        C[(r, "todas:cash<suma")] += cash < so
        C[(r, "todas:cash==suma")] += cash == so
    if len(ex[(r, k)]) < 3: ex[(r, k)].append((b, cash, len(cited), len(unc), txn["text"][:80]))
for k, v in sorted(C.items()): print(k, v)
for k, v in ex.items(): print(k, v)
