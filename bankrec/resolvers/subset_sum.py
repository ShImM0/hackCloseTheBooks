"""Subset-sum sobre partidas abiertas del tercero. Dueño: P2.

Qué hace (POLICIES §5.1, §5.6, §5.9): un tercero identificado por IBAN o nombre paga VARIAS partidas abiertas sin citar
número (ar_ref_noise 1:N): el importe es la suma de un subconjunto de sus partidas abiertas; las notas de crédito
(ARC/APC) restan. Abono -> ARI/ARC del cliente; cargo -> API/APC del proveedor (ES00 paga por ES01/ES02/PT01).
Cómo: tercero con parties.score >= NAME_MIN (IBAN = 100); partidas del tercero abiertas, de la misma divisa y emitidas
antes del movimiento; meet-in-the-middle con numpy sobre las K más antiguas, K creciente (el cliente suele pagar lo más
antiguo, FIFO). Entre varias soluciones, la de menor coste FIFO; sin solución o ambigua -> None (lo decide otro módulo).
Entradas: txn, ctx, ledger. Salida: Proposal con una Alloc por partida al saldo abierto, o None.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter, defaultdict
from itertools import chain

import numpy as np

from bankrec import parties
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers.amount_match import PAYMENT_FACTORY

NAME = "subset_sum"
log = logging.getLogger("bankrec")

NAME_MIN = 85          # parecido de nombre mínimo (0-100), igual que amount_match
STAGES = (8, 14, 20, 24)   # nº de facturas más antiguas que se prueban, de menos a más
MAX_NOTES = 6          # notas de crédito abiertas que se incluyen siempre
MAX_CANDS = 15         # terceros que se estudian por movimiento (los que más palabras de nombre comparten)
MAX_SOL = 50           # soluciones que se enumeran como máximo por etapa
MIN_FRESH = dt.timedelta(days=0)

_TYPES = {True: ("ARI", "ARC"), False: ("API", "APC")}   # True = abono (cobro)


def prepare(ctx) -> None:
    parties.prepare(ctx)
    sh = {"by_party": defaultdict(list), "tok": defaultdict(set), "ents": defaultdict(set)}
    it = ctx.items
    for iid, pid, ent in it.select("item_id", "party_id", "entity").iter_rows():
        if pid:
            sh["by_party"][pid].append(iid)
            sh["ents"][pid].add(ent)
    # índice invertido palabra de nombre -> terceros, para no puntuar a todos los terceros en cada movimiento
    for pid, names in ctx.shared["parties"]["names"].items():
        if len(sh["by_party"].get(pid, ())) >= 2:
            for n in names:
                for w in n.split():
                    if len(w) >= 4:
                        for e in sh["ents"][pid]:
                            sh["tok"][(e, w)].add(pid)
    ctx.shared[NAME] = sh


def _shortlist(tctx: dict, ctx, ents: tuple[str, ...]) -> list[tuple[int, str]]:
    """Terceros candidatos de la entidad con >= 2 partidas, por IBAN (100) o por nº de palabras del nombre en el texto."""
    sh = ctx.shared[NAME]
    P = ctx.shared["parties"]
    ent = set(ents)
    hits: Counter = Counter()
    for ib in tctx["ibans"]:
        for p in P["iban"].get(ib, ()):
            if len(sh["by_party"].get(p, ())) >= 2 and sh["ents"][p] & ent:
                hits[p] += 100
    words = {w for w in tctx["hay"].split() if len(w) >= 4}
    sets = [sh["tok"][(e, w)] for e in ents for w in words if (e, w) in sh["tok"]]
    hits.update(chain.from_iterable(sets))      # nº de palabras de nombre compartidas, a velocidad de C
    return [(h, p) for p, h in hits.most_common(MAX_CANDS)]


def _halves(vals: np.ndarray, cost: np.ndarray):
    """Todas las sumas de un trozo, con su coste FIFO y su nº de partidas (máscara = posición en el array)."""
    s, c, n = np.zeros(1, np.int64), np.zeros(1, np.int64), np.zeros(1, np.int64)
    for v, k in zip(vals, cost):
        s, c, n = np.concatenate([s, s + v]), np.concatenate([c, c + k]), np.concatenate([n, n + 1])
    return s, c, n


def _solve(vals: list[int], rank: list[int], target: int):
    """Mejor subconjunto (índices) con suma == target: menor coste FIFO, luego menos partidas; None si no hay."""
    m = len(vals)
    h = m // 2
    v = np.array(vals, dtype=np.int64)
    r = np.array(rank, dtype=np.int64)
    ls, lc, ln = _halves(v[:h], r[:h])
    rs, rc, rn = _halves(v[h:], r[h:])
    order = np.argsort(rs, kind="stable")
    rs_sorted = rs[order]
    need = target - ls
    lo = np.searchsorted(rs_sorted, need, side="left")
    hi = np.searchsorted(rs_sorted, need, side="right")
    hit = np.nonzero(hi > lo)[0]
    best = None
    seen = 0
    for i in hit:
        for jj in range(lo[i], min(hi[i], lo[i] + MAX_SOL)):
            j = order[jj]
            if ln[i] + rn[j] < 2:
                continue
            key = (int(lc[i] + rc[j]), int(ln[i] + rn[j]))
            seen += 1
            if best is None or key < best[0]:
                best = (key, int(i), int(j))
        if seen > MAX_SOL:
            break
    if best is None:
        return None
    _, i, j = best
    idx = [k for k in range(h) if i >> k & 1] + [h + k for k in range(m - h) if j >> k & 1]
    return idx


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    amt = txn["amount_cents"]
    if not amt:
        return None
    credit = amt > 0
    types = _TYPES[credit]
    ents = (txn["entity"],) if credit else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    target = abs(amt)
    tctx = parties.context(txn)
    short = _shortlist(tctx, ctx, ents)
    if not short:
        return None
    sh = ctx.shared[NAME]
    for _, pid in short:
        inv, notes = [], []
        for iid in sh["by_party"][pid]:
            i = ledger.info[iid]
            oc = ledger.open.get(iid, 0)
            if oc <= 0 or i[0] not in types or i[1] not in ents or i[2] != txn["currency"]:
                continue
            if i[4] is not None and i[4] > txn["booking_date"]:
                continue
            (inv if i[0] == types[0] else notes).append(iid)
        if len(inv) + len(notes) < 2:
            continue
        inv.sort(key=ledger._fifo)
        notes.sort(key=ledger._fifo)
        notes = notes[:MAX_NOTES]
        total_inv = sum(ledger.open[i] for i in inv)
        if total_inv - sum(ledger.open[i] for i in notes) < target and total_inv < target:
            continue    # ni todas las partidas del tercero llegan al importe
        score = parties.score(tctx, pid, ctx)
        if score < NAME_MIN:
            continue
        sol = None
        seen_k = set()
        for k in STAGES:
            k = min(k, len(inv))
            if k in seen_k or k + len(notes) > 30:
                continue
            seen_k.add(k)
            pool = inv[:k] + notes
            vals = [ledger.open[i] for i in inv[:k]] + [-ledger.open[i] for i in notes]
            rank = list(range(k)) + [0] * len(notes)      # las notas no penalizan: se compensan siempre que cuadre
            idx = _solve(vals, rank, target)
            if idx:
                sol = [pool[j] for j in idx]
                break
        if sol:
            allocs = [Alloc(i, ledger.open[i]) for i in sol]
            return Proposal(txn["bank_txn_id"], allocs, confidence=0.8 * score / 100, resolver=NAME,
                            reason=f"tercero {pid} ({score:.0f}): {len(sol)} partidas suman el importe")
    return None
