"""Extracción de texto de los PDFs a parquet. Dueño: P3.

Qué hace: recorre <fase>/pdfs/index.csv (y remittance_advices/ en PDF), extrae el texto con pymupdf y lo guarda
en caché parquet (file, document_type, document_number, entity, party_id, document_date, text).
Entradas: ctx.dir. Salida: ctx.shared["pdf_text"] = DataFrame (o nada si aún no está hecho).
Multiproceso: worker a nivel de módulo, pasar rutas (no DataFrames) y guarda if __name__ == "__main__".
Rutas: ctx.dir / fila["file"] con pathlib; respetar mayúsculas.

TODO(P3): extraer, cachear y medir tiempo en LIVE (~miles de PDFs).
"""
from __future__ import annotations

NAME = "pdf_text"


def prepare(ctx) -> None:
    return None
