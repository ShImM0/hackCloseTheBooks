"""Avisos de remesa (remittance_advices/). Dueño: P2.

Qué hace: el aviso del cliente (PDF/CSV/XLSX/TXT) lista los documentos que paga y sus deducciones. Se casa con el
cobro por (entidad, suma de lo pagado == importe del movimiento) y fecha de recepción (el aviso puede llegar después
del cobro). Cada línea del aviso -> su partida (entidad + número impreso) por lo pagado; abonos en su fila, positivos.
Deducción con motivo (precio, daño, faltante, penalización, marketing, logística: todo salvo pronto pago y retención)
-> SHORT_DISPUTE por lo pagado (POLICIES §2/§3). Pronto pago y retención se dejan sin excepción: exceptions.finalize los
reconoce con el importe pagado (SHORT_DISCOUNT / PARTIAL por plazo del PDF).
Entradas: txn, ctx (bankrec.phase.Phase), ledger. Salida: Proposal o None (la cascada sigue). Segmentos
ar_short_dispute, ar_1toN_remittance, ar_dispute_settlement.
Solo se leen los avisos recibidos entre ctx.start - 14 días y ctx.end + 30 días.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

from bankrec.contract import Alloc, Proposal
from bankrec.resolvers import docs_remit_parse as P

NAME = "docs_remit"
log = logging.getLogger("bankrec")

BEFORE = dt.timedelta(days=14)    # el aviso se recibe como mucho este tiempo antes de la fecha de apunte
AFTER = dt.timedelta(days=45)     # y como mucho este tiempo después
MIN_LINES = 2                     # un aviso de una línea ya lo resuelven el texto y el importe


def _work(args):
    base, rel, docs = args
    p = P.find_file(Path(base), rel)
    if p is None:
        return rel, []
    try:
        return rel, P.parse(p, docs)
    except Exception:
        log.warning("aviso ilegible: %s", rel)
        return rel, []


def prepare(ctx) -> None:
    """Lee los avisos de la ventana de la fase; ctx.shared[NAME] = {(entidad, total céntimos): [aviso, ...]}."""
    ctx.shared[NAME] = {}
    base = ctx.dir / "remittance_advices"
    idx_path = base / "index.csv"
    if not idx_path.exists():
        return
    t0 = time.time()
    idx = pl.read_csv(idx_path, infer_schema_length=0, encoding="utf8")
    lo, hi = ctx.start - BEFORE, ctx.end + AFTER
    idx = idx.with_columns(pl.col("received_date").str.to_date("%Y-%m-%d", strict=False).alias("rd")) \
        .filter(pl.col("rd").is_between(lo, hi))
    it = ctx.items
    docs = set(it["document_number"].drop_nulls().to_list())
    by_doc: dict[tuple, list] = defaultdict(list)
    for iid, ent, dn in zip(it["item_id"].to_list(), it["entity"].to_list(), it["document_number"].to_list()):
        by_doc[(ent, dn)].append(iid)
    jobs = [(str(base), r["file"], docs) for r in idx.iter_rows(named=True)]
    meta = {r["file"]: (r["entity"], r["rd"], r["payer_name"]) for r in idx.iter_rows(named=True)}
    with ProcessPoolExecutor(max_workers=min(8, max(1, len(jobs) // 200 + 1))) as ex:
        res = list(ex.map(_work, jobs, chunksize=64))
    out: dict[tuple, list] = defaultdict(list)
    for rel, lines in res:
        if len(lines) < MIN_LINES:
            continue
        ent, rd, payer = meta[rel]
        total = sum(l[4] for l in lines if l[4] is not None)
        out[(ent, total)].append({"file": rel, "received": rd, "payer": payer, "entity": ent,
                                  "lines": lines, "items": [by_doc.get((ent, l[0]), []) for l in lines]})
    ctx.shared[NAME] = out
    ctx.shared[NAME + "_used"] = set()
    log.info("docs_remit: %d avisos leídos de %d en %.0fs", sum(len(v) for v in out.values()), len(jobs), time.time() - t0)


def _pick(ids: list[str], ledger) -> tuple[str | None, bool]:
    """(partida del documento, ¿abierta?): la que tiene saldo vivo (a igualdad, la primera) o, si ya está cobrada
    (otro movimiento la liquidó antes), la primera; resolve descarta el aviso entonces."""
    if not ids:
        return None, False
    live = [i for i in ids if ledger.open.get(i, 0) > 0]
    return (live[0], True) if live else (ids[0], False)


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cents = txn["amount_cents"]
    if cents <= 0:
        return None
    cand = ctx.shared.get(NAME, {}).get((txn["entity"], cents), ())
    used = ctx.shared.setdefault(NAME + "_used", set())
    date = txn["booking_date"]
    best = None
    for a in cand:
        if a["file"] in used or not (date - BEFORE <= a["received"] <= date + AFTER):
            continue
        picks = [_pick(x, ledger) for x in a["items"]]
        ids = [i for i, _ in picks]
        if any(i is None for i in ids) or len({*ids}) != len(ids):
            continue            # documento desconocido o repetido: no se fuerza
        if not all(o for _, o in picks):
            continue            # alguna ya cobrada: finalize la marcaría DUPLICATE_PAYMENT (en val casi siempre falso)
        key = abs((a["received"] - date).days)
        if best is None or key < best[0]:
            best = (key, a, ids)
    if best is None:
        return None
    _, a, ids = best
    used.add(a["file"])
    allocs = []
    for iid, (doc, gross, ded, reason, paid) in zip(ids, a["lines"]):
        amount = abs(paid)
        exc = ""
        if iid.startswith("ARI") and ded and ded < 0 and reason and not P.NOT_DISPUTE.search(reason):
            exc = "SHORT_DISPUTE"
        allocs.append(Alloc(iid, amount, exc))
    return Proposal(txn["bank_txn_id"], allocs, confidence=0.95, resolver=NAME,
                    reason=f"aviso {a['file']} ({len(allocs)} líneas)")
