"""Conceptos que citan varias facturas. Dueño: P2.

Qué hace: extrae varios números del texto y asigna si la suma cuadra.
Entradas: txn (dict con las columnas de bank_transactions + amount_cents, booking_date...), ctx (bankrec.phase.Phase),
          ledger (bankrec.ledger.Ledger, saldo vivo por item_id).
Salida:   Proposal (bankrec.contract) o None si no aplica (la cascada sigue).

TODO(P2): usar refs.py; tolerar cortes a 16/35/38 caracteres.
"""
from __future__ import annotations

from bankrec.contract import Alloc, Proposal  # noqa: F401

NAME = "multi_ref"


def prepare(ctx) -> None:
    """Indexa una vez por fase; guarda en ctx.shared[NAME]."""
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    return None
