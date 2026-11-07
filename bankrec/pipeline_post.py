"""Post-pass tras la cascada: suma exacta ÚNICA para los cobros que quedaron sin partida. Dueño: P1.

Qué hace (POLICIES §5.1, §5.5, §5.6): `nonitem_unmatched` deja en CUSTOMER_ADVANCE (sin excepción) los cobros de un
cliente identificado (IBAN, nombre o referencia) sin partida aplicada; casi todos pagan varias facturas sin citar
ninguna. Al final de la fase el ledger ya no tiene lo que cobraron otros movimientos, así que el conjunto de facturas
vivas del cliente es mucho más pequeño que el que veía `subset_sum` en su momento. Para cada cobro, en orden:
1. terceros = los de su IBAN; si no hay, los de nombre ≥ PARTY_MIN (y contraparte ≥ CP_MIN) más el de la referencia;
2. facturas vivas del tercero, en la divisa del cobro, emitidas antes y con vencimiento ≤ fecha + DUE_SLACK días;
3. si son ≤ NCAP: subconjuntos con suma exacta igual al cobro (meet-in-the-middle). Solo si hay UNA solución y un
   solo tercero con solución se asigna (saldo vivo de cada factura; exceptions.finalize decide la excepción).
Medido offline (val 2025-H1, sobre el ledger final de la ejecución base): 1.676 cobros, 8.690 pares con 96,5 % de
precisión y 1.587 movimientos exactos. Umbrales vecinos (NCAP 20-36, DUE_SLACK 5-15) en docs/PLAN_MEJORAS.md.
No toca movimientos ya resueltos ni consume partidas antes que ellos: solo usa lo que queda vivo al final.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict

import numpy as np
import polars as pl

from bankrec import exceptions
from bankrec.contract import Alloc, Proposal

log = logging.getLogger("bankrec")

NAME = "postpass_subset"
NCAP = 30          # facturas candidatas como máximo (2^15 sumas por mitad)
DUE_SLACK = 10     # días de margen sobre la fecha del cobro para el vencimiento
PARTY_MIN = 95     # parecido de nombre mínimo si no hay IBAN
CP_MIN = 90        # parecido mínimo con la contraparte del banco, si la trae
PENDING = "CUSTOMER_ADVANCE"


def _sums(vals: list[int]) -> np.ndarray:
    s = np.zeros(1 << len(vals), dtype=np.int64)
    for k, v in enumerate(vals):
        s[1 << k:1 << (k + 1)] = s[:1 << k] + v
    return s


def unique_subset(vals: list[int], target: int) -> tuple[int, list[int] | None]:
    """(nº de subconjuntos con suma == target, índices del único si hay exactamente uno)."""
    h = len(vals) // 2
    a, b = _sums(vals[:h]), _sums(vals[h:])
    order = np.argsort(b, kind="stable")
    bs = b[order]
    lo = np.searchsorted(bs, target - a, "left")
    hi = np.searchsorted(bs, target - a, "right")
    cnt = hi - lo
    tot = int(cnt.sum())
    if tot != 1:
        return tot, None
    am = int(np.nonzero(cnt)[0][0])
    bm = int(order[lo[am]])
    idx = [k for k in range(h) if am >> k & 1] + [h + k for k in range(len(vals) - h) if bm >> k & 1]
    return 1, idx


def _features(reason: str) -> dict | None:
    k = reason.find(" | {")
    if k < 0:
        return None
    try:
        return json.loads(reason[k + 3:])
    except ValueError:
        return None


def _parties(f: dict, ledger) -> list[str]:
    ib = [x["pid"] for x in f.get("iban", ()) if x.get("aa")]
    if ib:
        return ib
    out = [c["pid"] for c in f.get("cli", ()) if c["sc"] >= PARTY_MIN and (c.get("cpr") is None or c["cpr"] >= CP_MIN)]
    ref = f.get("ref")
    if ref and ref.get("t") in ("ARI", "ARC") and ref.get("iid") in ledger.info:
        rp = ledger.info[ref["iid"]][3]
        if rp not in out:
            out.insert(0, rp)
    return list(dict.fromkeys(out))


def run(ctx, ledger, final: list) -> int:
    """final = [(txn, prop)] en orden de la cascada; sustituye en sitio las propuestas resueltas. Devuelve cuántas."""
    inv = ctx.items.filter(pl.col("item_type") == "ARI").select("item_id", "party_id", "issue_date", "due_date")
    by_party: dict[str, list[str]] = defaultdict(list)
    due: dict[str, dt.date] = {}
    for iid, pid, iss, d in inv.iter_rows():
        if iss is None:
            continue
        by_party[pid].append(iid)
        due[iid] = d or iss
    n = 0
    for k, (txn, prop) in enumerate(final):
        if prop.allocs or prop.resolver != "nonitem_unmatched" or prop.category != PENDING or txn["amount_cents"] <= 0:
            continue
        try:
            new = _solve(txn, prop, ctx, ledger, by_party, due)
        except Exception:
            log.exception("post-pass falló en %s: se deja como estaba", txn["bank_txn_id"])
            continue
        if new is not None:
            ledger.apply(new, txn["booking_date"])
            final[k] = (txn, new)
            n += 1
    return n


def _solve(txn: dict, prop, ctx, ledger, by_party: dict, due: dict) -> Proposal | None:
    f = _features(prop.reason or "")
    if not f:
        return None
    day, cur, cash = txn["booking_date"], txn["currency"], txn["amount_cents"]
    last_due = day + dt.timedelta(days=DUE_SLACK)
    sols = []
    for pid in _parties(f, ledger):
        pool = [i for i in by_party.get(pid, ()) if ledger.open.get(i, 0) > 0 and ledger.info[i][2] == cur
                and ledger.info[i][4] <= day and due[i] <= last_due]
        if len(pool) > NCAP:
            return None   # un tercero que no se puede revisar entero podría tener otra solución (matriz/filial)
        if not pool:
            continue
        pool.sort(key=lambda i: (due[i], ledger.info[i][4], i))
        tot, idx = unique_subset([ledger.open[i] for i in pool], cash)
        if tot:
            sols.append((pid, [pool[j] for j in idx] if idx is not None else None))
    if len(sols) != 1 or sols[0][1] is None:
        return None
    pid, got = sols[0]
    new = Proposal(txn["bank_txn_id"], [Alloc(i, ledger.open[i]) for i in got], confidence=0.5, resolver=NAME,
                   reason=f"suma exacta única: {len(got)} facturas vivas de {pid} (de ≤ {NCAP})")
    return exceptions.finalize(new, txn, ctx, ledger) or new


if __name__ == "__main__":
    assert unique_subset([500, 300, 200, 700], 1000) == (2, None)          # 500+300+200 y 300+700
    tot, idx = unique_subset([500, 300, 250, 700], 1250)                   # solo 500+250+... no: 300+250+700
    assert tot == 1 and sorted(idx) == [1, 2, 3], (tot, idx)
    assert unique_subset([100, 200], 50) == (0, None)
    print("pipeline_post OK")
