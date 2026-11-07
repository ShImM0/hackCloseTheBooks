# Análisis OFFLINE (golden TRAIN). ar_n_to_m con TODAS las partidas citadas y una PARTIAL: ¿cuál es la parcial?
import sys
from collections import Counter
import polars as pl
from bankrec import load, refs
yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
g = load.table("train", "golden_allocations").filter(pl.col("segment") == "ar_n_to_m")
t = load.table("train", "bank_transactions").filter(pl.col("booking_date").dt.year() == yr)
inv = load.table("train", "ar_invoices").select("item_id", "document_number", "issue_date", "due_date", "total_amount_cents")
g = g.join(t.select("bank_txn_id"), on="bank_txn_id").join(inv, on="item_id", how="left")
tx = {r["bank_txn_id"]: r for r in t.filter(pl.col("bank_txn_id").is_in(g["bank_txn_id"].unique().implode())).iter_rows(named=True)}
C = Counter()
for (bid,), grp in g.group_by("bank_txn_id"):
    txn = tx[bid]; hay = refs.joined(refs.haystack(txn))
    toks = [x for x in refs.tokens(hay) if len(x) >= 5]
    rows = grp.to_dicts()
    if not all(refs.ref_score(r["document_number"], toks) >= 85 for r in rows): continue
    C["todas_citadas"] += 1
    full = [r for r in rows if r["exception"] is None and r["amount_cents"] == r["total_amount_cents"]]
    C["todas_completas"] += len(full) == len(rows)
    part = [r for r in rows if r["exception"] == "PARTIAL"]
    if len(part) != 1: continue
    p = part[0]; C["una_parcial"] += 1
    # posición en el texto: índice del primer token que casa
    def pos(r):
        d = refs.digits(r["document_number"])
        for k, tk in enumerate(toks):
            if refs.ref_score(r["document_number"], [tk]) >= 85: return k
        return 99
    C["parcial=ultima_en_texto"] += pos(p) == max(pos(r) for r in rows)
    C["parcial=mas_reciente"] += p["issue_date"] == max(r["issue_date"] for r in rows)
    C["parcial=mas_antigua"] += p["issue_date"] == min(r["issue_date"] for r in rows)
    C["parcial=mayor_total"] += p["total_amount_cents"] == max(r["total_amount_cents"] for r in rows)
    C["parcial=vence_ultima"] += p["due_date"] == max(r["due_date"] for r in rows)
    C["n_items"] += len(rows)
print(yr, dict(C))
