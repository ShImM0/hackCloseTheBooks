"""Identificación del tercero (cliente/proveedor) de un movimiento. Dueño: P1.

Qué hace (POLICIES §5.1, §5.5): para un party_id dice cuánto encaja con el movimiento:
- IBAN del maestro (customers.iban / suppliers.iban) = 100;
- nombre (actual, comercial e impreso en sus facturas) con rapidfuzz + unidecode, tolerando cortes;
- la matriz (parent_customer_id) puede pagar por sus filiales: los nombres de la matriz cuentan para la filial.
Uso: prepare(ctx) una vez; luego score(txn_ctx, party_id) con txn_ctx = context(txn).

TODO(P1): avisos NOTICE_* de IBAN nuevo (pdf_facts de P3), confirming/factoring/agencias (§5.1).
"""
from __future__ import annotations

import re
from collections import defaultdict

import polars as pl
from rapidfuzz import fuzz
from unidecode import unidecode

from bankrec import refs

NAME = "parties"
_LEGAL = set("""SL SLU SA SAU SCOOP SC SCP SRL SRLS SNC SAS SARL EURL SASU GMBH CO KG EK EV AG UG MBH LTD LIMITED PLC LLP
PTY LLC INC CORP CORPORATION BV NV SPZOO SP Z OO LDA EIRELI ME DE CV RL SPA SPZ OHG THE AND Y E ET UND""".split())
_NONALPHA = re.compile(r"[^A-Z0-9 ]+")


def norm_name(s: str | None) -> str:
    """'Prieto y Llorente Suministros S.L.' -> 'PRIETO LLORENTE SUMINISTROS'."""
    s = _NONALPHA.sub(" ", unidecode(s or "").upper().replace(".", "").replace("&", " "))
    return " ".join(w for w in s.split() if w not in _LEGAL)


def prepare(ctx) -> None:
    """ctx.shared['parties'] = {'names': party -> set(nombres), 'iban': iban -> set(party)}."""
    if "parties" in ctx.shared:
        return
    names: dict[str, set[str]] = defaultdict(set)
    iban: dict[str, set[str]] = defaultdict(set)
    cust = ctx.master("customers")
    for r in cust.select("customer_id", "name", "trade_name", "iban").iter_rows():
        for n in r[1:3]:
            if n:
                names[r[0]].add(norm_name(n))
        if r[3]:
            iban[refs.norm(r[3])].add(r[0])
    sup = ctx.master("suppliers")
    for r in sup.select("supplier_id", "name", "iban").iter_rows():
        if r[1]:
            names[r[0]].add(norm_name(r[1]))
        if r[2]:
            iban[refs.norm(r[2])].add(r[0])
    # nombre impreso en las facturas (antes de renombrados)
    for pid, pname in ctx.items.select("party_id", "party_name").unique().iter_rows():
        if pid and pname:
            names[pid].add(norm_name(pname))
    # la matriz paga por la filial: la filial hereda nombres e IBAN de la matriz
    parents = cust.filter(pl.col("parent_customer_id").is_not_null()).select("customer_id", "parent_customer_id")
    ibans_of: dict[str, list[str]] = defaultdict(list)
    for ib, ps in iban.items():
        for p in ps:
            ibans_of[p].append(ib)
    for child, parent in parents.iter_rows():
        names[child] |= names.get(parent, set())
        for ib in ibans_of.get(parent, ()):
            iban[ib].add(child)
    # nombres de < 5 letras ("SOL") encajan con cualquier palabra del concepto: fuera
    names = {k: [n for n in v if len(n) >= 5] for k, v in names.items()}
    # índice inverso palabra -> terceros, para buscar el tercero sin conocer la partida (find)
    words: dict[str, set[str]] = defaultdict(set)
    for pid, ns in names.items():
        for n in ns:
            for w in n.split():
                if len(w) >= 4:
                    words[w].add(pid)
    ctx.shared["parties"] = {"names": names, "iban": iban, "words": words}


def context(txn: dict) -> dict:
    """Lo que se compara una vez por movimiento."""
    hay = refs.haystack(txn)
    return {"hay": norm_name(hay), "ibans": refs.ibans(txn)}


def score(tctx: dict, party_id: str, ctx) -> float:
    """0..100. 100 = IBAN conocido del tercero; si no, mejor parecido de nombre (partial_ratio)."""
    P = ctx.shared["parties"]
    for ib in tctx["ibans"]:
        if party_id in P["iban"].get(ib, ()):
            return 100.0
    hay = tctx["hay"]
    best = 0.0
    for n in P["names"].get(party_id, ()):
        best = max(best, fuzz.partial_ratio(n, hay))
        if len(n) > 16:  # el banco corta el nombre a 16-20 caracteres
            best = max(best, 0.95 * fuzz.partial_ratio(n[:16], hay))
    return best


WORD_DF_MAX = 60   # palabras compartidas por más terceros ("POOL", "SUPPLY") no sirven para buscar


def find(tctx: dict, ctx, prefix: str = "") -> list[tuple[str, float]]:
    """Terceros candidatos del movimiento (IBAN o palabras poco frecuentes del nombre), con su score, de mayor a
    menor. prefix filtra por id (p. ej. "CES01" = clientes de ES01)."""
    P = ctx.shared["parties"]
    pids = set()
    for ib in tctx["ibans"]:
        pids |= P["iban"].get(ib, set())
    for w in set(tctx["hay"].split()):
        ps = P["words"].get(w)
        if ps and len(ps) <= WORD_DF_MAX:
            pids |= ps
    out = [(p, score(tctx, p, ctx)) for p in pids if p.startswith(prefix)]
    return sorted(out, key=lambda x: -x[1])
