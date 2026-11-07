"""Construcción del contexto de una fase (ctx). Dueño: P1.

Una fase = movimientos del periodo + partidas abiertas al inicio + partidas emitidas en el periodo
+ documentos (remesas, tarjetas, cheques, payment runs, avisos, PDFs) + maestros.

Tabla unificada de partidas (ctx.items), una fila por item_id:
  item_id, item_type (ARI/ARC/API/APC), entity, party_id, party_name, document_number, issue_date, due_date,
  currency, total_cents, open_cents, payment_terms, po, payment_method, related_document, reason
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from bankrec import config, load
from bankrec.ledger import open_at

log = logging.getLogger("bankrec")

ITEM_COLS = ["item_id", "item_type", "entity", "party_id", "party_name", "document_number", "issue_date", "due_date",
             "currency", "total_cents", "payment_terms", "po", "payment_method", "related_document", "reason"]


@dataclass
class Phase:
    name: str
    folder: str
    dir: Path                      # carpeta de la fase (docs: dir / "direct_debit_batches", etc.)
    start: dt.date
    end: dt.date
    txns: pl.DataFrame             # movimientos ordenados por (booking_date, bank_txn_id)
    items: pl.DataFrame            # partidas candidatas con open_cents al inicio
    golden: pl.DataFrame | None    # solo sample/val
    shared: dict = field(default_factory=dict)   # índices de cada resolver: ctx.shared[NAME]

    def master(self, name: str) -> pl.DataFrame:
        return load.master(name, self.name)

    def docs(self, sub: str, pattern: str = "*.csv") -> pl.DataFrame:
        """Documentos de la fase: direct_debit_batches, card_settlements, cheque_deposits, payment_runs..."""
        return load.read_folder(self.dir / sub, pattern)


def _items_of(folder: str) -> pl.DataFrame:
    parts = []
    ar = load.table(folder, "ar_invoices")
    if ar is not None:
        parts.append(ar.select(
            "item_id", pl.lit("ARI").alias("item_type"), "entity", pl.col("customer_id").alias("party_id"),
            pl.col("customer_name").alias("party_name"), "document_number", "issue_date", "due_date", "currency",
            pl.col("total_amount_cents").alias("total_cents"), "payment_terms", pl.col("customer_po").alias("po"), "payment_method",
            pl.lit(None, pl.String).alias("related_document"), pl.lit(None, pl.String).alias("reason")))
    ap = load.table(folder, "ap_invoices")
    if ap is not None:
        parts.append(ap.select(
            "item_id", pl.lit("API").alias("item_type"), "entity", pl.col("supplier_id").alias("party_id"),
            pl.col("supplier_name").alias("party_name"), pl.col("supplier_invoice_number").alias("document_number"),
            "issue_date", "due_date", "currency", pl.col("total_amount_cents").alias("total_cents"), "payment_terms", pl.col("our_po").alias("po"),
            "payment_method", pl.lit(None, pl.String).alias("related_document"), pl.lit(None, pl.String).alias("reason")))
    cn = load.table(folder, "credit_notes")
    if cn is not None:
        parts.append(cn.select(
            "item_id", pl.col("item_id").str.slice(0, 3).alias("item_type"), "entity", "party_id", "party_name",
            "document_number", "issue_date", pl.lit(None, pl.Date).alias("due_date"), "currency", pl.col("total_amount_cents").alias("total_cents"),
            pl.lit(None, pl.String).alias("payment_terms"), pl.lit(None, pl.String).alias("po"),
            pl.lit(None, pl.String).alias("payment_method"), "related_document", "reason"))
    return pl.concat(parts, how="vertical_relaxed").select(ITEM_COLS)


def window(name: str, months: int | None = None) -> tuple[dt.date, dt.date]:
    """[inicio, fin) de los movimientos de la fase; months recorta a los N primeros meses."""
    _, start_s, end_s = config.PHASES[name]
    start, end = dt.date.fromisoformat(start_s), dt.date.fromisoformat(end_s)
    if months:
        y, m = divmod(start.month - 1 + months, 12)
        end = min(end, dt.date(start.year + y, m + 1, 1))
    return start, end


def build(name: str, months: int | None = None) -> Phase:
    folder = config.PHASES[name][0]
    start, end = window(name, months)
    txns_all = load.table(folder, "bank_transactions")
    if txns_all is None:
        raise FileNotFoundError(f"No hay {config.folder_dir(folder)}/bank_transactions.csv.gz (¿BANKREC_DATA?)")
    txns = (txns_all.filter((pl.col("booking_date") >= start) & (pl.col("booking_date") < end))
            .sort("booking_date", "bank_txn_id"))
    golden = load.table(folder, "golden_allocations")
    items_here = _items_of(folder)

    if name == "sample":
        items = items_here.with_columns(pl.col("total_cents").alias("open_cents"))
    elif name == "val":
        before = open_at(items_here, golden, txns_all, start)
        issued = items_here.filter((pl.col("issue_date") >= start) & (pl.col("issue_date") < end))
        items = pl.concat([before, issued.with_columns(pl.col("total_cents").alias("open_cents"))])
    else:
        # live/final: saldo abierto del fichero oficial + atributos de las fases anteriores
        oi = load.table(folder, "open_items_at_start")
        prior = pl.concat([_items_of(f) for f in config.PRIOR_FOLDERS[name]], how="vertical_relaxed")
        before = (oi.select("item_id", pl.col("open_amount_cents").alias("oc"))
                  .join(prior, on="item_id", how="left")
                  .with_columns(pl.col("oc").alias("open_cents")).drop("oc"))
        missing = before.filter(pl.col("entity").is_null()).height
        if missing:
            log.warning("%d partidas abiertas sin atributos en fases previas", missing)
        items = pl.concat([before.select(ITEM_COLS + ["open_cents"]),
                           items_here.with_columns(pl.col("total_cents").alias("open_cents"))])

    if golden is not None:
        golden = golden.filter(pl.col("bank_txn_id").is_in(txns["bank_txn_id"].implode()))
    if name not in ("sample", "val"):
        golden = None
    items = items.unique("item_id", keep="first")
    log.info("fase %s: %d movimientos, %d partidas (%s..%s)", name, txns.height, items.height, start, end)
    return Phase(name, folder, config.folder_dir(folder), start, end, txns, items, golden)
