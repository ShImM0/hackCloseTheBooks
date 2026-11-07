"""Ledger de partidas abiertas: saldo vivo en céntimos. Dueño: P1.

Reglas de saldo (POLICIES §2-3):
- una fila normal / SHORT_* / FX_DIFFERENCE / OVERPAYMENT / PARTIAL reduce el saldo en su amount
  (en las de liquidación total amount == saldo abierto, así que la partida queda a 0);
- RETURNED reabre: suma el amount;
- DUPLICATE_PAYMENT no cambia el saldo.

Índices (para resolvers 1:1):
- ledger.info[item_id] = (item_type, entity, currency, party_id, issue_date, document_number, po)
- ledger.by_amount[(entity, currency, open_cents)] = [item_id...] ordenados por (issue_date, item_id) = FIFO.
  Se mantiene al aplicar cada movimiento (solo partidas con saldo > 0).
"""
from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict

import polars as pl

from bankrec.contract import Proposal

NO_REDUCE = {"RETURNED", "DUPLICATE_PAYMENT"}
_MIN_DATE = dt.date(1900, 1, 1)


class Ledger:
    def __init__(self, items: pl.DataFrame):
        self.items = items  # tabla unificada de partidas (ver phase.py)
        self.open: dict[str, int] = dict(zip(items["item_id"].to_list(), items["open_cents"].to_list()))
        cols = ["item_id", "item_type", "entity", "currency", "party_id", "issue_date", "document_number", "po"]
        self.info = {r[0]: r[1:] for r in items.select(cols).iter_rows()}
        self.by_amount: dict[tuple, list[str]] = defaultdict(list)
        self.closed_by_amount: dict[tuple, list[tuple]] = defaultdict(list)
        for iid, oc in self.open.items():
            if oc > 0:
                self.by_amount[self._key(iid, oc)].append(iid)
        for lst in self.by_amount.values():
            lst.sort(key=self._fifo)

    def _key(self, iid: str, cents: int) -> tuple:
        i = self.info[iid]
        return (i[1], i[2], cents)

    def _fifo(self, iid: str):
        return (self.info[iid][4] or _MIN_DATE, iid)

    def is_open(self, item_id: str) -> bool:
        return self.open.get(item_id, 0) > 0

    def with_amount(self, entity: str, currency: str, cents: int) -> list[str]:
        """Partidas abiertas con saldo exactamente `cents`, en orden FIFO."""
        return self.by_amount.get((entity, currency, cents), [])

    def _set(self, iid: str, new: int) -> None:
        old = self.open[iid]
        if old == new:
            return
        if old > 0:
            lst = self.by_amount.get(self._key(iid, old))
            if lst and iid in lst:
                lst.remove(iid)
        if new > 0:
            lst = self.by_amount[self._key(iid, new)]
            keys = [self._fifo(x) for x in lst]
            lst.insert(bisect.bisect(keys, self._fifo(iid)), iid)
        self.open[iid] = new

    def apply(self, p: Proposal, when=None) -> None:
        """when = fecha del movimiento: las partidas que cierra quedan en closed_by_amount (DUPLICATE_PAYMENT)."""
        for a in p.allocs:
            if a.item_id not in self.open:
                continue
            old = self.open[a.item_id]
            if a.exception == "RETURNED":
                self._set(a.item_id, old + a.amount_cents)
            elif a.exception != "DUPLICATE_PAYMENT":
                self._set(a.item_id, max(0, old - a.amount_cents))
                if when is not None and old > 0 and self.open[a.item_id] == 0:
                    self.closed_by_amount[self._key(a.item_id, old)].append((a.item_id, when))

    def closed_with(self, entity: str, currency: str, cents: int) -> list[tuple[str, object]]:
        """(item_id, fecha de cierre) de partidas cerradas en esta fase cuyo último saldo era `cents`."""
        return [(i, d) for i, d in self.closed_by_amount.get((entity, currency, cents), []) if self.open.get(i, 0) == 0]


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
