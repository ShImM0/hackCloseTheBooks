"""Contrato común entre resolvers y pipeline. Dueño: P1 (fichero compartido: pide cambios a P1).

Un resolver es un módulo de bankrec/resolvers/ (o bankrec/) con:
- NAME: str
- prepare(ctx) -> None: indexa una vez por fase (guarda lo que necesite en ctx.shared[NAME]).
- resolve_batch(ctx) -> dict[str, Proposal]  (opcional): joins vectorizados, sin estado del ledger.
- resolve(txn: dict, ctx, ledger) -> Proposal | None  (opcional): para lo que depende del ledger.

Importes SIEMPRE en céntimos (int) y en la divisa de la partida. Nunca floats para comparar ni sumar.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Alloc:
    item_id: str
    amount_cents: int          # positivo, divisa de la partida (POLICIES §2)
    exception: str = ""        # excepción de ESTA fila (se puntúa por fila)


@dataclass
class Proposal:
    bank_txn_id: str
    allocs: list[Alloc] = field(default_factory=list)
    category: str = ""         # solo si allocs está vacío (categories.csv)
    exception: str = ""        # solo si allocs está vacío (p. ej. UNIDENTIFIED)
    confidence: float = 0.0
    resolver: str = ""
    reason: str = ""


def cents(s) -> int:
    """'1210.5' -> 121050. Acepta str/float/int/None."""
    if s is None or s == "":
        return 0
    return int(round(float(s) * 100))


def fmt_cents(c: int) -> str:
    """121050 -> '1210.50' (sin pasar por float)."""
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"
