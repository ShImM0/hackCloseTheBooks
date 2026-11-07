"""Extracción y normalización de números impresos (referencias). Dueño: P1.

Qué hace: del texto bancario saca tokens de dígitos y los compara con el número impreso de una partida
(FV25-0400572, RE250101141, FT24-01-31790, INV24-0110545, NF0125000328, 1142501297...).
El banco los trae sin prefijo ("FRAS 310926"), cortados ("FA25010", "FT24-01-"), con erratas o transpuestos
("INV25-1001240" por INV25-0101240) — POLICIES §5.5. Por eso se compara solo la parte numérica.
También detecta meses citados ("MANT. MAYO 25", "SERVICE APRIL 2024") para POLICIES §5.6.
"""
from __future__ import annotations

import re

from rapidfuzz.distance import Levenshtein
from unidecode import unidecode

_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9/\-\.]*[A-Z0-9]|[0-9]")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_DIGITS = re.compile(r"\D")


def norm(s: str | None) -> str:
    """'FV25-0400572' -> 'FV250400572'."""
    return _NON_ALNUM.sub("", (s or "").upper())


def digits(s: str | None) -> str:
    return _DIGITS.sub("", s or "")


def haystack(txn: dict) -> str:
    """Texto libre del movimiento en mayúsculas y sin acentos (sin ref1: es la referencia del banco)."""
    parts = (txn.get("text"), txn.get("ref2"), txn.get("remittance_info"), txn.get("counterparty_name"))
    return unidecode(" ".join(p for p in parts if p)).upper()


def joined(hay: str) -> str:
    """N43 parte el concepto cada 38 caracteres (' | '): 'NOVIEMBR | E' -> 'NOVIEMBRE'. Se añade al original."""
    return hay + " " + hay.replace(" | ", "") if " | " in hay else hay


def ibans(txn: dict) -> set[str]:
    out = set(_IBAN.findall(haystack(txn)))
    if txn.get("counterparty_account"):
        out.add(norm(txn["counterparty_account"]))
    return out


def tokens(hay: str) -> list[str]:
    """Parte numérica (≥ 4 dígitos) de cada token alfanumérico, sin IBAN."""
    hay = _IBAN.sub(" ", hay)
    out = []
    for t in _TOKEN.findall(hay):
        d = digits(t)
        if 4 <= len(d) <= 14:
            out.append(d)
    return out


def ref_score(doc_number: str | None, toks: list[str]) -> int:
    """0..100: cuánto se parece algún token al número impreso.
    100 igual · 90 sufijo ≥ 5 (sin prefijo) · 85 prefijo ≥ 5 (cortado) · 75 errata/transposición (≥ 7 dígitos)."""
    d = digits(doc_number)
    if len(d) < 4:
        return 0
    best = 0
    for t in toks:
        if t == d:
            return 100
        if len(t) >= 5 and d.endswith(t):
            best = max(best, 90)
        elif len(t) >= 5 and d.startswith(t) and not any(len(u) > len(t) and u.startswith(t) for u in toks):
            best = max(best, 85)   # prefijo = número cortado; si hay un token más largo con ese inicio, manda ese
        elif len(t) >= 7 and abs(len(t) - len(d)) <= 1 and Levenshtein.distance(t, d, weights=(1, 1, 1)) <= 2:
            best = max(best, 75)
    return best


_MONTHS = {
    1: "ENERO JANUARY JANVIER JANUAR GENNAIO JANEIRO STYCZEN ENE JAN GEN",
    2: "FEBRERO FEBRUARY FEVRIER FEBRUAR FEBBRAIO FEVEREIRO LUTY FEB FEV",
    3: "MARZO MARCH MARS MARZ MARCO MARZEC MAR",
    4: "ABRIL APRIL AVRIL APRILE KWIECIEN ABR APR AVR",
    5: "MAYO MAY MAI MAGGIO MAIO MAJ MAG",
    6: "JUNIO JUNE JUIN JUNI GIUGNO JUNHO CZERWIEC JUN GIU",
    7: "JULIO JULY JUILLET JULI LUGLIO JULHO LIPIEC JUL LUG",
    8: "AGOSTO AUGUST AOUT SIERPIEN AGO AUG",
    9: "SEPTIEMBRE SEPTEMBER SEPTEMBRE SETTEMBRE SETEMBRO WRZESIEN SEP SEPT SET",
    10: "OCTUBRE OCTOBER OCTOBRE OKTOBER OTTOBRE OUTUBRO PAZDZIERNIK OCT OKT OTT OUT",
    11: "NOVIEMBRE NOVEMBER NOVEMBRE NOVEMBRO LISTOPAD NOV",
    12: "DICIEMBRE DECEMBER DECEMBRE DEZEMBER DICEMBRE DEZEMBRO GRUDZIEN DIC DEC DEZ",
}
_MONTH_OF = {w: m for m, ws in _MONTHS.items() for w in ws.split()}
_WORD = re.compile(r"[A-Z]+")


def months(hay: str) -> set[int]:
    """Meses citados en el texto (palabras completas; 'MAR' solo si no es parte de otra palabra)."""
    return {_MONTH_OF[w] for w in _WORD.findall(hay) if w in _MONTH_OF}


if __name__ == "__main__":
    toks = tokens("TRANSFERENCIA DE X | CONCEPTO: FRAS 310926 | ORDENANTE ES5694539532156647287462")
    assert toks == ["310926"], toks
    assert ref_score("FV24-0310926", toks) == 90
    assert ref_score("FA250100633", tokens("MOTIF FACTURE FA25010")) == 85
    assert ref_score("INV25-0101240", tokens("INVOICE INV25-1001240")) == 75
    assert ref_score("FV24-0310926", tokens("FRA 999999")) == 0
    assert months("SERVICE APRIL 2024") == {4} and months("MANT. MAR 24") == {3}
    assert 11 in months(joined("CUOTA MANTENIMIENTO NOVIEMBR | E 24"))
    assert ref_score("FV25-0107998", tokens("FACTURA FV25-0101272 FRA FV25-010")) == 0
    print("refs OK")
