"""Pagos a proveedores (payment_runs/: transfers, aggregated, confirming). Dueño: P2.

Qué hace (POLICIES §5.3: un cargo agregado o de confirming liquida todos los documentos del run; un cargo por
transferencia, los de su end_to_end_id; las notas de crédito compensadas van en su fila, positivas):
- transfers: cargo con end_to_end_id de un run -> sus documentos. Si el banco no trae el e2e (N43), por
  (cuenta, importe == transferencia, fecha de ejecución..+7 días) y, con empate, por el nombre del beneficiario.
- aggregated: un cargo por el total del run (suma de transferencias) en la cuenta y fecha del run.
- confirming: texto "REMESA CF… N PROVEEDORES": un cargo por vencimiento; se reparten los beneficiarios del run por
  la fecha de vencimiento de sus partidas y se casa con el importe y la fecha del cargo.
Fila del run -> partida: nº de documento normalizado + tipo (INVOICE->API, CREDIT_NOTE->APC); si hay varias, el
proveedor del IBAN del beneficiario, el importe del documento y la entidad. Importe de la fila = |document_amount|.
No pone excepciones (PARTIAL, FX_DIFFERENCE… las decide exceptions.finalize).
Entradas: ctx (bankrec.phase.Phase). Salida: dict bank_txn_id -> Proposal (resolve_batch).
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import polars as pl

from bankrec import config, load
from bankrec.contract import Alloc, Proposal

NAME = "docs_ap"
log = logging.getLogger("bankrec")

_RUN = re.compile(r"\b(CF\d{9,})\b")
_COUNT = re.compile(r"CF\d{9,}\s+(\d+)\s+PROVEEDOR", re.I)   # "REMESA CF… 8 PROVEEDORES"
_LOOKBACK = dt.timedelta(days=150)   # los cargos de confirming/agregados citan runs de semanas antes
_LOOKAHEAD = dt.timedelta(days=45)  # confirming: la remesa se genera antes del cargo, pero su fecha puede ser posterior
_SLACK = 7                          # días entre la ejecución y el cargo en banco


def _norm(col: str) -> pl.Expr:
    return pl.col(col).str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


def _runs(ctx) -> pl.DataFrame:
    parts = []
    for folder in [*config.PRIOR_FOLDERS.get(ctx.name, []), ctx.folder]:
        d = config.folder_dir(folder) / "payment_runs"
        df = load.read_folder(d) if d.exists() else pl.DataFrame()
        if not df.is_empty():
            parts.append(df.filter((pl.col("execution_date") >= ctx.start - _LOOKBACK) & (pl.col("execution_date") < ctx.end + _LOOKAHEAD)))
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="vertical_relaxed")


def _map_items(rows: pl.DataFrame, ctx) -> pl.DataFrame:
    """Añade item_id a cada fila del run (nulo si no se puede decidir)."""
    sup = ctx.master("suppliers").select(pl.col("iban").alias("beneficiary_iban"), pl.col("supplier_id")).drop_nulls().unique()
    acc = ctx.master("bank_accounts").select(pl.col("account_id").alias("debtor_account_id"), pl.col("entity").alias("acc_entity"))
    items = (ctx.items.filter(pl.col("item_type").is_in(["API", "APC"]))
             .select("item_id", "item_type", "entity", "party_id", "due_date", "total_cents", "open_cents",
                     _norm("document_number").alias("dn")))
    r = (rows.with_row_index("rid")
         .with_columns(_norm("document_number").alias("dn"),
                       pl.when(pl.col("document_type") == "CREDIT_NOTE").then(pl.lit("APC")).otherwise(pl.lit("API")).alias("item_type"),
                       pl.col("document_amount_cents").abs().alias("doc_cents"))
         .join(acc, on="debtor_account_id", how="left")
         # cuentas secundarias ("IT01-VALD-EUR-2") no están en el maestro: la entidad es el prefijo del id
         .with_columns(pl.coalesce("acc_entity", pl.col("debtor_account_id").str.split("-").list.first()).alias("acc_entity")))
    c = r.select("rid", "dn", "item_type", "doc_cents", "acc_entity", "beneficiary_iban").join(items, on=["dn", "item_type"])
    sup_by_iban = sup.rename({"supplier_id": "party_id"})
    c = (c.join(sup_by_iban.with_columns(pl.lit(1).alias("s_iban")), on=["beneficiary_iban", "party_id"], how="left")
         .with_columns(pl.col("s_iban").fill_null(0),
                       ((pl.col("total_cents") == pl.col("doc_cents")) | (pl.col("open_cents") == pl.col("doc_cents"))).cast(pl.Int32).alias("s_amt"),
                       (pl.col("open_cents") > 0).cast(pl.Int32).alias("s_open"),
                       (pl.col("entity") == pl.col("acc_entity")).fill_null(False).cast(pl.Int32).alias("s_ent"))
         .with_columns((pl.col("s_iban") * 8 + pl.col("s_amt") * 4 + pl.col("s_open") * 2 + pl.col("s_ent")).alias("score")))
    best = c.group_by("rid").agg(pl.col("score").max().alias("best"), pl.len().alias("n"))
    c = (c.join(best, on="rid").filter(pl.col("score") == pl.col("best")))
    # empate real entre dos partidas con la misma puntuación: no se decide
    uniq = c.group_by("rid").agg(pl.len().alias("k"), pl.col("item_id").first(), pl.col("due_date").first())
    uniq = uniq.filter(pl.col("k") == 1).select("rid", "item_id", "due_date")
    return r.join(uniq, on="rid", how="left")


def prepare(ctx) -> None:
    sh: dict = {"by_txn": {}}
    ctx.shared[NAME] = sh
    runs = _runs(ctx)
    if runs.is_empty():
        return
    rows = _map_items(runs, ctx)
    # grupos de transferencia (un e2e = un beneficiario)
    grp = (rows.group_by("run_id", "end_to_end_id", "debtor_account_id")
           .agg(pl.col("run_type").first(), pl.col("execution_date").first(),
                pl.col("transfer_amount_cents").first(), pl.col("beneficiary").first(),
                pl.col("due_date").max().alias("maturity"),
                pl.struct("item_id", "doc_cents").alias("docs")))
    debits = ctx.txns.filter(pl.col("amount_cents") < 0).select(
        "bank_txn_id", "account_id", "booking_date", pl.col("amount_cents").neg().alias("amt"), "end_to_end_id",
        pl.concat_str([pl.col("text"), pl.col("ref1"), pl.col("ref2")], separator=" ", ignore_nulls=True).alias("txt"))

    def alloc_of(docs) -> list[tuple[str | None, int]]:
        return [(d["item_id"], d["doc_cents"]) for d in docs]

    used: set[str] = set()
    out: dict[str, list] = {}

    # 1) transfers por (cuenta, end_to_end_id): el e2e se repite entre cuentas y ficheros -> fecha de ejecución..+7
    tr = grp.filter(pl.col("run_type") == "transfers")
    j1 = (tr.select("end_to_end_id", "debtor_account_id", "execution_date", "docs")
          .join(debits.filter(pl.col("end_to_end_id").is_not_null()), left_on=["debtor_account_id", "end_to_end_id"],
                right_on=["account_id", "end_to_end_id"])
          .with_columns((pl.col("booking_date") - pl.col("execution_date")).dt.total_days().alias("lag"))
          .filter(pl.col("lag").is_between(0, _SLACK)).sort("lag"))
    for t, docs in zip(j1["bank_txn_id"].to_list(), j1["docs"].to_list()):
        if t not in used:
            out[t] = alloc_of(docs); used.add(t)

    # 2) transfers sin e2e en el banco: (cuenta, importe, ventana de fecha), empate -> nombre del beneficiario
    rest = debits.filter(~pl.col("bank_txn_id").is_in(list(used)))
    cand = (tr.select("end_to_end_id", "debtor_account_id", "transfer_amount_cents", "execution_date", "beneficiary", "docs")
            .join(rest, left_on=["debtor_account_id", "transfer_amount_cents"], right_on=["account_id", "amt"])
            .with_columns((pl.col("booking_date") - pl.col("execution_date")).dt.total_days().alias("lag"))
            .filter(pl.col("lag").is_between(0, _SLACK)))
    if cand.height:
        cand = cand.with_columns(
            pl.struct("txt", "beneficiary").map_elements(lambda s: _name_hit(s["txt"], s["beneficiary"]), return_dtype=pl.Int32).alias("nh"))
        by_txn = cand.group_by("bank_txn_id").agg(pl.len().alias("k"), pl.col("nh").max().alias("nhm"))
        for t, k, nhm in by_txn.iter_rows():
            if t in used:
                continue
            sub = cand.filter(pl.col("bank_txn_id") == t)
            if k > 1:
                sub = sub.filter(pl.col("nh") == nhm)
                if sub.height != 1 or nhm == 0:
                    continue
            # un mismo e2e no se reparte a dos cargos
            out[t] = alloc_of(sub["docs"][0]); used.add(t)

    # 3) aggregated: total del run
    ag = (grp.filter(pl.col("run_type") == "aggregated")
          .group_by("run_id").agg(pl.col("debtor_account_id").first(), pl.col("execution_date").first(),
                                  pl.col("transfer_amount_cents").sum().alias("tot"),
                                  pl.col("docs").explode().alias("docs")))
    if ag.height:
        rest = debits.filter(~pl.col("bank_txn_id").is_in(list(used)))
        j = (ag.join(rest, left_on=["debtor_account_id", "tot"], right_on=["account_id", "amt"])
             .with_columns((pl.col("booking_date") - pl.col("execution_date")).dt.total_days().alias("lag"))
             .filter(pl.col("lag").is_between(0, _SLACK)).sort("lag"))
        used_runs: set[str] = set()
        for t, rid, docs in zip(j["bank_txn_id"].to_list(), j["run_id"].to_list(), j["docs"].to_list()):
            if t in used or rid in used_runs:
                continue
            out[t] = alloc_of(docs); used.add(t); used_runs.add(rid)

    # 4) confirming: "REMESA CF… N PROVEEDORES": N beneficiarios del run cuyas transferencias suman el cargo.
    #    Cada beneficiario se usa en un solo cargo: se resuelven antes los cargos con menos beneficiarios.
    cf = grp.filter(pl.col("run_type") == "confirming")
    if cf.height:
        byrun: dict[str, list] = {}
        for rid, mat, amt, docs in zip(cf["run_id"].to_list(), cf["maturity"].to_list(), cf["transfer_amount_cents"].to_list(), cf["docs"].to_list()):
            byrun.setdefault(rid, []).append([mat, amt, docs, False])
        rest = debits.filter(~pl.col("bank_txn_id").is_in(list(used)) & pl.col("txt").str.contains(r"CF\d{9,}"))
        todo = []
        for t, d, amt, txt in zip(rest["bank_txn_id"].to_list(), rest["booking_date"].to_list(), rest["amt"].to_list(), rest["txt"].to_list()):
            m = _RUN.search(txt)
            n = _COUNT.search(txt)
            if m and m.group(1) in byrun:
                todo.append((int(n.group(1)) if n else 0, d, t, m.group(1), amt))
        for n, d, t, rid, amt in sorted(todo):
            free = [g for g in byrun[rid] if not g[3]]
            sel = None
            for back in (10, 30, 400):      # vencimientos en [cargo-back, cargo]; se amplía si no cuadra
                pool = [g for g in free if g[0] is None or 0 <= (d - g[0]).days <= back] if back < 400 else free
                if n and len(pool) >= n:
                    sel = _subset(pool, n, amt, d)
                if sel:
                    break
            if sel:
                for g in sel:
                    g[3] = True
                out[t] = [x for g in sel for x in alloc_of(g[2])]; used.add(t)

    sh["by_txn"] = out
    log.info("docs_ap: %d filas de run, %d cargos casados", rows.height, len(out))


def _name_hit(txt: str | None, name: str | None) -> int:
    """1 si la primera palabra significativa del beneficiario aparece en el texto del cargo."""
    toks = [w for w in re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split() if len(w) >= 4]
    up = (txt or "").upper()
    return int(any(w in up for w in toks[:2]))


def _subset(pool: list, n: int, amt: int, booking: dt.date) -> list | None:
    """n grupos de `pool` con suma == amt (meet-in-the-middle sobre (cuenta, suma)). Si hay varias soluciones,
    la de vencimientos más cercanos al cargo; si no hay ninguna, None. Pool grande (> 28): no se intenta."""
    k = len(pool)
    if n == 1:      # un solo beneficiario: el que tenga exactamente ese importe (si hay varios, el de vencimiento más cercano)
        hit = [g for g in pool if g[1] == amt]
        return [min(hit, key=lambda g: abs((booking - g[0]).days) if g[0] else 99)] if hit else None
    if n < 1 or n > k or k > 28:
        return None
    if n == k:
        return pool if sum(g[1] for g in pool) == amt else None
    h = k // 2
    left, right = pool[:h], pool[h:]

    def subsets(items):
        out = [(0, 0, 0)]                                  # (mascara, cuenta, suma)
        for i, g in enumerate(items):
            out += [(m | (1 << i), c + 1, s_ + g[1]) for m, c, s_ in out]
        return out

    idx: dict[tuple[int, int], list[int]] = {}
    for m, c, s_ in subsets(right):
        idx.setdefault((c, s_), []).append(m)
    best, best_cost, nsol = None, None, 0
    for m, c, s_ in subsets(left):
        for mr in idx.get((n - c, amt - s_), ()):
            sel = [left[i] for i in range(len(left)) if m >> i & 1] + [right[i] for i in range(len(right)) if mr >> i & 1]
            cost = sum(abs((booking - g[0]).days) if g[0] else 30 for g in sel)
            nsol += 1
            if best is None or cost < best_cost:
                best, best_cost = sel, cost
        if nsol > 50:
            break
    return best


def resolve_batch(ctx) -> dict[str, Proposal]:
    out = {}
    for t, rows in ctx.shared.get(NAME, {}).get("by_txn", {}).items():
        allocs, seen, miss = [], set(), 0
        for item, cents in rows:
            if item is None or item in seen:
                miss += 1
                continue
            seen.add(item)
            allocs.append(Alloc(item, cents))
        if allocs:
            out[t] = Proposal(t, allocs, confidence=0.95 if not miss else 0.7, resolver=NAME,
                              reason=f"payment_run {len(rows)} docs, {miss} sin partida")
    return out
