"""Resolver 1:1 por tercero + importe exacto. Dueño: P1.

Reglas (POLICIES §3 UNIDENTIFIED, §5.1, §5.6), sobre partidas abiertas cuyo saldo == importe del movimiento
(misma entidad y divisa; abono -> ARI, cargo -> API; ES00 paga API de ES01/ES02/PT01, §5.2):
1. el tercero de la partida encaja con el movimiento (IBAN o nombre ≥ NAME_MIN) y es el único que encaja;
2. si ese tercero tiene varias partidas con el mismo importe: la del mes de servicio citado en el texto
   (factura emitida el mes siguiente) y si no, FIFO por fecha de factura;
3. si solo coincide el importe (ningún tercero encaja): no se asigna (sale UNIDENTIFIED por el fallback).
Entradas: txn, ctx, ledger. Salida: Proposal con una Alloc al saldo completo, o None.

candidates() la reutiliza text_ref.
"""
from __future__ import annotations

from bankrec import parties
from bankrec.contract import Alloc, Proposal

NAME = "amount_match"
NAME_MIN = 85      # parecido de nombre mínimo (0-100); valor redondo, ver CLAUDE.md anti-overfitting
MARGIN = 5         # ventaja mínima del mejor tercero sobre el segundo
PAYMENT_FACTORY = {"ES00": ("ES00", "ES01", "ES02", "PT01")}


def prepare(ctx) -> None:
    parties.prepare(ctx)


def candidates(txn: dict, ledger) -> list[str]:
    """Partidas abiertas con saldo == |importe|, emitidas como tarde el día del movimiento, en orden FIFO."""
    cents = txn["amount_cents"]
    if not cents:
        return []
    typ = "ARI" if cents > 0 else "API"
    ents = (txn["entity"],) if cents > 0 else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    out = []
    for e in ents:
        for iid in ledger.with_amount(e, txn["currency"], abs(cents)):
            i = ledger.info[iid]
            if i[0] == typ and (i[4] is None or i[4] <= txn["booking_date"]):
                out.append(iid)
    if len(ents) > 1:
        out.sort(key=ledger._fifo)
    return out


def pick_month(cands: list[str], ledger, months: set[int]) -> str | None:
    """Mes de servicio citado -> factura emitida el mes siguiente (§5.6)."""
    hits = [c for c in cands if ledger.info[c][4] and (ledger.info[c][4].month - 2) % 12 + 1 in months]
    return hits[0] if hits else None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cands = candidates(txn, ledger)
    if not cands:
        return None
    tctx = parties.context(txn)
    score = {}
    for c in cands:
        pid = ledger.info[c][3]
        if pid not in score:
            score[pid] = parties.score(tctx, pid, ctx)
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    best_pid, best = ranked[0]
    if best < NAME_MIN or (len(ranked) > 1 and ranked[1][1] > best - MARGIN):
        return None
    mine = [c for c in cands if ledger.info[c][3] == best_pid]
    pick, why = mine[0], "único" if len(mine) == 1 else "FIFO"
    if len(mine) > 1:
        from bankrec import refs
        m = pick_month(mine, ledger, refs.months(refs.haystack(txn)))
        if m:
            pick, why = m, "mes de servicio"
    return Proposal(txn["bank_txn_id"], [Alloc(pick, abs(txn["amount_cents"]))], confidence=best / 100,
                    reason=f"tercero {best_pid} ({best:.0f}) + importe, {why}")
