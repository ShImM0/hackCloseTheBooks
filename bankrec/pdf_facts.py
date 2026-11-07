"""Hechos de los PDFs que no están en ningún CSV. Dueño: P3.

Qué hace: a partir de ctx.shared["pdf_text"] detecta por documento: plazos ("3 vencimientos"), retención de
garantía (5-10 %), anticipo a deducir, "anula y sustituye a X", mes de servicio, rappel compensado/pagado,
abono proveedor reembolsado/compensado, cesión (factoring/confirming) y aviso de IBAN nuevo (NOTICE_*).
Entradas: ctx. Salida: ctx.shared["pdf_facts"] = DataFrame (document_number, entity, fact, value).

TODO(P3): regex por idioma (ES/FR/DE/IT/PT/EN/PL/...); validar contra segmentos ar_instalment, ar_retention,
ar_replaced_invoice, ar_advance_deduction, ar_new_iban del golden.
"""
from __future__ import annotations

NAME = "pdf_facts"


def prepare(ctx) -> None:
    return None
