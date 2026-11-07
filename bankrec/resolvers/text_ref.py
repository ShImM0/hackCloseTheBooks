"""Resolver 1:1 por referencia impresa en el texto. Dueño: P1.

Qué hace: número impreso (refs.py) + tercero + importe -> una partida.
Entradas: txn (dict con las columnas de bank_transactions + amount_cents, booking_date...), ctx (bankrec.phase.Phase),
          ledger (bankrec.ledger.Ledger, saldo vivo por item_id).
Salida:   Proposal (bankrec.contract) o None si no aplica (la cascada sigue).

TODO(P1): referencia+importe; tercero+importe único; FIFO; solo importe -> UNIDENTIFIED.
"""
from __future__ import annotations

from bankrec.contract import Alloc, Proposal  # noqa: F401

NAME = "text_ref"


def prepare(ctx) -> None:
    """Indexa una vez por fase; guarda en ctx.shared[NAME]."""
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    return None
