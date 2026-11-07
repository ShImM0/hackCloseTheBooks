"""Ledger de partidas abiertas: saldo vivo en céntimos. Dueño: P1.

Reglas de saldo (POLICIES §2-3):
- una fila normal / SHORT_* / FX_DIFFERENCE / OVERPAYMENT / PARTIAL reduce el saldo en su amount
  (en las de liquidación total amount == saldo abierto, así que la partida queda a 0);
- RETURNED reabre: suma el amount;
- DUPLICATE_PAYMENT no cambia el saldo.

TODO(P1): índices por (entidad, tercero), por importe y FIFO por fecha de emisión.
"""
from __future__ import annotations

import polars as pl

from bankrec.contract import Proposal

NO_REDUCE = {"RETURNED", "DUPLICATE_PAYMENT"}


class Ledger:
    def __init__(self, items: pl.DataFrame):
        self.items = items  # tabla unificada de partidas (ver phase.py)
        self.open: dict[str, int] = dict(zip(items["item_id"].to_list(), items["open_cents"].to_list()))

    def is_open(self, item_id: str) -> bool:
        return self.open.get(item_id, 0) > 0

    def apply(self, p: Proposal) -> None:
        for a in p.allocs:
            if a.item_id not in self.open:
                continue
            if a.exception == "RETURNED":
                self.open[a.item_id] += a.amount_cents
            elif a.exception != "DUPLICATE_PAYMENT":
                self.open[a.item_id] = max(0, self.open[a.item_id] - a.amount_cents)


def open_at(items: pl.DataFrame, golden: pl.DataFrame, txns: pl.DataFrame, cutoff,
            later_open: pl.Series | None = None) -> pl.DataFrame:
    """Reconstruye las partidas abiertas a `cutoff` (date) con el golden de los movimientos anteriores.
    items: tabla unificada con total_cents; devuelve items emitidos antes de cutoff con open_cents > 0.

    El golden no ve los cierres fuera de banco (facturas anuladas y su abono, partidas pre-2021-07 cobradas antes
    de TRAIN, restos dados de baja): sin filtro sobran ~237 k partidas a 2025-07-01. Por eso solo se conservan las
    que siguen vivas más tarde: tienen golden en/tras cutoff o están en `later_open` (ids de open_items_at_start
    de la fase siguiente). Comprobado a 2025-07-01 contra live/open_items_at_start: 100 % de ids, 99,7 % de importes.
    """
    gj = golden.filter(pl.col("item_id").is_not_null()).join(txns.select("bank_txn_id", "booking_date"), on="bank_txn_id")
    alive = gj.filter(pl.col("booking_date") >= cutoff)["item_id"]
    if later_open is not None:
        alive = pl.concat([alive, later_open])
    g = (
        gj.filter(pl.col("booking_date") < cutoff)
        .group_by("item_id")
        .agg(
            pl.when(pl.col("exception") == "RETURNED").then(-pl.col("amount_cents"))
            .when(pl.col("exception") == "DUPLICATE_PAYMENT").then(0)
            .otherwise(pl.col("amount_cents")).sum().alias("applied")
        )
    )
    return (
        items.filter((pl.col("issue_date") < cutoff) & pl.col("item_id").is_in(alive.unique().implode()))
        .join(g, on="item_id", how="left")
        .with_columns((pl.col("total_cents") - pl.col("applied").fill_null(0)).alias("open_cents"))
        .drop("applied")
        .filter(pl.col("open_cents") > 0)
    )
