"""Remesas SEPA/BACS/ACH (direct_debit_batches/). Dueño: P2.

Qué hace: un abono de remesa liquida todos los recibos del batch; devoluciones R -> RETURNED.
Entradas: txn (dict con las columnas de bank_transactions + amount_cents, booking_date...), ctx (bankrec.phase.Phase),
          ledger (bankrec.ledger.Ledger, saldo vivo por item_id).
Salida:   Proposal (bankrec.contract) o None si no aplica (la cascada sigue).

TODO(P2): emparejar batch_total/fecha/cuenta con el movimiento; recibo -> partida (mandato/IBAN/remittance_info).
"""
from __future__ import annotations

from bankrec.contract import Alloc, Proposal  # noqa: F401

NAME = "docs_dd"


def prepare(ctx) -> None:
    """Indexa una vez por fase; guarda en ctx.shared[NAME]."""
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    return None
