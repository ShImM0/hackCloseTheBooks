"""Pipeline: fase -> cascada de resolvers -> excepciones -> ledger -> submission + audit. Dueño: P1.

Ningún resolver puede tumbar la ejecución: cada llamada va en try/except y se registra.
Un movimiento sin propuesta sale como FALLBACK (abono -> UNIDENTIFIED, cargo -> BANK_FEES).
"""
from __future__ import annotations

import csv
import re
import gzip
import logging
import time
from collections import defaultdict

import polars as pl

from bankrec import config, exceptions, phase, registry
from bankrec.contract import Proposal, fmt_cents
from bankrec.ledger import Ledger

log = logging.getLogger("bankrec")


_BATCH = re.compile(r"LOCKBOX|REMISE|CHEQUE|CHQ|CARD SETTLEMENT|LIQUIDACION TPV|ABONO TPV|\bCB\b|DEPOSIT")
KNOWN_PARTY = 90      # fallback: cobro de un cliente reconocible = casi seguro tiene partidas (1:N no resuelto)
OVERPAY_MAX = 1.11    # efectivo/saldo: en TRAIN los OVERPAYMENT reales son +1 % o +10 % (p90 1,10); más = 1:N incompleto


def fallback(txn: dict, ctx=None) -> Proposal:
    # shortcut: cargo sin resolver -> BANK_FEES (la categoría de cargo más frecuente); P4 lo afina en nonitem.py
    if txn["amount_cents"] > 0:
        # UNIDENTIFIED es una clase del macro-F1: solo con exception si el pagador no es un cliente reconocible
        # (§3: "no open item exists, unknown payer"). En val, 85 % de los UNIDENTIFIED del fallback eran falsos.
        if _BATCH.search(txn.get("text") or ""):   # remesa de cheques/tarjeta: tiene partidas aunque no las hallemos
            return Proposal(txn["bank_txn_id"], category="UNIDENTIFIED", resolver="fallback_tercero", reason="lote")
        if ctx is not None and "parties" in ctx.shared:
            from bankrec import parties
            found = parties.find(parties.context(txn), ctx, "C" + txn["entity"])
            if found and found[0][1] >= KNOWN_PARTY:
                return Proposal(txn["bank_txn_id"], category="UNIDENTIFIED", resolver="fallback_tercero",
                                reason=f"cliente {found[0][0]} reconocido sin partida")
        return Proposal(txn["bank_txn_id"], category="UNIDENTIFIED", exception="UNIDENTIFIED", resolver="fallback")
    return Proposal(txn["bank_txn_id"], category="BANK_FEES", resolver="fallback")


def _overpay_guard(prop: Proposal, txn: dict, ledger) -> None:
    """Una sola partida con OVERPAYMENT y efectivo muy superior al saldo = pago de varias facturas del que solo se
    ha encontrado una: el par suele ser correcto, la excepción no (en val, 97 % de OVERPAYMENT eran falsos)."""
    if len(prop.allocs) != 1 or prop.allocs[0].exception != "OVERPAYMENT" or not prop.allocs[0].amount_cents:
        return
    a = prop.allocs[0]
    info = ledger.info.get(a.item_id)
    if not info:
        return
    cash = abs(txn["amount_cents"])
    if info[2] != txn["currency"]:
        if txn.get("orig_currency") != info[2] or not txn.get("orig_amount_cents"):
            return
        cash = abs(txn["orig_amount_cents"])
    if cash > OVERPAY_MAX * a.amount_cents:
        a.exception = ""


def run(phase_name: str, disabled: set[str] = frozenset(), months: int | None = None) -> dict:
    t0 = time.perf_counter()
    ctx = phase.build(phase_name, months)
    timings: dict[str, float] = defaultdict(float)
    timings["build"] = time.perf_counter() - t0
    mods = registry.load(disabled)

    active, batch = [], {}
    for m in mods:
        t = time.perf_counter()
        try:
            if hasattr(m, "prepare"):
                m.prepare(ctx)
            if hasattr(m, "resolve_batch"):
                batch[m.NAME] = m.resolve_batch(ctx) or {}
            if hasattr(m, "resolve") or m.NAME in batch:
                active.append(m)
        except Exception:
            log.exception("resolver %s falló en prepare/resolve_batch: se desactiva", m.NAME)
        timings[m.NAME] += time.perf_counter() - t

    ledger = Ledger(ctx.items)
    known = set(ctx.items["item_id"].to_list())
    rows, audit = [], []
    errors: dict[str, int] = defaultdict(int)
    for txn in ctx.txns.iter_rows(named=True):
        tid = txn["bank_txn_id"]
        prop = None
        for m in active:
            t = time.perf_counter()
            try:
                prop = batch.get(m.NAME, {}).get(tid)
                if prop is None and hasattr(m, "resolve"):
                    prop = m.resolve(txn, ctx, ledger)
            except Exception:
                if errors[m.NAME] == 0:
                    log.exception("resolver %s falló en %s (se sigue; solo se muestra el primero)", m.NAME, tid)
                errors[m.NAME] += 1
                prop = None
            timings[m.NAME] += time.perf_counter() - t
            if prop is not None:
                prop.resolver = prop.resolver or m.NAME
                break
        if prop is None:
            prop = fallback(txn, ctx)
        try:
            prop = exceptions.finalize(prop, txn, ctx, ledger) or prop
            _overpay_guard(prop, txn, ledger)
        except Exception:
            if errors["exceptions"] == 0:
                log.exception("exceptions.finalize falló en %s", tid)
            errors["exceptions"] += 1
        prop.bank_txn_id = tid
        # higiene: partidas desconocidas o repetidas no pueden salir
        seen, allocs = set(), []
        for a in prop.allocs:
            if a.item_id in known and a.item_id not in seen and a.amount_cents >= 0:
                seen.add(a.item_id)
                allocs.append(a)
            else:
                errors[f"alloc_descartada:{prop.resolver}"] += 1
        if prop.allocs and not allocs:
            prop = fallback(txn, ctx)
        prop.allocs = allocs
        ledger.apply(prop, txn["booking_date"])
        if allocs:
            rows.extend([tid, a.item_id, fmt_cents(a.amount_cents), "", a.exception] for a in allocs)
        else:
            cat = prop.category or "UNIDENTIFIED"
            # en el golden UNIDENTIFIED siempre va con category y exception UNIDENTIFIED
            exc = "UNIDENTIFIED" if cat == "UNIDENTIFIED" and prop.resolver != "fallback_tercero" else prop.exception
            rows.append([tid, "", "", cat, exc])
        audit.append((tid, prop.resolver, float(prop.confidence), prop.reason))

    out = config.out_dir(phase_name)
    sub = out / "submission.csv.gz"
    with gzip.open(sub, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(config.HEADER)
        w.writerows(rows)
    audit_df = pl.DataFrame(audit, schema=["bank_txn_id", "resolver", "confidence", "reason"], orient="row")
    audit_df.write_parquet(out / "audit.parquet")
    timings["total"] = time.perf_counter() - t0
    by_res = audit_df["resolver"].value_counts(sort=True)
    log.info("movimientos por resolver:\n%s", "\n".join(f"  {r:16s} {n:8d}" for r, n in by_res.iter_rows()))
    log.info("tiempos (s):\n%s", "\n".join(f"  {k:16s} {v:8.1f}" for k, v in timings.items()))
    if errors:
        log.warning("errores por resolver: %s", dict(errors))
    log.info("%d filas -> %s", len(rows), sub)
    return {"submission": sub, "rows": len(rows), "txns": ctx.txns.height, "timings": dict(timings), "errors": dict(errors)}
