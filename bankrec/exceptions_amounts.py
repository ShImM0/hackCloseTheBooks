"""Resolver 1:1 por tercero + importe con descuento o retención. Dueño: P3 (módulo propio, a añadir a la cascada por P1).

Qué hace: un cobro que paga UNA factura con diferencia explicada no tiene saldo == efectivo, así que amount_match no
lo ve (ar_short_discount, ar_short_withholding, ar_discount_late). Aquí se indexa, por (entidad, divisa, céntimos), el
efectivo esperado de cada factura abierta:
- pronto pago: total − % × total (condiciones con descuento, factura sin cobros previos);
- retención en origen (clientes de MX/BR/MA): total − 1,25 %/1,5 %/10 % × total.
Se aplica el mismo criterio de tercero que amount_match (nombre/IBAN ≥ NAME_MIN y único); varias candidatas del mismo
tercero -> FIFO. Devuelve Alloc(partida, efectivo): exceptions.finalize reconoce el descuento / la retención con ese
importe y pone excepción e importe de salida. Va DESPUÉS de amount_match (el saldo exacto manda).

Segunda etapa (tercero primero): si el tercero se identifica (nombre/IBAN, único) se buscan en SUS facturas las formas
de cobro que ningún otro resolver ve, por este orden: saldo exacto (también en divisa distinta vía orig_amount), duplicado
(factura ya cobrada por el total), plazo/retención/anticipo del PDF, parcial (fracción redonda del saldo), gastos
bancarios (10-71 unidades y ≤ 2 %), sobrepago exacto (+1..100 unidades enteras, +1 % o +10 %; factura única) y
sobrepago (única factura abierta del tercero, misma forma de exceso).

Colocación en registry.CASCADE: justo después de "bankrec.resolvers.amount_match".
"""
from __future__ import annotations

from collections import defaultdict

from bankrec import parties
from bankrec.contract import Alloc, Proposal
from bankrec.exceptions import (cross_border, overpay_shape, PARTIAL_FRACS, DISPUTE_FRACS, WH_BPS, _plan,
                                _state)
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
    byp: dict[str, list] = defaultdict(list)     # tercero -> partidas (ARI/API) en orden FIFO
    allit = ctx.items.filter(ctx.items["item_type"].is_in(["ARI", "API"])).sort("issue_date", "item_id")
    for iid, ent, party, issue, cur, total, typ in zip(
            allit["item_id"].to_list(), allit["entity"].to_list(), allit["party_id"].to_list(),
            allit["issue_date"].to_list(), allit["currency"].to_list(), allit["total_cents"].to_list(),
            allit["item_type"].to_list()):
        if issue is not None and total:
            byp[party].append((iid, ent, issue, cur, total))
    ctx.shared[NAME + "_party"] = byp
    tot: dict[tuple, list] = defaultdict(list)   # (entidad, divisa, total) -> partidas, para duplicados
    for party, lst in byp.items():
        for iid, ent, issue, cur, total in lst:
            tot[(ent, cur, total)].append((iid, party, issue))
    ctx.shared[NAME + "_total"] = tot
    # importes que dicen los PDFs (plazos, retención a pagar, anticipo neto) -> partida
    fidx: dict[tuple, list] = defaultdict(list)
    meta = {r[0]: r[1:] for r in zip(allit["item_id"].to_list(), allit["entity"].to_list(), allit["party_id"].to_list(),
                                     allit["issue_date"].to_list(), allit["currency"].to_list())}
    for iid in st.docnum:
        m = meta.get(iid)
        if m and iid.startswith("ARI"):
            for a, _, _ in _plan(st.item_facts(iid)):
                fidx[(m[0], m[3], a)].append((iid, m[1], m[2]))
    ctx.shared[NAME + "_facts"] = fidx


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


PARTIAL_SET = sorted(set(PARTIAL_FRACS + DISPUTE_FRACS + [0.95, 0.98, 0.99]))
SHAPE_RANK = {"exacto": 0, "duplicado": 1, "plan PDF": 2, "sobrepago exacto": 3, "parcial": 4, "gastos": 5,
              "sobrepago": 6}
PAYMENT_FACTORY = {"ES00": ("ES00", "ES01", "ES02", "PT01")}


def _party(txn, ctx):
    """(party_id, score) único del movimiento o None (mismo criterio que amount_match)."""
    pref = "C" + txn["entity"] if txn["amount_cents"] > 0 else "S"
    found = parties.find(parties.context(txn), ctx, pref)
    if not found or found[0][1] < NAME_MIN or (len(found) > 1 and found[1][1] > found[0][1] - MARGIN):
        return None
    return found[0]


def _shapes(txn, ctx, ledger, pid) -> tuple[str, str, int] | None:
    """(item_id, forma, efectivo en divisa de la partida) de la mejor coincidencia entre las facturas del tercero."""
    st = _state(ctx)
    credit = txn["amount_cents"] > 0
    ents = (txn["entity"],) if credit else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    cash = abs(txn["amount_cents"])
    tcur, date = txn["currency"], txn["booking_date"]
    orig = None
    if txn.get("orig_amount") and txn.get("orig_currency"):
        try:
            orig = (txn["orig_currency"], abs(round(float(txn["orig_amount"]) * 100)))
        except ValueError:
            pass
    want = "ARI" if credit else "API"
    live = []
    for iid, ent, issue, cur, total in ctx.shared[NAME + "_party"].get(pid, ()):
        if ent not in ents or not iid.startswith(want) or issue > date:
            continue
        if cur == tcur:
            c = cash
        elif orig and orig[0] == cur:
            c = orig[1]
        else:
            continue
        live.append((iid, issue, cur, total, ledger.open.get(iid, 0), c))
    hits = {k: [] for k in range(7)}
    xb = cross_border(txn)
    openl = [x for x in live if x[4] > 0]
    for iid, issue, cur, total, o, c in live:
        if o > 0:
            if abs(c - o) <= 1:
                hits[0].append((iid, "exacto", c))
            f = st.item_facts(iid)
            if f and c < o - 1 and any(abs(a - c) <= 1 for a, _, _ in _plan(f)):
                hits[2].append((iid, "plan PDF", c))
            if credit and overpay_shape(c, o):
                hits[3].append((iid, "sobrepago exacto", c))
            if c < o - 1 and any(abs(c - round(o * fr)) <= 1 for fr in PARTIAL_SET):
                hits[4].append((iid, "parcial", c))
            fee = o - c
            if xb and 900 <= fee <= min(0.02 * o, 8000) and cur == tcur:
                hits[5].append((iid, "gastos", c))
        elif abs(c - total) <= 1 and (date - issue).days <= 240:
            hits[1].append((iid, "duplicado", c))
    if len(openl) == 1 and overpay_shape(openl[0][5], openl[0][4]):   # única abierta y exceso con forma
        hits[6].append((openl[0][0], "sobrepago", openl[0][5]))
    for k in range(7):
        # formas por importe que pueden coincidir por azar (sobrepago exacto, gastos): solo si la factura es única
        if hits[k] and (k not in (3, 5) or len(hits[k]) == 1):
            return hits[k][0]
    return None


DUP_MIN = 70   # parecido de nombre mínimo: el importe exacto de una factura ya cobrada es una señal fuerte
PARTIAL_MIN = 95   # un parcial por fracción redonda coincide por azar con más facturas: exige tercero claro
DUP_PAY_MIN = 95   # duplicado por importe (sin referencia): en val con parecido 70-94 solo 5 de 78 eran duplicados
# (los demás, lookalikes UNIDENTIFIED o cobros de otra factura); el parcial con 85-94 acertaba 1 de 139


def _duplicate(txn, ctx, ledger) -> Proposal | None:
    """Cobro/pago repetido: importe == total de una factura ya cobrada (≤ 240 días) y tercero que encaja."""
    cents = txn["amount_cents"]
    credit = cents > 0
    want = "ARI" if credit else "API"
    ents = (txn["entity"],) if credit else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    tctx = None
    best = None
    for e in ents:
        for iid, party, issue in ctx.shared[NAME + "_total"].get((e, txn["currency"], abs(cents)), ()):
            if not iid.startswith(want) or ledger.open.get(iid, 1) != 0 or issue > txn["booking_date"]:
                continue
            if (txn["booking_date"] - issue).days > 240:
                continue
            tctx = tctx or parties.context(txn)
            sc = parties.score(tctx, party, ctx)
            if sc >= DUP_PAY_MIN and (best is None or sc > best[0]):
                best = (sc, iid, party)
    if not best:
        return None
    return Proposal(txn["bank_txn_id"], [Alloc(best[1], abs(cents))], confidence=best[0] / 100 * 0.8,
                    reason=f"tercero {best[2]} ({best[0]:.0f}) + total de factura ya cobrada")


def _by_amount(txn, ctx, ledger) -> Proposal | None:
    """Parcial / plazo / retención / anticipo hallados por el importe (no hace falta que parties.find vea al tercero)."""
    cents = txn["amount_cents"]
    if cents <= 0:
        return None
    c, ent, cur, date = cents, txn["entity"], txn["currency"], txn["booking_date"]
    cand = []                                   # (rango, item_id, party)
    for iid, party, issue in ctx.shared[NAME + "_facts"].get((ent, cur, c), ()):
        o = ledger.open.get(iid, 0)
        if issue <= date and o > 0 and (o > c + 1 or abs(o - c) <= 1):
            cand.append((2, iid, party))
    for fr in PARTIAL_SET:
        o0 = round(c / fr)
        for o in range(o0 - 2, o0 + 3):
            if abs(round(o * fr) - c) > 1:
                continue
            for iid in ledger.with_amount(ent, cur, o):
                if iid.startswith("ARI") and ledger.info[iid][4] and ledger.info[iid][4] <= date:
                    cand.append((3, iid, ledger.info[iid][3]))
    if not cand:
        return None
    tctx = parties.context(txn)
    scored = {}
    best = None
    for rank, iid, party in cand:
        if party not in scored:
            scored[party] = parties.score(tctx, party, ctx)
        sc = scored[party]
        if sc < (DUP_MIN if rank == 2 else PARTIAL_MIN):
            continue
        key = (rank, -sc, iid)
        if best is None or key < best[0]:
            best = (key, iid, party, sc)
    if not best:
        return None
    others = {p for r, i, p in cand if p != best[2] and scored.get(p, 0) >= best[3] - MARGIN}
    if others:
        return None
    return Proposal(txn["bank_txn_id"], [Alloc(best[1], c)], confidence=best[3] / 100 * 0.85,
                    reason=f"tercero {best[2]} ({best[3]:.0f}) + importe de {'plan PDF' if best[0][0] == 2 else 'parcial'}")


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cands = candidates(txn, ctx, ledger)
    if cands:
        tctx = parties.context(txn)
        score = {}
        for c in cands:
            if c[1] not in score:
                score[c[1]] = parties.score(tctx, c[1], ctx)
        ranked = sorted(score.items(), key=lambda kv: -kv[1])
        best_pid, best = ranked[0]
        if best >= NAME_MIN and not (len(ranked) > 1 and ranked[1][1] > best - MARGIN):
            mine = [c for c in cands if c[1] == best_pid]
            return Proposal(txn["bank_txn_id"], [Alloc(mine[0][0], abs(txn["amount_cents"]))], confidence=best / 100,
                            reason=f"tercero {best_pid} ({best:.0f}) + efectivo con {mine[0][4]}")
    dup = _duplicate(txn, ctx, ledger)
    if dup:
        return dup
    am = _by_amount(txn, ctx, ledger)
    if am:
        return am
    # tercero primero: cualquier tercero con parecido >= DUP_MIN cuyas facturas encajen al céntimo con alguna forma de
    # cobro; gana la forma más específica y, a igualdad, el mejor parecido; si dos terceros empatan, no se asigna
    pref = "C" + txn["entity"] if txn["amount_cents"] > 0 else "S"
    found = [f for f in parties.find(parties.context(txn), ctx, pref)[:6] if f[1] >= DUP_MIN]
    best = None
    for pid, sc in found:
        hit = _shapes(txn, ctx, ledger, pid)
        if not hit:
            continue
        key = (SHAPE_RANK[hit[1]], -sc)
        if best is None or key < best[0]:
            best = (key, hit, pid, sc, False)
        elif key[0] == best[0][0] and sc > best[3] - MARGIN:
            best = (best[0], best[1], best[2], best[3], True)     # empate
    if not best or best[4]:
        return None
    # formas débiles (parcial, gastos, sobrepago) exigen un tercero claro; las que usan el PDF o el total, no
    if SHAPE_RANK[best[1][1]] >= 3 and best[3] < NAME_MIN - 15:
        return None
    hit, pid, sc = best[1], best[2], best[3]
    return Proposal(txn["bank_txn_id"], [Alloc(hit[0], abs(txn["amount_cents"]) if hit[1] != "exacto" else hit[2])],
                    confidence=sc / 100 * 0.9, reason=f"tercero {pid} ({sc:.0f}) + forma {hit[1]}")
