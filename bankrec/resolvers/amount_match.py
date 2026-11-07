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


PARTY_STRONG = 90  # by_ratio: el tercero tiene que estar claro
MAX_OPEN = 30      # by_ratio: terceros con muchas partidas abiertas (tarjeta web) dan coincidencias espurias
RATIOS = (1 / 2, 1 / 3, 1 / 4, 0.9, 0.985, 0.9875)   # plazos 1/2-1/4, retenciones 10 %, 1,5 %, 1,25 % (§3)


def prepare(ctx) -> None:
    """ctx.shared[NAME] = {by_party: party -> [ARI abiertas, FIFO], disc: item -> 1 - % descuento pronto pago}."""
    import polars as pl
    parties.prepare(ctx)
    it = ctx.items.filter((pl.col("open_cents") > 0) & (pl.col("item_type") == "ARI")).sort("issue_date", "item_id")
    by_party: dict[str, list[str]] = {}
    for iid, pid in it.select("item_id", "party_id").iter_rows():
        by_party.setdefault(pid, []).append(iid)
    pt = ctx.master("payment_terms")
    pct = {c: float(x) for c, x in pt.select("code", "early_payment_discount_pct").iter_rows() if x}
    disc = {iid: 1 - pct[t] / 100 for iid, t in it.select("item_id", "payment_terms").iter_rows() if t in pct}
    ctx.shared[NAME] = {"by_party": by_party, "disc": disc}


def by_ratio(txn: dict, ctx, ledger) -> Proposal | None:
    """Cobro sin referencia ni importe exacto (§3: plazos, retención, pronto pago): tercero claro y UNA sola de sus
    partidas abiertas cuyo saldo × ratio típico == efectivo (tolerancia 0,5 %). Importe/excepción: exceptions.py."""
    cents = txn["amount_cents"]
    if cents <= 0 or NAME not in ctx.shared:
        return None
    cur, amt = txn["currency"], cents
    if txn.get("orig_currency") and txn["orig_currency"] != cur and txn.get("orig_amount_cents"):
        cur, amt = txn["orig_currency"], abs(txn["orig_amount_cents"])
    found = parties.find(parties.context(txn), ctx, "C" + txn["entity"])
    if not found or found[0][1] < PARTY_STRONG or (len(found) > 1 and found[1][1] > found[0][1] - MARGIN):
        return None
    S = ctx.shared[NAME]
    items = [i for i in S["by_party"].get(found[0][0], ()) if ledger.open.get(i, 0) > 0
             and ledger.info[i][2] == cur and (ledger.info[i][4] is None or ledger.info[i][4] <= txn["booking_date"])]
    if not items or len(items) > MAX_OPEN:
        return None
    hits = []
    for i in items:
        oc = ledger.open[i]
        rs = RATIOS + ((S["disc"][i],) if i in S["disc"] else ())
        if any(abs(amt - r * oc) <= max(1, 0.005 * r * oc) for r in rs):
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    return Proposal(txn["bank_txn_id"], [Alloc(i, ledger.open[i])], confidence=0.8,
                    reason=f"tercero {found[0][0]} ({found[0][1]:.0f}) + ratio típico sobre saldo")


def candidates(txn: dict, ledger) -> list[str]:
    """Partidas abiertas con saldo == |importe|, emitidas como tarde el día del movimiento, en orden FIFO."""
    cents = txn["amount_cents"]
    if not cents:
        return []
    typ = "ARI" if cents > 0 else "API"
    ents = (txn["entity"],) if cents > 0 else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    # FX (§3 FX_DIFFERENCE): el banco trae el importe original en la divisa de la factura (orig_amount);
    # en TRAIN 2024 orig_currency == divisa de la partida en el 100 % de las líneas FX
    cur, amt = txn["currency"], abs(cents)
    if txn.get("orig_currency") and txn["orig_currency"] != cur and txn.get("orig_amount_cents"):
        cur, amt = txn["orig_currency"], abs(txn["orig_amount_cents"])
    out = []
    for e in ents:
        for iid in ledger.with_amount(e, cur, amt):
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
    p = _exact(txn, ctx, ledger)
    if p is None:
        from bankrec.resolvers import text_ref
        p = text_ref.by_ref(txn, ctx, ledger)
    if p is None:
        p = by_ratio(txn, ctx, ledger)
    if p is None and DUP_ENABLED:
        p = duplicate(txn, ctx, ledger)
    return p


# Desactivado: en val sube 65,87 -> 67,70 (P 0,98), pero en LIVE bajó 65,09 -> 64,93 (envío de 572e36e).
# No generaliza y no sabemos por qué: no se usa hasta entenderlo.
DUP_ENABLED = False
DUP_DAYS = 60      # en TRAIN 2024 el p99 entre el pago y su duplicado es 49 días


def duplicate(txn: dict, ctx, ledger) -> Proposal | None:
    """§3 DUPLICATE_PAYMENT: nada abierto encaja, pero el mismo tercero pagó hace ≤ DUP_DAYS una partida por
    exactamente este importe (el banco repite el pago). Amount = lo pagado otra vez (= saldo que se cerró)."""
    cents = txn["amount_cents"]
    if not cents:
        return None
    typ = "ARI" if cents > 0 else "API"
    ents = (txn["entity"],) if cents > 0 else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    cur, amt = txn["currency"], abs(cents)
    if txn.get("orig_currency") and txn["orig_currency"] != cur and txn.get("orig_amount_cents"):
        cur, amt = txn["orig_currency"], abs(txn["orig_amount_cents"])
    cands = [(i, d) for e in ents for i, d in ledger.closed_with(e, cur, amt)
             if ledger.info[i][0] == typ and 0 <= (txn["booking_date"] - d).days <= DUP_DAYS]
    if not cands:
        return None
    from bankrec import refs
    tctx = parties.context(txn)
    toks = refs.tokens(refs.joined(refs.haystack(txn)))
    good = []
    for i, d in cands:
        r = refs.ref_score(ledger.info[i][5], toks) if toks else 0
        s = parties.score(tctx, ledger.info[i][3], ctx)
        if r >= 85 or s >= NAME_MIN:
            good.append((r, s, i))
    if len({g[2] for g in good}) != 1:
        return None
    r, s, i = good[0]
    return Proposal(txn["bank_txn_id"], [Alloc(i, amt, "DUPLICATE_PAYMENT")], confidence=0.8,
                    reason=f"pago repetido de {ledger.info[i][5]} (ref {r}, tercero {s:.0f})")


def _exact(txn: dict, ctx, ledger) -> Proposal | None:
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
        m = pick_month(mine, ledger, refs.months(refs.joined(refs.haystack(txn))))
        if m:
            pick, why = m, "mes de servicio"
    # el texto cita (≥ 90) otra factura abierta del mismo tercero: no es esta (la resolverá by_ref)
    from bankrec import refs
    toks = [t for t in refs.tokens(refs.joined(refs.haystack(txn))) if len(t) >= 6]
    if toks and refs.ref_score(ledger.info[pick][5], toks) < 90:
        S = ctx.shared.get(NAME, {})
        for o in S.get("by_party", {}).get(best_pid, ()):
            if o != pick and ledger.open.get(o, 0) > 0 and refs.ref_score(ledger.info[o][5], toks) >= 90:
                return None
    return Proposal(txn["bank_txn_id"], [Alloc(pick, ledger.open[pick])], confidence=best / 100,
                    reason=f"tercero {best_pid} ({best:.0f}) + importe, {why}")
