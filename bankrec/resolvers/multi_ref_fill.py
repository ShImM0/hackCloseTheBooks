"""Complemento de multi_ref (P2) para conceptos CORTADOS: citadas + subconjunto único del mismo cliente. Dueño: P1.

Qué hace (POLICIES §5.3-5.5, §5.9): el texto cita uno o varios números impresos de un mismo cliente, a menudo cortados
(el banco trunca a 35/38/140 caracteres), y el cobro paga esas facturas y quizá otras del mismo cliente, netas de sus
abonos. Pasos:
1. tercero = el de las partidas abiertas citadas (ref ≥ REF_MIN) y que encaja con el texto (parties.score);
2. citadas = partidas abiertas de ese tercero cuyo número aparece en el texto (obligatorias);
3. si su suma == efectivo: se asignan; si no, se completa con un subconjunto ÚNICO de las demás partidas abiertas del
   tercero (facturas +, abonos −) cuya suma cuadre exacto. Si hay varias soluciones o ninguna: None.
   - Si las citadas ya superan el cobro, la diferencia es una deducción (descuento, retención, disputa o pago
     parcial: §3) o un abono neteado (§5.9): solo se completa con abonos.
Importes en céntimos y en la divisa de la partida (FX: orig_amount). Las excepciones las decide exceptions.py.
Entradas: txn, ctx, ledger. Salida: Proposal o None.
"""
from __future__ import annotations

from collections import defaultdict

import polars as pl

from bankrec import parties, refs
from bankrec.contract import Alloc, Proposal

NAME = "multi_ref_fill"
REF_MIN = 85       # mismo umbral que text_ref
PARTY_MIN = 80
MAX_FREE = 22      # partidas no citadas que entran en la búsqueda (2^11 por mitad)


def prepare(ctx) -> None:
    """ctx.shared[NAME][party] = [item_id] de ARI/ARC con saldo > 0 al inicio o emitidas en la fase."""
    parties.prepare(ctx)
    it = ctx.items.filter((pl.col("open_cents") > 0) & pl.col("item_type").is_in(["ARI", "ARC"]))
    by_party: dict[str, list[str]] = defaultdict(list)
    for iid, pid in it.select("item_id", "party_id").iter_rows():
        by_party[pid].append(iid)
    ctx.shared[NAME] = by_party


def _subsets(vals: list[int], target: int) -> list[int] | None:
    """Índices del ÚNICO subconjunto de vals que suma exactamente target (meet-in-the-middle). None si 0 o > 1."""
    n = len(vals)
    h = n // 2
    left, right = vals[:h], vals[h:]

    def sums(v):
        out = {}
        for mask in range(1 << len(v)):
            s = 0
            for i in range(len(v)):
                if mask >> i & 1:
                    s += v[i]
            out.setdefault(s, []).append(mask)
        return out

    L, R = sums(left), sums(right)
    sol = None
    for s, lms in L.items():
        rms = R.get(target - s)
        if not rms:
            continue
        if len(lms) * len(rms) > 1 or sol is not None:
            return None   # ambiguo
        sol = (lms[0], rms[0])
    if sol is None:
        return None
    lm, rm = sol
    return [i for i in range(h) if lm >> i & 1] + [h + i for i in range(n - h) if rm >> i & 1]


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cents = txn["amount_cents"]
    if cents <= 0 or NAME not in ctx.shared:
        return None
    toks = [t for t in refs.tokens(refs.joined(refs.haystack(txn))) if len(t) >= 5]
    if not toks:
        return None
    cur, cash = txn["currency"], cents
    if txn.get("orig_currency") and txn["orig_currency"] != cur and txn.get("orig_amount_cents"):
        cur, cash = txn["orig_currency"], abs(txn["orig_amount_cents"])
    # partidas citadas, agrupadas por tercero (solo índices de text_ref: número impreso → partida)
    idx = ctx.shared.get("text_ref", {})
    cited: dict[str, set[str]] = defaultdict(set)
    for t in toks:
        for key in {"s" + t[-6:], "p" + t[:6], "s" + t}:
            hit = idx.get((txn["entity"], key), ())
            if len(hit) > 300:
                continue
            for c in hit:
                i = ledger.info[c]
                if (i[0] in ("ARI", "ARC") and ledger.open.get(c, 0) > 0 and i[2] == cur
                        and (i[4] is None or i[4] <= txn["booking_date"]) and refs.ref_score(i[5], toks) >= REF_MIN):
                    cited[i[3]].add(c)
    if len(cited) != 1:
        return None
    pid, forced = next(iter(cited.items()))
    if parties.score(parties.context(txn), pid, ctx) < PARTY_MIN:
        return None
    sign = {c: (-1 if ledger.info[c][0] == "ARC" else 1) for c in forced}
    rest = cash - sum(sign[c] * ledger.open[c] for c in forced)
    chosen = sorted(forced, key=ledger._fifo)
    if rest != 0:
        free = [c for c in ctx.shared[NAME].get(pid, ()) if c not in forced and ledger.open.get(c, 0) > 0
                and ledger.info[c][1] == txn["entity"] and ledger.info[c][2] == cur
                and (ledger.info[c][4] is None or ledger.info[c][4] <= txn["booking_date"])]
        if rest < 0:   # citadas > cobro: solo un abono puede netear la diferencia (§3, §5.9)
            free = [c for c in free if ledger.info[c][0] == "ARC"]
        if len(free) > MAX_FREE:
            # §3 UNIDENTIFIED: una solución única sobre una muestra no prueba unicidad en el conjunto real.
            return None
        free.sort(key=ledger._fifo)
        vals = [(-1 if ledger.info[c][0] == "ARC" else 1) * ledger.open[c] for c in free]
        pick = _subsets(vals, rest)
        if pick is None:
            return None
        chosen += [free[k] for k in pick]
    if len(chosen) < 2:
        return None   # 1:1 lo resuelven text_ref / amount_match
    return Proposal(txn["bank_txn_id"], [Alloc(c, ledger.open[c]) for c in chosen], confidence=0.9,
                    reason=f"{len(forced)} citadas + {len(chosen) - len(forced)} por suma exacta, tercero {pid}")


if __name__ == "__main__":
    assert sorted(_subsets([100, 250, 300, 75], 325)) == [1, 3]
    assert _subsets([100, 100], 100) is None          # ambiguo
    assert _subsets([500, -200, 50], 300) == [0, 1]
    print("multi_ref OK")
