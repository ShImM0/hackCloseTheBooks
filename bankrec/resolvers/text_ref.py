"""Resolver 1:1 por referencia impresa. Dueño: P1.

Regla 1 de la cascada 1:1 (POLICIES §5.5). Dos pasos:
- resolve(): entre las partidas abiertas con saldo == importe (amount_match.candidates), la única cuyo número
  impreso (o PO del cliente) aparece en el texto. Si hay empate no decide (lo intenta amount_match por tercero).
- by_ref(): (lo llama amount_match cuando no hay importe exacto) partida abierta cuyo número aparece en el texto
  aunque el importe no cuadre: descuentos, parciales, plazos, retenciones, FX (§3). La partida se propone con su
  saldo completo y exceptions.py (P3) decide excepción e importe. Exige además que el tercero encaje.
Tolera número sin prefijo, cortado y con erratas (refs.ref_score).
"""
from __future__ import annotations

from collections import defaultdict

from bankrec import parties, refs
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers.amount_match import PAYMENT_FACTORY, candidates

NAME = "text_ref"
REF_MIN = 85       # sufijo/prefijo ≥ 5 dígitos o número completo; las erratas (75) no deciden solas
PARTY_MIN = 80     # by_ref: el tercero debe encajar (el número solo puede coincidir con el de otro cliente)
MAX_RATIO = 1.5    # by_ref (misma divisa): efectivo ≤ 1,5 × saldo (sobrepagos) …
MIN_RATIO = 0.02   # … y ≥ 2 % del saldo (plazos, a cuenta)
KEY_LEN = 6        # índice por los 6 primeros y 6 últimos dígitos del número impreso
MAX_POOL = 300     # se ignoran claves con más partidas (prefijos genéricos)


def prepare(ctx) -> None:
    """ctx.shared['text_ref'][(entity, key)] = [item_id] para partidas con saldo > 0 al inicio o emitidas en la fase."""
    parties.prepare(ctx)
    idx: dict[tuple, list[str]] = defaultdict(list)
    it = ctx.items.filter(ctx.items["open_cents"] > 0)
    for iid, typ, ent, dn in it.select("item_id", "item_type", "entity", "document_number").iter_rows():
        if typ not in ("ARI", "API"):
            continue
        d = refs.digits(dn)
        if len(d) >= KEY_LEN:
            idx[(ent, "s" + d[-KEY_LEN:])].append(iid)
            idx[(ent, "p" + d[:KEY_LEN])].append(iid)
    ctx.shared[NAME] = idx


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cands = candidates(txn, ledger)
    if not cands:
        return None
    toks = refs.tokens(refs.haystack(txn))
    if not toks:
        return None
    scored = []
    for c in cands:
        i = ledger.info[c]
        s = max(refs.ref_score(i[5], toks), refs.ref_score(i[6], toks) if i[6] else 0)
        if s >= REF_MIN:
            scored.append((s, c))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    s, c = scored[0]
    return Proposal(txn["bank_txn_id"], [Alloc(c, abs(txn["amount_cents"]))], confidence=s / 100,
                    reason=f"ref {ledger.info[c][5]} ({s}) + importe")


def by_ref(txn: dict, ctx, ledger) -> Proposal | None:
    """Número impreso + tercero, sin exigir importe exacto. Una sola partida o nada."""
    cents = txn["amount_cents"]
    if not cents or NAME not in ctx.shared:   # text_ref desactivado
        return None
    toks = [t for t in refs.tokens(refs.haystack(txn)) if len(t) >= 5]
    if not toks:
        return None
    typ = "ARI" if cents > 0 else "API"
    ents = (txn["entity"],) if cents > 0 else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    idx = ctx.shared[NAME]
    pool = set()
    for e in ents:
        for t in toks:
            for key in ("s" + t[-KEY_LEN:], "p" + t[:KEY_LEN]):
                hit = idx.get((e, key), ())
                if len(hit) <= MAX_POOL:   # claves tipo "AAMM.." genéricas: demasiado ambiguas y lentas
                    pool.update(hit)
    if not pool:
        return None
    tctx = parties.context(txn)
    pscore: dict[str, float] = {}
    best = []
    for c in pool:
        i = ledger.info[c]
        oc = ledger.open.get(c, 0)
        if i[0] != typ or oc <= 0 or (i[4] and i[4] > txn["booking_date"]):
            continue
        if i[2] == txn["currency"] and not (MIN_RATIO * oc <= abs(cents) <= MAX_RATIO * oc):
            continue
        s = refs.ref_score(i[5], toks)
        if s < REF_MIN:
            continue
        if i[3] not in pscore:
            pscore[i[3]] = parties.score(tctx, i[3], ctx)
        p = pscore[i[3]]
        if p < PARTY_MIN:
            continue
        best.append((s, p, c))
    if not best:
        return None
    best.sort(key=lambda x: (-x[0], -x[1]))
    if len(best) > 1 and best[1][:2] == best[0][:2]:
        return None
    s, p, c = best[0]
    return Proposal(txn["bank_txn_id"], [Alloc(c, ledger.open[c])], confidence=min(s, p) / 100,
                    resolver=NAME, reason=f"ref {ledger.info[c][5]} ({s}) + tercero ({p:.0f}), importe distinto")
