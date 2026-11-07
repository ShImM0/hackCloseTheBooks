"""Resolver 1:1 por referencia impresa + importe exacto. Dueño: P1.

Regla 1 de la cascada 1:1 (POLICIES §5.5): entre las partidas abiertas con saldo == importe
(amount_match.candidates), la única cuyo número impreso (o PO del cliente) aparece en el texto.
Tolera número sin prefijo, cortado, con erratas (refs.ref_score). Si hay empate, no decide (lo intenta
amount_match por tercero).
Entradas: txn, ctx, ledger. Salida: Proposal o None.
"""
from __future__ import annotations

from bankrec import refs
from bankrec.contract import Alloc, Proposal
from bankrec.resolvers.amount_match import candidates

NAME = "text_ref"
REF_MIN = 85       # sufijo/prefijo ≥ 5 dígitos o número completo; las erratas (75) solo desempatan con tercero


def prepare(ctx) -> None:
    return None


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    cands = candidates(txn, ledger)
    if not cands:
        return None
    toks = refs.tokens(refs.haystack(txn))
    if not toks:
        return None
    scored = []
    for c in cands:
        i = ledger.info[c]
        s = max(refs.ref_score(i[5], toks), refs.ref_score(i[6], toks) if i[6] else 0)
        if s >= REF_MIN:
            scored.append((s, c))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    s, c = scored[0]
    return Proposal(txn["bank_txn_id"], [Alloc(c, abs(txn["amount_cents"]))], confidence=s / 100,
                    reason=f"ref {ledger.info[c][5]} ({s}) + importe")
