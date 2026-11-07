"""Pagos a proveedores (payment_runs/: transfers, aggregated, confirming). Dueño: P2.

Qué hace (POLICIES §5.3: un cargo agregado o de confirming liquida todos los documentos del run; un cargo por
transferencia, los de su end_to_end_id; las notas de crédito compensadas van en su fila, positivas):
- transfers: cargo con end_to_end_id de un run -> sus documentos. Si el banco no trae el e2e (N43, MT940), por
  (cuenta, importe == transferencia, fecha de ejecución..+7 días) y, con empate, por el nombre del beneficiario.
  Transferencia en otra divisa que la cuenta (§3 FX_DIFFERENCE): por el importe original del banco
  (orig_amount/orig_currency == importe y divisa de la transferencia).
- aggregated: un cargo por el total del run en la cuenta y fecha del run. Las transferencias en otra divisa que la
  cuenta se convierten con fx_rates y el cargo debe caer en ±3 % de esa parte (§3); con varios candidatos decide el
  run_id citado en el texto ("BULK PAYMENT PR…", "FICHERO C34 PR…").
- confirming: texto "REMESA CF… N PROVEEDORES": un cargo por vencimiento; los beneficiarios del run cuyo vencimiento
  cae entre el cargo anterior del run y este (exactamente N, sumando el cargo) y, si no, N que sumen el cargo.
El run_id se repite entre ficheros de entidades distintas: un run es (run_id, cuenta deudora).
Fila del run -> partida: nº de documento normalizado + tipo (INVOICE->API, CREDIT_NOTE->APC); si hay varias, el
proveedor del IBAN o del nombre del beneficiario, el importe del documento, la divisa y la entidad.
Importe de la fila = |document_amount|.
- reembolso del proveedor (§5.9, resolve): abono por el saldo exacto de una APC abierta, con tercero por IBAN/nombre
  o por el nº de abono/factura citado.
- transferencia devuelta (§2 RETURNED): abono = importe de una transferencia anterior de la misma cuenta, con su e2e
  en el texto o el beneficiario y un motivo de devolución ISO (AC01, AC04, BE04, MD07…) -> sus documentos
  reabiertos como RETURNED. Sin motivo, el nombre solo no basta (reembolsos del proveedor: SUPPLIER_REFUND / APC).
Excepciones (PARTIAL, FX_DIFFERENCE…): las decide exceptions.finalize, salvo RETURNED.
Entradas: ctx (bankrec.phase.Phase). Salida: dict bank_txn_id -> Proposal (resolve_batch).
"""
from __future__ import annotations

import bisect
import datetime as dt
import logging
import re

import polars as pl

from bankrec import config, load, parties, refs
from bankrec.contract import Alloc, Proposal

NAME = "docs_ap"
log = logging.getLogger("bankrec")

_RUN = re.compile(r"\b(CF\d{9,})\b")
_COUNT = re.compile(r"CF\d{9,}\s+(\d+)\s+PROVEEDOR", re.I)   # "REMESA CF… 8 PROVEEDORES"
_LOOKBACK = dt.timedelta(days=150)   # los cargos de confirming/agregados citan runs de semanas antes
_LOOKAHEAD = dt.timedelta(days=45)  # confirming: la remesa se genera antes del cargo, pero su fecha puede ser posterior
_NAME_MIN = 85                      # parecido de nombre mínimo (0-100), igual que amount_match
_MARGIN = 5
_FACTORY = {"ES00": ("ES00", "ES01", "ES02", "PT01")}   # §5.2: ES00 cobra/paga por sus filiales
_RETURN_DAYS = 45                   # una transferencia rechazada vuelve en días o semanas
# motivo ISO 20022 de una transferencia devuelta (AC01 cuenta errónea, AC04 cerrada, BE04, MD07...): sin el e2e en el
# texto, el nombre del beneficiario solo basta con un motivo de devolución (si no, suele ser un reembolso del proveedor)
_RET_CODE = re.compile(r"\b(?:AC|AG|AM|BE|MD|MS|RC|RR|FF|DT|SL)\d{2}\b")
_SLACK = 7                          # días entre la ejecución y el cargo en banco
_CF_DAYS = 3                        # confirming: vencimiento en fin de semana/festivo -> cargo el siguiente día hábil
_FX_BAND = 0.03                     # §3: el cambio del banco queda "well within the 3 % FX tolerance"


def _norm(col: str) -> pl.Expr:
    return pl.col(col).str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


class _Fx:
    """Cambio de referencia de fx_rates.csv (unidades por EUR; fin de semana/festivo: último disponible)."""

    def __init__(self, fx: pl.DataFrame):
        self.r: dict[str, tuple[list, list]] = {}
        for cur, g in fx.sort("date").group_by("currency", maintain_order=True):
            self.r[cur[0]] = (g["date"].to_list(), [float(x) for x in g["units_per_eur"].to_list()])

    def rate(self, cur: str, date) -> float | None:
        if cur == "EUR":
            return 1.0
        ds, vs = self.r.get(cur, ([], []))
        i = bisect.bisect_right(ds, date) - 1
        return vs[i] if i >= 0 else (vs[0] if vs else None)

    def convert(self, cents: int, src: str, dst: str, date) -> float:
        a, b = self.rate(src, date), self.rate(dst, date)
        return cents / a * b if a and b else float("nan")


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


def _norm_names(s: pl.Series) -> pl.DataFrame:
    """Nombre -> nombre normalizado (parties.norm_name), calculado una vez por valor distinto."""
    u = s.drop_nulls().unique()
    return pl.DataFrame({"nm": u, "nb": [parties.norm_name(x) for x in u.to_list()]}, schema={"nm": pl.String, "nb": pl.String})


def _map_items(rows: pl.DataFrame, ctx) -> pl.DataFrame:
    """Añade item_id a cada fila del run (nulo si no se puede decidir).

    Candidatas: partidas AP con el mismo nº de documento normalizado y tipo (INVOICE->API, CREDIT_NOTE->APC). Se elige
    por orden lexicográfico: IBAN del beneficiario = IBAN del proveedor > nombre del beneficiario = nombre del proveedor
    (maestro o impreso en la factura; los runs de fuera de SEPA no traen IBAN y los nº de factura de proveedor se repiten
    entre proveedores) > importe (total o saldo == importe del documento) > divisa de la fila == divisa de la partida >
    partida abierta > entidad de la cuenta. Empate en lo más alto: no se decide."""
    sup_m = ctx.master("suppliers")
    sup = sup_m.select(pl.col("iban").alias("beneficiary_iban"), pl.col("supplier_id")).drop_nulls().unique()
    acc = ctx.master("bank_accounts").select(pl.col("account_id").alias("debtor_account_id"), pl.col("entity").alias("acc_entity"))
    items = (ctx.items.filter(pl.col("item_type").is_in(["API", "APC"]))
             .select("item_id", "item_type", "entity", "party_id", "party_name", pl.col("currency").alias("icur"), "due_date",
                     "total_cents", "open_cents", _norm("document_number").alias("dn")))
    r = (rows.with_row_index("rid")
         .with_columns(_norm("document_number").alias("dn"),
                       pl.when(pl.col("document_type") == "CREDIT_NOTE").then(pl.lit("APC")).otherwise(pl.lit("API")).alias("item_type"),
                       pl.col("document_amount_cents").abs().alias("doc_cents"))
         .join(acc, on="debtor_account_id", how="left")
         # cuentas secundarias ("IT01-VALD-EUR-2") no están en el maestro: la entidad es el prefijo del id
         .with_columns(pl.coalesce("acc_entity", pl.col("debtor_account_id").str.split("-").list.first()).alias("acc_entity")))
    # nombres normalizados: beneficiario de la fila y nombres conocidos de cada proveedor
    pnames = pl.concat([sup_m.select(pl.col("supplier_id").alias("party_id"), pl.col("name").alias("nm")),
                        items.select("party_id", pl.col("party_name").alias("nm"))]).drop_nulls().unique()
    pnames = pnames.join(_norm_names(pnames["nm"]), on="nm").select("party_id", "nb").filter(pl.col("nb") != "").unique()
    r = r.join(_norm_names(r["beneficiary"]).rename({"nm": "beneficiary", "nb": "ben_nb"}), on="beneficiary", how="left")
    c = (r.select("rid", "dn", "item_type", "doc_cents", "acc_entity", "beneficiary_iban", "ben_nb", pl.col("currency").alias("rcur"))
         .join(items, on=["dn", "item_type"]))
    sup_by_iban = sup.rename({"supplier_id": "party_id"})
    c = (c.join(sup_by_iban.with_columns(pl.lit(1).alias("s_iban")), on=["beneficiary_iban", "party_id"], how="left")
         .join(pnames.rename({"nb": "ben_nb"}).with_columns(pl.lit(1).alias("s_name")), on=["party_id", "ben_nb"], how="left")
         .with_columns(pl.col("s_iban").fill_null(0), pl.col("s_name").fill_null(0),
                       ((pl.col("total_cents") == pl.col("doc_cents")) | (pl.col("open_cents") == pl.col("doc_cents"))).cast(pl.Int32).alias("s_amt"),
                       (pl.col("icur") == pl.col("rcur")).fill_null(False).cast(pl.Int32).alias("s_cur"),
                       (pl.col("open_cents") > 0).cast(pl.Int32).alias("s_open"),
                       (pl.col("entity") == pl.col("acc_entity")).fill_null(False).cast(pl.Int32).alias("s_ent"))
         .with_columns((pl.col("s_iban") * 32 + pl.col("s_name") * 16 + pl.col("s_amt") * 8 + pl.col("s_cur") * 4
                        + pl.col("s_open") * 2 + pl.col("s_ent")).alias("score")))
    best = c.group_by("rid").agg(pl.col("score").max().alias("best"), pl.len().alias("n"))
    c = (c.join(best, on="rid").filter(pl.col("score") == pl.col("best")))
    # empate real entre dos partidas con la misma puntuación: no se decide
    uniq = c.group_by("rid").agg(pl.len().alias("k"), pl.col("item_id").first(), pl.col("due_date").first())
    uniq = uniq.filter(pl.col("k") == 1).select("rid", "item_id", "due_date")
    return r.join(uniq, on="rid", how="left")


def prepare(ctx) -> None:
    sh: dict = {"by_txn": {}}
    ctx.shared[NAME] = sh
    parties.prepare(ctx)
    # factura original de cada abono (related_document), para el reembolso del proveedor
    apc = ctx.items.filter((pl.col("item_type") == "APC") & pl.col("related_document").is_not_null())
    sh["rel"] = dict(zip(apc["item_id"].to_list(), apc["related_document"].to_list()))
    runs = _runs(ctx)
    if runs.is_empty():
        return
    rows = _map_items(runs, ctx)
    # grupos de transferencia (un e2e = un beneficiario). El run_id se repite entre ficheros de entidades distintas:
    # la clave de un run es (run_id, cuenta deudora) -> rk
    grp = (rows.group_by("run_id", "end_to_end_id", "debtor_account_id")
           .agg(pl.col("run_type").first(), pl.col("execution_date").first(),
                pl.col("transfer_amount_cents").first(), pl.col("currency").first().alias("rcur"), pl.col("beneficiary").first(),
                pl.col("due_date").max().alias("maturity"),
                pl.struct("item_id", "doc_cents").alias("docs"))
           .with_columns(pl.concat_str([pl.col("run_id"), pl.col("debtor_account_id")], separator="@").alias("rk")))
    debits = ctx.txns.filter(pl.col("amount_cents") < 0).select(
        "bank_txn_id", "account_id", "booking_date", "currency", pl.col("amount_cents").neg().alias("amt"), "end_to_end_id",
        pl.col("orig_amount_cents").abs().alias("oamt"), pl.col("orig_currency").alias("ocur"),
        pl.concat_str([pl.col("text"), pl.col("ref1"), pl.col("ref2")], separator=" ", ignore_nulls=True).alias("txt"),
        # texto normalizado para buscar el run_id (N43 parte el concepto en trozos de 38 unidos por " | ")
        pl.concat_str([pl.col("text"), pl.col("ref1"), pl.col("ref2"), pl.col("remittance_info")], separator=" ", ignore_nulls=True)
        .str.replace_all(" | ", "", literal=True).str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "").alias("tn"))

    def alloc_of(docs) -> list[tuple[str | None, int]]:
        return [(d["item_id"], d["doc_cents"]) for d in docs]

    used: set[str] = set()
    out: dict[str, list] = {}

    # 1) transfers por (cuenta, end_to_end_id): el e2e se repite entre cuentas y ficheros -> fecha de ejecución..+7
    tr = grp.filter(pl.col("run_type") == "transfers")
    j1 = (tr.select("rk", "end_to_end_id", "debtor_account_id", "execution_date", "docs")
          .join(debits.filter(pl.col("end_to_end_id").is_not_null()), left_on=["debtor_account_id", "end_to_end_id"],
                right_on=["account_id", "end_to_end_id"])
          .with_columns((pl.col("booking_date") - pl.col("execution_date")).dt.total_days().alias("lag"))
          .filter(pl.col("lag").is_between(0, _SLACK)).sort("lag"))
    used_tr: set[tuple] = set()
    for t, rk, e2e, docs in zip(j1["bank_txn_id"].to_list(), j1["rk"].to_list(), j1["end_to_end_id"].to_list(), j1["docs"].to_list()):
        if t not in used:
            out[t] = alloc_of(docs); used.add(t); used_tr.add((rk, e2e))

    # 2) transfers sin e2e en el banco: (cuenta, importe, ventana de fecha), empate -> nombre del beneficiario.
    #    Transferencia en otra divisa que la cuenta (POLICIES §3 FX_DIFFERENCE): el banco muestra el importe original
    #    (orig_amount/orig_currency) -> se casa por (cuenta, divisa de la transferencia, importe original).
    rest = debits.filter(~pl.col("bank_txn_id").is_in(list(used)))
    trs = tr.select("rk", "end_to_end_id", "debtor_account_id", "transfer_amount_cents", "rcur", "execution_date", "beneficiary", "docs")
    cand = pl.concat([
        trs.join(rest.drop("oamt", "ocur"), left_on=["debtor_account_id", "transfer_amount_cents"], right_on=["account_id", "amt"]),
        trs.join(rest.filter(pl.col("oamt").is_not_null() & (pl.col("ocur") != pl.col("currency"))).drop("amt"),
                 left_on=["debtor_account_id", "transfer_amount_cents", "rcur"], right_on=["account_id", "oamt", "ocur"]),
    ], how="diagonal").unique(["bank_txn_id", "rk", "end_to_end_id"])
    cand = (cand.with_columns((pl.col("booking_date") - pl.col("execution_date")).dt.total_days().alias("lag"))
            .filter(pl.col("lag").is_between(0, _SLACK)))
    if cand.height:
        cand = cand.with_columns(
            pl.struct("txt", "beneficiary").map_elements(lambda s: _name_hit(s["txt"], s["beneficiary"]), return_dtype=pl.Int32).alias("nh"))
        by_txn: dict[str, list] = {}
        for t, rk, e2e, nh, docs in cand.select("bank_txn_id", "rk", "end_to_end_id", "nh", "docs").iter_rows():
            by_txn.setdefault(t, []).append((nh, rk, e2e, docs))
        for t in sorted(by_txn):
            if t in used:
                continue
            sub = by_txn[t]
            if len(sub) > 1:
                nhm = max(s[0] for s in sub)
                sub = [s for s in sub if s[0] == nhm]
                if len(sub) != 1 or nhm == 0:
                    continue
            _, rk, e2e, docs = sub[0]
            if (rk, e2e) in used_tr:      # un mismo e2e no se reparte a dos cargos
                continue
            out[t] = alloc_of(docs); used.add(t); used_tr.add((rk, e2e))

    # 3) aggregated: un cargo por el total del run (§5.3). Si el run tiene transferencias en otra divisa que la cuenta
    #    (§3 FX_DIFFERENCE), el banco las convierte con su cambio, peor que fx_rates en un 0,1-1,5 %: el cargo debe
    #    quedar dentro de ±3 % de la parte convertida (tolerancia FX de §3). Si hay varios candidatos, decide el
    #    run_id citado en el texto del cargo.
    ag = grp.filter(pl.col("run_type") == "aggregated")
    if ag.height:
        acc_cur = dict(ctx.txns.select("account_id", "currency").unique("account_id").iter_rows())
        fx = _Fx(ctx.master("fx_rates"))
        exp: dict[str, list] = {}
        for rk, rid, acc, ex, cur, amt in ag.select("rk", "run_id", "debtor_account_id", "execution_date", "rcur",
                                                    "transfer_amount_cents").iter_rows():
            acur = acc_cur.get(acc)
            if acur is None or amt is None:
                continue
            e = exp.setdefault(rk, [rid, acc, ex, 0, 0.0])
            if cur in (None, acur):
                e[3] += amt
            else:
                e[4] += fx.convert(amt, cur, acur, ex)
        bands = [(rk, rid, acc, ex, same + round(fc) - round(_FX_BAND * fc), same + round(fc) + round(_FX_BAND * fc))
                 for rk, (rid, acc, ex, same, fc) in exp.items() if fc == fc]          # fc NaN: divisa sin cambio
        if bands:
            band = pl.DataFrame(bands, schema={"rk": pl.String, "run_id": pl.String, "debtor_account_id": pl.String,
                                               "execution_date": pl.Date, "lo": pl.Int64, "hi": pl.Int64}, orient="row")
            rest = debits.filter(~pl.col("bank_txn_id").is_in(list(used)))
            j = (band.join(rest, left_on="debtor_account_id", right_on="account_id")
                 .with_columns((pl.col("booking_date") - pl.col("execution_date")).dt.total_days().alias("lag"))
                 .filter(pl.col("lag").is_between(0, _SLACK) & pl.col("amt").is_between(pl.col("lo"), pl.col("hi")))
                 .with_columns(pl.col("tn").str.contains(pl.col("run_id"), literal=True).alias("cite")))
            j = (j.join(j.group_by("rk").len("n_run"), on="rk").join(j.group_by("bank_txn_id").len("n_txn"), on="bank_txn_id")
                 .sort(["cite", "lag", "bank_txn_id"], descending=[True, False, False]))
            docs_of = {rk: [d for ds in grp_docs for d in ds] for rk, grp_docs in ag.group_by("rk").agg(pl.col("docs")).iter_rows()}
            used_runs: set[str] = set()
            for t, rk, cite, nr, nt in j.select("bank_txn_id", "rk", "cite", "n_run", "n_txn").iter_rows():
                if t in used or rk in used_runs or not (cite or (nr == 1 and nt == 1)):
                    continue
                out[t] = alloc_of(docs_of[rk]); used.add(t); used_runs.add(rk)

    # 4) confirming: "REMESA CF… N PROVEEDORES": N beneficiarios del run cuyas transferencias suman el cargo.
    #    a) por fecha: un cargo por vencimiento (§5.3; DATA_DICTIONARY "one debit per maturity date") -> los
    #       beneficiarios aún libres cuyo vencimiento (máx. de sus documentos) cae entre el cargo anterior del mismo
    #       run y este (fin de semana o festivo: se carga el siguiente día hábil; sin cargo anterior, _CF_DAYS días).
    #       Si son exactamente N y suman el cargo, son esos (vale con runs de > 28 beneficiarios, donde no se intenta
    #       el subconjunto); si son menos de N, el resto sale de vencimientos atrasados (subconjunto exacto).
    #    b) el resto: N beneficiarios cuyo importe suma el cargo (subconjunto), ampliando la ventana de vencimientos.
    #    Cada beneficiario se usa en un solo cargo: en b) se resuelven antes los cargos con menos beneficiarios.
    cf = grp.filter(pl.col("run_type") == "confirming")
    if cf.height:
        byrun: dict[tuple, list] = {}
        for rid, acc, mat, amt, docs in cf.select("run_id", "debtor_account_id", "maturity", "transfer_amount_cents", "docs").iter_rows():
            byrun.setdefault((rid, acc), []).append([mat, amt, docs, False])
        rest = debits.filter(~pl.col("bank_txn_id").is_in(list(used)) & pl.col("txt").str.contains(r"CF\d{9,}"))
        todo = []
        for t, acc, d, amt, txt in rest.select("bank_txn_id", "account_id", "booking_date", "amt", "txt").iter_rows():
            m = _RUN.search(txt)
            n = _COUNT.search(txt)
            if m and (m.group(1), acc) in byrun:
                todo.append((int(n.group(1)) if n else 0, d, t, (m.group(1), acc), amt))
        dates: dict[tuple, list] = {}
        for n, d, t, key, amt in todo:
            dates.setdefault(key, []).append(d)
        for d, n, t, key, amt in sorted((d, n, t, key, amt) for n, d, t, key, amt in todo):
            if not n:
                continue
            prev = max((x for x in dates[key] if x < d), default=d - dt.timedelta(days=_CF_DAYS))
            free = [g for g in byrun[key] if not g[3]]
            win = [g for g in free if g[0] is not None and prev < g[0] <= d]
            sel = None
            if len(win) == n and sum(g[1] for g in win) == amt:
                sel = win
            elif 0 < len(win) < n and sum(g[1] for g in win) < amt:
                # vencimientos atrasados (beneficiarios de cargos anteriores que no se pagaron entonces)
                older = [g for g in free if g[0] is None or g[0] <= prev]
                extra = _subset(older, n - len(win), amt - sum(g[1] for g in win), d) if older else None
                sel = win + extra if extra else None
            if sel:
                for g in sel:
                    g[3] = True
                out[t] = [x for g in sel for x in alloc_of(g[2])]; used.add(t)
        for n, d, t, rid, amt in sorted(todo):
            if t in used:
                continue
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

    # 5) transferencia devuelta por el banco del beneficiario (POLICIES §2 RETURNED): abono en la misma cuenta, mismo
    #    importe que una transferencia anterior y el beneficiario en el texto (o su e2e completo) -> reabre sus documentos.
    credits = ctx.txns.filter(pl.col("amount_cents") > 0).select(
        "bank_txn_id", "account_id", "booking_date", pl.col("amount_cents").alias("amt"),
        pl.concat_str([pl.col("text"), pl.col("ref1"), pl.col("ref2"), pl.col("remittance_info"), pl.col("end_to_end_id")],
                      separator=" ", ignore_nulls=True).alias("txt"))
    cand = (tr.select("end_to_end_id", "debtor_account_id", "transfer_amount_cents", "execution_date", "beneficiary", "docs")
            .join(credits, left_on=["debtor_account_id", "transfer_amount_cents"], right_on=["account_id", "amt"])
            .with_columns((pl.col("booking_date") - pl.col("execution_date")).dt.total_days().alias("lag"))
            .filter(pl.col("lag").is_between(0, _RETURN_DAYS)))
    ret: dict[str, list] = {}
    if cand.height:
        cand = cand.with_columns(
            pl.struct("txt", "beneficiary", "end_to_end_id").map_elements(
                lambda r: 2 if r["end_to_end_id"] and r["end_to_end_id"] in (r["txt"] or "")
                else _name_hit(r["txt"], r["beneficiary"]) * bool(_RET_CODE.search(r["txt"] or "")),
                return_dtype=pl.Int32).alias("hit")).filter(pl.col("hit") > 0)
        for t, grp_t in cand.group_by("bank_txn_id"):
            best = grp_t.filter(pl.col("hit") == grp_t["hit"].max())
            if best.height == 1:       # varias transferencias posibles: no se decide
                ret[t[0]] = alloc_of(best["docs"][0])
    sh["returns"] = ret
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
    for t, rows in ctx.shared.get(NAME, {}).get("returns", {}).items():
        allocs = [Alloc(i, c, "RETURNED") for i, c in dict(((i, c) for i, c in rows if i)).items()]
        if allocs and t not in out:
            out[t] = Proposal(t, allocs, confidence=0.9, resolver=NAME, reason=f"transferencia devuelta, {len(allocs)} docs")
    return out


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    """Reembolso del proveedor (POLICIES §5.9): abono bancario que liquida una nota de crédito de proveedor (APC)
    abierta por su saldo exacto. Tercero por IBAN/nombre o por el número de factura/abono citado en el texto."""
    amt = txn["amount_cents"]
    if amt <= 0:
        return None
    ents = _FACTORY.get(txn["entity"], (txn["entity"],))
    cands = [i for e in ents for i in ledger.with_amount(e, txn["currency"], amt)
             if ledger.info[i][0] == "APC" and (ledger.info[i][4] is None or ledger.info[i][4] <= txn["booking_date"])]
    if not cands:
        return None
    hay = refs.haystack(txn)
    toks = refs.tokens(hay)
    tctx = parties.context(txn)
    rel = ctx.shared[NAME]["rel"]
    scored = {}
    for c in cands:
        pid = ledger.info[c][3]
        name = parties.score(tctx, pid, ctx)
        ref = max(refs.ref_score(ledger.info[c][5], toks), refs.ref_score(rel.get(c), toks))
        scored[c] = (name, ref)
    # 1) número citado (abono o factura original) + nombre razonable; 2) nombre claro y único
    by_ref = [c for c, (n, r) in scored.items() if r >= 85 and n >= 60]
    if len(by_ref) == 1:
        c = by_ref[0]
        return Proposal(txn["bank_txn_id"], [Alloc(c, amt)], confidence=0.9, resolver=NAME, reason="reembolso proveedor: nº citado")
    ranked = sorted(((n, c) for c, (n, r) in scored.items()), reverse=True)
    best_n, best_c = ranked[0]
    if best_n >= _NAME_MIN and (len(ranked) == 1 or ranked[1][0] <= best_n - _MARGIN or ledger.info[ranked[1][1]][3] == ledger.info[best_c][3]):
        mine = sorted((c for n, c in ranked if ledger.info[c][3] == ledger.info[best_c][3]), key=ledger._fifo)
        return Proposal(txn["bank_txn_id"], [Alloc(mine[0], amt)], confidence=best_n / 100, resolver=NAME,
                        reason=f"reembolso proveedor: nombre {best_n:.0f}")
    return None
