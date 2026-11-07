"""Movimientos sin partida (categoría). Dueño: P4.

Qué hace: asigna category de categories.csv: comisiones, nómina, cash pool, impuestos, reversos...
Entradas: txn (dict con las columnas de bank_transactions + amount_cents, booking_date...), ctx (bankrec.phase.Phase),
          ledger (bankrec.ledger.Ledger, saldo vivo por item_id).
Salida:   Proposal (bankrec.contract) o None si no aplica (la cascada sigue).

TODO(P4): reglas por bank_code/texto; UNIDENTIFIED solo si es un cobro sin partida (precisión!).
"""
from __future__ import annotations

from bankrec.contract import Alloc, Proposal  # noqa: F401

NAME = "nonitem"


def prepare(ctx) -> None:
    """Indexa una vez por fase; guarda en ctx.shared[NAME]."""
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    return None
