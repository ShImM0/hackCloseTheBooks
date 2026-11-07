"""Carga de CSV a polars con caché parquet en <data_dir>/_cache. Dueño: P1.

Reglas:
- Todo se lee como texto (infer_schema=False): así no se pierden ceros a la izquierda (cuentas, PO, códigos).
- Solo se tipan las columnas de importe (-> *_cents Int64, la original se conserva) y de fecha (-> Date).
- Se ignoran los ficheros AppleDouble "._*" que vienen en los zips.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl

from bankrec import config

AMOUNT_COLS = {
    "amount", "total_amount", "net_amount", "tax_amount", "open_amount", "orig_amount", "batch_total",
    "gross", "fee", "net", "deposit_total", "transfer_amount", "document_amount",
}
DATE_COLS = {
    "booking_date", "value_date", "issue_date", "due_date", "collection_date", "settlement_date",
    "operation_date", "deposit_date", "execution_date", "received_date", "document_date", "date",
}


def _typed(df: pl.DataFrame) -> pl.DataFrame:
    exprs = []
    for c in df.columns:
        if c in AMOUNT_COLS:
            exprs.append((pl.col(c).str.strip_chars().cast(pl.Float64, strict=False) * 100).round().cast(pl.Int64).alias(f"{c}_cents"))
        elif c in DATE_COLS:
            exprs.append(pl.col(c).str.to_date("%Y-%m-%d", strict=False))
    return df.with_columns(exprs) if exprs else df


def read_csv(path: Path | str) -> pl.DataFrame:
    """CSV/CSV.GZ -> DataFrame tipado, cacheado en parquet (se regenera si el CSV cambia)."""
    path = Path(path)
    st = path.stat()
    key = hashlib.md5(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:12]
    cached = config.cache_dir() / f"{path.name}.{key}.parquet"
    if cached.exists():
        return pl.read_parquet(cached)
    df = _typed(pl.read_csv(path, infer_schema=False, encoding="utf8"))
    df.write_parquet(cached)
    return df


def read_folder(folder: Path, pattern: str = "*.csv") -> pl.DataFrame:
    """Concatena los CSV de una carpeta (p. ej. direct_debit_batches/) añadiendo source_file."""
    files = sorted(p for p in Path(folder).rglob(pattern) if not p.name.startswith("._") and p.name != "index.csv")
    if not files:
        return pl.DataFrame()
    dfs = [pl.read_csv(p, infer_schema=False, encoding="utf8").with_columns(pl.lit(p.name).alias("source_file")) for p in files]
    return _typed(pl.concat(dfs, how="diagonal"))


def master(name: str, phase: str = "live") -> pl.DataFrame:
    """Maestro (customers, suppliers, bank_accounts, categories, payment_terms, fx_rates, entities)."""
    p = config.masters_dir(phase) / f"{name}.csv"
    if not p.exists():   # masters_final puede no traer todos los maestros: se usa el del repo
        p = config.DOCS_DIR / "masters" / f"{name}.csv"
    return read_csv(p)


def table(folder: str, name: str) -> pl.DataFrame | None:
    """Tabla grande de una carpeta de fase: bank_transactions, ar_invoices, ap_invoices, credit_notes,
    open_items_at_start, golden_allocations. None si no existe."""
    p = config.folder_dir(folder) / f"{name}.csv.gz"
    return read_csv(p) if p.exists() else None
