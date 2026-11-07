"""Familia orientativa por movimiento, sin proponer asignaciones. Dueño: P4."""
from __future__ import annotations

from bankrec.resolvers.nonitem import classify

NAME = "router"


def prepare(ctx) -> None:
    """Guarda bank_txn_id → nonitem / AR / AP / lote para consulta opcional."""
    routes = {}
    for txn in ctx.txns.iter_rows(named=True):
        code = (txn.get('bank_code') or '').upper()
        if classify(txn):
            family = 'nonitem'
        elif txn['amount_cents'] > 0 and code in {'19', 'NDDT', 'PMNT/RDDT/ESDD', 'PMNT/MCRD/POSC'}:
            family = 'lote'
        else:
            family = 'AR' if txn['amount_cents'] > 0 else 'AP'
        routes[txn['bank_txn_id']] = family
    ctx.shared[NAME] = routes
