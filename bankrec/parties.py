"""Identificación del tercero (cliente/proveedor) de un movimiento. Dueño: P1.

Qué hace: IBAN del maestro (customers.iban / suppliers.iban, avisos NOTICE_* de P3), nombre con
rapidfuzz + unidecode (nombres truncados, comerciales, renombrados) y grupo (parent_customer_id).
Entradas: txn (dict), ctx. Salida: lista de (party_id, score) ordenada; vacía si no hay pista.

TODO(P1): índice IBAN -> party; índice de nombres normalizados por entidad; matrices y filiales.
"""
from __future__ import annotations


def prepare(ctx) -> None:
    return None


def candidates(txn: dict, ctx) -> list[tuple[str, float]]:
    return []
