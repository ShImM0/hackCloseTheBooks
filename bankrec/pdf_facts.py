"""Hechos de los PDFs que no están en ningún CSV. Dueño: P3.

Qué hace: a partir de ctx.shared["pdf_text"] detecta por documento (regex en ES/EN, que son los idiomas que
aparecen en los PDFs): plazos, retención de garantía, anticipo a deducir, "anula y sustituye a X", mes de
servicio, rappel compensado/pagado, abono de proveedor reembolsado/compensado, anulación total y aviso de IBAN nuevo.
Entradas: ctx. Salida: ctx.shared["pdf_facts"] = DataFrame
(document_type, document_number, entity, party_id, document_date, fact, value).

Hechos (value, importes en céntimos):
- instalment_plan  "n|d1/d2/..|c1;c2;.."   plazos de la factura (todos menos el último son PARTIAL)
- retention        "pct|retenido|fecha_liberación|a_pagar"   retención de garantía (el primer cobro es PARTIAL)
- advance          "proforma|anticipo|neto"   anticipo a deducir (el cobro es PARTIAL)
- replaces         número de la factura anulada y sustituida
- cancels_invoice  (abono) número de la factura anulada por completo
- service_month    "YYYY-MM" del mes de servicio de una cuota de mantenimiento
- rebate           "offset" | "paid"   rappel (abono ARC) compensado en el siguiente cobro o pagado por transferencia
- supplier_cn      "offset" | "refund"   abono de proveedor (APC) compensado o reembolsado
- new_iban         "iban_nuevo|iban_anterior|fecha|pay|dd"   aviso de cuenta (pay = pagos desde, dd = recibos a cargar)
- proforma_total   total del pedido de una proforma (céntimos)
"""
from __future__ import annotations

import logging
import re
import sys

import polars as pl

NAME = "pdf_facts"
log = logging.getLogger("bankrec")

SCHEMA = {"document_type": pl.String, "document_number": pl.String, "entity": pl.String, "party_id": pl.String,
          "document_date": pl.Date, "fact": pl.String, "value": pl.String}

MESES = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
         "september": 9, "october": 10, "november": 11, "december": 12,
         "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
         "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}

AMT = r"[\d][\d.,\s ]*\d"            # importe en cualquier formato regional
CUR = r"[A-Z]{3}"
RE_INSTAL = re.compile(rf"(\d+)\s+(?:instalments|vencimientos)\s+(?:at|a)\s+([\d/]+)\s+(?:days|días)\s*\(([^)]*?)\s*{CUR}\)")
RE_RETEN = re.compile(rf"(?:Retention|Retención en garantía del)\s+(\d+(?:[.,]\d+)?)\s*%\s*\(({AMT})\s*{CUR}\)"
                      rf"[^()]*\((\d\d/\d\d/\d{{4}})\)[^:]*:\s*({AMT})\s*{CUR}")
RE_ADV = re.compile(rf"(?:advance received against proforma|anticipo recibido según proforma)\s+(\S+?)\s*(?::|de)\s*"
                    rf"({AMT})\s*{CUR}\.\s*(?:Net amount due|Importe neto a pagar)\s*({AMT})")
RE_REPL = re.compile(r"(?:cancels and replaces invoice|anula y sustituye a la factura)\s+(\S+)", re.I)
RE_CANCEL = re.compile(r"Abono por anulación total de la factura\s+(\S+?)\.?(?:\s|$)")
RE_MONTH = re.compile(r"(?:for the month of|correspondiente a)\s+([A-Za-zñÑ]+)\s+(20\d\d)\.")
RE_NOTICE = re.compile(r"(?:from|a partir del)\s+(\d\d/\d\d/\d{4})\s+(.*?)\s*(?:account|cuenta)\s+([A-Z]{2}\d{2}[A-Z0-9]{8,30})"
                       r"\s*,?\s*\(?(?:previously|en sustitución de la anterior)\s+([A-Z]{2}\d{2}[A-Z0-9]{8,30})", re.S)
RE_PROF = re.compile(rf"(?:Order total|Importe total del pedido:?)\s*({AMT})\s*{CUR}")


def to_cents(s: str) -> int:
    """'1.941,76' / '1,941.76' / '10 582,82' -> 194176 (decimales = últimos 2 dígitos tras el último separador)."""
    s = re.sub(r"[\s ]", "", s)
    m = re.search(r"[.,](\d{2})$", s)
    ent = re.sub(r"[.,]", "", s[: m.start()] if m else s)
    return int(ent) * 100 + (int(m.group(1)) if m else 0)


def _date(s: str) -> str:
    d, m, y = s.split("/")
    return f"{y}-{m}-{d}"


def facts_of(doc_type: str, text: str) -> list[tuple[str, str]]:
    """Hechos (fact, value) de un documento a partir de su texto."""
    t = re.sub(r"[ \t]*\n[ \t]*", " ", text)   # las frases se parten en varias líneas
    out: list[tuple[str, str]] = []
    if doc_type == "invoice":
        if m := RE_INSTAL.search(t):
            imps = [to_cents(x) for x in m.group(3).split("/")]
            out.append(("instalment_plan", f"{m.group(1)}|{m.group(2)}|{';'.join(map(str, imps))}"))
        if m := RE_RETEN.search(t):
            out.append(("retention", f"{m.group(1).replace(',', '.')}|{to_cents(m.group(2))}|{_date(m.group(3))}|{to_cents(m.group(4))}"))
        if m := RE_ADV.search(t):
            out.append(("advance", f"{m.group(1)}|{to_cents(m.group(2))}|{to_cents(m.group(3))}"))
        if m := RE_REPL.search(t):
            out.append(("replaces", m.group(1).rstrip(".,")))
        if m := RE_MONTH.search(t):
            mes = MESES.get(m.group(1).lower())
            if mes:
                out.append(("service_month", f"{m.group(2)}-{mes:02d}"))
    elif doc_type == "credit_note":
        if m := RE_CANCEL.search(t):
            out.append(("cancels_invoice", m.group(1)))
        if re.search(r"(?i)rappel|volume rebate", t):
            paid = re.search(r"(?i)paid to your bank account|se abonar[aá] por transferencia", t)
            offset = re.search(r"(?i)offset against your next payment|se compensar[aá]", t)
            if paid or offset:
                out.append(("rebate", "paid" if paid else "offset"))
    elif doc_type == "supplier_credit_note":
        if re.search(r"(?i)refunded by bank transfer|reembolsado mediante transferencia", t):
            out.append(("supplier_cn", "refund"))
        elif re.search(r"(?i)deducted from your next payment|se compensará en sus próximos pagos", t):
            out.append(("supplier_cn", "offset"))
    elif doc_type == "proforma":
        if m := RE_PROF.search(t):
            out.append(("proforma_total", str(to_cents(m.group(1)))))
    elif doc_type == "bank_account_notice":
        if m := RE_NOTICE.search(t):
            kind = "dd" if re.search(r"(?i)recibos domiciliados|direct debit", m.group(2)) else "pay"
            out.append(("new_iban", f"{m.group(3)}|{m.group(4)}|{_date(m.group(1))}|{kind}"))
    return out


def build_facts(texts: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for r in texts.select("document_type", "document_number", "entity", "party_id", "document_date", "text").iter_rows():
        for fact, value in facts_of(r[0], r[5] or ""):
            rows.append((r[0], r[1], r[2], r[3], r[4], fact, value))
    return pl.DataFrame(rows, schema=SCHEMA, orient="row")


def prepare(ctx) -> None:
    texts = ctx.shared.get("pdf_text")
    if texts is None:
        return
    ctx.shared[NAME] = build_facts(texts)


if __name__ == "__main__":
    # uso: uv run python -m bankrec.pdf_facts train  (resumen de hechos detectados)
    sys.stdout.reconfigure(encoding="utf-8")
    from bankrec import pdf_text
    for f in sys.argv[1:] or ["sample"]:
        tx = pdf_text.folder_text(f)
        fa = build_facts(tx)
        print(f, tx.height, "PDFs")
        print(fa["fact"].value_counts(sort=True))
        print(fa.group_by("document_type").len())
