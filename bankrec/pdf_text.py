"""Extracción de texto de los PDFs a parquet. Dueño: P3.

Qué hace: recorre <carpeta>/pdfs/index.csv (invoice, credit_note, supplier_credit_note, proforma,
bank_account_notice; se omiten las facturas de proveedor, que no aportan hechos) y extrae el texto con pymupdf.
Caché parquet por carpeta física (train/live/final/sample) en config.cache_dir(); se recalcula si cambia el índice.
Los avisos de remesa en PDF (remittance_advices/) son de P2 (docs_remit) y no se tocan aquí.

Entradas: ctx.dir (+ carpetas previas de config.PRIOR_FOLDERS).
Salida: ctx.shared["pdf_text"] = DataFrame (folder, file, document_type, document_number, entity, party_id,
document_date, text), con los documentos fechados antes del fin de la fase.
"""
from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

from bankrec import config

NAME = "pdf_text"
log = logging.getLogger("bankrec")

TIPOS = ["invoice", "credit_note", "supplier_credit_note", "proforma", "bank_account_notice"]
SCHEMA = {"folder": pl.String, "file": pl.String, "document_type": pl.String, "document_number": pl.String,
          "entity": pl.String, "party_id": pl.String, "document_date": pl.Date, "text": pl.String}


def _extract(path_str: str) -> str:
    """Worker a nivel de módulo (spawn): texto de un PDF, páginas unidas con salto de línea."""
    import pymupdf
    try:
        with pymupdf.open(path_str) as doc:
            return "\n".join(p.get_text() for p in doc)
    except Exception:
        return ""


def _index(folder_dir: Path) -> pl.DataFrame | None:
    idx = folder_dir / "pdfs" / "index.csv"
    if not idx.exists():
        return None
    df = pl.read_csv(idx, infer_schema=False, encoding="utf8")
    df = df.filter(pl.col("document_type").is_in(TIPOS) & ~pl.col("file").str.contains(r"(^|/)\._"))
    return df.with_columns(pl.col("document_date").str.to_date("%Y-%m-%d", strict=False))


def folder_text(folder: str, workers: int | None = None) -> pl.DataFrame:
    """Texto de todos los PDFs relevantes de una carpeta física, con caché parquet."""
    fdir = config.folder_dir(folder)
    idx = _index(fdir)
    if idx is None:
        return pl.DataFrame(schema=SCHEMA)
    cache = config.cache_dir() / f"pdf_text_{folder}.parquet"
    if cache.exists():
        df = pl.read_parquet(cache)
        if df.height == idx.height:
            return df
    t0 = time.perf_counter()
    paths = [str(fdir / "pdfs" / f) for f in idx["file"].to_list()]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        texts = list(ex.map(_extract, paths, chunksize=64))
    df = idx.select(pl.lit(folder).alias("folder"), "file", "document_type", "document_number", "entity",
                    "party_id", "document_date").with_columns(pl.Series("text", texts, dtype=pl.String))
    df.write_parquet(cache)
    log.info("pdf_text %s: %d PDFs en %.1f s", folder, df.height, time.perf_counter() - t0)
    return df


def prepare(ctx) -> None:
    folders = list(config.PRIOR_FOLDERS[ctx.name]) + [ctx.folder]
    parts = [folder_text(f) for f in folders]
    df = pl.concat(parts, how="vertical_relaxed")
    ctx.shared[NAME] = df.filter(pl.col("document_date").is_null() | (pl.col("document_date") < ctx.end))


if __name__ == "__main__":
    # uso: uv run python -m bankrec.pdf_text train live  (precalienta la caché)
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    for f in sys.argv[1:] or ["sample"]:
        print(f, folder_text(f).height)
