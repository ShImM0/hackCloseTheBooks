"""Cheques / lockbox y contracargos de tarjeta (helpers de docs_card). Dueño: P2.

POLICIES §5.3: un depósito de cheques/lockbox liquida los cheques de `cheque_deposits/`; §2 RETURNED: un cheque
devuelto o un contracargo de tarjeta reabre la partida (importe reabierto, positivo).
- cheque_matches: abono = `deposit_total` de un depósito (misma cuenta, fecha de depósito..+5 días, importe exacto).
  Cada cheque -> ARI de la entidad de la cuenta: tercero por nombre del librador (`drawer`, = nombre del cliente
  normalizado) y, dentro del tercero, la factura cuyo número encaja con `stub_reference` (refs.ref_score) o cuyo
  saldo/total es el importe del cheque; sin tercero, solo si el número impreso es inequívoco y el importe cuadra.
- chargebacks: cargo con "****NNNN" (últimos 4 de la tarjeta) y comercio de 9 dígitos -> pedido de card_settlements con
  esa tarjeta, ese comercio y bruto == importe -> su ARI con RETURNED.
Entradas: ctx (bankrec.phase.Phase). Salida: dict bank_txn_id -> list[(item_id | None, cents, exception)].
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

import polars as pl

from bankrec import config, load, parties, refs

_SLACK = 5               # días entre el depósito y el abono
_CHARGEBACK_DAYS = 240   # el contracargo llega semanas o meses después de la liquidación
_CARD = re.compile(r"\*{4}\s*(\d{4})")
_MID = re.compile(r"\b(\d{9})\b")
_NUM = re.compile(r"\b(\d{6,8})\b")   # nº de cheque (los ceros a la izquierda cuentan: se lee como texto)


def _folders(ctx) -> list[str]:
    return [*config.PRIOR_FOLDERS.get(ctx.name, []), ctx.folder]


def _read(ctx, sub: str, date_col: str, lookback: dt.timedelta) -> pl.DataFrame:
    parts = []
    for folder in _folders(ctx):
        d = config.folder_dir(folder) / sub
        df = load.read_folder(d) if d.exists() else pl.DataFrame()
        if not df.is_empty():
            parts.append(df.filter((pl.col(date_col) >= ctx.start - lookback) & (pl.col(date_col) < ctx.end)))
    return pl.concat(parts, how="vertical_relaxed") if parts else pl.DataFrame()


def _index(ctx) -> dict:
    """Índices de partidas ARI: por tercero y nombre de tercero -> terceros (se construye una vez por fase)."""
    if "docs_card_idx" in ctx.shared:
        return ctx.shared["docs_card_idx"]
    parties.prepare(ctx)
    by_party: dict[str, list] = defaultdict(list)
    ari = ctx.items.filter(pl.col("item_type") == "ARI").select(
        "item_id", "entity", "party_id", "document_number", "currency", "total_cents", "open_cents", "issue_date")
    for r in ari.iter_rows():
        if r[2]:
            by_party[r[2]].append(r)
    names: dict[str, set[str]] = defaultdict(set)
    for pid, ns in ctx.shared["parties"]["names"].items():
        if pid in by_party:
            for n in ns:
                names[n].add(pid)
    ctx.shared["docs_card_idx"] = {"by_party": by_party, "names": names}
    return ctx.shared["docs_card_idx"]


def _pick(cheque: dict, ent: str, idx: dict) -> str | None:
    """Partida de un cheque: tercero por nombre del librador + número de la factura / importe."""
    cents = cheque["amount_cents"]
    toks = refs.tokens((cheque.get("stub_reference") or "").upper())
    pids = idx["names"].get(parties.norm_name(cheque.get("drawer")), ())
    best, best_key, best_pid, rival = None, None, None, False
    for pid in pids:                     # varios terceros pueden compartir nombre (sucursales): decide el nº y el importe
        for it in idx["by_party"][pid]:
            if it[1] != ent or (it[4] is not None and cheque.get("currency") and it[4] != cheque["currency"]):
                continue
            ref = refs.ref_score(it[3], toks)
            amt = int(cents in (it[6], it[5]))
            if ref < 75 and not amt:
                continue
            key = (ref >= 85, amt and it[6] > 0, amt, ref, -(it[7].toordinal() if it[7] else 0))
            if best_key is None or key > best_key:
                best, best_key, best_pid, rival = it[0], key, pid, False
            elif key[:4] == best_key[:4] and pid != best_pid:
                rival = True             # dos terceros distintos empatan en número e importe: no se decide
    return None if rival else best


def cheque_matches(ctx) -> dict[str, list]:
    ch = _read(ctx, "cheque_deposits", "deposit_date", dt.timedelta(days=10))
    if ch.is_empty():
        return {}
    dep = (ch.group_by("deposit_id")
           .agg(pl.col("account_id").first(), pl.col("deposit_date").first(), pl.col("deposit_total_cents").first()))
    credits = ctx.txns.filter(pl.col("amount_cents") > 0).select("bank_txn_id", "account_id", "booking_date", "amount_cents")
    j = (dep.join(credits, left_on=["account_id", "deposit_total_cents"], right_on=["account_id", "amount_cents"])
         .with_columns((pl.col("booking_date") - pl.col("deposit_date")).dt.total_days().alias("lag"))
         .filter(pl.col("lag").is_between(0, _SLACK)).sort(["lag", "deposit_id"]))
    idx = _index(ctx)
    rows_by_dep = {k[0]: g.to_dicts() for k, g in ch.group_by("deposit_id")}
    used_t, used_d, out = set(), set(), {}
    for t, d, acc in zip(j["bank_txn_id"].to_list(), j["deposit_id"].to_list(), j["account_id"].to_list()):
        if t in used_t or d in used_d:
            continue
        used_t.add(t); used_d.add(d)
        ent = acc.split("-")[0]
        out[t] = [(_pick(c, ent, idx), c["amount_cents"], "") for c in rows_by_dep[d]]
    return out


def returned_cheques(ctx) -> dict[str, list]:
    """Cheque devuelto (§2 RETURNED): cargo cuyo texto trae el nº de cheque de un depósito anterior (misma cuenta e
    importe) -> la partida de ese cheque, reabierta por su importe."""
    debits = ctx.txns.filter(pl.col("amount_cents") < 0).select(
        "bank_txn_id", "account_id", "amount_cents",
        pl.concat_str(["text", "ref2", "remittance_info"], separator=" ", ignore_nulls=True).alias("txt"))
    debits = debits.filter(pl.col("txt").str.contains(r"\b\d{6,8}\b"))
    if debits.is_empty():
        return {}
    ch = _read(ctx, "cheque_deposits", "deposit_date", dt.timedelta(days=_CHARGEBACK_DAYS))
    if ch.is_empty():
        return {}
    by_num: dict[tuple, list] = defaultdict(list)
    for c in ch.to_dicts():
        by_num[(c["account_id"], c["cheque_number"], c["amount_cents"])].append(c)
    idx = _index(ctx)
    out: dict[str, list] = {}
    for t, acc, amt, txt in debits.iter_rows():
        cands = [c for tok in set(_NUM.findall(txt)) for c in by_num.get((acc, tok, -amt), [])]
        if len(cands) != 1:            # ningún cheque o varios posibles: no se decide
            continue
        item = _pick(cands[0], acc.split("-")[0], idx)
        if item:
            out[t] = [(item, -amt, "RETURNED")]
    return out


def chargebacks(ctx) -> dict[str, list]:
    debits = ctx.txns.filter(pl.col("amount_cents") < 0).select("bank_txn_id", "account_id", "booking_date", "amount_cents",
                                                                pl.concat_str(["text", "ref2"], separator=" ", ignore_nulls=True).alias("txt"))
    debits = debits.filter(pl.col("txt").str.contains(r"\*{4}\s*\d{4}"))
    if debits.is_empty():
        return {}
    cs = _read(ctx, "card_settlements", "settlement_date", dt.timedelta(days=_CHARGEBACK_DAYS))
    if cs.is_empty():
        return {}
    ari = ctx.items.filter(pl.col("item_type") == "ARI").select(
        "item_id", "entity", pl.col("document_number").str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "").alias("o"))
    cs = (cs.with_columns(pl.col("order_number").str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "").alias("o"),
                          pl.col("account_id").str.split("-").list.first().alias("ent"))
          .join(ari, on="o", how="inner").filter(pl.col("entity") == pl.col("ent")))
    key: dict[tuple, list] = defaultdict(list)
    for card, mid, gross, ent, item, d in cs.select("card", "merchant_id", "gross_cents", "ent", "item_id", "settlement_date").iter_rows():
        key[(card[-4:] if card else "", mid, gross, ent)].append((d, item))
    out: dict[str, list] = {}
    for t, acc, d, amt, txt in debits.iter_rows():
        c, m = _CARD.search(txt), _MID.search(txt)
        if not c or not m:
            continue
        cands = [x for x in key.get((c.group(1), m.group(1), -amt, acc.split("-")[0]), []) if x[0] <= d]
        items = {i for _, i in cands}
        if len(items) == 1:      # varios pedidos con la misma tarjeta, comercio e importe: no se decide
            out[t] = [(items.pop(), -amt, "RETURNED")]
    return out
