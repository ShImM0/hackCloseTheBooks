"""Excepciones e importes de cada fila. Dueño: P3.

Se llama después de que un resolver asigne partidas y ANTES de actualizar el ledger.
Qué hace: según POLICIES §2-3 decide la excepción de cada Alloc y su amount:
- liquidación total con diferencia explicada (SHORT_DISCOUNT, SHORT_BANK_CHARGES, SHORT_WITHHOLDING,
  FX_DIFFERENCE, OVERPAYMENT): amount = saldo abierto completo;
- PARTIAL / SHORT_DISPUTE: amount = lo realmente aplicado;
- DUPLICATE_PAYMENT: lo pagado otra vez; RETURNED: lo que se reabre.
FX_DIFFERENCE sustituye a vacío/SHORT_DISCOUNT/SHORT_WITHHOLDING/SHORT_BANK_CHARGES en filas de factura;
los abonos (ARC/APC) quedan sin excepción.
Entradas: prop (Proposal), txn (dict), ctx, ledger (saldo ANTES de aplicar este movimiento).
Salida: Proposal corregida, o None para dejarla como está.

Contrato con los resolvers (lo que P3 respeta de lo que ellos deciden):
- Si algún Alloc llega con exception != "" el resolver ya ha decidido la fila (p. ej. SHORT_DISPUTE desde un
  aviso de remesa, RETURNED desde un lote de devoluciones): se conservan excepción e importe de TODAS las filas;
  solo se aplica la sustitución por FX_DIFFERENCE.
- Si todos llegan con exception == "": el importe del resolver solo se usa si la suma (neta de abonos) cuadra con
  el cobro; si no, se recalcula desde el ledger (saldo abierto) y el efectivo del movimiento.

Algoritmo (en la divisa de la partida, céntimos enteros):
 1. sentido contrario al de la factura (cobro con signo − de una ARI, abono + de una API) -> RETURNED;
 2. partidas ya cerradas (saldo 0) -> DUPLICATE_PAYMENT;
 3. efectivo = saldo de facturas − saldo de abonos -> sin excepción;
 4. efectivo < esperado: parcial explicado por el PDF (retención, anticipo, plazos) > pronto pago (subconjunto
    de facturas con plazo vigente + 3 días de gracia) > retención en origen (MX 1,25 %/10 %, BR/MA 1,5 %/10 %)
    > gastos bancarios (≤ 60 EUR y ≤ 2 %) > PARTIAL (importe = lo aplicado);
 5. efectivo > esperado en una sola factura, con exceso de forma reconocible (overpay_shape) -> OVERPAYMENT;
 6. divisa del movimiento != divisa de la partida -> FX_DIFFERENCE en lugar de vacío/SHORT_*.
Módulos de apoyo propios: bankrec/exceptions_oracle.py (mide solo esta etapa con el golden como resolver).
"""
from __future__ import annotations

import bisect
import logging

from bankrec.contract import Alloc, Proposal

log = logging.getLogger("bankrec")

KEY = "exceptions"
# retención en origen en 1/10000 (125 = 1,25 %; 1000 = 10 %), por país del cliente
WH_BPS = {"MX": (125, 1000), "BR": (150, 1000), "MA": (150, 1000)}
GRACE_DAYS = 3   # POLICIES §3: 3 días de gracia. Con 9 la franja +4..+9 acertaba 1 de 3 en TRAIN (690 SD vs 1.536 PARTIAL); val igual (89,67)
BC_MAX_EUR = 60.0
BC_MAX_PCT = 0.02
FX_BAND = (0.965, 1.02)          # efectivo/esperado de una liquidación total en otra divisa (tolerancia FX 3 %)
INVOICES = ("ARI", "API")
# POLICIES §3 DUPLICATE_PAYMENT ("customer pays twice"): en TRAIN 2021-2025 los 4.927 DUPLICATE_PAYMENT del golden son
# pagos sueltos; ninguno es línea de remesa, liquidación de tarjeta, depósito de cheques, aviso de remesa ni payment
# run. En val, las 1.140 filas DUPLICATE_PAYMENT de estos resolvers eran todas falsas (otro movimiento había consumido
# antes la partida por error): con saldo 0, la fila va sin excepción por el importe del documento.
DOC_RESOLVERS = {"docs_remit", "docs_dd", "docs_card", "docs_ap"}


OVERPAY_UNITS = 100   # exceso en unidades enteras: en TRAIN 2021-2025 los OVERPAYMENT llevan +1..+100 unidades exactas


def overpay_shape(c: int, o: int) -> bool:
    """¿El efectivo c es el saldo o más un sobrepago de forma reconocible? (POLICIES §3 OVERPAYMENT). En TRAIN
    2021-2025 el 100 % de los OVERPAYMENT (2.838 filas, todos los años) tienen una de tres formas: exceso de unidades
    enteras (≤ 100), o efectivo = saldo × 1,01 o × 1,10 redondeado al céntimo. Un exceso sin esa forma suele ser
    un cobro de varias facturas del que solo se ha encontrado una (en val: 0 de 218 OVERPAYMENT verdaderos lo tienen,
    138 de 138 falsos sí)."""
    ex = c - o
    if ex <= 0:
        return False
    if ex % 100 == 0 and ex <= OVERPAY_UNITS * 100:
        return True
    return any(abs(c - round(o * f)) <= 1 for f in (1.01, 1.10))


def cross_border(txn: dict) -> bool:
    """Cobro transfronterizo de fuera de SEPA (POLICIES §3 SHORT_BANK_CHARGES): familia ISO 20022 XBCT o texto
    'TRANSFER. EN DIV.'. Los cobros domésticos (SEPA ESCT, ACH, transferencias nacionales) nunca llevan gastos."""
    return "XBCT" in (txn.get("bank_code") or "") or "EN DIV" in (txn.get("text") or "").upper()


def charges_possible(txn: dict, st, party) -> bool:
    """¿Puede el banco intermediario haber cobrado gastos? (POLICIES §3 SHORT_BANK_CHARGES). Además de la marca del
    extracto (cross_border), un cliente de otro país que la sociedad: los MT940 no traen la familia ISO y en TRAIN
    2021-2025 hay 109 SHORT_BANK_CHARGES así (CH/MT→IT, CH/MA→FR, US→MX) y 0 con cliente del mismo país. El descuento
    por pronto pago (LU→NL, FR→ES) se comprueba antes. Países de los maestros: sin listas fijas."""
    if cross_border(txn):
        return True
    cc, ec = st.country.get(party), st.ent_country.get(txn.get("entity"))
    return bool(cc and ec and cc != ec)


# ----------------------------------------------------------------------------------------------- índices

class _State:
    """Índices construidos una vez por fase (ctx.shared['exceptions'])."""

    def __init__(self, ctx):
        it = ctx.items
        self.items = {
            r[0]: r[1:] for r in zip(
                it["item_id"].to_list(), it["item_type"].to_list(), it["party_id"].to_list(),
                it["issue_date"].to_list(), it["currency"].to_list(), it["total_cents"].to_list(),
                it["payment_terms"].to_list())
        }
        pt = ctx.master("payment_terms")
        self.terms = {}
        for r in pt.iter_rows(named=True):
            if r["early_payment_discount_pct"]:
                self.terms[r["code"]] = (round(float(r["early_payment_discount_pct"]) * 100), int(float(r["discount_days"])))
        cu = ctx.master("customers")
        self.country = dict(zip(cu["customer_id"].to_list(), cu["country"].to_list()))
        en = ctx.master("entities")
        self.ent_country = dict(zip(en["entity"].to_list(), en["country"].to_list()))
        # tarjeta web y recibos domiciliados nunca llevan excepción salvo RETURNED (el efectivo es el neto del lote)
        self.noexc = {c for c, t in zip(cu["customer_id"].to_list(), cu["customer_type"].to_list())
                      if t in ("WEB_CARD", "INSTALLER_DD")}
        fx = ctx.master("fx_rates")
        self.fx: dict[str, tuple[list, list]] = {}
        for cur, grp in fx.sort("date").group_by("currency", maintain_order=True):
            self.fx[cur[0]] = (grp["date"].to_list(), [float(x) for x in grp["units_per_eur"].to_list()])
        self.facts: dict[str, dict] = {}
        ft = ctx.shared.get("pdf_facts")
        if ft is not None and ft.height:
            for dn, ent, fact, val in ft.select("document_number", "entity", "fact", "value").iter_rows():
                self.facts.setdefault((ent, dn), {})[fact] = val
        self.docnum = {}
        # (entity, document_number) por partida, solo para las que tienen hechos (retención, anticipo, plazos)
        if self.facts:
            for iid, ent, dn in zip(it["item_id"].to_list(), it["entity"].to_list(), it["document_number"].to_list()):
                if (ent, dn) in self.facts:
                    self.docnum[iid] = (ent, dn)

    def rate(self, cur: str, date) -> float:
        """Unidades de `cur` por EUR en `date` (último día disponible <= date)."""
        if cur == "EUR":
            return 1.0
        dates, vals = self.fx.get(cur, ([], []))
        if not dates:
            return 1.0
        i = bisect.bisect_right(dates, date) - 1
        return vals[max(i, 0)]

    def convert(self, cents: int, src: str, dst: str, date) -> int:
        if src == dst:
            return cents
        return round(cents / self.rate(src, date) * self.rate(dst, date))

    def item_facts(self, item_id: str) -> dict:
        k = self.docnum.get(item_id)
        return self.facts.get(k, {}) if k else {}


def _state(ctx) -> _State:
    st = ctx.shared.get(KEY)
    if st is None:
        st = ctx.shared[KEY] = _State(ctx)
    return st


# ----------------------------------------------------------------------------------------------- utilidades

def _subset_sum(vals: list[int], target: int, tol: int) -> list[int] | None:
    """Índices de un subconjunto de `vals` cuya suma está a ≤ tol de `target` (meet-in-the-middle). None si no hay."""
    n = len(vals)
    if n == 0 or n > 16 or target <= 0:   # con más partidas aparecen coincidencias espurias
        return None
    h = n // 2
    left, right = vals[:h], vals[h:]

    def sums(v):
        out = []
        for mask in range(1 << len(v)):
            s, m, i = 0, mask, 0
            while m:
                if m & 1:
                    s += v[i]
                m >>= 1
                i += 1
            out.append((s, mask))
        return out

    L, R = sums(left), sorted(sums(right))
    rs = [x[0] for x in R]
    best = None
    for s, lm in L:
        lo = bisect.bisect_left(rs, target - tol - s)
        while lo < len(rs) and rs[lo] <= target + tol - s:
            cnt = bin(lm).count("1") + bin(R[lo][1]).count("1")
            if cnt and (best is None or cnt < best[0]):
                best = (cnt, lm, R[lo][1])
            lo += 1
        if best and best[0] == 1:
            break
    if best is None:
        return None
    _, lm, rm = best
    return [i for i in range(h) if lm >> i & 1] + [h + i for i in range(n - h) if rm >> i & 1]


def _cents(s) -> int | None:
    try:
        return round(float(s) * 100)
    except (TypeError, ValueError):
        return None


def _plan(f: dict) -> list[tuple[int, bool, bool]]:
    """(importe, es_último, plazos_iguales) de los cobros que el PDF documenta: retención de garantía (importe a
    pagar), anticipo (neto a pagar) y plazos. En el golden el plazo "último" es el de importe distinto (el que
    absorbe los céntimos) y no lleva excepción aunque se cobre antes que los demás, y los demás son PARTIAL aunque
    el último ya se cobrase y cierren el saldo. Si todos los plazos son iguales, el que cierra el saldo no lleva
    excepción. La retención y el anticipo son PARTIAL aunque la liberación se haya cobrado antes."""
    out = []
    if "retention" in f:
        out.append((int(f["retention"].split("|")[3]), False, False))
    if "advance" in f:
        out.append((int(f["advance"].split("|")[2]), False, False))
    if "instalment_plan" in f:
        amts = [int(x) for x in f["instalment_plan"].split("|")[2].split(";")]
        equal = len(set(amts)) == 1
        for k, a in enumerate(amts):
            out.append((a, k == len(amts) - 1 and not equal, equal))
    return out


def _fact_label(f: dict, amount: int, open_c: int, tol: int, age: int | None = None) -> str | None:
    """Excepción ("PARTIAL" o "") de un cobro de `amount` según el PDF; None si no coincide con ningún cobro documentado."""
    best = min(_plan(f), key=lambda p: (abs(p[0] - amount), p[1]), default=None)
    if best is None or abs(best[0] - amount) > tol:
        return None
    _, last, equal = best
    if last:
        return ""
    if equal and "instalment_plan" in f:
        # plazos iguales: no hay "último" por importe. Sin excepción el que cierra el saldo o el que llega tras el
        # último vencimiento (TRAIN: 95,7 % de acierto; el orden de pago real se desvía en un 5 %)
        d_last = int(f["instalment_plan"].split("|")[1].split("/")[-1])
        if abs(open_c - amount) <= tol or (age is not None and age >= d_last):
            return ""
    return "PARTIAL"


# ----------------------------------------------------------------------------------------------- núcleo

def finalize(prop: Proposal, txn: dict, ctx, ledger) -> Proposal | None:
    if not prop.allocs:
        return None
    st = _state(ctx)
    rows = []
    for a in prop.allocs:
        info = st.items.get(a.item_id)
        if info is None:
            return None
        rows.append((a, info, ledger.open.get(a.item_id, 0)))
    typ = [r[1][0] for r in rows]
    inv_idx = [i for i, t in enumerate(typ) if t in INVOICES]
    cn_idx = [i for i, t in enumerate(typ) if t not in INVOICES]
    ar = typ[0].startswith("AR") if typ else True
    s = 1 if txn["amount_cents"] > 0 else -1
    tcur = txn.get("currency")
    icur = rows[0][1][3]
    fx = bool(tcur and icur and tcur != icur)
    locked = any(a.exception and a.exception != "FX_DIFFERENCE" for a, _, _ in rows)
    doc = prop.resolver in DOC_RESOLVERS

    if locked and not any(not a.exception for a, _, _ in rows):
        out = [Alloc(a.item_id, abs(a.amount_cents), a.exception) for a, _, _ in rows]   # RETURNED llega con signo −
    elif locked:
        # el resolver fijó algunas filas (p. ej. SHORT_DISPUTE de un aviso): se conservan y las libres se explican
        # con el efectivo que queda (retención en origen, pronto pago...)
        fixed = [i for i, (a, _, _) in enumerate(rows) if a.exception and a.exception != "FX_DIFFERENCE"]
        free = [i for i in range(len(rows)) if i not in fixed]
        if doc:
            # resolver de documentos (aviso de remesa): el aviso dice qué se pagó de cada fila libre; se conserva.
            # Recalcular con "efectivo − filas fijadas" falla: las fijadas SHORT_DISCOUNT/SHORT_WITHHOLDING llevan el
            # bruto (POLICIES §2) pero se pagó el neto, y la suma de deducciones acababa como PARTIAL en una fila libre
            # (val 6 meses: 49 de 50 cobros con ese fallo). Solo se explica lo pagado < saldo (a cuenta en el aviso).
            sub = []
            for i in free:
                a, info, op = rows[i]
                amt = abs(a.amount_cents)
                if info[0] in INVOICES and op > 0 and 0 < amt < op - 2:
                    sub.append(_explain_explicit(rows[i], op, amt, txn["booking_date"], st, 2,
                                                 charges_possible(txn, st, info[1])))
                else:
                    sub.append(Alloc(a.item_id, amt or max(op, 0), ""))
        else:
            offset = 0
            for i in fixed:
                a, info, _ = rows[i]
                offset += abs(a.amount_cents) if info[0] in INVOICES else -abs(a.amount_cents)
            sub = _free_rows([rows[i] for i in free], txn, st, fx, tcur, icur, offset, ar, doc)
        out = [None] * len(rows)
        for i in fixed:
            out[i] = Alloc(rows[i][0].item_id, abs(rows[i][0].amount_cents), rows[i][0].exception)
        for i, o in zip(free, sub):
            out[i] = o
    elif not inv_idx:
        # solo abonos (rappel pagado, reembolso del proveedor): fila normal con su saldo completo
        out = [Alloc(a.item_id, op if op > 0 else a.amount_cents, "") for a, _, op in rows]
    else:
        out = _free_rows(rows, txn, st, fx, tcur, icur, 0, ar, doc)
    tc = txn.get("currency")
    if fx or any(r[1][3] != tc for r in rows):
        for a, (_, info, _) in zip(out, rows):
            if info[3] != tc and info[0] in INVOICES and a.exception in ("", "SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES",
                                                       "OVERPAYMENT"):
                a.exception = "FX_DIFFERENCE"
    prop.allocs = out
    return prop


def _free_rows(rows, txn, st, fx, tcur, icur, offset, ar, doc=False):
    """Filas sin excepción decidida por el resolver: sentido del cobro, moneda mixta y cuadre de importes.
    doc = el resolver es de documentos (DOC_RESOLVERS): nunca DUPLICATE_PAYMENT."""
    inv_idx = [i for i, r in enumerate(rows) if r[1][0] in INVOICES]
    cn_idx = [i for i, r in enumerate(rows) if r[1][0] not in INVOICES]
    s = 1 if txn["amount_cents"] > 0 else -1
    if not inv_idx:
        return [Alloc(a.item_id, op if op > 0 else a.amount_cents, "") for a, _, op in rows]
    side_sign = 1 if ar else -1
    if len({r[1][3] for r in rows}) > 1 and s == side_sign:
        # partidas en varias divisas (pago de varias facturas desde una cuenta): sin cuadre de importes;
        # cada partida en divisa distinta de la cuenta lleva FX_DIFFERENCE (más abajo)
        return [Alloc(a.item_id, op if op > 0 else a.amount_cents, "") for a, _, op in rows]
    if s != side_sign:
        return _returned(rows, txn, st, fx)
    return _normal(rows, inv_idx, cn_idx, txn, st, fx, tcur, icur, offset, doc)


def _item_cash(txn, st, icur) -> int:
    """Efectivo del movimiento en la divisa de la partida (céntimos, positivo)."""
    cash = abs(txn["amount_cents"])
    tcur = txn.get("currency")
    if tcur == icur:
        return cash
    if txn.get("orig_currency") == icur:
        o = _cents(txn.get("orig_amount"))
        if o:
            return abs(o)
    return st.convert(cash, tcur, icur, txn["booking_date"])


def _returned(rows, txn, st, fx):
    """Cobro/pago que vuelve: reabre las partidas (RETURNED). Importe: el del resolver, o el efectivo si es una sola."""
    icur = rows[0][1][3]
    out = []
    for a, info, op in rows:
        amt = a.amount_cents
        if amt <= 0:
            amt = _item_cash(txn, st, icur) if len(rows) == 1 else max(info[4] - op, 0)
        out.append(Alloc(a.item_id, amt, "RETURNED"))
    return out


def _age(row, date) -> int | None:
    issue = row[1][2]
    return (date - issue).days if issue is not None else None


def _explain_explicit(row, open_c, amt, date, st, tol, xb=True) -> Alloc:
    """Fila con importe explícito del resolver menor que el saldo: ¿pronto pago, retención en origen, plazo del PDF o
    parcial? Devuelve el Alloc con su excepción e importe (completo si la partida queda liquidada)."""
    a, info, _ = row
    _, party, issue, _, total, terms = info
    t = st.terms.get(terms)
    if t and open_c == total and issue is not None:
        d = (total * t[0] + 5000) // 10000
        if abs(amt - (total - d)) <= 2:
            if (date - issue).days <= t[1] + GRACE_DAYS:
                return Alloc(a.item_id, open_c, "SHORT_DISCOUNT")
            return Alloc(a.item_id, amt, "PARTIAL")
    r = WH_BPS.get(st.country.get(party))
    if r and open_c == total:
        for bps in r:
            if abs(amt - (total - (total * bps + 5000) // 10000)) <= 2:
                f = st.item_facts(a.item_id)
                if f and _fact_label(f, amt, open_c, tol, _age(row, date)) is not None:
                    break
                return Alloc(a.item_id, open_c, "SHORT_WITHHOLDING")
    f = st.item_facts(a.item_id)
    lbl = _fact_label(f, amt, open_c, tol, _age(row, date)) if f else None
    if xb and lbl is None and not any(abs(amt - round(open_c * fr)) <= 1 for fr in PARTIAL_FRACS + DISPUTE_FRACS + [0.95, 0.98, 0.99]):
        fee = open_c - amt
        if 0 < fee <= BC_MAX_PCT * open_c and st.convert(fee, info[3], "EUR", date) <= BC_MAX_EUR * 100:
            return Alloc(a.item_id, open_c, "SHORT_BANK_CHARGES")
    return Alloc(a.item_id, amt, "PARTIAL" if lbl is None else lbl)


def _normal(rows, inv_idx, cn_idx, txn, st, fx, tcur, icur, offset=0, doc=False):
    n = len(rows)
    out = [Alloc(a.item_id, a.amount_cents, "") for a, _, _ in rows]
    date = txn["booking_date"]
    cash = _item_cash(txn, st, icur) - offset
    # tarjeta web y recibos domiciliados: nunca DUPLICATE_PAYMENT (0 de 713 k filas ar_dd_batch en TRAIN)
    if inv_idx and all(rows[i][1][1] in st.noexc for i in inv_idx):
        return out
    # partidas ya cerradas -> DUPLICATE_PAYMENT (salvo resolvers de documentos, ver DOC_RESOLVERS); el resto entra en
    # el cálculo
    live, closed = [], []
    for i in inv_idx:
        a, info, op = rows[i]
        if op <= 0:
            amt = a.amount_cents if a.amount_cents > 0 else (cash if len(inv_idx) == 1 else info[4])
            out[i] = Alloc(a.item_id, amt, "" if doc else "DUPLICATE_PAYMENT")
            closed.append(i)
        else:
            live.append(i)
    live_cn = [i for i in cn_idx if rows[i][2] > 0]
    for i in cn_idx:
        out[i] = Alloc(rows[i][0].item_id, rows[i][2] if rows[i][2] > 0 else rows[i][0].amount_cents, "")
    if not live:
        return out
    dup_cash = sum(out[i].amount_cents for i in closed)
    if dup_cash and len(live) + len(inv_idx) > len(live):
        cash = max(cash - dup_cash, 0) if len(live) else cash
    if all(rows[i][1][1] in st.noexc for i in live):
        return out
    opens = {i: rows[i][2] for i in live}
    E = sum(opens.values()) - sum(rows[i][2] for i in live_cn)
    for i in live:
        out[i] = Alloc(rows[i][0].item_id, opens[i], "")
    tol = max(2, n)
    delta = E - cash
    # importes explícitos del resolver (sin excepción): si cuadran con el cobro, los parciales se respetan
    explicit = sum(rows[i][0].amount_cents for i in live) - sum(rows[i][2] for i in live_cn)
    amts = {i: rows[i][0].amount_cents for i in live}
    if (all(a > 0 for a in amts.values()) and abs(explicit - cash) <= tol and abs(delta) > tol
            and any(amts[i] < opens[i] - tol for i in live) and all(amts[i] <= opens[i] + tol for i in live)):
        for i in live:
            if amts[i] < opens[i] - tol:
                out[i] = _explain_explicit(rows[i], opens[i], amts[i], date, st, tol,
                                                 charges_possible(txn, st, rows[i][1][1]))
        return out

    if fx:
        ratio = cash / E if E > 0 else 1.0
        if _cents(txn.get("orig_amount")) and txn.get("orig_currency") == icur:
            full = abs(delta) <= max(tol, round(E * 0.0005))
        else:
            full = FX_BAND[0] <= ratio <= FX_BAND[1]
        if full:
            return out   # FX_DIFFERENCE se aplica fuera
    if abs(delta) <= tol:
        # liquidación total: el PDF puede marcarla PARTIAL (retención/anticipo/plazo cobrados fuera de orden)
        for i in live:
            f = st.item_facts(rows[i][0].item_id)
            if f and _fact_label(f, opens[i], opens[i], tol, _age(rows[i], date)) == "PARTIAL":
                out[i] = Alloc(rows[i][0].item_id, opens[i], "PARTIAL")
        return out
    if delta < 0:
        if len(live) == 1 and not live_cn and not fx and overpay_shape(cash, opens[live[0]]):
            out[live[0]] = Alloc(rows[live[0]][0].item_id, opens[live[0]], "OVERPAYMENT")
        return out

    # ---- efectivo < esperado: explicar el defecto delta
    # 1. hechos del PDF (retención de garantía, anticipo, plazos): parcial de esa partida
    for i in live:
        f = st.item_facts(rows[i][0].item_id)
        if f and opens[i] > 0:
            applied = opens[i] - delta
            lbl = _fact_label(f, applied, opens[i], tol, _age(rows[i], date))
            if lbl is not None and applied < opens[i] - tol:
                out[i] = Alloc(rows[i][0].item_id, applied, lbl)
                return out
    # 2. pronto pago: subconjunto de facturas con descuento en sus condiciones. Dentro de plazo (+3 días de
    #    gracia) -> SHORT_DISCOUNT; fuera de plazo el descuento no se acepta y queda PARTIAL por lo cobrado.
    elig, dvals = [], []
    for i in live:
        _, party, issue, _, total, terms = rows[i][1]
        t = st.terms.get(terms)
        if t and opens[i] == total and issue is not None:
            elig.append(i)
            dvals.append((total * t[0] + 5000) // 10000)

    def in_window(i):
        _, _, issue, _, _, terms = rows[i][1]
        return (date - issue).days <= st.terms[terms][1] + GRACE_DAYS

    if elig:
        win = [k for k, i in enumerate(elig) if in_window(i)]
        late = [k for k in range(len(elig)) if k not in win]
        sub = None
        sw = sum(dvals[k] for k in win)
        if win and abs(sw - delta) <= tol:                       # todas las de plazo con descuento
            sub = win
        elif win and late and sw < delta:                        # las de plazo + algunas fuera de plazo
            r = _subset_sum([dvals[k] for k in late], delta - sw, 3)
            if r:
                sub = win + [late[x] for x in r]
        if sub is None:
            sub = list(range(len(elig))) if abs(sum(dvals) - delta) <= tol else _subset_sum(dvals, delta, 3)
        if sub:
            for k in sub:
                i = elig[k]
                if in_window(i):
                    out[i] = Alloc(rows[i][0].item_id, opens[i], "SHORT_DISCOUNT")
                else:
                    out[i] = Alloc(rows[i][0].item_id, opens[i] - dvals[k], "PARTIAL")
            return out
    # 3. retención en origen (MX/BR/MA): todas las facturas libres del movimiento
    for rate_idx in (0, 1):
        cand, wvals = [], []
        for i in live:
            _, party, _, _, total, _ = rows[i][1]
            r = WH_BPS.get(st.country.get(party))
            if r and opens[i] == total:
                cand.append(i)
                wvals.append((total * r[rate_idx] + 5000) // 10000)
        if cand:
            # en TRAIN todas las facturas del movimiento llevan la retención (salvo las en disputa); abonos vacíos
            if abs(sum(wvals) - delta) <= tol:
                for i in cand:
                    out[i] = Alloc(rows[i][0].item_id, opens[i], "SHORT_WITHHOLDING")
                return out
    # 4. gastos bancarios del banco intermediario: una sola factura, ≤ 60 EUR y ≤ 2 %
    if len(live) == 1 and not live_cn and charges_possible(txn, st, rows[live[0]][1][1]):
        i = live[0]
        eur = st.convert(delta, icur, "EUR", date)
        if 0 < delta <= BC_MAX_PCT * opens[i] and eur <= BC_MAX_EUR * 100:
            out[i] = Alloc(rows[i][0].item_id, opens[i], "SHORT_BANK_CHARGES")
            return out
    # 5. parcial
    if len(live) == 1 and not live_cn:
        i = live[0]
        out[i] = Alloc(rows[i][0].item_id, max(cash, 0) if cash > 0 else opens[i], "PARTIAL")
        return out
    k, exc = _partial_item(live, opens, delta, tol, rows[live[0]][1][0].startswith("AR"))
    out[k] = Alloc(rows[k][0].item_id, max(opens[k] - delta, 1), exc)
    return out


# fracciones del saldo que se aplican (el resto queda abierto), medidas en TRAIN según el segmento
DISPUTE_FRACS = [0.7, 0.8, 0.9, 0.95, 0.97]                      # ar_short_dispute (AR, varias partidas)
PARTIAL_FRACS = [0.4, 0.5, 0.6, 0.3, 0.25, 1 / 3, 1 / 6, 0.35]   # ar_n_to_m / ap_partial


def _partial_item(live, opens, delta, tol, ar):
    """Qué partida queda a medias y con qué excepción: la que cumple opens[k] - delta = fracción redonda de su saldo.
    AR con varias partidas y fracción de disputa -> SHORT_DISPUTE; el resto PARTIAL. Sin coincidencia: la mayor."""
    for fracs, exc in ((DISPUTE_FRACS if ar else [], "SHORT_DISPUTE"), (PARTIAL_FRACS + ([] if ar else [0.7, 0.9]), "PARTIAL")):
        for k in live:
            if delta < opens[k]:
                applied = opens[k] - delta
                if any(abs(applied - round(opens[k] * f)) <= tol for f in fracs):
                    return k, exc
    cands = [k for k in live if opens[k] > delta]
    return (max(cands, key=lambda k: opens[k]) if cands else live[-1]), "PARTIAL"
