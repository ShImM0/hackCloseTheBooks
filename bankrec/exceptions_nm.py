"""Resolver N:M por referencias: cobro que cita VARIAS facturas y deja la última a medias. Dueño: P3 (módulo propio,
a añadir a la cascada por P1 justo después de "bankrec.resolvers.multi_ref_fill").

Caso (POLICIES §3 PARTIAL "unplanned partial payments", §5.5 referencias): el texto cita todas las facturas abiertas
de un cliente que paga el cobro, pero el efectivo no llega a la suma de sus saldos: todas se liquidan completas salvo
UNA, que recibe el resto (PARTIAL; lo pone exceptions.finalize porque el importe < saldo).
multi_ref / multi_ref_fill exigen suma exacta y text_ref.by_ref solo asigna una partida, así que hoy se pierden.

Cuál es la parcial: la única citada cuyo resto es una fracción redonda de su total (exceptions.PARTIAL_FRACS).
TRAIN ar_n_to_m: la parcial es exactamente 40/50/60 % del total en los 6.937 casos; las fracciones de PARTIAL tienen
el mismo reparto en 2022, 2023 y 2024 (no es ajuste a val).
Si ninguna o varias encajan: None (la "última citada en el texto" solo acierta el 71-75 %; FALLBACK_LAST la activa).
Análisis offline: scripts/p3_nm_train.py, scripts/p3_nm_which.py.

Condiciones (precisión antes que recall):
- ≥ 2 citadas (ref ≥ REF_MIN), todas ARI abiertas del MISMO tercero, misma divisa y emitidas antes del cobro;
- el tercero encaja con el texto (parties.score ≥ PARTY_MIN);
- efectivo < suma de saldos y el resto para la parcial entre MIN_FRAC y MAX_FRAC de su saldo
  (si el resto es casi el saldo, es un descuento o una deducción, no un parcial: lo deja a los demás).
Medido en val (2 meses): 93,92 → 94,02, ningún segmento baja; umbrales vecinos (0,10/0,80 y 0,02/0,95) ±0,01.
Entradas: txn, ctx, ledger. Salida: Proposal o None.
"""
from __future__ import annotations

from collections import defaultdict

from bankrec import parties, refs
from bankrec.contract import Alloc, Proposal
from bankrec.exceptions import PARTIAL_FRACS

NAME = "nm_partial"
REF_MIN = 85        # mismo umbral que text_ref / multi_ref_fill
PARTY_MIN = 80
MIN_FRAC = 0.05     # resto para la última ≥ 5 % de su saldo
MAX_FRAC = 0.90     # … y ≤ 90 % (por encima suele ser descuento, retención o disputa: §3)
MAX_HIT = 300       # claves de índice demasiado genéricas
FALLBACK_LAST = False   # sin fracción redonda única: la última citada en el texto (TRAIN 71-75 %)


def prepare(ctx) -> None:
    """Reutiliza el índice de text_ref (número impreso → partida); solo necesita los terceros."""
    parties.prepare(ctx)
    it = ctx.items.filter(ctx.items["item_type"] == "ARI")
    ctx.shared[NAME] = dict(zip(it["item_id"].to_list(), it["total_cents"].to_list()))


def _first_pos(doc: str, toks: list[str]) -> int | None:
    """Posición del primer token del texto que casa con el número impreso (orden de cita)."""
    for k, tk in enumerate(toks):
        if refs.ref_score(doc, [tk]) >= REF_MIN:
            return k
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cash = txn["amount_cents"]
    idx = ctx.shared.get("text_ref")
    if cash <= 0 or not idx:
        return None
    if txn.get("orig_currency") and txn["orig_currency"] != txn["currency"]:
        return None     # FX: el importe aplicado no es comparable sin tipo de cambio
    toks = [t for t in refs.tokens(refs.joined(refs.haystack(txn))) if len(t) >= 5]
    if len(toks) < 2:
        return None
    cited: dict[str, set[str]] = defaultdict(set)
    for t in toks:
        for key in {"s" + t[-6:], "p" + t[:6], "s" + t}:
            hit = idx.get((txn["entity"], key), ())
            if len(hit) > MAX_HIT:
                continue
            for c in hit:
                i = ledger.info[c]
                if (i[0] == "ARI" and ledger.open.get(c, 0) > 0 and i[2] == txn["currency"]
                        and (i[4] is None or i[4] <= txn["booking_date"]) and refs.ref_score(i[5], toks) >= REF_MIN):
                    cited[i[3]].add(c)
    if len(cited) != 1:
        return None
    pid, items = next(iter(cited.items()))
    if len(items) < 2:
        return None
    pos = {c: _first_pos(ledger.info[c][5], toks) for c in items}
    if any(p is None for p in pos.values()):
        return None
    order = sorted(items, key=lambda c: (pos[c], ledger.info[c][4] or txn["booking_date"], c))
    tie_last = pos[order[-1]] == pos[order[-2]]   # la última citada no está clara (mismo token)
    total = sum(ledger.open[c] for c in order)
    if cash >= total:
        return None     # exacto o sobrepago: multi_ref / exceptions
    # parcial = la citada cuyo resto es una fracción redonda de su total (TRAIN ar_n_to_m: 40/50/60 % exactos);
    # si no hay una única, la última citada en el texto
    tot = ctx.shared[NAME]
    def rest_of(c):
        return cash - (total - ledger.open[c])
    fr = [c for c in order if tot.get(c) and any(abs(rest_of(c) - round(tot[c] * f)) <= 1 for f in PARTIAL_FRACS)]
    if len(fr) == 1:
        last = fr[0]
    elif FALLBACK_LAST and not tie_last:
        last = order[-1]
    else:
        return None
    rest = rest_of(last)
    if not (MIN_FRAC * ledger.open[last] <= rest <= MAX_FRAC * ledger.open[last]):
        return None
    order = [c for c in order if c != last] + [last]
    if parties.score(parties.context(txn), pid, ctx) < PARTY_MIN:
        return None
    allocs = [Alloc(c, ledger.open[c]) for c in order[:-1]] + [Alloc(last, rest)]
    return Proposal(txn["bank_txn_id"], allocs, confidence=0.85,
                    reason=f"{len(order)} citadas, la última ({ledger.info[last][5]}) parcial; tercero {pid}")
