"""Último paso de la cascada: cobros que ningún resolver supo casar. Dueño: P4.

POLICIES §3: UNIDENTIFIED es un cobro para el que NO EXISTE partida abierta (tercero desconocido, beneficiario
equivocado, ingreso en efectivo…). El fallback de P1 marca UNIDENTIFIED todo abono sin resolver, aunque solo
signifique que la cascada no encontró la partida. Cada UNIDENTIFIED de más es un falso positivo de esa clase en el
macro-F1 de excepciones; la fila del movimiento sigue siendo un fallo en txn accuracy con cualquier etiqueta.

Aquí un abono sin resolver solo se marca UNIDENTIFIED si nada lo ata a una partida abierta de su sociedad
(factura de cliente ARI o abono de proveedor APC, cuyo reembolso liquida la APC: POLICIES §4):
- el IBAN ordenante es de un cliente (o de su matriz, POLICIES §5.1) o proveedor con partidas abiertas;
- el nombre del ordenante encaja con un cliente con facturas abiertas (nombre actual, comercial, impreso en sus
  facturas o el de la matriz: `bankrec.parties`), solo si el nombre es distintivo (≥ 3 palabras sin forma social):
  los cobros no identificados traen a menudo nombre y apellido de particulares que coinciden con clientes;
- el texto cita el número impreso de una factura abierta (≥ 6 dígitos, sin prefijo o cortado: POLICIES §5.5);
- el texto describe un abono agregado (liquidación de tarjetas/TPV, remesa, lockbox o depósito de cheques): por
  construcción liquida los pedidos o cheques de sus ficheros (POLICIES §4), nunca es un ordenante desconocido.
Si hay evidencia, se emite una fila sin excepción con category CUSTOMER_ADVANCE: cobro de un cliente identificado
pendiente de aplicar (cuenta 438000 «anticipos de clientes» del maestro). No afirma una excepción que no consta.

Los cargos no se tocan (None → fallback de P1). No lee golden, segmentos ni importes concretos.
Integración: P1 añade "bankrec.resolvers.nonitem_unmatched" al FINAL de registry.CASCADE.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from rapidfuzz import fuzz

from bankrec import parties, refs
from bankrec.contract import Proposal
from bankrec.resolvers.nonitem import normalized

NAME = "nonitem_unmatched"
PENDING = "CUSTOMER_ADVANCE"   # categoría de un cobro identificado sin partida aplicada
NAME_MIN = 90                  # partial_ratio nombre del cliente vs texto (redondo; ver informe P4)
NAME_LEN = 8                   # nombres normalizados más cortos no deciden (siglas, "SPA", iniciales)
NAME_WORDS = 3                 # "NOMBRE APELLIDO" es ambiguo; con 3+ palabras el nombre identifica
KEY_LEN = 6                    # dígitos de las claves de número impreso (como text_ref)
MAX_KEY = 300                  # claves de nombre o número demasiado genéricas se ignoran
BATCH = re.compile(r"\bLOCKBOX\b|\bDEPOSIT \d+ ITEMS?\b|\bCARD SETTLEMENT\b|\bSETTLEMENT\b|\bREMESA\b|\bTPV\b"
                   r"|\bACQUIR|\bADQUIRENTE\b|\bCHEQUES?\b|\bCHQ\b|\bCHECKS?\b")


def _bigrams(text: str) -> set[str]:
    w = text.split()
    return {f"{a} {b}" for a, b in zip(w, w[1:])}


def _name_keys(name: str, df: Counter) -> list[str]:
    """Los dos pares de palabras seguidas menos frecuentes del nombre: los nombres sintéticos repiten vocabulario
    ("POOL BUILDERS", "SUN PRAIRIE HILLS") y una sola palabra es demasiado genérica; dos claves toleran cortes."""
    return sorted(_bigrams(name), key=lambda b: (df[b], b))[:2]


def prepare(ctx) -> None:
    """Índices por sociedad de partidas que un abono puede liquidar, con saldo: facturas de cliente (ARI) y abonos
    de proveedor (APC, su reembolso liquida la APC: POLICIES §4). Nombre → (nombre, party) y dígitos → item_id."""
    parties.prepare(ctx)
    names = ctx.shared["parties"]["names"]
    ar = ctx.items.filter(ctx.items["item_type"].is_in(["ARI", "APC"]) & (ctx.items["open_cents"] > 0))
    items_of: dict[str, list[str]] = defaultdict(list)
    party_ent: dict[str, set[str]] = defaultdict(set)
    ref_idx: dict[tuple, list[str]] = defaultdict(list)
    for iid, ent, pid, dn in ar.select("item_id", "entity", "party_id", "document_number").iter_rows():
        items_of[pid].append(iid)
        party_ent[ent].add(pid)
        d = refs.digits(dn)
        if len(d) >= KEY_LEN:
            ref_idx[(ent, "s" + d[-KEY_LEN:])].append(iid)
            ref_idx[(ent, "p" + d[:KEY_LEN])].append(iid)
    name_idx: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    for ent, pids in party_ent.items():
        pairs = [(n, pid) for pid in pids for n in names.get(pid, ())
                 if len(n) >= NAME_LEN and len(n.split()) >= NAME_WORDS]
        df = Counter(b for n, _ in pairs for b in _bigrams(n))
        for n, pid in pairs:
            for k in _name_keys(n, df):
                name_idx[(ent, k)].append((n, pid))
    ctx.shared[NAME] = {"items_of": items_of, "ref": ref_idx, "name": name_idx}


def evidence(txn: dict, ctx, ledger) -> str:
    """Motivo por el que probablemente SÍ existe partida abierta; '' si no hay ninguno."""
    S = ctx.shared[NAME]
    ent = txn["entity"]

    def has_open(pid: str) -> bool:
        return any(ledger.is_open(i) for i in S["items_of"].get(pid, ()))

    m = BATCH.search(normalized(" ".join(txn.get(k) or "" for k in ("text", "remittance_info"))))
    if m:
        return f"abono agregado ({m.group(0)})"
    tctx = parties.context(txn)
    for ib in tctx["ibans"]:
        for pid in ctx.shared["parties"]["iban"].get(ib, ()):
            if has_open(pid):
                return f"IBAN de {pid} con partidas abiertas"
    for tok in refs.tokens(refs.haystack(txn)):
        if len(tok) < KEY_LEN:
            continue
        for key in ("s" + tok[-KEY_LEN:], "p" + tok[:KEY_LEN]):
            hits = S["ref"].get((ent, key), ())
            if len(hits) <= MAX_KEY:
                for iid in hits:
                    if ledger.is_open(iid):
                        return f"texto cita el número de {iid} (abierta)"
    hay = tctx["hay"]
    seen = set()
    for w in _bigrams(hay):
        cands = S["name"].get((ent, w), ())
        if len(cands) > MAX_KEY:
            continue
        for n, pid in cands:
            if (n, pid) in seen:
                continue
            seen.add((n, pid))
            if fuzz.partial_ratio(n, hay) >= NAME_MIN and has_open(pid):
                return f"ordenante ≈ {n} ({pid}) con partidas abiertas"
    return ""


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    if txn["amount_cents"] <= 0:
        return None
    why = evidence(txn, ctx, ledger)
    if why:
        return Proposal(txn["bank_txn_id"], category=PENDING, confidence=0.1, resolver=NAME,
                        reason=f"no casado pero existe partida probable: {why}")
    return Proposal(txn["bank_txn_id"], category="UNIDENTIFIED", exception="UNIDENTIFIED", confidence=0.5,
                    resolver=NAME, reason="sin IBAN, nombre ni número que lo ate a una factura abierta (POLICIES §3)")
