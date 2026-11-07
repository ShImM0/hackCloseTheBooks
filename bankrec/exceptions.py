"""Excepciones e importes de cada fila. Dueño: P3.

Se llama después de que un resolver asigne partidas y ANTES de actualizar el ledger.
Qué hace: según POLICIES §2-3 decide la excepción de cada Alloc y su amount:
- liquidación total con diferencia explicada (SHORT_DISCOUNT, SHORT_BANK_CHARGES, SHORT_WITHHOLDING,
  FX_DIFFERENCE, OVERPAYMENT): amount = saldo abierto completo;
- PARTIAL / SHORT_DISPUTE: amount = lo realmente aplicado;
- DUPLICATE_PAYMENT: lo pagado otra vez; RETURNED: lo que se reabre.
FX_DIFFERENCE sustituye a vacío/SHORT_DISCOUNT/SHORT_WITHHOLDING/SHORT_BANK_CHARGES en filas de factura;
los abonos (ARC/APC) quedan sin excepción.
Entradas: prop (Proposal), txn (dict), ctx, ledger (saldo ANTES de aplicar este movimiento).
Salida: Proposal corregida, o None para dejarla como está.

TODO(P3): descuento pronto pago (payment_terms + 3 días de gracia), gastos ≤60 EUR y ≤2 %, retenciones MX/BR/MA,
FX con fx_rates.csv, PARTIAL por plazos/retención/anticipo (pdf_facts).
"""
from __future__ import annotations

from bankrec.contract import Proposal


def finalize(prop: Proposal, txn: dict, ctx, ledger) -> Proposal | None:
    return None
