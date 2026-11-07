"""Resolver 1:1 por tercero + importe con descuento o retención. Dueño: P3 (módulo propio, a añadir a la cascada por P1).

Qué hace: un cobro que paga UNA factura con diferencia explicada no tiene saldo == efectivo, así que amount_match no
lo ve (ar_short_discount, ar_short_withholding, ar_discount_late). Aquí se indexa, por (entidad, divisa, céntimos), el
efectivo esperado de cada factura abierta:
- pronto pago: total − % × total (condiciones con descuento, factura sin cobros previos);
- retención en origen (clientes de MX/BR/MA): total − 1,25 %/1,5 %/10 % × total.
Se aplica el mismo criterio de tercero que amount_match (nombre/IBAN ≥ NAME_MIN y único); varias candidatas del mismo
tercero -> FIFO. Devuelve Alloc(partida, efectivo): exceptions.finalize reconoce el descuento / la retención con ese
importe y pone excepción e importe de salida. Va DESPUÉS de amount_match (el saldo exacto manda).

Colocación en registry.CASCADE: justo después de "bankrec.resolvers.amount_match".
"""
from __future__ import annotations

from collections import defaultdict

from bankrec import parties
from bankrec.contract import Alloc, Proposal
from bankrec.exceptions import WH_BPS, _state
from bankrec.resolvers.amount_match import MARGIN, NAME_MIN

NAME = "short_cash"


def prepare(ctx) -> None:
    parties.prepare(ctx)
    st = _state(ctx)
    idx: dict[tuple, list] = defaultdict(list)
    it = ctx.items.filter(ctx.items["item_type"] == "ARI")
    for iid, ent, party, issue, cur, total, terms in zip(
            it["item_id"].to_list(), it["entity"].to_list(), it["party_id"].to_list(), it["issue_date"].to_list(),
            it["currency"].to_list(), it["total_cents"].to_list(), it["payment_terms"].to_list()):
        if issue is None or not total:
            continue
        t = st.terms.get(terms)
        if t:
            d = (total * t[0] + 5000) // 10000
            idx[(ent, cur, total - d)].append((iid, party, issue, total, "discount"))
        for bps in WH_BPS.get(st.country.get(party), ()):
            idx[(ent, cur, total - (total * bps + 5000) // 10000)].append((iid, party, issue, total, "withholding"))
    ctx.shared[NAME] = idx


def candidates(txn: dict, ctx, ledger) -> list[tuple]:
    """(item_id, party, issue, total, tipo) de facturas abiertas cuyo efectivo esperado == |importe| (±1 cent)."""
    cents = txn["amount_cents"]
    if cents <= 0:
        return []
    idx = ctx.shared[NAME]
    out, seen = [], set()
    for delta in (0, -1, 1):
        for c in idx.get((txn["entity"], txn["currency"], cents + delta), ()):
            iid = c[0]
            if iid in seen or ledger.open.get(iid) != c[3] or c[2] > txn["booking_date"]:
                continue
            seen.add(iid)
            out.append(c)
    out.sort(key=lambda c: (c[2], c[0]))
    return out


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cands = candidates(txn, ctx, ledger)
    if not cands:
        return None
    tctx = parties.context(txn)
    score = {}
    for c in cands:
        if c[1] not in score:
            score[c[1]] = parties.score(tctx, c[1], ctx)
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    best_pid, best = ranked[0]
    if best < NAME_MIN or (len(ranked) > 1 and ranked[1][1] > best - MARGIN):
        return None
    mine = [c for c in cands if c[1] == best_pid]
    return Proposal(txn["bank_txn_id"], [Alloc(mine[0][0], abs(txn["amount_cents"]))], confidence=best / 100,
                    reason=f"tercero {best_pid} ({best:.0f}) + efectivo con {mine[0][4]}")
