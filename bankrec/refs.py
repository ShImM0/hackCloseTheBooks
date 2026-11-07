"""Extracción y normalización de números impresos (referencias). Dueño: P1.

Qué hace: del texto bancario (text, ref1, ref2, remittance_info, end_to_end_id) saca candidatos a número de
documento por familia (FV25-0400572, AB24-…, FA/AV…, TW22-…, numéricos US, formatos de proveedor) y los normaliza
para buscarlos en un índice document_number -> item_id.
Entradas: str. Salidas: list[str] de claves normalizadas.

TODO(P1): familias por entidad, tolerar cortes a 16/35/38, ceros a la izquierda, dígitos transpuestos, PO del cliente.
"""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def norm(s: str | None) -> str:
    """'FV25-0400572' -> 'FV250400572'."""
    return _NON_ALNUM.sub("", (s or "").upper())


def extract(text: str) -> list[str]:
    """Candidatos normalizados encontrados en el texto."""
    return []
