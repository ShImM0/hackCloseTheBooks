"""Avisos de remesa (remittance_advices/). Dueño: P2.

Qué hace (POLICIES §5.4, §2, §3): un cliente grande envía un aviso (csv, xlsx, txt o pdf) que lista los documentos que
paga, las deducciones y sus motivos. Puede llegar días después del cobro y usa el número de factura sin prefijo
(cliente que cambia de layout en sep-2025: Voucher/Invoice/PO). Cómo:
1. Los avisos de la ventana de la fase se leen con docs_remit_parse (formato por contenido) y se cachean.
2. Aviso -> movimiento: misma entidad, abono == efectivo del aviso, fecha de llegada del aviso entre el cobro y unos
   días después; con varios candidatos, el tercero (nombre del pagador) desempata; si sigue ambiguo no se asigna.
3. Fila del aviso -> partida: nº de documento normalizado o por sufijo de dígitos (sin prefijo), desempatando por
   importe bruto, fecha y PO del cliente; nota de crédito (bruto < 0) -> ARC.
4. Importe y excepción por fila (§2-§3): sin deducción -> importe bruto; DISCOUNT -> SHORT_DISCOUNT al bruto (saldo
   completo) si el cobro llega dentro del plazo de pronto pago + 3 días de gracia, y PARTIAL por lo pagado si llega
   tarde; motivos de disputa (precio, daños, faltante, penalización, logística, marketing, RET) -> SHORT_DISPUTE por
   lo pagado; abonos compensados -> sin excepción. Si ninguna fila lleva motivo se deja la excepción vacía y
   exceptions.finalize la calcula.
Entradas: ctx (bankrec.phase.Phase). Salida: dict bank_txn_id -> Proposal (resolve_batch).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import pickle
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl
from rapidfuzz import fuzz

from bankrec import config, load, parties, refs
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers import docs_remit_parse as P

NAME = "docs_remit"
log = logging.getLogger("bankrec")

_BEFORE = dt.timedelta(days=3)      # el aviso puede fecharse justo antes del abono en banco
_AFTER = 25                         # días máximos entre el abono y la llegada del aviso
_NAME_MIN = 60                      # parecido mínimo del pagador del aviso con el texto del movimiento (0-100)
# §3: retención en origen ("RET": en TRAIN 2025-H1 el 100 % de sus filas son SHORT_WITHHOLDING al bruto) y pronto
# pago; cualquier otro motivo declarado (precio, daños, faltante, penalización, logística, co-op...) es SHORT_DISPUTE.
_WITHHOLD = re.compile(r"\bRET\b|RETENC|WITHHOLD|\bWHT\b")
_DISCOUNT = re.compile(r"DISCOUNT|DESCUENTO|\bDTO\b|ESCOMPTE|SKONTO|SCONTO|DESCONTO")
_PARSE_VERSION = 2                  # súbelo al cambiar docs_remit_parse: invalida la caché de avisos
_GRACE = 3                          # POLICIES §3 SHORT_DISCOUNT: plazo de pronto pago + 3 días (TRAIN: corte exacto)


def _norm(col: str) -> pl.Expr:
    return pl.col(col).str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


def _parse_one(path: str):
    return P.parse_file(Path(path))


def _advices(ctx) -> list[dict]:
    """Avisos de la ventana de la fase (por fecha de llegada), parseados y cacheados en parquet-pickle."""
    root = config.folder_dir(ctx.folder) / "remittance_advices"
    idx = root / "index.csv"
    if not idx.exists():
        return []
    ix = load.read_csv(idx) if hasattr(load, "read_csv") else pl.read_csv(idx)
    ix = ix.filter((pl.col("received_date") >= ctx.start - _BEFORE) &
                   (pl.col("received_date") < ctx.end + dt.timedelta(days=_AFTER)))
    files = [(r[0], r[1], r[2], r[3]) for r in ix.select("file", "entity", "payer_name", "received_date").iter_rows()]
    cache = config.cache_dir() / f"remit_v{_PARSE_VERSION}_{ctx.folder}_{ctx.start}_{ctx.end}_{len(files)}.pkl"
    if cache.exists():
        with open(cache, "rb") as fh:
            return pickle.load(fh)
    paths = [str(root / f[0]) for f in files]
    try:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
            parsed = list(ex.map(_parse_one, paths, chunksize=64))
    except Exception:
        log.exception("docs_remit: lectura en paralelo falló; se lee en serie")
        parsed = [_parse_one(p) for p in paths]
    out = []
    for (f, ent, payer, rec), a in zip(files, parsed):
        if a and a["rows"]:
            a["entity"], a["payer_idx"], a["received"] = ent, payer or "", rec
            out.append(a)
    with open(cache, "wb") as fh:
        pickle.dump(out, fh)
    return out


def _map_rows(advs: list[dict], ctx) -> pl.DataFrame:
    """item_id de cada fila de cada aviso (nulo si no hay una única candidata)."""
    rows = []
    for ai, a in enumerate(advs):
        for ri, r in enumerate(a["rows"]):
            rows.append((ai * 10000 + ri, ai, ri, a["entity"], r[0], r[1], r[2], r[3], r[4], r[5], r[6] if len(r) > 6 else ""))
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows, schema=["key", "ai", "ri", "entity", "doc", "ddate", "gross", "ded", "reason", "paid", "po"],
                      orient="row")
    df = df.with_columns(_norm("doc").alias("dn"), pl.col("doc").str.replace_all(r"\D", "").alias("dd"),
                         pl.when(pl.col("gross") < 0).then(pl.lit("ARC")).otherwise(pl.lit("ARI")).alias("itype"),
                         pl.col("gross").abs().alias("g"))
    it = (ctx.items.filter(pl.col("item_type").is_in(["ARI", "ARC"]))
          .select("item_id", pl.col("item_type").alias("itype"), "entity", "total_cents", "open_cents", "issue_date",
                  pl.col("po").alias("ipo"), _norm("document_number").alias("dn"),
                  pl.col("document_number").str.replace_all(r"\D", "").alias("dd")))
    # 1) número normalizado igual
    need = ["key", "item_id", "total_cents", "open_cents", "issue_date", "ipo", "g", "ddate", "po"]
    c1 = df.select("key", "entity", "itype", "dn", "g", "ddate", "po").join(it, on=["entity", "itype", "dn"]).select(need)
    parts = [c1]
    # 2) sin prefijo: los dígitos del aviso son sufijo de los del documento (nº distinto de longitud L)
    left = df.filter(~pl.col("key").is_in(c1["key"].implode()) & (pl.col("dd").str.len_chars() >= 5))
    for L in sorted(left["dd"].str.len_chars().unique().to_list()):
        sub = left.filter(pl.col("dd").str.len_chars() == L).select("key", "entity", "itype", "dd", "g", "ddate", "po")
        itl = it.with_columns(pl.col("dd").str.slice(-L).alias("suf"), pl.col("dd").str.len_chars().alias("ln")) \
                .filter(pl.col("ln") >= L).drop("dd", "dn")
        parts.append(sub.join(itl, left_on=["entity", "itype", "dd"], right_on=["entity", "itype", "suf"])
                     .select(need))
    c = pl.concat(parts, how="vertical_relaxed")
    c = c.with_columns(
        ((pl.col("total_cents") == pl.col("g")) | (pl.col("open_cents") == pl.col("g"))).cast(pl.Int32).alias("s_amt"),
        (pl.col("issue_date") == pl.col("ddate")).fill_null(False).cast(pl.Int32).alias("s_date"),
        ((pl.col("ipo") == pl.col("po")) & (pl.col("po") != "")).fill_null(False).cast(pl.Int32).alias("s_po"))
    c = c.with_columns((pl.col("s_amt") * 4 + pl.col("s_date") * 2 + pl.col("s_po")).alias("score"))
    best = c.group_by("key").agg(pl.col("score").max().alias("best"))
    c = c.join(best, on="key").filter(pl.col("score") == pl.col("best"))
    uniq = c.group_by("key").agg(pl.len().alias("k"), pl.col("item_id").first(), pl.col("score").first())
    uniq = uniq.filter((pl.col("k") == 1) & (pl.col("score") >= 4)).select("key", "item_id")   # al menos el importe cuadra
    return df.join(uniq, on="key", how="left")


def _name_score(a: dict, tctx: dict) -> float:
    """Parecido del pagador del aviso con el texto del movimiento (el índice trae el nombre; pdf/xlsx también)."""
    hay = tctx["hay"]
    best = 0.0
    for nm in {a.get("payer_idx") or "", a.get("payer") or ""}:
        n = parties.norm_name(nm)
        if len(n) >= 3:
            best = max(best, fuzz.partial_ratio(n, hay))
            if len(n) > 16:
                best = max(best, 0.95 * fuzz.partial_ratio(n[:16], hay))
    return best


def prepare(ctx) -> None:
    sh: dict = {"by_txn": {}}
    ctx.shared[NAME] = sh
    advs = _advices(ctx)
    if not advs:
        return
    rows = _map_rows(advs, ctx)
    if rows.is_empty():
        return
    by_adv: dict[int, list] = {}
    for r in rows.iter_rows(named=True):
        by_adv.setdefault(r["ai"], []).append(r)
    # aviso -> movimiento
    credits = ctx.txns.filter(pl.col("amount_cents") > 0)
    idx: dict[tuple, list[dict]] = {}
    for t in credits.iter_rows(named=True):
        idx.setdefault((t["entity"], t["amount_cents"]), []).append(t)
    cand: dict[str, list[tuple]] = {}
    for ai, a in enumerate(advs):
        cash = a["total_cents"]
        for t in idx.get((a["entity"], cash), ()):
            lag = (a["received"] - t["booking_date"]).days
            if -_BEFORE.days <= lag <= _AFTER and (not a.get("currency") or a["currency"] == t["currency"]):
                cand.setdefault(t["bank_txn_id"], []).append((ai, lag, t))
    out, used = {}, set()
    for tid, lst in sorted(cand.items()):
        if len(lst) > 1:
            tctx = parties.context(lst[0][2])
            sc = sorted(((_name_score(advs[ai], tctx), -abs(lag), ai) for ai, lag, _ in lst), reverse=True)
            if sc[0][0] < _NAME_MIN or (len(sc) > 1 and sc[1][0] >= sc[0][0] - 5):
                continue
            ai = sc[0][2]
        else:
            ai = lst[0][0]
            tctx = parties.context(lst[0][2])
            if _name_score(advs[ai], tctx) < _NAME_MIN and advs[ai].get("payer_idx"):
                continue
        if ai in used:
            continue
        used.add(ai)
        out[tid] = by_adv.get(ai, [])
    ids = {r["item_id"] for rs in out.values() for r in rs if r["item_id"]}
    info = ctx.items.filter(pl.col("item_id").is_in(list(ids))).select("item_id", "issue_date", "payment_terms")
    terms = ctx.master("payment_terms").filter(pl.col("discount_days").is_not_null())
    dd = {c: int(float(d)) for c, d in zip(terms["code"].to_list(), terms["discount_days"].to_list())}
    sh["info"] = {i: (d, dd.get(c)) for i, d, c in info.iter_rows()}
    sh["booking"] = {t: lst[0][2]["booking_date"] for t, lst in cand.items() if t in out}
    sh["by_txn"] = out
    log.info("docs_remit: %d avisos, %d abonos casados", len(advs), len(out))


def _allocs(rows: list[dict], booking: dt.date, info: dict) -> list[Alloc]:
    out, seen = [], set()
    for r in rows:
        iid = r["item_id"]
        if iid is None or iid in seen:
            continue
        seen.add(iid)
        reason = r["reason"] or ""
        if r["gross"] < 0:
            out.append(Alloc(iid, -r["gross"]))                   # nota de crédito compensada, sin excepción (§2)
        elif r["paid"] >= r["gross"]:
            out.append(Alloc(iid, r["gross"]))                    # pagada entera; finalize decide (FX, etc.)
        elif _WITHHOLD.search(reason):
            out.append(Alloc(iid, r["gross"], "SHORT_WITHHOLDING"))   # §3: liquidada entera, el certificado cubre
        elif _DISCOUNT.search(reason):
            issue, days = info.get(iid, (None, None))
            late = issue is not None and days is not None and booking > issue + dt.timedelta(days=days + _GRACE)
            # §3: dentro de plazo -> SHORT_DISCOUNT al saldo completo; tarde -> PARTIAL por lo pagado
            out.append(Alloc(iid, r["paid"], "PARTIAL") if late else Alloc(iid, r["gross"], "SHORT_DISCOUNT"))
        elif reason:
            out.append(Alloc(iid, r["paid"], "SHORT_DISPUTE"))    # §3: importe = lo pagado, el resto sigue abierto
        else:
            out.append(Alloc(iid, r["paid"]))                     # deducción sin motivo: finalize la explica
    return out


def resolve_batch(ctx) -> dict[str, Proposal]:
    out = {}
    sh = ctx.shared.get(NAME, {})
    for t, rows in sh.get("by_txn", {}).items():
        al = _allocs(rows, sh["booking"][t], sh["info"])
        if al:
            miss = sum(1 for r in rows if r["item_id"] is None)
            out[t] = Proposal(t, al, confidence=0.97 if not miss else 0.75, resolver=NAME,
                              reason=f"aviso de remesa: {len(rows)} líneas, {miss} sin partida")
    return out
