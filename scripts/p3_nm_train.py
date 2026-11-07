# Análisis OFFLINE (lee golden de TRAIN): no es un resolver. Uso: uv run python scripts/p3_nm_train.py [año]
"""ar_n_to_m en TRAIN: ¿qué partidas cita el texto?, ¿cuál queda parcial y qué posición ocupa por fecha?"""
import sys
from collections import Counter, defaultdict
import polars as pl
from bankrec import load, refs
yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
g = load.table("train", "golden_allocations").filter(pl.col("segment") == "ar_n_to_m")
t = load.table("train", "bank_transactions").filter(pl.col("booking_date").dt.year() == yr)
inv = load.table("train", "ar_invoices").select("item_id", "customer_id", "document_number", "issue_date", "total_amount_cents")
g = g.join(t.select("bank_txn_id", "booking_date"), on="bank_txn_id").join(inv, on="item_id", how="left")
tx = {r["bank_txn_id"]: r for r in t.filter(pl.col("bank_txn_id").is_in(g["bank_txn_id"].unique().implode())).iter_rows(named=True)}
C = Counter()
for (bid,), grp in g.group_by("bank_txn_id"):
    txn = tx[bid]
    toks = [x for x in refs.tokens(refs.joined(refs.haystack(txn))) if len(x) >= 5]
    rows = grp.sort("issue_date", "item_id").to_dicts()
    cited = [refs.ref_score(r["document_number"], toks) >= 85 for r in rows]
    part = [r["exception"] == "PARTIAL" for r in rows]
    C["txns"] += 1; C["pares"] += len(rows); C["pares_citados"] += sum(cited)
    C["todos_citados"] += all(cited); C["ninguno_citado"] += not any(cited)
    C["con_parcial"] += any(part)
    if any(part):
        k = part.index(True)
        C["parcial_es_la_mas_reciente"] += (k == len(rows) - 1)
        C["parcial_citada"] += cited[k]
    # ¿las no citadas son posteriores a todas las citadas?
    if any(cited) and not all(cited):
        last_c = max(r["issue_date"] for r, c in zip(rows, cited) if c)
        C["no_citadas_posteriores"] += all(r["issue_date"] >= last_c for r, c in zip(rows, cited) if not c)
        C["mixtos"] += 1
    C["texto_cortado_aprox"] += len(txn["text"] or "") in (16, 35, 38) or (txn["text"] or "").endswith(" ")
print(yr, dict(C))
