"""Lectura de avisos de remesa (csv/txt/xlsx/pdf) a líneas (documento, bruto, deducción, motivo, pagado). Dueño: P2.

Parser genérico por tokens, sin depender del diseño de cada cliente ni del idioma: se localizan los números de
documento conocidos (los de las partidas) y, tras cada uno, los importes (token con 2 decimales) y las palabras de
motivo hasta el siguiente documento. 3+ importes = bruto, deducción, pagado; 2 = bruto, pagado; 1 = pagado.
Formatos de número: 1,614.89 / 1.614,89 / 1614.89 (el último separador con 2 cifras detrás es el decimal).
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from pathlib import Path

AMT = re.compile(r"^[-+]?\(?\d[\d.,\s]*[.,]\d{2}\)?$")
EOL = "\x00"   # fin de fila (csv/xlsx/txt): una línea de documento no pasa de su fila; en pdf no hay marca
DATE = re.compile(r"^\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}$")
# motivos que NO son disputa: pronto pago y retención (POLICIES §3: SHORT_DISCOUNT / garantía); el resto es SHORT_DISPUTE
NOT_DISPUTE = re.compile(r"\b(DISCOUNT|DTO|DESCUENTO|SKONTO|ESCOMPTE|SCONTO|RET|RETEN\w*|RETENU\w*|WITHHOLD\w*|"
                         r"RETENTION|HOLDBACK)\b", re.I)


def cents(tok: str) -> int | None:
    """'1.614,89' / '-2,126.05' / '(12.00)' -> céntimos con signo."""
    t = tok.strip().replace(" ", "").replace(" ", "")
    neg = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = t.strip("-+()")
    if len(t) < 4 or t[-3] not in ".,":
        return None
    digits = re.sub(r"[.,]", "", t)
    if not digits.isdigit():
        return None
    v = int(digits)
    return -v if neg else v


def find_file(base: Path, rel: str) -> Path | None:
    """Ruta del aviso respetando mayúsculas y la normalización unicode del nombre (NFC/NFD)."""
    for form in ("NFC", "NFD", None):
        p = base / (unicodedata.normalize(form, rel) if form else rel)
        if p.exists():
            return p
    return None


def _tokens(path: Path) -> list[str]:
    suf = path.suffix.lower()
    if suf == ".csv":
        raw = path.read_bytes().decode("utf-8", errors="replace")
        first = raw.split("\n", 1)[0]
        sep = ";" if first.count(";") > first.count(",") else ","
        return [w for row in csv.reader(io.StringIO(raw), delimiter=sep)
                for w in [x for c in row for x in c.split()] + [EOL]]
    if suf == ".txt":
        return [w for line in path.read_bytes().decode("utf-8", errors="replace").splitlines()
                for w in line.split() + [EOL]]
    if suf == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                out.append(EOL)
                for c in row:
                    if c is None or c == "":
                        continue
                    if isinstance(c, float):
                        c = f"{c:.2f}"
                    out.extend(str(c).split())
        wb.close()
        return out
    if suf == ".pdf":
        import pymupdf
        with pymupdf.open(path) as d:
            return "\n".join(p.get_text() for p in d).split()
    return []


def parse(path: Path, docs: set[str]) -> list[tuple[str, int | None, int | None, str, int | None]]:
    """Líneas del aviso: (documento, bruto, deducción, motivo, pagado) en céntimos con signo.
    `docs`: números de documento conocidos (ARI/ARC); un token que coincide abre una línea."""
    toks = _tokens(path)
    marks = [i for i, t in enumerate(toks) if t in docs]
    out = []
    for k, i in enumerate(marks):
        j = marks[k + 1] if k + 1 < len(marks) else len(toks)
        if EOL in toks[i:j]:
            j = i + toks[i:j].index(EOL)
        amts, words = [], []
        for t in toks[i + 1:j]:
            if DATE.match(t):
                continue
            if AMT.match(t):
                c = cents(t)
                if c is not None:
                    amts.append(c)
            elif re.fullmatch(r"[A-Za-zÀ-ÿ.]+", t):
                # el motivo va justo tras la deducción (importe negativo distinto del bruto; un abono repite el bruto)
                if (len(amts) >= 2 and amts[1] < 0 and amts[1] != amts[0] and len(amts) < 3 and len(words) < 3):
                    words.append(t)
            elif amts:
                break          # pie del documento (otro texto tras los importes)
        if not amts:
            continue
        reason = " ".join(words).upper()
        if len(amts) >= 3:
            out.append((toks[i], amts[0], amts[1], reason, amts[-1]))
        elif len(amts) == 2 and reason:       # txt: bruto + deducción + motivo, sin columna de pagado
            out.append((toks[i], amts[0], amts[1], reason, amts[0] + amts[1]))
        elif len(amts) == 2:
            out.append((toks[i], amts[0], None, "", amts[1]))
        else:
            out.append((toks[i], None, None, "", amts[0]))
    return out
