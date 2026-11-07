"""Orden de la cascada. Dueño: P1 (fichero compartido: pide cambios a P1).

Cada entrada es un módulo con NAME y prepare(); los que además tienen resolve_batch()/resolve() proponen
asignaciones. El primero que devuelve una Proposal para un movimiento gana. Desactivar: --disable a,b.
"""
from __future__ import annotations

import importlib
import logging

log = logging.getLogger("bankrec")

CASCADE = [
    # preparación (solo prepare): texto y hechos de PDFs
    "bankrec.pdf_text",
    "bankrec.pdf_facts",
    # enrutado y no-partidas
    "bankrec.resolvers.router",
    "bankrec.resolvers.nonitem",
    # documentos 1:N (alta precisión)
    "bankrec.resolvers.docs_dd",
    "bankrec.resolvers.docs_card",
    "bankrec.resolvers.docs_ap",
    "bankrec.resolvers.docs_remit",
    "bankrec.resolvers.multi_ref",
    # 1:1
    "bankrec.resolvers.text_ref",
    "bankrec.resolvers.amount_match",
    "bankrec.exceptions_amounts",   # P3: si aún no existe, registry.load lo omite con aviso
    "bankrec.resolvers.subset_sum",
    "bankrec.resolvers.ranker",
    "bankrec.resolvers.nonitem_unmatched",   # P4: último paso, decide UNIDENTIFIED vs categoría
]


def load(disabled: set[str] = frozenset()) -> list:
    mods = []
    for path in CASCADE:
        try:
            m = importlib.import_module(path)
        except Exception:
            log.exception("no se pudo importar %s: se omite", path)
            continue
        if m.NAME in disabled:
            log.info("desactivado: %s", m.NAME)
            continue
        mods.append(m)
    return mods
