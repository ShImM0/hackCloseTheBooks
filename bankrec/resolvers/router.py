"""Enrutado por familia de movimiento. Dueño: P4.

Qué hace: clasifica cada movimiento (nonitem / AR / AP / lote) y lo deja en ctx.shared['router'] para que los demás resolvers se salten lo que no les toca.
Entradas: txn (dict con las columnas de bank_transactions + amount_cents, booking_date...), ctx (bankrec.phase.Phase),
          ledger (bankrec.ledger.Ledger, saldo vivo por item_id).
Salida:   Proposal (bankrec.contract) o None si no aplica (la cascada sigue).

TODO(P4): usar resolve_batch(ctx) vectorizado si conviene; no propone asignaciones.
"""
from __future__ import annotations

from bankrec.contract import Alloc, Proposal  # noqa: F401

NAME = "router"


def prepare(ctx) -> None:
    """Indexa una vez por fase; guarda en ctx.shared[NAME]."""
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    return None
