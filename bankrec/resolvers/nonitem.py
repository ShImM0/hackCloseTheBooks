"""Categorías sin partida por señales bancarias explícitas. Dueño: P4.

No usa golden ni convierte cobros genéricos en UNIDENTIFIED: las señales
ambiguas siguen hacia los resolvers de partidas.
"""
from __future__ import annotations

from collections import defaultdict
import re
import unicodedata

from bankrec.contract import Proposal

NAME = "nonitem"


def normalized(value: str) -> str:
    """Normaliza acentos y cortes de concepto N43 sin perder palabras."""
    value = unicodedata.normalize("NFKD", value or "")
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).upper().replace(" | ", " ").split())


def pool_accounts(ctx) -> frozenset[str]:
    """Cuentas cabecera o participantes de cash pooling según `bank_accounts.purpose` (en FINAL, masters_final)."""
    master = getattr(ctx, "master", None)
    if master is None:
        return frozenset()
    acc = master("bank_accounts")
    return frozenset(a for a, purpose in acc.select("account_id", "purpose").iter_rows()
                     if "cash pool" in (purpose or "").lower())


def classify(txn: dict, pool: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """Devuelve categoría y evidencia; None conserva la cascada de partidas.

    `pool`: cuentas del cash pooling (pool_accounts). POLICIES §4: los barridos entre participantes y cabecera son
    CASH_POOL; en esas cuentas el banco los describe también como traspaso propio o entre cuentas.
    Patrones añadidos tras la auditoría 2023/2024 (100 % de una sola categoría en ambos años, sin partidas):
    CASH CONCENTRATION, PRES.XX / AMORTIZACION POLIZA, DISPOSICION POLIZA, COMPRA/VENTA DIVISA,
    LIQUIDACION CTA.CTO, TAX AUTHORITY / DGFIP, TGSS, SEGUROS y DEVOLUCION PRESTAMO / PRESTAMO INTRAGRUPO;
    abono con PAGO DUPLICADO / PAGO DOBLE / DUPLICATE PAYMENT → SUPPLIER_REFUND.
    """
    code = (txn.get("bank_code") or "").upper()
    text = normalized(" ".join(txn.get(k) or "" for k in ("text", "remittance_info")))
    amount = txn["amount_cents"]
    credit = amount > 0

    def hit(pattern):
        return re.search(pattern, text) is not None

    category = ""
    if hit(r"ANULACION OPERACION INDEBIDA|BANK ERROR REVERSAL|REVERSAL OF BANK ERROR"):
        category = "BANK_REVERSAL"
    elif hit(r"DEVOLUCION PAGO DUPLICADO|REFUND DUPLICATE PAYMENT") and not credit:
        category = "CUSTOMER_REFUND"
    elif credit and hit(r"PAGO DUPLICADO|PAGO DOBLE|DUPLICATE PAYMENT|\bIV\*DUPLICADO\b"):
        # POLICIES §4: reembolso de un proveedor por un pago nuestro hecho dos veces (153 de 153 en 2023/24)
        category = "SUPPLIER_REFUND"
    # Las palabras de categoría pueden ser el servicio facturado o una deducción.
    # Un código bancario aislado no basta: DATA_DICTIONARY avisa de inconsistencias.
    elif hit(r"\bFACTURA\b|\bINVOICE\b|\bFACT\.\s|\bDEDUCIR\b|\bDEDUCCION\b|\bDEDUCTION\b|\bMENOS PROFORMA\b"):
        return None
    elif hit(r"CASH ?POOL|ZBA SWEEP|ZERO BALANCING|NIVELACION SALDO|CASH CONCENTRATION"):
        category = "CASH_POOL"
    elif txn.get("account_id") in pool and hit(r"\bTRASPASO (?:PROPIO|ENTRE CUENTAS)\b"):
        category = "CASH_POOL"
    elif hit(r"INTRAGRUPO|INTERCOMPANY|\b(?:FINANCIACION|PRESTAMO|TRANSF\.) IC\b|DEVOLUCION PRESTAM|PRESTAMO INTRAGRUP"):
        category = "INTERCOMPANY"
    elif hit(r"TRASPASO (?:A|DE) CTA\b|TRANSFER BETWEEN OWN ACCOUNTS"):
        category = "INTERNAL_TRANSFER"
    elif hit(r"\bNOMINAS?\b|\bPAYROLL\b|SALARY PAYMENT"):
        if not credit:
            category = "PAYROLL"
    elif hit(r"SEGURIDAD SOCIAL|SOCIAL CHARGES|\bURSSAF\b|SOZIALVERSICHERUNG|\bTGSS\b"):
        if not credit:
            category = "SOCIAL_SECURITY"
    elif hit(r"\bIMPUESTOS?\b|\bTAX (?:PAYMENT|REFUND)\b|\bVAT RETURN\b|DEVOLUCION (?:IVA|HACIENDA)|\bTAX AUTHORITY\b|\bDGFIP\b"
             r"|\bAEAT\b"):
        # AEAT (agencia tributaria española): 4 de 4 TAX_REFUND en TRAIN (2022, 2024, 2025)
        category = "TAX_REFUND" if credit else "TAX_PAYMENT"
    elif hit(r"LIQUIDACION (?:DE )?INTERESES|INTEREST (?:CREDIT|DEBIT|SETTLEMENT|PAYMENT)") and not hit(r"COMISION"):
        category = "INTEREST_INCOME" if credit else "INTEREST_EXPENSE"
    elif hit(r"\bCOMISION(?:ES)?\b|\bCOM\. |FRAIS VIREMENT|MAINTENANCE FEE|SERV\. EM\. TRANSF\.|LIQUIDACION CTA\.? ?CTO\b"):
        if not credit:
            category = "BANK_FEES"
    elif hit(r"\bCUOTA (?:LEASING|RENTING|ARRENDAMIENTO|LSG)\b"):
        if not credit:
            category = "LEASING"
    elif hit(r"AMORTIZACION (?:DE )?(?:PRESTAMO|POLIZA)|LOAN REPAYMENT|\bPRES\.[A-Z]{2}\d"):
        if not credit:
            category = "LOAN_REPAYMENT"
    elif hit(r"DISPOSICION (?:DE )?(?:PRESTAMO|POLIZA)|LOAN DRAWDOWN"):
        if credit:
            category = "LOAN_DRAWDOWN"
    elif hit(r"COMPRAVENTA (?:DE )?DIVISA|\b(?:COMPRA|VENTA) (?:DE )?DIVISA\b|LIQUIDACION (?:DE )?FORWARD|FX DEAL|FX FORWARD"):
        category = "FX_DEAL"
    elif hit(r"ADEUDO MENSUAL DE TARJETA|LIQUIDACION TARJETAS DE EMPRESA|CORPORATE CARD"):
        if not credit:
            category = "CARD_EXPENSES"
    elif hit(r"PRIMA (?:DE )?SEGURO|INSURANCE PREMIUM|\bSEGUROS?\b"):
        if not credit:
            category = "INSURANCE"
    elif hit(r"\bPROFORMA\b"):
        if credit:
            category = "CUSTOMER_ADVANCE"
    return (category, f"bank_code={code}; concepto={text[:160]}") if category else None


def prepare(ctx) -> None:
    """Identifica también el apunte original de una anulación inequívoca."""
    by_amount = defaultdict(list)
    reversals = []
    for txn in ctx.txns.iter_rows(named=True):
        by_amount[(txn['account_id'], txn['currency'], txn['amount_cents'])].append(txn)
        if 'ANULACION OPERACION INDEBIDA' in normalized(txn.get('text')):
            reversals.append(txn)
    paired = set()
    for rev in reversals:
        text = normalized(rev.get('text'))
        candidates = [t for t in by_amount[(rev['account_id'], rev['currency'], -rev['amount_cents'])]
                      if 0 <= (rev['booking_date'] - t['booking_date']).days <= 7
                      and normalized(t.get('text'))
                      and text.endswith(normalized(t.get('text')))
                      and t['bank_txn_id'] != rev['bank_txn_id']]
        if len(candidates) == 1:
            paired.update((rev['bank_txn_id'], candidates[0]['bank_txn_id']))
    ctx.shared[NAME] = paired
    ctx.shared[NAME + ".pool"] = pool_accounts(ctx)


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    if txn['bank_txn_id'] in ctx.shared.get(NAME, set()):
        result = ('BANK_REVERSAL', 'Anulación explícita y original único por cuenta, divisa, importe, fecha y concepto')
    else:
        result = classify(txn, ctx.shared.get(NAME + ".pool", frozenset()))
    if result is None:
        return None
    category, reason = result
    return Proposal(txn['bank_txn_id'], category=category, confidence=0.9, resolver=NAME, reason=reason)
