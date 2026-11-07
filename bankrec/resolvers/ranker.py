"""Ranker LightGBM para cobros/pagos 1:1 que la cascada no ha resuelto. Dueño: P1.

Qué hace: para un movimiento sin propuesta genera partidas candidatas abiertas (misma entidad y tipo; abono -> ARI,
cargo -> API; ES00 paga API de ES01/ES02/PT01, POLICIES §5.2) por importe cercano (±BAND), por referencia impresa
(§5.5) y por IBAN del tercero (§5.1); las puntúa con un clasificador binario y devuelve la mejor con su saldo completo
si prob ≥ THRESH y saca MARGIN a la segunda. Si no, None (sale UNIDENTIFIED / categoría). exceptions.finalize (P3)
recalcula importe y excepción (SHORT_*, PARTIAL, FX_DIFFERENCE...).

Entrenamiento (anti-overfitting, CLAUDE.md): solo movimientos de TRAIN con booking_date < ctx.start (último año), sin
segment/difficulty/ids. Partidas abiertas a la fecha aproximadas: emitidas en los OPEN_DAYS previos y sin pago anterior
según el golden (el golden solo se usa para etiquetar el histórico, nunca en resolve). Modelo en caché por fase.
Entradas: txn, ctx, ledger. Salida: Proposal o None.
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import logging
import math
from collections import defaultdict

import numpy as np
import polars as pl

from bankrec import config, load, parties, refs
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers.amount_match import PAYMENT_FACTORY

log = logging.getLogger("bankrec")

NAME = "ranker"
BAND = 0.25          # importe del movimiento dentro de ±25 % del total de la partida
MAX_C = 30           # candidatas máximas por movimiento
THRESH = 0.5         # prob. mínima; en val la precisión de pares es 0,991 plana de 0,5 a 0,8 (el margen filtra)
MARGIN = 0.3         # ventaja mínima sobre la segunda candidata
OPEN_DAYS = 400      # ventana de emisión de partidas abiertas en el entrenamiento
HIST_DAYS = 365      # histórico de entrenamiento: último año antes de ctx.start
N_TRAIN = 40_000     # movimientos muestreados para entrenar (~2 min)
VERSION = "v1"       # cambia si cambian las features (invalida la caché)

FEATS = ["ratio", "logdiff", "exact", "ratio_total", "ref", "pscore", "days_issue", "days_due", "fx", "is_ap",
         "n_cands", "n_party", "fifo_rank", "month_hit", "diff_rank", "pscore_rank", "logamt", "n_exact", "other_ent"]


class _Index:
    """Partidas indexadas por (entidad, divisa, tipo) -> totales ordenados, por sufijo de 5 dígitos y por tercero."""

    def __init__(self, items: pl.DataFrame):
        cols = ["item_id", "item_type", "entity", "currency", "party_id", "issue_date", "due_date", "document_number",
                "po", "total_cents"]
        self.info: dict[str, tuple] = {}
        amt = defaultdict(list)
        self.ref: dict[str, list[str]] = defaultdict(list)
        self.party: dict[str, list[str]] = defaultdict(list)
        for r in items.select(cols).iter_rows():
            iid, typ = r[0], r[1]
            if typ not in ("ARI", "API") or r[9] is None or r[9] <= 0:
                continue
            self.info[iid] = r[1:]
            amt[(r[2], r[3], typ)].append((r[9], iid))
            self.party[r[4]].append(iid)
            for s in (r[7], r[8]):
                d = refs.digits(s)
                if len(d) >= 5:
                    self.ref[d[-5:]].append(iid)
        self.amt = {}
        for k, v in amt.items():
            v.sort()
            self.amt[k] = ([x[0] for x in v], [x[1] for x in v])


def _cands(txn: dict, I: _Index, ibmap: dict, open_of, toks: list[str]) -> list[str]:
    """Candidatas abiertas (open_of(iid) > 0) del tipo/entidad del movimiento, emitidas como tarde ese día."""
    cents = txn["amount_cents"]
    typ = "ARI" if cents > 0 else "API"
    ents = (txn["entity"],) if cents > 0 else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    d = txn["booking_date"]

    def ok(iid):
        i = I.info.get(iid)
        return i and i[0] == typ and i[1] in ents and (i[4] is None or i[4] <= d) and open_of(iid) > 0

    found: dict[str, int] = {}   # iid -> prioridad (0 ref/IBAN, 1 importe)
    for t in toks:
        if len(t) >= 5:
            for iid in I.ref.get(t[-5:], ()):
                if ok(iid) and refs.ref_score(I.info[iid][6], [t]) + refs.ref_score(I.info[iid][7], [t]) >= 75:
                    found[iid] = 0
    for ib in refs.ibans(txn):
        for pid in ibmap.get(ib, ()):
            for iid in I.party.get(pid, ()):
                if len(found) >= 4 * MAX_C:   # shortcut: terceros enormes se cortan; el importe sigue aportando
                    break
                if ok(iid):
                    found[iid] = 0
    near = []
    for cur, a in ((txn["currency"], abs(cents)), (txn.get("orig_currency"), abs(txn.get("orig_amount_cents") or 0))):
        if not cur or not a:
            continue
        for e in ents:
            tot = I.amt.get((e, cur, typ))
            if not tot:
                continue
            # se recorre hacia fuera desde el importe exacto: las MAX_C válidas más cercanas, sin barrer toda la banda
            vals, ids = tot
            lo_v, hi_v = a * (1 - BAND), a * (1 + BAND)
            r = bisect.bisect_left(vals, a)
            l, n = r - 1, 0
            while n < MAX_C and ((l >= 0 and vals[l] >= lo_v) or (r < len(vals) and vals[r] <= hi_v)):
                if r < len(vals) and vals[r] <= hi_v and (l < 0 or vals[l] < lo_v or vals[r] - a <= a - vals[l]):
                    j, r = r, r + 1
                else:
                    j, l = l, l - 1
                if ids[j] not in found and ok(ids[j]):
                    near.append((abs(vals[j] - a), ids[j]))
                    n += 1
    near.sort()
    a0 = abs(cents)
    out = sorted(found, key=lambda iid: abs(open_of(iid) - a0))[:MAX_C]
    for _, iid in near:   # sin duplicados (la pasada en divisa original puede repetir partidas)
        if len(out) >= MAX_C:
            break
        if iid not in out:
            out.append(iid)
    return out


def _feats(txn: dict, cands: list[str], I: _Index, open_of, tctx: dict, toks: list[str], ctx) -> np.ndarray:
    d = txn["booking_date"]
    cents = abs(txn["amount_cents"])
    ocur, oamt = txn.get("orig_currency"), abs(txn.get("orig_amount_cents") or 0)
    mon = refs.months(tctx["hay_raw"])
    rows, pcache = [], {}
    for iid in cands:
        typ, ent, cur, pid, iss, due, doc, po, tot = I.info[iid]
        op = open_of(iid)
        a = oamt if (cur != txn["currency"] and cur == ocur and oamt) else cents
        if pid not in pcache:
            pcache[pid] = parties.score(tctx, pid, ctx)
        rs = max(refs.ref_score(doc, toks), refs.ref_score(po, toks) if po else 0)
        hit = 1.0 if (mon and iss and (iss.month - 2) % 12 + 1 in mon) else 0.0
        rows.append([a / op, math.log1p(abs(a - op)), float(a == op), a / tot, rs, pcache[pid],
                     (d - iss).days if iss else np.nan, (d - due).days if due else np.nan,
                     float(cur != txn["currency"]), float(typ == "API"), len(cands), 0, 0, hit, 0, 0,
                     math.log1p(a), 0, float(ent != txn["entity"])])
    X = np.array(rows, dtype=np.float64)
    # features relativas al conjunto de candidatas
    pids = [I.info[c][3] for c in cands]
    issues = [I.info[c][4] or dt.date(1900, 1, 1) for c in cands]
    for k, c in enumerate(cands):
        same = [j for j in range(len(cands)) if pids[j] == pids[k]]
        X[k, 11] = len(same)
        X[k, 12] = sum(1 for j in same if (issues[j], cands[j]) < (issues[k], c))
    X[:, 14] = np.argsort(np.argsort(X[:, 1], kind="stable"), kind="stable")
    X[:, 15] = np.argsort(np.argsort(-X[:, 5], kind="stable"), kind="stable")
    X[:, 17] = X[:, 2].sum()
    return X


def _tctx(txn: dict) -> tuple[dict, list[str]]:
    tc = parties.context(txn)
    tc["hay_raw"] = refs.haystack(txn)
    return tc, refs.tokens(tc["hay_raw"])


def _train(ctx, cut: dt.date) -> "object":
    """Entrena con TRAIN, booking_date en el último año antes de min(cut, fin de TRAIN)."""
    import lightgbm as lgb

    t0 = dt.datetime.now()
    tx = load.table("train", "bank_transactions")
    gold = load.table("train", "golden_allocations").select("bank_txn_id", "item_id")
    hi = min(cut, tx["booking_date"].max() + dt.timedelta(days=1))   # FINAL: último año de TRAIN, no 2025-H1 solo
    lo = hi - dt.timedelta(days=HIST_DAYS)
    tx = tx.filter((pl.col("booking_date") >= lo) & (pl.col("booking_date") < hi) & (pl.col("amount_cents") != 0))
    # etiqueta: movimientos con 0 o 1 partida (lo que puede resolver un 1:1)
    g = gold.filter(pl.col("bank_txn_id").is_in(tx["bank_txn_id"].implode()))
    per = g.group_by("bank_txn_id").agg(pl.col("item_id").drop_nulls().alias("items"))
    tx = tx.join(per, on="bank_txn_id", how="inner").filter(pl.col("items").list.len() <= 1)
    if tx.height > N_TRAIN:
        tx = tx.sample(N_TRAIN, seed=7)
    # partidas de TRAIN con fecha del primer pago (golden de movimientos anteriores a ctx.start)
    from bankrec.phase import _items_of
    items = _items_of("train").filter((pl.col("issue_date") < hi) & (pl.col("issue_date") >= lo - dt.timedelta(days=OPEN_DAYS)))
    allt = load.table("train", "bank_transactions").select("bank_txn_id", "booking_date")
    first = (gold.filter(pl.col("item_id").is_not_null()).join(allt, on="bank_txn_id")
             .filter(pl.col("booking_date") < hi).group_by("item_id").agg(pl.col("booking_date").min().alias("fp")))
    fp = dict(zip(*first.select("item_id", "fp").to_dict(as_series=False).values()))
    I = _Index(items)
    ibmap = ctx.shared["parties"]["iban"]
    Xs, ys = [], []
    for txn in tx.iter_rows(named=True):
        d = txn["booking_date"]

        def open_of(iid, d=d):
            i = I.info[iid]
            f = fp.get(iid)
            return i[8] if (i[4] and (d - i[4]).days <= OPEN_DAYS and (f is None or f >= d)) else 0

        tc, toks = _tctx(txn)
        cands = _cands(txn, I, ibmap, open_of, toks)
        if not cands:
            continue
        X = _feats(txn, cands, I, open_of, tc, toks, ctx)
        gi = set(txn["items"])
        Xs.append(X)
        ys.append(np.array([c in gi for c in cands], dtype=np.int8))
    X, y = np.vstack(Xs), np.concatenate(ys)
    log.info("ranker: %d pares (%d positivos) de %d movimientos", len(y), int(y.sum()), len(Xs))
    m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=63, min_child_samples=50,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1, random_state=7)
    m.fit(X, y)
    log.info("ranker: entrenado en %.0f s", (dt.datetime.now() - t0).total_seconds())
    return m.booster_


def prepare(ctx) -> None:
    """Entrena o carga de caché el modelo e indexa las partidas de la fase."""
    import lightgbm as lgb

    parties.prepare(ctx)
    # sample (2025-06) está dentro de val: reutiliza el modelo de val (corte 2025-01), sin reentrenar
    name, cut = ("val", dt.date.fromisoformat(config.PHASES["val"][1])) if ctx.name == "sample" else (ctx.name, ctx.start)
    key = hashlib.md5(f"{VERSION}|{cut}|{N_TRAIN}|{HIST_DAYS}|{FEATS}".encode()).hexdigest()[:10]
    path = config.cache_dir() / f"ranker_{name}_{key}.txt"
    if path.exists():
        booster = lgb.Booster(model_file=str(path))
    else:
        booster = _train(ctx, cut)
        booster.save_model(str(path))
    ctx.shared[NAME] = {"model": booster, "index": _Index(ctx.items)}


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    if not txn["amount_cents"]:
        return None
    S = ctx.shared[NAME]
    I = S["index"]
    open_of = lambda iid: ledger.open.get(iid, 0)  # noqa: E731
    tc, toks = _tctx(txn)
    cands = _cands(txn, I, ctx.shared["parties"]["iban"], open_of, toks)
    if not cands:
        return None
    X = _feats(txn, cands, I, open_of, tc, toks, ctx)
    p = S["model"].predict(X, num_threads=1)
    order = np.argsort(-p)
    best = p[order[0]]
    second = p[order[1]] if len(p) > 1 else 0.0
    k = order[0]
    # §3: sin referencia ni tercero no se fuerza (lookalikes con el importe de otra factura)
    if best < THRESH or best - second < MARGIN or (X[k, 4] < 75 and X[k, 5] < 70):
        return None
    iid = cands[k]
    return Proposal(txn["bank_txn_id"], [Alloc(iid, ledger.open[iid])], confidence=float(best),
                    reason=f"ranker p={best:.2f} 2a={second:.2f} n={len(cands)}")
