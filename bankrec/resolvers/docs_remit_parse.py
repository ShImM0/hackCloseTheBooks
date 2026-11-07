"""Lectura de avisos de remesa (remittance_advices/): csv, xlsx, txt (correo) y pdf. Dueño: P2.

Un aviso es un dict:
  {file, payment_ref, payment_date (date|None), currency, total_cents (int|None), payer (str), rows: [Row]}
y cada Row = (document, doc_date, gross_cents, ded_cents, reason, paid_cents). Importes en céntimos con el signo del
aviso (gross < 0 = nota de crédito compensada). El formato se detecta por el contenido, no por fecha ni entidad
(POLICIES §5.4: un cliente grande cambia el layout en sep-2025).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from pathlib import Path

_NUM = r"(?:-?\d[\d.,]*\d|-?\d)"
_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})|(\d{4})-(\d{2})-(\d{2})")
_AMT = re.compile(r"(-?\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2}|-?\d+[.,]\d{2})\s*([A-Z]{3})\b")
_ROW = re.compile(r"^\s*(\S+)\s+(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2})\s+(.*)$")
_WORD = re.compile(r"[A-Z][A-Z.]*")


def _reason(rest: str) -> str:
    """Motivo de la deducción en una línea de txt/pdf: el texto que queda al quitar los importes
    ('2,463.78 -739.13 SHORT DELIVERY 1,724.65' -> 'SHORT DELIVERY'; 'PENAL. RETRASO' no se confunde con 'RET')."""
    return " ".join(_WORD.findall(re.sub(_NUM, " ", rest.upper())))


def to_cents(s) -> int | None:
    """'4.405,54' / '51,901.66' / '1086.17' / 1086.17 -> céntimos. El último separador es el decimal."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(round(float(s) * 100))
    t = str(s).strip().replace(" ", "")
    if not re.fullmatch(r"-?[\d.,]+", t):
        return None
    neg = t.startswith("-")
    t = t.lstrip("-")
    last = max(t.rfind(","), t.rfind("."))
    if last >= 0 and len(t) - last - 1 in (1, 2):
        ip, dp = t[:last], t[last + 1:]
    else:
        ip, dp = t, ""
    ip = re.sub(r"[.,]", "", ip)
    c = int(ip or 0) * 100 + int((dp + "00")[:2] or 0)
    return -c if neg else c


def to_date(s, month_first: bool = False) -> dt.date | None:
    if isinstance(s, dt.datetime):
        return s.date()
    if isinstance(s, dt.date):
        return s
    m = _DATE.search(str(s or ""))
    if not m:
        return None
    try:
        if m.group(4):
            return dt.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        d, mo = (b, a) if month_first else (a, b)
        if mo > 12:
            d, mo = mo, d
        return dt.date(y, mo, d)
    except ValueError:
        return None


_TOTAL = re.compile(r"^\s*(total|totales|suma|somme|gesamt|totale|subtotal)\b", re.I)


def _row(doc, ddate, gross, ded, reason, paid, po=""):
    g, d, p = to_cents(gross), to_cents(ded), to_cents(paid)
    if g is None and p is None:
        return None
    if g is None:
        g = p - (d or 0)
    if p is None:
        p = g + (d or 0)
    return (str(doc).strip(), ddate, g, d or 0, (reason or "").strip().upper(), p, str(po or "").strip())


# ------------------------------------------------------------------------------------------------ csv

def parse_csv(text: str, name: str = "") -> dict | None:
    lines = text.splitlines()
    if len(lines) < 3:
        return None
    sep = ";" if lines[0].count(";") > lines[0].count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=sep))
    head = {rows[0][i].strip().lower(): rows[0][i + 1].strip() for i in range(0, len(rows[0]) - 1, 2)}
    h = [c.strip().lower() for c in rows[1]]
    col = {k: h.index(k) for k in h if k}
    out, tot = [], None
    for r in rows[2:]:
        if len(r) < 3 or not r[0].strip():
            continue
        g = lambda k: r[col[k]] if k in col and col[k] < len(r) else None
        if _TOTAL.match(r[0]):               # fila final de totales: es el efectivo, no un documento
            tot = to_cents(g("paid"))
            continue
        x = _row(g("document"), to_date(g("doc_date")), g("gross"), g("discount_or_deduction"), g("reason"), g("paid"), g("your_order"))
        if x:
            out.append(x)
    return {"file": name, "payment_ref": head.get("payment_ref", ""), "payment_date": to_date(head.get("payment_date")),
            "currency": head.get("currency", ""), "total_cents": tot if tot is not None else sum(x[5] for x in out),
            "payer": "", "rows": out}


# ------------------------------------------------------------------------------------------------ xlsx

def parse_xlsx(path: Path) -> dict | None:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    allrows = [list(r) for r in ws.iter_rows(values_only=True)]
    payer, ref, pdate, cur = "", "", None, ""
    hdr = None
    for i, r in enumerate(allrows):
        cells = [str(c).strip().lower() if c is not None else "" for c in r]
        if any(c.startswith(("document", "invoice", "voucher")) for c in cells) and any(c.startswith(("paid", "net paid", "net")) for c in cells):
            hdr = i
            break
        txt = [c for c in r if c is not None]
        if i == 0 and txt:
            payer = str(txt[0])
        for c in txt:
            d = to_date(c) if not isinstance(c, (int, float)) else None
            if d and not pdate:
                pdate = d
        if len(txt) >= 2 and not ref:
            ref = str(txt[1])
        for c in txt:
            if isinstance(c, str) and re.fullmatch(r"[A-Z]{3}", c.strip()):
                cur = c.strip()
    if hdr is None:
        return None
    h = [str(c).strip().lower() if c is not None else "" for c in allrows[hdr]]

    def ix(*keys):
        for k in keys:
            for j, c in enumerate(h):
                if c.startswith(k):
                    return j
        return None

    # layout nuevo: Voucher (nº del cliente) / Invoice (nuestro nº sin prefijo) / PO; layout clásico: Document
    ci = {"doc": ix("invoice", "document"), "date": ix("inv. date", "date"), "gross": ix("gross", "amount"),
          "ded": ix("deduction", "dto"), "reason": ix("reason"), "paid": ix("net paid", "paid"), "po": ix("po", "your")}
    out, tot = [], None
    for r in allrows[hdr + 1:]:
        if not r or ci["doc"] is None or r[ci["doc"]] is None:
            continue
        if _TOTAL.match(str(r[ci["doc"]])) or (ci["paid"] is not None and any(_TOTAL.match(str(c)) for c in r[:2] if c)):
            tot = to_cents(r[ci["paid"]]) if ci["paid"] is not None and ci["paid"] < len(r) else None
            continue
        g = lambda k: r[ci[k]] if ci[k] is not None and ci[k] < len(r) else None
        x = _row(g("doc"), to_date(g("date")), g("gross"), g("ded"), g("reason"), g("paid"), g("po"))
        if x:
            out.append(x)
    return {"file": path.name, "payment_ref": ref, "payment_date": pdate, "currency": cur,
            "total_cents": tot if tot is not None else sum(x[5] for x in out), "payer": payer, "rows": out}


# ------------------------------------------------------------------------------------------------ txt / pdf

def _amounts(nums: list[str]) -> tuple[int, int, int] | None:
    """(bruto, deducción, pagado) con columnas en cualquier orden: la deducción es la cifra negativa de una factura.
    Una nota de crédito (bruto < 0) repite su importe como pagado. Con dos cifras positivas y la segunda menor
    (columna de deducción en blanco: 'NF0125007817 18/04/2025 41,703.70 37,533.33') la deducción es la diferencia."""
    v = [to_cents(n) for n in nums]
    if any(x is None for x in v) or not v:
        return None
    g = v[0]
    if len(v) == 1:
        return g, 0, g
    if g < 0:
        return g, 0, v[-1]
    if len(v) == 2:
        if v[1] < 0:
            return g, v[1], g + v[1]
        return (g, v[1] - g, v[1]) if 0 < v[1] < g else (g, 0, v[1])
    if len(v) == 3:
        negs = [x for x in v[1:] if x < 0]
        if negs:
            ded = negs[0]
            others = [x for x in v[1:] if x != ded]
            return g, ded, others[0] if others else g + ded
        return g, v[1], v[2]
    return None


def _rows_from_lines(lines: list[str], month_first: bool) -> list:
    out = []
    for ln in lines:
        m = _ROW.match(ln)
        if not m:
            continue
        rest = m.group(3)
        nums = re.findall(_NUM, rest)
        am = _amounts(nums)
        if am is None:
            continue
        g, d, p = am
        if abs(g + d - p) > 1:           # columnas incoherentes: la fila no es de importes
            continue
        reason = _reason(rest) if p != g else ""   # solo las filas con deducción llevan motivo
        if len(nums) == 2 and 0 < p < g:
            d = 0                        # deducción no impresa (columna en blanco): pagado < bruto sin motivo
        x = _row(m.group(1), to_date(m.group(2), month_first), g / 100, d / 100, reason, p / 100)
        if x:
            out.append(x)
    return out


def _from_text(text: str, name: str, payer: str = "") -> dict | None:
    lines = text.splitlines()
    cur_total = None
    cur = ""
    for ln in lines:
        m = _AMT.search(ln)
        if m and re.search(r"(?i)importe|amount|montant|betrag|importo|payment of|importe de|transferencia", ln):
            cur_total, cur = to_cents(m.group(1)), m.group(2)
            break
    # fecha del pago: primera fecha dd/mm/aaaa tras "fecha|date|on"; mes primero si el día no cabe (US/AU)
    pdate = None
    for ln in lines:
        if re.search(r"(?i)fecha|\bdate\b|con fecha| on |datum|data", ln) and not ln.lower().startswith(("from", "to:")):
            if ln.lower().startswith("date:"):
                continue
            pdate = to_date(ln, month_first=bool(re.search(r"(?i)payment of|please be advised|hemos", ln)) and "payment" in ln.lower())
            if pdate:
                break
    ref = ""
    for ln in lines:
        m = re.search(r"(?i)(?:ref\.?|subject:.*?(?:aviso de pago|payment advice))\s*([A-Z0-9/-]{6,})", ln)
        if m:
            ref = m.group(1)
            break
    rows = _rows_from_lines(lines, False)
    if not rows:
        return None
    return {"file": name, "payment_ref": ref, "payment_date": pdate, "currency": cur,
            "total_cents": cur_total if cur_total is not None else sum(x[5] for x in rows), "payer": payer, "rows": rows}


def parse_txt(text: str, name: str = "") -> dict | None:
    m = re.search(r"(?im)^from:\s*\S*@([\w-]+)", text)
    return _from_text(text, name, m.group(1) if m else "")


def _page_lines(page) -> str:
    """Reconstruye las líneas de la página uniendo palabras con la misma altura (get_text() da una celda por línea)."""
    words = sorted(page.get_text("words"), key=lambda w: (round(w[1] / 3), w[0]))
    lines, cur, y = [], [], None
    for w in words:
        if y is not None and abs(w[1] - y) > 3:
            lines.append(" ".join(cur)); cur = []
        cur.append(w[4]); y = w[1]
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


def parse_pdf(path: Path) -> dict | None:
    import pymupdf   # 'fitz' está obsoleto en PyMuPDF 2.x
    with pymupdf.open(path) as doc:
        text = "\n".join(_page_lines(p) for p in doc)
    payer = next((ln.strip() for ln in text.splitlines()[1:3] if ln.strip()), "")
    return _from_text(text, path.name, payer)


def parse_file(path: Path) -> dict | None:
    suf = path.suffix.lower()
    try:
        if suf == ".csv":
            return parse_csv(path.read_text(encoding="utf-8", errors="replace"), path.name)
        if suf == ".txt":
            return parse_txt(path.read_text(encoding="utf-8", errors="replace"), path.name)
        if suf == ".xlsx":
            return parse_xlsx(path)
        if suf == ".pdf":
            return parse_pdf(path)
    except Exception:
        return None
    return None
