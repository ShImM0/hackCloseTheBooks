"""Subset-sum sobre partidas abiertas del tercero. Dueño: P2.

Qué hace (POLICIES §5.1, §5.6, §5.9): un tercero identificado por IBAN o nombre paga VARIAS partidas abiertas sin citar
número (ar_ref_noise 1:N): el importe es la suma de un subconjunto de sus partidas abiertas; las notas de crédito
(ARC/APC) restan. Abono -> ARI/ARC del cliente; cargo -> API/APC del proveedor (ES00 paga por ES01/ES02/PT01).
Cómo: tercero con parties.score >= PARTY_MIN (IBAN = 100); partidas del tercero abiertas, de la misma divisa y emitidas
antes del movimiento; meet-in-the-middle con numpy sobre las K más antiguas, K creciente (el cliente suele pagar lo más
antiguo, FIFO). Entre varias soluciones, la de menor coste FIFO (suma de posiciones; §5.6 entre importes iguales).

Precisión (§3 UNIDENTIFIED: "do not force a match"; §5.5 lookalikes): con 20-30 partidas hay 2^20+ sumas y casi
cualquier importe coincide por casualidad con alguna (cobros con descuento, disputa, de otro tercero o de partidas que no
están entre las K). Sin guardas acertaba el 52 % de los pares en val (TRAIN 2025-01..06) y el 67 % en 2024-01..02.
Se acepta solo si:
- la coincidencia casual es improbable: LAM = sumas NO exactas del grupo por céntimo en ±max(W_MIN, 1 %) del importe
  (= nº esperado de subconjuntos que cuadran por azar; x mcd si los importes son redondos) <= LAM_MAX. Con más partidas
  LAM crece, así que la búsqueda se para al superarlo. Precisión de pares por LAM, val (2024-01..02): <=0,001 99 %
  (99 %); ~0,01 94 % (97 %); ~0,1 62 % (78 %); >=1 26-32 % (31-41 %). Con todas las guardas: 98,5 % (99,1 %); con
  LAM_MAX 0,02 / 0,1: 99,0 / 98,0 % (99,5 / 99,0 %), sin saltos;
- el tercero está claro: IBAN o nombre >= PARTY_MIN (con 85-94 casi todo era de otro cliente con nombre parecido);
- la solución neta como mucho MAX_NOTES_SOL abonos (con 2 o más, 25 % de acierto: compensaciones casuales) y solo
  junto a su factura (§5.9: el abono de devolución/precio cita su factura en related_document; abono con su factura en
  la solución 99 % de acierto, sin ella 19 %);
- ninguna partida sola cuadra con el importe (eso es un 1:1, no un 1:N) y hay <= MAX_SOL soluciones exactas.
Si la búsqueda libre no da una solución segura, segunda pasada "FIFO con huecos" (§5.6: se paga lo más antiguo): las L
facturas más antiguas (hasta K_GAPS) menos como mucho MAX_GAPS, sin abonos. Es una familia mucho más pequeña que 2^K,
así que su LAM (misma definición, sobre la familia) es bajo aunque el cliente tenga muchas partidas abiertas. Estimado
sobre las soluciones que la búsqueda libre rechaza: <= 2 huecos 98,7 % en val (100 % en 2024-01..02); 3 huecos 96 % (89 %).
Sin solución segura -> None (ranker / UNIDENTIFIED / categoría): un par falso resta precisión, rompe el movimiento y
consume la partida que luego cobra el movimiento correcto (que saldría DUPLICATE_PAYMENT).
Entradas: txn, ctx, ledger. Salida: Proposal con una Alloc por partida al saldo abierto, o None.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import chain, combinations

import numpy as np
import polars as pl

from bankrec import parties
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers.amount_match import PAYMENT_FACTORY

NAME = "subset_sum"
log = logging.getLogger("bankrec")

PARTY_MIN = 95         # parecido de nombre mínimo (0-100); IBAN = 100
STAGES = (8, 14, 20, 24)   # nº de facturas más antiguas que se prueban, de menos a más
MAX_NOTES = 6          # notas de crédito abiertas que se incluyen siempre
MAX_CANDS = 15         # terceros que se estudian por movimiento (los que más palabras de nombre comparten)
MAX_SOL = 50           # más soluciones exactas que esto = ambigua (no se enumeran)
LAM_MAX = 0.05         # coincidencias casuales esperadas como máximo (densidad de sumas por céntimo)
W_MIN = 100            # semiventana mínima (céntimos) para estimar la densidad; si no, 1 % del importe
MAX_NOTES_SOL = 1      # abonos netos como máximo en la solución
MAX_GAPS = 2           # FIFO con huecos: facturas antiguas sin pagar (saltadas) como máximo
K_GAPS = 40            # FIFO con huecos: facturas más antiguas que se miran

_TYPES = {True: ("ARI", "ARC"), False: ("API", "APC")}   # True = abono (cobro)
_AMBIG = "ambigua"


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
    # abono -> su factura (related_document = nº impreso de la factura del mismo tercero y entidad; §5.9)
    inv = it.filter(pl.col("item_type").is_in(["ARI", "API"])).select(
        "entity", pl.col("document_number").alias("related_document"), pl.col("item_id").alias("inv_id"),
        pl.col("party_id").alias("inv_party"))
    cn = it.filter(pl.col("item_type").is_in(["ARC", "APC"]) & pl.col("related_document").is_not_null()).select(
        "item_id", "entity", "related_document", "party_id")
    j = (cn.join(inv, on=["entity", "related_document"]).filter(pl.col("party_id") == pl.col("inv_party"))
         .sort("item_id", "inv_id"))      # orden fijo: si un nº de factura se repite, gana siempre la misma
    sh["rel"] = dict(zip(j["item_id"].to_list(), j["inv_id"].to_list()))
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
    # empates por party_id: most_common los rompe por orden de inserción (sets -> depende de PYTHONHASHSEED)
    return sorted(((h, p) for p, h in hits.items()), key=lambda x: (-x[0], x[1]))[:MAX_CANDS]


def _halves(vals: np.ndarray, cost: np.ndarray):
    """Todas las sumas de un trozo, con su coste FIFO y su nº de partidas (máscara = posición en el array)."""
    s, c, n = np.zeros(1, np.int64), np.zeros(1, np.int64), np.zeros(1, np.int64)
    for v, k in zip(vals, cost):
        s, c, n = np.concatenate([s, s + v]), np.concatenate([c, c + k]), np.concatenate([n, n + 1])
    return s, c, n


def _solve(vals: list[int], rank: list[int], target: int, rel: list[int] | None = None):
    """(solución, lam). solución = índices del mejor subconjunto con suma == target (menor coste FIFO, luego menos
    partidas; como mucho MAX_NOTES_SOL abonos = valores negativos, y cada abono junto a su factura: rel[k] = posición
    de la factura del abono k, -1 si no está en el grupo), _AMBIG si una partida sola cuadra o hay más de MAX_SOL
    soluciones, o None si no hay. lam = sumas no exactas por céntimo en ±max(W_MIN, 1 %) del importe, x mcd de
    los importes: nº esperado de subconjuntos que cuadran por azar."""
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
    n_exact = int((hi - lo).sum())
    w = max(W_MIN, target // 100)
    lw = np.searchsorted(rs_sorted, need - w, side="left")
    hw = np.searchsorted(rs_sorted, need + w, side="right")
    # importes redondos (todos múltiplos de g céntimos): las sumas caen en una rejilla g veces más densa
    lam = (int((hw - lw).sum()) - n_exact) / (2 * w) * int(np.gcd.reduce(np.abs(v)))
    if n_exact == 0:
        return None, lam
    if n_exact > MAX_SOL:
        return _AMBIG, lam
    best = None
    for i in np.nonzero(hi > lo)[0]:
        for jj in range(lo[i], hi[i]):
            j = order[jj]
            idx = [k for k in range(h) if i >> k & 1] + [h + k for k in range(m - h) if j >> k & 1]
            if len(idx) < 2:
                return _AMBIG, lam      # una partida sola cuadra: es un 1:1 (amount_match / ranker), no un 1:N
            cn = [k for k in idx if vals[k] < 0]
            if len(cn) > MAX_NOTES_SOL or (rel is not None and any(rel[k] not in idx for k in cn)):
                continue
            key = (int(lc[i] + rc[j]), len(idx))
            if best is None or key < best[0]:
                best = (key, idx)
    return (best[1] if best else None), lam


@lru_cache(maxsize=None)
def _combos(n: int, g: int) -> np.ndarray:
    """Huecos posibles: combinaciones de g posiciones entre 0..n-2 (la última pagada nunca es hueco)."""
    if g == 0:
        return np.zeros((1, 0), dtype=np.int64)
    return np.array(list(combinations(range(n - 1), g)), dtype=np.int64).reshape(-1, g)


def _fifo_gaps(vals: list[int], target: int):
    """(solución, lam) en la familia "las L más antiguas menos <= MAX_GAPS" (la L-ésima pagada; >= 2 pagadas).
    vals = saldos de las facturas en orden FIFO (> 0). Mejor = menos huecos, luego menor L, luego huecos más recientes
    (§5.6: entre importes iguales se paga la más antigua). lam como en _solve, sobre los miembros de la familia."""
    n = len(vals)
    v = np.array(vals, dtype=np.int64)
    P = np.concatenate([np.zeros(1, np.int64), np.cumsum(v)])     # P[L] = suma de las L más antiguas
    w = max(W_MIN, target // 100)
    best, n_exact, n_win = None, 0, 0
    for g in range(MAX_GAPS + 1):
        R = _combos(n, g)
        if not len(R):
            break
        sR = v[R].sum(axis=1) if g else np.zeros(1, np.int64)
        Lmin = np.maximum(R.max(axis=1) + 2, g + 2) if g else np.full(1, 2, np.int64)
        need = target + sR
        lo = np.maximum(np.searchsorted(P, need, side="left"), Lmin)
        hi = np.searchsorted(P, need, side="right")
        lw = np.maximum(np.searchsorted(P, need - w, side="left"), Lmin)
        hw = np.searchsorted(P, need + w, side="right")
        n_win += int(np.clip(hw - lw, 0, None).sum())
        hit = np.nonzero(hi > lo)[0]
        n_exact += len(hit)
        if best is None and len(hit):
            k = min(hit.tolist(), key=lambda x: (int(lo[x]), -int(R[x].sum())))
            rem = set(R[k].tolist())
            best = [i for i in range(int(lo[k])) if i not in rem]
    gcd = int(np.gcd.reduce(v)) if n else 1
    return best, (n_win - n_exact) / (2 * w) * gcd


def _claimed(ctx) -> dict:
    """{item_id: bank_txn_id} de las partidas de avisos de remesa casados (docs_remit); {} si no lo publica."""
    sh = ctx.shared.get("docs_remit")
    cl = sh.get("claimed") if isinstance(sh, dict) else None
    return cl if isinstance(cl, dict) else {}


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
    tid = txn["bank_txn_id"]
    claimed = _claimed(ctx)
    for _, pid in short:
        inv, notes = [], []
        for iid in sh["by_party"][pid]:
            i = ledger.info[iid]
            oc = ledger.open.get(iid, 0)
            if oc <= 0 or i[0] not in types or i[1] not in ents or i[2] != txn["currency"]:
                continue
            if claimed and claimed.get(iid, tid) != tid:
                continue    # la lista un aviso de remesa casado con otro movimiento (§5.4): la cobra ese
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
        if score < PARTY_MIN:
            continue
        sol, lam, ambig = None, 0.0, False
        seen_k = set()
        for k in STAGES:
            k = min(k, len(inv))
            if k in seen_k or k + len(notes) > 30:
                continue
            seen_k.add(k)
            pool = inv[:k] + notes
            vals = [ledger.open[i] for i in inv[:k]] + [-ledger.open[i] for i in notes]
            rank = list(range(k)) + [0] * len(notes)      # las notas no penalizan: se compensan siempre que cuadre
            pos = {iid: p for p, iid in enumerate(inv[:k])}
            rel = [-1] * k + [pos.get(sh["rel"].get(n), -1) for n in notes]
            idx, lam = _solve(vals, rank, target, rel)
            if idx is _AMBIG:
                ambig = True
                break
            if lam > LAM_MAX:
                break       # con más partidas solo crecen las coincidencias casuales
            if idx:
                sol = [pool[j] for j in idx]
                break
        how = "libre"
        if not sol and not ambig and len(inv) >= 2:
            gv = [ledger.open[i] for i in inv[:K_GAPS]]
            if target not in gv:        # una factura sola con ese importe: es un 1:1, no un 1:N
                idx, lam = _fifo_gaps(gv, target)
                if idx and lam <= LAM_MAX:
                    sol, how = [inv[j] for j in idx], "fifo"
        if sol:
            allocs = [Alloc(i, ledger.open[i]) for i in sol]
            return Proposal(txn["bank_txn_id"], allocs, confidence=0.8 * score / 100, resolver=NAME,
                            reason=f"tercero {pid} ({score:.0f}): {len(sol)} partidas suman el importe "
                                   f"({how}, lam {lam:.3f})")
    return None
