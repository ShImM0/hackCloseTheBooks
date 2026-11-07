"""Último recurso antes de nonitem_unmatched: asigna SOLO las partidas que el texto cita. Dueño: P1.

Qué hace (POLICIES §5.3-5.5, §5.9): un cobro grande cita algunas de las facturas que paga (el banco corta el concepto a
35/38/140 caracteres o lo parte en trozos ' | '), así que la suma de las citadas no llega al cobro y multi_ref /
multi_ref_fill / subset_sum no pueden cerrar el conjunto. Sin este paso el movimiento acaba sin partidas. Aquí se
asignan las citadas que son inequívocas: cada par correcto suma en Allocation F1 aunque el conjunto quede incompleto,
y el movimiento ya estaba perdido para la exactitud por movimiento.
Pasos:
1. tokens numéricos (≥ MIN_DIG dígitos) del texto y del texto con los trozos N43 pegados;
2. partidas vivas ARI/ARC de la entidad, en la divisa de la cuenta o la original, emitidas antes del movimiento:
   dígitos iguales, o sufijo único (token ≥ 6 dígitos) o prefijo (token ≥ 7, número cortado);
3. tercero = el mayoritario entre las citas inequívocas, con parties.score ≥ PARTY_MIN;
4. se asigna solo si la suma neta de las citadas (facturas − abonos) no supera el cobro y hay ≥ 2 citadas o el cobro
   supera OVER × la única (si no, sería un 1:1 con diferencia: lo deciden otros resolvers / exceptions).
Medido (val 2025-H1, sobre los cobros que acababan sin partidas): ~5.000 pares con 98,5 % de precisión; en 2024-H1
98,7 %. Importe = saldo vivo de cada partida (las excepciones las decide exceptions.py).
Entradas: txn, ctx, ledger. Salida: Proposal o None.
"""
from __future__ import annotations

from collections import defaultdict

import polars as pl

from bankrec import parties, refs
from bankrec.contract import Alloc, Proposal
from bankrec.exceptions import overpay_shape

NAME = "multi_ref_cited"
MIN_DIG = 5        # dígitos mínimos de un token citado
PARTY_MIN = 80     # mismo umbral que multi_ref_fill
OVER = 1.11        # una sola citada: el cobro debe superarla claramente (como pipeline.OVERPAY_MAX)


def prepare(ctx) -> None:
    """Índices sin tope por (entidad, dígitos): completo, sufijo 6 y prefijo 6 del número impreso."""
    parties.prepare(ctx)
    full, suf, pre = defaultdict(list), defaultdict(list), defaultdict(list)
    dig, info = {}, {}
    it = ctx.items.filter(pl.col("item_type").is_in(["ARI", "ARC"]))
    for iid, typ, ent, dn, pid, cur, iss in it.select("item_id", "item_type", "entity", "document_number", "party_id",
                                                     "currency", "issue_date").iter_rows():
        d = refs.digits(dn)
        if len(d) < 4:
            continue
        dig[iid], info[iid] = d, (typ, pid, cur, iss)
        full[(ent, d)].append(iid)
        if len(d) >= 6:
            suf[(ent, d[-6:])].append(iid)
            pre[(ent, d[:6])].append(iid)
    ctx.shared[NAME] = {"full": full, "suf": suf, "pre": pre, "dig": dig, "info": info}


def _tokens(txn: dict) -> list[str]:
    hay = refs.haystack(txn)
    ts = set(refs.tokens(hay))
    if " | " in hay:
        ts |= set(refs.tokens(hay.replace(" | ", "")))
    return sorted(t for t in ts if len(t) >= MIN_DIG)


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cash = txn["amount_cents"]
    if cash <= 0:
        return None
    S = ctx.shared[NAME]
    info, dig = S["info"], S["dig"]
    ent, day = txn["entity"], txn["booking_date"]
    curs = {c for c in (txn.get("currency"), txn.get("orig_currency")) if c}

    def live(i: str) -> bool:
        typ, _, cur, iss = info[i]
        return cur in curs and (iss is None or iss <= day) and ledger.is_open(i)

    cites = []
    for t in _tokens(txn):
        c = [i for i in S["full"].get((ent, t), ()) if live(i)]
        if not c and len(t) >= 6:
            c = [i for i in S["suf"].get((ent, t[-6:]), ()) if dig[i].endswith(t) and live(i)]
        if not c and len(t) >= 7:
            c = [i for i in S["pre"].get((ent, t[:6]), ()) if dig[i].startswith(t) and live(i)]
        if c:
            cites.append(c)
    by_party = defaultdict(set)
    for c in cites:
        if len(c) == 1:
            by_party[info[c[0]][1]].add(c[0])
    if not by_party:
        return None
    tctx = parties.context(txn)
    sc = {p: parties.score(tctx, p, ctx) for p in by_party}
    maj = min(by_party, key=lambda p: (-len(by_party[p]), -sc[p], p))
    if sc[maj] < PARTY_MIN:
        return None
    got: list[str] = []
    for c in cites:
        mine = [i for i in c if info[i][1] == maj]
        if len(mine) == 1 and mine[0] not in got:
            got.append(mine[0])
    icur = {info[i][2] for i in got}
    if len(icur) != 1:
        return None
    icur = icur.pop()
    if icur == txn.get("currency"):
        cash_i = cash
    elif icur == txn.get("orig_currency") and txn.get("orig_amount_cents"):
        cash_i = abs(txn["orig_amount_cents"])
    else:
        return None
    tot = sum(ledger.open[i] * (-1 if info[i][0] == "ARC" else 1) for i in got)
    if tot <= 0 or tot > cash_i:
        return None
    if len(got) < 2 and (cash_i <= OVER * tot or overpay_shape(cash_i, tot)):
        return None   # una sola citada: 1:1 con diferencia o sobrepago reconocible (exceptions lo decide)
    got.sort(key=lambda i: (info[i][3] or day, i))
    return Proposal(txn["bank_txn_id"], [Alloc(i, ledger.open[i]) for i in got], confidence=0.6, resolver=NAME,
                    reason=f"{len(got)} citadas de {maj} (tercero {sc[maj]:.0f}); suman {tot} de {cash_i}")
