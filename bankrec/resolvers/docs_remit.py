"""Avisos de remesa (remittance_advices/). Dueño: P2.

Qué hace: aviso del cliente (PDF/CSV/XLSX/TXT) lista documentos pagados y deducciones.
Entradas: txn (dict con las columnas de bank_transactions + amount_cents, booking_date...), ctx (bankrec.phase.Phase),
          ledger (bankrec.ledger.Ledger, saldo vivo por item_id).
Salida:   Proposal (bankrec.contract) o None si no aplica (la cascada sigue).

TODO(P2): parsear formatos; casar aviso con movimiento (importe/fecha posterior); cambio de layout sep-2025.
"""
from __future__ import annotations

from bankrec.contract import Alloc, Proposal  # noqa: F401

NAME = "docs_remit"


def prepare(ctx) -> None:
    """Indexa una vez por fase; guarda en ctx.shared[NAME]."""
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    return None
