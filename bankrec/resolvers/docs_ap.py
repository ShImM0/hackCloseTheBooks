"""Pagos a proveedores (payment_runs/: transfers, aggregated, confirming). Dueño: P2.

Qué hace: un cargo liquida los documentos del run / end_to_end_id; abonos netos como filas propias.
Entradas: txn (dict con las columnas de bank_transactions + amount_cents, booking_date...), ctx (bankrec.phase.Phase),
          ledger (bankrec.ledger.Ledger, saldo vivo por item_id).
Salida:   Proposal (bankrec.contract) o None si no aplica (la cascada sigue).

TODO(P2): run_type; pagos de ES00 por cuenta de ES01/ES02/PT01; factoring; recibos dd de proveedores.
"""
from __future__ import annotations

from bankrec.contract import Alloc, Proposal  # noqa: F401

NAME = "docs_ap"


def prepare(ctx) -> None:
    """Indexa una vez por fase; guarda en ctx.shared[NAME]."""
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    return None
