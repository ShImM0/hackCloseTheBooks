"""Liquidaciones de tarjeta (card_settlements/). Dueño: P2.

Qué hace (POLICIES §2 y §5.3: una liquidación del adquirente liquida los pedidos web al BRUTO y sin excepción;
la comisión que retiene el adquirente es contractual, no una diferencia):
- abono bancario = suma de `net` de una liquidación (cuenta, fecha de liquidación..+5 días, importe exacto);
- cada pedido (`order_number`) es el número de la factura ARI (normalizado), importe = `gross`.
Además (docs_card_chq.py): depósitos de cheques/lockbox (§5.3) y contracargos de tarjeta -> RETURNED (§2).
Entradas: ctx (bankrec.phase.Phase). Salida: dict bank_txn_id -> Proposal (resolve_batch).
"""
from __future__ import annotations

import datetime as dt
import logging

import polars as pl

from bankrec import config, load
from bankrec.resolvers import docs_card_chq
from bankrec.contract import Alloc, Proposal

NAME = "docs_card"
log = logging.getLogger("bankrec")

_LOOKBACK = dt.timedelta(days=20)   # la liquidación puede ser de días antes de que el banco la abone
_SLACK = 5                          # días entre la liquidación y el abono


def _norm(col: str) -> pl.Expr:
    return pl.col(col).str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


def _settlements(ctx) -> pl.DataFrame:
    parts = []
    for folder in [*config.PRIOR_FOLDERS.get(ctx.name, []), ctx.folder]:
        d = config.folder_dir(folder) / "card_settlements"
        df = load.read_folder(d) if d.exists() else pl.DataFrame()
        if not df.is_empty():
            parts.append(df.filter((pl.col("settlement_date") >= ctx.start - _LOOKBACK) & (pl.col("settlement_date") < ctx.end)))
    return pl.concat(parts, how="vertical_relaxed") if parts else pl.DataFrame()


def prepare(ctx) -> None:
    sh: dict = {"by_txn": {}, "cheques": {}, "returned": {}, "chargebacks": {}}
    ctx.shared[NAME] = sh
    for key, fn in (("cheques", docs_card_chq.cheque_matches), ("returned", docs_card_chq.returned_cheques),
                    ("chargebacks", docs_card_chq.chargebacks)):
        try:
            sh[key] = fn(ctx)
        except Exception:
            log.exception("docs_card: %s falló, se omite", key)
    log.info("docs_card: %d depósitos de cheques, %d cheques devueltos, %d contracargos casados",
             len(sh["cheques"]), len(sh["returned"]), len(sh["chargebacks"]))
    cs = _settlements(ctx)
    if cs.is_empty():
        return
    ari = (ctx.items.filter(pl.col("item_type") == "ARI")
           .select("item_id", "entity", _norm("document_number").alias("o")))
    cs = (cs.with_columns(_norm("order_number").alias("o"),
                          pl.col("account_id").str.split("-").list.first().alias("ent"))
          .join(ari, on="o", how="left"))
    # pedido repetido en varias entidades: el de la entidad de la cuenta
    cs = (cs.with_columns((pl.col("entity") == pl.col("ent")).fill_null(False).alias("same_ent"))
          .sort("same_ent", descending=True)
          .unique(["settlement_id", "order_number"], keep="first", maintain_order=True))
    cs = cs.with_columns(pl.when(pl.col("same_ent")).then(pl.col("item_id")).otherwise(None).alias("item_id"))
    sett = (cs.group_by("settlement_id")
            .agg(pl.col("account_id").first(), pl.col("settlement_date").first(), pl.col("net_cents").sum().alias("net"),
                 pl.struct("item_id", pl.col("gross_cents")).alias("orders")))
    credits = ctx.txns.filter(pl.col("amount_cents") > 0).select("bank_txn_id", "account_id", "booking_date", "amount_cents")
    j = (sett.join(credits, left_on=["account_id", "net"], right_on=["account_id", "amount_cents"])
         .with_columns((pl.col("booking_date") - pl.col("settlement_date")).dt.total_days().alias("lag"))
         .filter(pl.col("lag").is_between(0, _SLACK)).sort(["lag", "settlement_id"]))
    used_t, used_s, out = set(), set(), {}
    for t, sid, orders in zip(j["bank_txn_id"].to_list(), j["settlement_id"].to_list(), j["orders"].to_list()):
        if t in used_t or sid in used_s:
            continue
        used_t.add(t); used_s.add(sid)
        out[t] = [(o["item_id"], o["gross_cents"]) for o in orders]
    sh["by_txn"] = out
    log.info("docs_card: %d liquidaciones, %d abonos casados", sett.height, len(out))


def resolve_batch(ctx) -> dict[str, Proposal]:
    out = {}
    for t, rows in ctx.shared.get(NAME, {}).get("by_txn", {}).items():
        allocs, seen, miss = [], set(), 0
        for item, gross in rows:
            if item is None or item in seen or gross <= 0:
                miss += 1
                continue
            seen.add(item)
            allocs.append(Alloc(item, gross))
        if allocs:
            out[t] = Proposal(t, allocs, confidence=0.99 if not miss else 0.8, resolver=NAME,
                              reason=f"liquidación tarjeta {len(rows)} pedidos, {miss} sin partida")
    sh = ctx.shared.get(NAME, {})
    for key, label in (("cheques", "depósito de cheques"), ("returned", "cheque devuelto"), ("chargebacks", "contracargo")):
        for t, rows in sh.get(key, {}).items():
            allocs, seen, miss = [], set(), 0
            for item, cents, exc in rows:
                if item is None or item in seen or cents <= 0:
                    miss += 1
                    continue
                seen.add(item)
                allocs.append(Alloc(item, cents, exc))
            if allocs and t not in out:
                out[t] = Proposal(t, allocs, confidence=0.9 if not miss else 0.6, resolver=NAME,
                                  reason=f"{label} {len(rows)} filas, {miss} sin partida")
    return out
