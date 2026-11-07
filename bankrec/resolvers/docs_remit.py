"""Avisos de remesa (remittance_advices/). Dueño: P2.

Qué hace (POLICIES §5.4, §5.5, §2, §3): un cliente grande envía un aviso (csv, xlsx, txt o pdf) que lista los documentos
que paga, las deducciones y sus motivos. Puede llegar días después del cobro, usa el número de factura sin prefijo
(cliente que cambia de layout en sep-2025: Voucher/Invoice/PO, sin columna de motivo) y el banco solo trae la referencia
de pago del cliente (payment_ref), a menudo cortada. Cómo:
1. Los avisos de la ventana de la fase se leen con docs_remit_parse (formato por contenido) y se cachean.
2. Fila del aviso -> partida: nº de documento normalizado o por sufijo de dígitos (sin prefijo), desempatando por
   importe bruto, fecha y PO del cliente; nota de crédito (bruto < 0) -> ARC.
3. Aviso -> movimiento: misma entidad, abono == efectivo del aviso en la divisa de la cuenta, o, si el aviso va en
   otra divisa (la de sus partidas), importe original del banco (orig_amount/orig_currency) == efectivo del aviso
   (§3 FX_DIFFERENCE); llegada del aviso entre 3 días antes y 25 después del abono. Evidencia: el payment_ref
   normalizado aparece en el texto del banco (entero o cortado, §5.5) o el nombre del pagador se parece (el banco a
   veces trae otro nombre: matriz, nombre comercial). Empates sin evidencia que los separe -> no se asigna.
4. Importe y excepción por fila (§2-§3): sin deducción -> importe bruto; DISCOUNT -> SHORT_DISCOUNT al bruto si el
   cobro llega dentro del plazo de pronto pago + 3 días de gracia, y PARTIAL por lo pagado si llega tarde; RET ->
   SHORT_WITHHOLDING al bruto; cualquier otro motivo (precio, daños, faltante, penalización, logística, marketing)
   -> SHORT_DISPUTE por lo pagado; abonos compensados -> sin excepción. Deducción SIN motivo impreso (layout nuevo):
   se deduce del importe (% de pronto pago de sus condiciones, % de retención en origen del país, o disputa).
5. resolve() propone todas las filas con partida; si otro movimiento anterior ya cerró alguna (otro resolver la tomó
   por error) se anota en la razón: el par es correcto y la excepción la decide exceptions.finalize.
Publica ctx.shared[NAME]["claimed"] = {item_id: bank_txn_id} (partidas de avisos casados) para otros resolvers.
Entradas: ctx (bankrec.phase.Phase). Salida: Proposal por movimiento casado (resolve).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import pickle
import re
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

import polars as pl
from rapidfuzz import fuzz

from bankrec import config, load, parties
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers import docs_remit_parse as P

NAME = "docs_remit"
log = logging.getLogger("bankrec")

_BEFORE = dt.timedelta(days=3)      # el aviso puede fecharse justo antes del abono en banco
_AFTER = 25                         # días máximos entre el abono y la llegada del aviso
_NAME_MIN = 60                      # parecido mínimo del pagador del aviso con el texto del movimiento (0-100)
_REF_CUT = 6                        # §5.5: prefijo mínimo (alfanumérico) de un payment_ref cortado por el banco
# §3: retención en origen ("RET": en TRAIN 2025-H1 el 100 % de sus filas son SHORT_WITHHOLDING al bruto) y pronto
# pago; cualquier otro motivo declarado (precio, daños, faltante, penalización, logística, co-op...) es SHORT_DISPUTE.
_WITHHOLD = re.compile(r"\bRET\b|RETENC|WITHHOLD|\bWHT\b")
_DISCOUNT = re.compile(r"DISCOUNT|DESCUENTO|\bDTO\b|ESCOMPTE|SKONTO|SCONTO|DESCONTO")
# §3 SHORT_WITHHOLDING: "some customers in Mexico, Brazil and Morocco; rates 1.25 %, 1.5 % or 10 %" (en 1/10000)
_WH_BPS = {"MX": (125, 1000), "BR": (150, 1000), "MA": (150, 1000)}
_TOL = 2                            # céntimos de redondeo al comparar una deducción con un % del total
_PARSE_VERSION = 3                  # súbelo al cambiar docs_remit_parse: invalida la caché de avisos
_GRACE = 3                          # POLICIES §3 SHORT_DISCOUNT: plazo de pronto pago + 3 días (TRAIN: corte exacto)
_ALNUM = re.compile(r"[^A-Z0-9]")


def _norm(col: str) -> pl.Expr:
    return pl.col(col).str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


def _nref(s) -> str:
    return _ALNUM.sub("", str(s or "").upper())


def _parse_all(paths: list[str]) -> list:
    """Parseo en paralelo por lotes de 64 ficheros. Si un worker muere (p. ej. sin memoria con otros procesos
    pesados: BrokenProcessPool), los lotes que faltan se leen en serie, sin repetir los que ya se leyeron."""
    chunks = [paths[i:i + 64] for i in range(0, len(paths), 64)]
    done: list = [None] * len(chunks)
    try:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
            futs = {ex.submit(P.parse_many, ch): k for k, ch in enumerate(chunks)}
            for f in as_completed(futs):
                try:
                    done[futs[f]] = f.result()
                except Exception:
                    pass
    except Exception:
        log.exception("docs_remit: lectura en paralelo falló")
    redo = [k for k, d in enumerate(done) if d is None]
    if redo:
        log.warning("docs_remit: %d de %d lotes de avisos se leen en serie", len(redo), len(chunks))
        for k in redo:
            done[k] = P.parse_many(chunks[k])
    return [a for d in done for a in d]


def _advices(ctx) -> list[dict]:
    """Avisos de la ventana de la fase (por fecha de llegada), parseados y cacheados en pickle."""
    root = config.folder_dir(ctx.folder) / "remittance_advices"
    idx = root / "index.csv"
    if not idx.exists():
        return []
    ix = load.read_csv(idx)
    ix = ix.filter((pl.col("received_date") >= ctx.start - _BEFORE) &
                   (pl.col("received_date") < ctx.end + dt.timedelta(days=_AFTER)))
    files = [(r[0], r[1], r[2], r[3]) for r in ix.select("file", "entity", "payer_name", "received_date").iter_rows()]
    cache = config.cache_dir() / f"remit_v{_PARSE_VERSION}_{ctx.folder}_{ctx.start}_{ctx.end}_{len(files)}.pkl"
    if cache.exists():
        with open(cache, "rb") as fh:
            return pickle.load(fh)
    parsed = _parse_all([str(root / f[0]) for f in files])
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


def _ref_level(ref: str, txn: dict) -> int:
    """§5.4-5.5: 2 = el payment_ref del aviso (normalizado) aparece entero en el texto del banco (N43 parte el concepto
    en trozos de 38 unidos por ' | ': se comparan sin separadores); 1 = aparece cortado: un campo o trozo termina en
    un prefijo de >= 6 caracteres del ref (el banco trunca a 16/35/38... caracteres); 0 = no aparece."""
    if len(ref) < _REF_CUT:
        return 0
    parts = [txn.get(k) or "" for k in ("text", "ref2", "remittance_info")]
    if ref in "".join(_nref(p) for p in parts):
        return 2
    for p in parts:
        for chunk in p.split("|"):
            c = _nref(chunk)
            for k in range(len(ref) - 1, _REF_CUT - 1, -1):
                if c.endswith(ref[:k]):
                    return 1
    return 0


def _assign(pairs: list[tuple]) -> dict[str, int]:
    """pairs = (evidencia, nombre, |días|, ai, txn_id). Casa cada movimiento con un aviso y cada aviso con un
    movimiento, primero los de más evidencia. Si el candidato empata con otro aún libre (misma evidencia y nombre a
    <= 5 puntos), ese movimiento o aviso queda bloqueado sin asignar (precisión antes que recall)."""
    pairs = sorted(pairs, key=lambda p: (-p[0], -p[1], p[2]))
    by_t: dict[str, list] = {}
    by_a: dict[int, list] = {}
    for p in pairs:
        by_t.setdefault(p[4], []).append(p)
        by_a.setdefault(p[3], []).append(p)
    out, used, blocked_t, blocked_a = {}, set(), set(), set()
    for ev, name, _, ai, tid in pairs:
        if tid in out or ai in used or tid in blocked_t or ai in blocked_a:
            continue
        if any(q[3] != ai and q[3] not in used and q[3] not in blocked_a and q[0] == ev and q[1] >= name - 5
               for q in by_t[tid]):
            blocked_t.add(tid)
            continue
        if any(q[4] != tid and q[4] not in out and q[4] not in blocked_t and q[0] == ev and q[1] >= name - 5
               for q in by_a[ai]):
            blocked_a.add(ai)
            continue
        out[tid] = ai
        used.add(ai)
    return out


def prepare(ctx) -> None:
    sh: dict = {"by_txn": {}, "claimed": {}}
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
    # divisa del aviso: la que trae, o la de sus partidas (xlsx no la indica)
    icur = dict(ctx.items.filter(pl.col("item_id").is_in(rows["item_id"].drop_nulls().implode()))
                .select("item_id", "currency").iter_rows())
    adv_cur = {}
    for ai, a in enumerate(advs):
        cs = Counter(icur.get(r["item_id"]) for r in by_adv.get(ai, ()) if r["item_id"])
        adv_cur[ai] = a.get("currency") or (cs.most_common(1)[0][0] if cs else "")
    # aviso -> movimiento: mismo importe en la divisa de la cuenta, o importe original en la divisa del aviso (FX)
    credits = ctx.txns.filter(pl.col("amount_cents") > 0)
    same: dict[tuple, list[dict]] = {}
    fx: dict[tuple, list[dict]] = {}
    for t in credits.iter_rows(named=True):
        same.setdefault((t["entity"], t["amount_cents"]), []).append(t)
        if t.get("orig_amount_cents") and t.get("orig_currency") and t["orig_currency"] != t["currency"]:
            fx.setdefault((t["entity"], t["orig_currency"], abs(t["orig_amount_cents"])), []).append(t)
    pairs, tctxs, txn_of = [], {}, {}
    for ai, a in enumerate(advs):
        cash, cur = a["total_cents"], adv_cur[ai]
        cands = [t for t in same.get((a["entity"], cash), ()) if not cur or t["currency"] == cur]
        if cur:
            cands += fx.get((a["entity"], cur, cash), [])
        ref = _nref(a.get("payment_ref"))
        for t in cands:
            lag = (a["received"] - t["booking_date"]).days
            if not (-_BEFORE.days <= lag <= _AFTER):
                continue
            tid = t["bank_txn_id"]
            if tid not in tctxs:
                tctxs[tid] = parties.context(t)
                txn_of[tid] = t
            lv = _ref_level(ref, t)
            name = _name_score(a, tctxs[tid])
            has_payer = bool(a.get("payer_idx") or a.get("payer"))
            ev = 2 if lv == 2 else (1 if lv == 1 or name >= _NAME_MIN or not has_payer else 0)
            if ev:
                pairs.append((ev, name, abs(lag), ai, tid))
    match = _assign(pairs)
    out = {tid: by_adv.get(ai, []) for tid, ai in match.items()}
    ids = {r["item_id"] for rs in out.values() for r in rs if r["item_id"]}
    info = ctx.items.filter(pl.col("item_id").is_in(list(ids))).select("item_id", "issue_date", "payment_terms",
                                                                         "total_cents", "party_id")
    terms = ctx.master("payment_terms").filter(pl.col("discount_days").is_not_null())
    dd = {c: (int(float(d)), round(float(p) * 100)) for c, d, p in
          terms.select("code", "discount_days", "early_payment_discount_pct").iter_rows() if p}
    cu = ctx.master("customers")
    country = dict(zip(cu["customer_id"].to_list(), cu["country"].to_list()))
    sh["info"] = {i: (d, *(dd.get(c) or (None, None)), tot, country.get(pid)) for i, d, c, tot, pid in info.iter_rows()}
    sh["booking"] = {tid: txn_of[tid]["booking_date"] for tid in out}
    sh["by_txn"] = out
    sh["claimed"] = {r["item_id"]: tid for tid, rs in out.items() for r in rs if r["item_id"]}
    n_fx = sum(1 for tid in out if txn_of[tid]["currency"] != adv_cur[match[tid]])
    log.info("docs_remit: %d avisos, %d abonos casados (%d en otra divisa, %d por referencia sin nombre)", len(advs),
             len(out), n_fx, sum(1 for p in pairs if match.get(p[4]) == p[3] and p[1] < _NAME_MIN))


def _deduction(r: dict, booking: dt.date, info: dict) -> Alloc:
    """Fila con deducción (pagado < bruto), §3: RET -> SHORT_WITHHOLDING al bruto; DISCOUNT -> SHORT_DISCOUNT al bruto
    dentro de plazo + 3 días, PARTIAL por lo pagado fuera; otro motivo -> SHORT_DISPUTE por lo pagado. Sin motivo
    impreso (layout nuevo, §5.4/§6) se deduce del importe con las mismas reglas: % de pronto pago de las condiciones de
    la factura -> descuento; % de retención en origen del país del cliente -> retención; si no -> disputa (en TRAIN
    toda deducción de un aviso que no es descuento ni retención es SHORT_DISPUTE)."""
    iid, gross, paid, reason = r["item_id"], r["gross"], r["paid"], r["reason"] or ""
    issue, days, bps, total, country = info.get(iid, (None, None, None, None, None))
    if not reason:
        base, ded = total or gross, gross - paid
        if bps and abs(ded - (base * bps + 5000) // 10000) <= _TOL:
            reason = "DISCOUNT"
        elif any(abs(ded - (base * w + 5000) // 10000) <= _TOL for w in _WH_BPS.get(country, ())):
            reason = "RET"
    if _WITHHOLD.search(reason):
        return Alloc(iid, gross, "SHORT_WITHHOLDING")              # §3: liquidada entera, el certificado cubre
    if _DISCOUNT.search(reason):
        late = issue is not None and days is not None and booking > issue + dt.timedelta(days=days + _GRACE)
        # §3: dentro de plazo -> SHORT_DISCOUNT al saldo completo; tarde -> PARTIAL por lo pagado
        return Alloc(iid, paid, "PARTIAL") if late else Alloc(iid, gross, "SHORT_DISCOUNT")
    return Alloc(iid, paid, "SHORT_DISPUTE")                       # §3: importe = lo pagado, el resto sigue abierto


def _allocs(rows: list[dict], booking: dt.date, info: dict) -> list[Alloc]:
    out, seen = [], set()
    for r in rows:
        iid = r["item_id"]
        if iid is None or iid in seen:
            continue
        seen.add(iid)
        if r["gross"] < 0:
            out.append(Alloc(iid, -r["gross"]))                   # nota de crédito compensada, sin excepción (§2)
        elif r["paid"] >= r["gross"]:
            out.append(Alloc(iid, r["gross"]))                    # pagada entera; finalize decide (FX, etc.)
        elif not r["reason"] and not r["ded"]:
            out.append(Alloc(iid, r["paid"]))     # pagado < bruto sin deducción impresa (a cuenta): finalize lo explica
        else:
            out.append(_deduction(r, booking, info))
    return out


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    sh = ctx.shared.get(NAME) or {}
    tid = txn["bank_txn_id"]
    rows = sh.get("by_txn", {}).get(tid)
    if not rows:
        return None
    al = _allocs(rows, sh["booking"][tid], sh["info"])
    if not al:
        return None
    miss = sum(1 for r in rows if r["item_id"] is None)
    # partidas que un movimiento anterior ya cerró (otro resolver se las llevó por error): el par es correcto y se
    # propone igual; la excepción la decide exceptions.finalize (en TRAIN ninguna fila de aviso es DUPLICATE_PAYMENT)
    gone = sum(1 for a in al if ledger.open.get(a.item_id, 0) <= 0)
    return Proposal(tid, al, confidence=0.97 if not (miss or gone) else 0.75, resolver=NAME,
                    reason=f"aviso de remesa: {len(rows)} líneas, {miss} sin partida, {gone} ya cerradas por otro")
