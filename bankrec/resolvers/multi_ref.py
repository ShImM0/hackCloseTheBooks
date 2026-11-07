"""Conceptos que citan varias facturas. Dueño: P2.

Qué hace (POLICIES §5.5 referencias ruidosas, §5.9 abonos netos): el texto cita DOS O MÁS números impresos
("FACTURA 223672 221609"); si resuelven a partidas abiertas del mismo tercero y la suma de sus saldos cuadra con el
importe, se asigna cada una a su saldo. Abonos (ARC/APC) restan: los que citan su factura en `related_document` se
ofrecen como compensación. Si no cuadra con todas, se prueban subconjuntos de las citadas (ref. cortadas o ajenas).
Una sola referencia la resuelve text_ref; sin referencias, subset_sum. Precisión antes que recall: importe EXACTO,
solución única (o la que usa más partidas citadas); si no, None.
Tolera número sin prefijo, cortado y con erratas (refs.ref_score >= REF_MIN); el tercero debe encajar (parties).
Entradas: txn, ctx, ledger. Salida: Proposal con una Alloc por partida al saldo abierto, o None.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from bankrec import parties, refs
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers.amount_match import PAYMENT_FACTORY

NAME = "multi_ref"
REF_MIN = 85        # sufijo/prefijo >= 5 dígitos o número completo (las erratas, 75, no deciden solas)
PARTY_MIN = 80      # el tercero debe encajar: el número puede coincidir con el de otro cliente
KEY_LEN = 6         # índice por los 6 últimos y 6 primeros dígitos del número impreso
MAX_POOL = 300      # claves con más partidas son prefijos genéricos: se ignoran
MAX_CITED = 12      # partidas citadas como máximo (2^12 subconjuntos)
MAX_NOTES = 4       # abonos de compensación que se ofrecen como máximo

_TYPES = {True: ("ARI", "ARC"), False: ("API", "APC")}   # True = abono bancario (cobro)


def prepare(ctx) -> None:
    """ctx.shared['multi_ref'] = {'idx': (entidad, clave) -> [item_id], 'rel': ARC/APC -> dígitos de su factura,
    'notes': tercero -> [ARC/APC]}."""
    parties.prepare(ctx)
    idx: dict[tuple, list[str]] = defaultdict(list)
    rel: dict[str, str] = {}
    it = ctx.items.filter(ctx.items["open_cents"] > 0)
    for iid, typ, ent, dn, rd in it.select("item_id", "item_type", "entity", "document_number", "related_document").iter_rows():
        d = refs.digits(dn)
        if len(d) >= KEY_LEN:
            idx[(ent, "s" + d[-KEY_LEN:])].append(iid)
            idx[(ent, "p" + d[:KEY_LEN])].append(iid)
        elif len(d) >= 4:
            idx[(ent, "s" + d)].append(iid)
        if typ in ("ARC", "APC") and rd:
            rel[iid] = refs.digits(rd)
    notes: dict[str, list[str]] = defaultdict(list)
    for iid, pid in it.filter(it["item_type"].is_in(["ARC", "APC"])).select("item_id", "party_id").iter_rows():
        notes[pid].append(iid)
    ctx.shared[NAME] = {"idx": idx, "rel": rel, "notes": notes}


def _pick(vals: dict[str, int], cited: list[str], notes: list[str], target: int) -> list[str] | None:
    """Subconjunto de `cited` (+ abonos) con suma == target. Prefiere el que usa más citadas; empate -> None."""
    n = len(cited)
    best: list[list[str]] = []
    for k in range(n, 0, -1):
        for comb in combinations(cited, k):
            base = sum(vals[c] for c in comb)
            if base == target:
                sols = [list(comb)]
            else:
                sols = []
                for m in range(1, min(len(notes), MAX_NOTES) + 1):
                    for nc in combinations(notes, m):
                        if base - sum(vals[x] for x in nc) == target:
                            sols.append(list(comb) + list(nc))
            best.extend(sols)
        if best:
            break      # solución con más citadas: no se miran subconjuntos menores
    if len(best) != 1 or len(best[0]) < 2:
        return None
    return best[0]


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    amt = txn["amount_cents"]
    if not amt:
        return None
    toks = [t for t in refs.tokens(refs.joined(refs.haystack(txn))) if len(t) >= 5]
    if len(set(toks)) < 2:
        return None
    sh = ctx.shared[NAME]
    credit = amt > 0
    types = _TYPES[credit]
    ents = (txn["entity"],) if credit else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    pool: set[str] = set()
    for e in ents:
        for t in toks:
            for key in {"s" + t[-KEY_LEN:], "p" + t[:KEY_LEN], "s" + t}:
                hit = sh["idx"].get((e, key), ())
                if len(hit) <= MAX_POOL:
                    pool.update(hit)
    by_party: dict[str, list[str]] = defaultdict(list)
    cited_notes: dict[str, set[str]] = defaultdict(set)     # abonos citados por su propio número
    for c in pool:
        i = ledger.info[c]
        if i[0] not in types or ledger.open.get(c, 0) <= 0 or i[2] != txn["currency"]:
            continue
        if i[4] and i[4] > txn["booking_date"]:
            continue
        if refs.ref_score(i[5], toks) >= REF_MIN:
            if i[0] == types[0]:
                by_party[i[3]].append(c)
            else:
                cited_notes[i[3]].add(c)
    # >= 2 facturas citadas, o 1 factura citada + algún abono (citado por número o que la cita en related_document)
    cands = {}
    for p, v in by_party.items():
        nums = {refs.digits(ledger.info[c][5]) for c in v}
        if len(v) >= 2 or cited_notes.get(p) or any(sh["rel"].get(n) in nums for n in sh["notes"].get(p, ())):
            cands[p] = v
    if not cands:
        return None
    tctx = parties.context(txn)
    ok = []
    for pid, cited in cands.items():
        s = parties.score(tctx, pid, ctx)
        if s >= PARTY_MIN:
            ok.append((s, pid, cited))
    if len(ok) != 1:
        return None
    s, pid, cited = ok[0]
    if len(cited) > MAX_CITED:
        return None
    cited.sort(key=ledger._fifo)
    # abonos abiertos del tercero que citan alguna de las facturas citadas (o, si no, los más antiguos)
    cited_digits = {refs.digits(ledger.info[c][5]) for c in cited}
    notes = []
    for iid in sh["notes"].get(pid, ()):
        if ledger.open.get(iid, 0) > 0 and ledger.info[iid][1] in ents and ledger.info[iid][2] == txn["currency"]:
            if sh["rel"].get(iid) in cited_digits or iid in cited_notes.get(pid, ()):
                notes.append(iid)
    vals = {c: ledger.open[c] for c in cited}
    vals.update({n: ledger.open[n] for n in notes})
    sol = _pick(vals, cited, notes, abs(amt))
    if not sol:
        return None
    return Proposal(txn["bank_txn_id"], [Alloc(i, ledger.open[i]) for i in sol], confidence=0.9 * min(s, 100) / 100,
                    reason=f"tercero {pid} ({s:.0f}): {len(sol)} partidas citadas suman el importe")
