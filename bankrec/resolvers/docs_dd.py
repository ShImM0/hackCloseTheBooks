"""Remesas SEPA/BACS/ACH (direct_debit_batches/). Dueño: P2.

Qué hace:
- Abono de remesa: (cuenta, fecha de cobro, batch_total) -> batch -> todos sus recibos, cada uno a su partida, por el
  importe del recibo y sin excepción (también los re-presentados, re_presentation=Y). Segmento ar_dd_batch.
- Devolución R (cargo con mandato en el texto): recibo devuelto -> su partida con RETURNED por el importe devuelto.
  Segmento ar_dd_returned.
Recibo -> partida: tercero por mandato (customers.sepa_mandate_id) o IBAN del deudor; dentro del tercero, la partida
cuyo número impreso acaba en los dígitos de remittance_info y, a igualdad, la de importe igual al recibo.
Entradas: ctx (bankrec.phase.Phase). Salida: dict bank_txn_id -> Proposal (resolve_batch) y resolve() para
devoluciones (dependen del ledger: se prefiere la partida ya cobrada).
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import polars as pl

from bankrec import config, load
from bankrec.contract import Alloc, Proposal

NAME = "docs_dd"
log = logging.getLogger("bankrec")

_MANDATE = re.compile(r"\b[A-Z]{3}-[A-Z]{2}-\d{4,}\b")   # mandato: prefijo-país-número (formato general)
_BATCH = re.compile(r"(?:REM|DEV)\s*(\d{11})")
_REP_DAYS = dt.timedelta(days=60)   # re-presentación tras la devolución (§5.10)
_MIN_LAG = 1                        # días mínimos entre el cobro y su devolución (con 3 ya se pierden devoluciones reales)


def _receipts(ctx) -> pl.DataFrame:
    """Recibos de la fase y de las fases previas (las devoluciones citan remesas anteriores)."""
    parts = []
    for folder in [*config.PRIOR_FOLDERS.get(ctx.name, []), ctx.folder]:
        d = config.folder_dir(folder) / "direct_debit_batches"
        df = load.read_folder(d) if d.exists() else pl.DataFrame()
        if not df.is_empty():
            parts.append(df)
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="vertical_relaxed").unique(["batch_id", "end_to_end_id"], keep="last")


def _map_items(rec: pl.DataFrame, ctx) -> pl.DataFrame:
    """Añade item_id a cada recibo (vectorizado). Recibos sin partida se quedan con item_id nulo."""
    cust = ctx.master("customers")
    by_mandate = (cust.filter(pl.col("sepa_mandate_id").is_not_null())
                  .select(pl.col("sepa_mandate_id").alias("mandate_id"), pl.col("customer_id").alias("pm")).unique("mandate_id"))
    by_iban = (cust.filter(pl.col("iban").is_not_null())
               .select(pl.col("iban").alias("debtor_iban"), pl.col("customer_id").alias("pi")).unique("debtor_iban"))
    acc = ctx.master("bank_accounts").select("account_id", pl.col("entity").alias("acc_entity"))
    rec = (rec.with_row_index("rid")
           .join(by_mandate, on="mandate_id", how="left")
           .join(by_iban, on="debtor_iban", how="left")
           .join(acc, on="account_id", how="left")
           # cuentas secundarias ("ES01-PENA-EUR-2") no están en el maestro: la entidad es el prefijo del id
           .with_columns(pl.coalesce("acc_entity", pl.col("account_id").str.split("-").list.first()).alias("acc_entity"))
           .with_columns(pl.coalesce("pm", "pi").alias("party_id"),
                         pl.col("remittance_info").str.extract(r"(\d{4,})\D*$").alias("rraw"))
           .with_columns(pl.col("rraw").str.strip_chars_start("0").alias("rdig")))
    ari = (ctx.items.filter(pl.col("item_type") == "ARI")
           .select("item_id", "party_id", "entity", "issue_date", "total_cents",
                   pl.col("document_number").str.replace_all(r"\D", "").alias("idig")))
    # candidatas: mismo tercero; puntuación por número impreso y por importe.
    # Número: el último grupo de >= 4 cifras del recibo cierra el nº de la factura, sin ceros a la izquierda
    # (POLICIES §5.5) o con ellos: "FT25-01-00434" -> "00434" (las series que empiezan cada año tienen < 4 cifras
    # significativas en enero; sin esto el recibo caía en una factura antigua del mismo importe).
    c = (rec.select("rid", "party_id", "acc_entity", "rdig", "rraw", "amount_cents")
         .join(ari, on="party_id")
         .with_columns(
             ((pl.col("rdig").is_not_null() & (pl.col("rdig").str.len_chars() >= 4)
               & pl.col("idig").str.ends_with(pl.col("rdig")))
              | (pl.col("rraw").is_not_null() & pl.col("idig").str.ends_with(pl.col("rraw")))).cast(pl.Int32).alias("s_doc"),
             (pl.col("total_cents") == pl.col("amount_cents")).cast(pl.Int32).alias("s_amt"),
             (pl.col("entity") == pl.col("acc_entity")).fill_null(False).cast(pl.Int32).alias("s_ent"))
         .with_columns((pl.col("s_doc") * 4 + pl.col("s_amt") * 2 + pl.col("s_ent")).alias("score"))
         .filter(pl.col("score") >= 2)
         .sort(["rid", "score", "issue_date"], descending=[False, True, False])
         .unique("rid", keep="first")
         .select("rid", "item_id", "score"))
    # sin tercero: número impreso dentro de la entidad de la cuenta, con importe igual
    miss = rec.filter(~pl.col("rid").is_in(c["rid"].implode()) & pl.col("rdig").is_not_null())
    if miss.height:
        c2 = (miss.select("rid", "acc_entity", "rdig", "amount_cents")
              .join(ari.rename({"entity": "acc_entity"}), left_on=["acc_entity", "amount_cents"],
                    right_on=["acc_entity", "total_cents"])
              .filter(pl.col("idig").str.ends_with(pl.col("rdig")))
              .sort(["rid", "issue_date"]).unique("rid", keep="first")
              .select("rid", "item_id", pl.lit(1, pl.Int32).alias("score")))
        c = pl.concat([c, c2])
    return rec.join(c, on="rid", how="left").drop("pm", "pi", "rraw")


def prepare(ctx) -> None:
    rec = _receipts(ctx)
    if rec.is_empty():
        ctx.shared[NAME] = {"by_txn": {}, "returns": {}}
        return
    rec = _map_items(rec, ctx)
    sh: dict = {}

    # 1) abonos de remesa: (cuenta, fecha, total) exacto; si no, fecha de cobro..+4 días
    batches = (rec.group_by("batch_id").agg(pl.col("account_id").first(), pl.col("collection_date").first(),
                                            pl.col("batch_total_cents").first()))
    credits = ctx.txns.filter(pl.col("amount_cents") > 0).select("bank_txn_id", "account_id", "booking_date",
                                                                "value_date", "amount_cents")
    j = (batches.join(credits, left_on=["account_id", "batch_total_cents"], right_on=["account_id", "amount_cents"])
         .with_columns((pl.col("booking_date") - pl.col("collection_date")).dt.total_days().alias("lag"))
         .filter(pl.col("lag").is_between(-1, 4))
         .with_columns(pl.col("lag").abs().alias("alag"))
         .sort(["alag", "batch_id"]))
    used_t, used_b, pairs = set(), set(), {}
    for b, t in zip(j["batch_id"].to_list(), j["bank_txn_id"].to_list()):
        if b in used_b or t in used_t:
            continue
        used_b.add(b); used_t.add(t); pairs[t] = b
    by_batch = {}
    for b, grp in rec.filter(pl.col("batch_id").is_in(list(used_b))).group_by("batch_id"):
        by_batch[b[0]] = list(zip(grp["item_id"].to_list(), grp["amount_cents"].to_list()))
    sh["by_txn"] = {t: by_batch[b] for t, b in pairs.items() if b in by_batch}

    # 2) devoluciones: (mandato, importe) -> [(batch_id, fecha, item_id, re-presentado)];
    #    partida -> fechas en que se re-presenta (re_presentation=Y, POLICIES §5.10)
    ret: dict[tuple[str, int], list] = {}
    rep: dict[str, list] = {}
    for m, a, b, d, i, y in rec.filter(pl.col("mandate_id").is_not_null()).select(
            "mandate_id", "amount_cents", "batch_id", "collection_date", "item_id", "re_presentation").iter_rows():
        ret.setdefault((m, a), []).append((b, d, i, y == "Y"))
        if y == "Y" and i is not None and d is not None:
            rep.setdefault(i, []).append(d)
    sh["returns"] = ret
    sh["represented"] = rep
    # respaldo: mandato -> cliente -> facturas por importe (remesas fuera de los ficheros cargados)
    cust = ctx.master("customers").filter(pl.col("sepa_mandate_id").is_not_null())
    sh["mandate_party"] = dict(zip(cust["sepa_mandate_id"].to_list(), cust["customer_id"].to_list()))
    ari = ctx.items.filter(pl.col("item_type") == "ARI").select("party_id", "total_cents", "issue_date", "item_id")
    by_pa: dict[tuple[str, int], list] = {}
    for pa, a, d, i in ari.iter_rows():
        by_pa.setdefault((pa, a), []).append((d, i))
    sh["party_amount"] = by_pa
    ctx.shared[NAME] = sh
    log.info("docs_dd: %d recibos, %d remesas casadas con abono, %d recibos sin partida",
             rec.height, len(sh["by_txn"]), rec["item_id"].null_count())


def resolve_batch(ctx) -> dict[str, Proposal]:
    out = {}
    for t, recs in ctx.shared.get(NAME, {}).get("by_txn", {}).items():
        allocs, seen, miss = [], set(), 0
        for item, amt in recs:
            if item is None or item in seen:
                miss += 1
                continue
            seen.add(item)
            allocs.append(Alloc(item, amt))
        if allocs:
            out[t] = Proposal(t, allocs, confidence=0.99 if not miss else 0.8, resolver=NAME,
                              reason=f"remesa {len(recs)} recibos, {miss} sin partida")
    return out


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    """Devolución de recibo: cargo con mandato VTM-xx-nnnn en el texto."""
    amt = txn["amount_cents"]
    if amt >= 0:
        return None
    text = " ".join(str(txn.get(k) or "") for k in ("text", "ref1", "ref2", "remittance_info"))
    m = _MANDATE.search(text)
    if not m:
        return None
    cands = ctx.shared.get(NAME, {}).get("returns", {}).get((m.group(0), -amt))
    if not cands:
        return None
    sh = ctx.shared.get(NAME, {})
    mandate = m.group(0)
    bm = _BATCH.search(text)
    batch = bm.group(1) if bm else None
    cands = sh.get("returns", {}).get((mandate, -amt), [])
    if batch:
        cands = [c for c in cands if c[0] == batch]
    prior = [c for c in cands if c[1] is not None and c[1] <= txn["booking_date"] and c[2] is not None]
    if prior:
        item = _returned_item(prior, txn["booking_date"], sh.get("represented", {}))
        return Proposal(txn["bank_txn_id"], [Alloc(item, -amt, "RETURNED")], confidence=0.95, resolver=NAME,
                        reason=f"devolución mandato {mandate} (recibo)")
    # respaldo: factura del cliente del mandato con ese importe, emitida antes de la remesa, ya cobrada si es posible
    party = sh.get("mandate_party", {}).get(mandate)
    limit = _batch_date(batch) or txn["booking_date"]
    its = [c for c in sh.get("party_amount", {}).get((party, -amt), []) if c[0] is not None and c[0] <= limit]
    if not its:
        return None
    _, item = max(its, key=lambda c: (not ledger.is_open(c[1]), c[0]))
    return Proposal(txn["bank_txn_id"], [Alloc(item, -amt, "RETURNED")], confidence=0.8, resolver=NAME,
                    reason=f"devolución mandato {mandate} (cliente+importe)")


def _returned_item(prior: list, booking: dt.date, rep: dict) -> str:
    """Partida del recibo devuelto entre los recibos anteriores del mismo mandato e importe (cuotas fijas).
    POLICIES §5.10: el recibo devuelto suele re-presentarse en una remesa posterior (re_presentation=Y):
    1) si una sola de las partidas candidatas se re-presenta en los _REP_DAYS días siguientes, es esa;
    2) si no, el recibo más reciente cobrado al menos _MIN_LAG días antes (la devolución no llega el mismo día).
    Offline con el golden (acierto de partida en ar_dd_returned; antes = el recibo más reciente): 2023-H1 0,965 -> 0,985,
    2024-H1 0,968 -> 0,986, 2025-H1 0,969 -> 0,987 (igual en todos los semestres 2021-2025; ventana 30/60/90 días igual)."""
    later = {c[2] for c in prior if any(booking < x <= booking + _REP_DAYS for x in rep.get(c[2], ()))}
    if len(later) == 1:
        return later.pop()
    ok = [c for c in prior if (booking - c[1]).days >= _MIN_LAG] or prior
    return max(ok, key=lambda c: (c[1], c[0] or "", c[2]))[2]   # desempate fijo: el envío debe ser reproducible


def _batch_date(batch: str | None):
    """'01250502827' -> 2025-05-02 (NN AAMMDD NNN)."""
    if not batch:
        return None
    try:
        return dt.date(2000 + int(batch[2:4]), int(batch[4:6]), int(batch[6:8]))
    except ValueError:
        return None
