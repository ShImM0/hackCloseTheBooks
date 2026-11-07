"""Conceptos que citan varias facturas. Dueño: P2.

Qué hace (POLICIES §5.5 referencias ruidosas, §5.9 abonos netos, §3 excepciones): el texto cita números impresos
("FACTURA 223672 221609"); si resuelven a partidas abiertas de un único tercero que además encaja con el movimiento
(parties), se busca el conjunto de partidas que explica el importe:
1. suma exacta de las citadas + abonos (citados por su número o que citan una factura citada en `related_document`;
   un abono citado trae su factura aunque el texto cite otro número, §5.9). Si no cuadra con todas, el subconjunto de
   citadas que usa más partidas (referencias ajenas o ya cobradas); empate -> None.
2. concepto cortado (§5.5: el banco corta a 16/35/38 caracteres y el texto libre a ~140): las citadas no llegan al
   importe y el resto son otras partidas abiertas del mismo tercero (solo si el tercero está claro, PARTY_FILL): todo
   lo abierto hasta una fecha (FIFO, §5.6; dos cortes posibles = None) o el único subconjunto pequeño (<= MAX_FILL)
   con suma exacta. No entran pares factura + abono que la anula por el mismo importe (suman 0: §5.7, no se
   liquidan por banco y harían ambigua la suma).
3. diferencia explicada (§3): las citadas suman más que el cobro y la diferencia es el pronto pago de un subconjunto
   de ellas (payment_terms con descuento), la retención en origen de todas (MX/BR/MA) o el parcial de UNA sola
   citada por una fracción redonda de su saldo. Se proponen al saldo abierto y exceptions.finalize (P3) decide
   SHORT_DISCOUNT / PARTIAL / SHORT_WITHHOLDING / SHORT_DISPUTE y el importe aplicado.
Números cortados con prefijo genérico (más de MAX_POOL partidas en la entidad): elegido el tercero, se buscan en sus
facturas abiertas y solo cuentan si el token casa con UNA. Si alguna factura citada tiene saldo == importe y no hay
suma exacta de >= 2 partidas, es un 1:1: lo resuelve text_ref (2 y 3 no se intentan).
Cobro en otra divisa (§3 FX_DIFFERENCE): partidas en `orig_currency` y objetivo `orig_amount` (divisa de la partida).
Sin referencias lo resuelve subset_sum. Precisión antes que recall.
Tolera número sin prefijo, cortado y con erratas (refs.ref_score >= REF_MIN); el tercero debe encajar (parties).
Entradas: txn, ctx, ledger. Salida: Proposal con una Alloc por partida al saldo abierto, o None.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from itertools import combinations

from bankrec import parties, refs
from bankrec.contract import Alloc, Proposal
from bankrec.exceptions import DISPUTE_FRACS, PARTIAL_FRACS, WH_BPS   # §3 SHORT_WITHHOLDING: POLICIES cita MX/BR/MA y 1,25/1,5/10 % (de P3)
from bankrec.resolvers.amount_match import PAYMENT_FACTORY

NAME = "multi_ref"
REF_MIN = 85        # sufijo/prefijo >= 5 dígitos o número completo (las erratas, 75, no deciden solas)
PARTY_MIN = 80      # el tercero debe encajar: el número puede coincidir con el de otro cliente
PARTY_GAP = 5       # con varios terceros candidatos, ventaja mínima del primero (1-5 igual en val; 10 y 20 pierden)
PARTY_FILL = 90     # relleno de concepto cortado (partidas no citadas): tercero claro (nombre casi exacto o IBAN = 100)
FILL_ON = True      # relleno FIFO / subconjunto pequeño (multi_ref_fill de P1 hace después el subconjunto único)
KEY_LEN = 6         # índice por los 6 últimos y 6 primeros dígitos del número impreso
MAX_POOL = 300      # claves con más partidas son prefijos genéricos: se ignoran (luego se buscan dentro del tercero)
MAX_CITED = 12      # subconjuntos de citadas solo hasta 12 (2^12); con más, solo el conjunto completo
MAX_NOTES = 4       # abonos de compensación que se ofrecen como máximo
MAX_FILL = 3        # relleno de concepto cortado: partidas no citadas por subconjunto (FIFO no tiene límite)
FILL_POOL = 40      # con más no citadas abiertas, el subconjunto de relleno se limita a 2 partidas
DISC_MAX = 16       # facturas con descuento para buscar el subconjunto que explica la diferencia

_TYPES = {True: ("ARI", "ARC"), False: ("API", "APC")}   # True = abono bancario (cobro)


def prepare(ctx) -> None:
    """ctx.shared['multi_ref'] = {'idx': (entidad, clave) -> [item_id], 'rel': ARC/APC -> dígitos de su factura,
    'notes': tercero -> [ARC/APC], 'party': tercero -> [item_id], 'doc': (tercero, dígitos) -> [item_id],
    'tot': item -> (total, payment_terms), 'terms': código -> (descuento en 1/10000), 'country': cliente -> país}."""
    parties.prepare(ctx)
    idx: dict[tuple, list[str]] = defaultdict(list)
    rel: dict[str, str] = {}
    party: dict[str, list[str]] = defaultdict(list)
    doc: dict[tuple, list[str]] = defaultdict(list)
    tot: dict[str, tuple] = {}
    it = ctx.items.filter(ctx.items["open_cents"] > 0)
    for iid, typ, ent, pid, dn, rd, total, pt in it.select(
            "item_id", "item_type", "entity", "party_id", "document_number", "related_document", "total_cents",
            "payment_terms").iter_rows():
        d = refs.digits(dn)
        if len(d) >= KEY_LEN:
            idx[(ent, "s" + d[-KEY_LEN:])].append(iid)
            idx[(ent, "p" + d[:KEY_LEN])].append(iid)
        elif len(d) >= 4:
            idx[(ent, "s" + d)].append(iid)
        if typ in ("ARC", "APC") and rd:
            rel[iid] = refs.digits(rd)
        party[pid].append(iid)
        if d:
            doc[(pid, d)].append(iid)
        tot[iid] = (total, pt)
    notes: dict[str, list[str]] = defaultdict(list)
    for iid, pid in it.filter(it["item_type"].is_in(["ARC", "APC"])).select("item_id", "party_id").iter_rows():
        notes[pid].append(iid)
    terms = {}
    for code, pct in ctx.master("payment_terms").select("code", "early_payment_discount_pct").iter_rows():
        if pct and float(pct) > 0:
            terms[code] = round(float(pct) * 100)
    cu = ctx.master("customers")
    country = dict(zip(cu["customer_id"].to_list(), cu["country"].to_list()))
    ctx.shared[NAME] = {"idx": idx, "rel": rel, "notes": notes, "party": party, "doc": doc, "tot": tot,
                        "terms": terms, "country": country}


def _pick(vals: dict[str, int], cited: list[str], notes: list[str], target: int) -> list[str] | None:
    """Subconjunto de `cited` (+ abonos) con suma == target. Prefiere el que usa más citadas; empate -> None."""
    n = len(cited)
    best: list[list[str]] = []
    ks = range(n, 0, -1) if n <= MAX_CITED else (n,)
    for k in ks:
        for comb in combinations(cited, k):
            base = sum(vals[c] for c in comb)
            if base == target:
                sols = [list(comb)]
            else:
                sols = []
                for m in range(1, min(len(notes), MAX_NOTES) + 1):
                    for nc in combinations(notes, m):
                        if base - sum(vals[x] for x in nc) == target:
                            sols.append(list(comb) + list(nc))
            best.extend(sols)
        if best:
            break      # solución con más citadas: no se miran subconjuntos menores
    if len(best) != 1 or len(best[0]) < 2:
        return None
    return best[0]


def _fill(units: list[tuple[list[str], int]], rest: int) -> list[str] | None:
    """Concepto cortado: partidas no citadas que cubren `rest`. `units` en orden FIFO: (factura + abonos que la citan
    en related_document, importe neto; §5.9 el abono se compensa con su factura) o abono suelto (negativo).
    Todo lo abierto hasta una fecha (FIFO, §5.6; dos cortes posibles = ambiguo) o el único subconjunto de
    <= MAX_FILL facturas (con sus abonos) con suma exacta."""
    if rest <= 0 or not units:
        return None
    acc, hits = 0, []
    for k, (_, v) in enumerate(units):
        acc += v
        if acc == rest:
            hits.append(k)
    if hits:
        return [m for u in units[:hits[0] + 1] for m in u[0]] if len(hits) == 1 else None
    inv = [u for u in units if u[1] > 0]
    kmax = MAX_FILL if len(inv) <= FILL_POOL else 2
    by_val: dict[int, list[int]] = defaultdict(list)
    for k, u in enumerate(inv):
        by_val[u[1]].append(k)
    sols: list[list[int]] = [[k] for k in by_val.get(rest, ())]
    for size in range(2, kmax + 1):
        for comb in combinations(range(len(inv)), size - 1):
            r = rest - sum(inv[k][1] for k in comb)
            if r <= 0:
                continue
            for k in by_val.get(r, ()):
                if k > comb[-1]:
                    sols.append(list(comb) + [k])
            if len(sols) > 1:
                return None
    return [m for k in sols[0] for m in inv[k][0]] if len(sols) == 1 else None


def _tok_hit(d: str, t: str) -> bool:
    """El token t es el número d entero, su final o su comienzo (>= 5 dígitos)."""
    return d == t or (len(t) >= 5 and (d.endswith(t) or d.startswith(t)))


def _subset_tol(vals: list[int], target: int, tol: int) -> bool:
    """¿Algún subconjunto no vacío de `vals` suma target ± tol? (meet-in-the-middle, n <= DISC_MAX)."""
    n = len(vals)
    if not n or n > DISC_MAX or target <= 0:
        return False
    h = n // 2

    def sums(v):
        out = [0]
        for x in v:
            out += [s + x for s in out]
        return out
    left, right = sums(vals[:h]), sorted(sums(vals[h:]))
    for s in left:
        i = bisect.bisect_left(right, target - tol - s)
        if i < len(right) and right[i] <= target + tol - s and (s or right[i]):
            return True
    return False


def _explained(sh: dict, pid: str, cited: list[str], ledger, delta: int, tol: int) -> str:
    """Diferencia (saldos − efectivo > 0) explicada por §3: pronto pago de un subconjunto de las citadas (al total,
    dentro o fuera de plazo: lo decide finalize) o retención en origen de todas. '' si no."""
    disc = []
    for c in cited:
        total, pt = sh["tot"].get(c, (0, None))
        bps = sh["terms"].get(pt)
        if bps and ledger.open[c] == total:
            disc.append((total * bps + 5000) // 10000)
    if disc and (abs(sum(disc) - delta) <= tol or _subset_tol(disc, delta, min(tol, 3))):
        return "pronto pago"
    rates = WH_BPS.get(sh["country"].get(pid))
    if rates and all(ledger.open[c] == sh["tot"].get(c, (0,))[0] for c in cited):
        for bps in rates:
            if abs(sum((sh["tot"][c][0] * bps + 5000) // 10000 for c in cited) - delta) <= tol:
                return "retención"
    # §2/§3 PARTIAL / SHORT_DISPUTE: una sola citada queda a medias por una fracción redonda de su saldo (fracciones
    # de P3; finalize elige la misma partida) y el resto se liquida entero
    frac = [k for k in cited if 0 < delta < ledger.open[k] and any(
        abs(ledger.open[k] - delta - round(ledger.open[k] * f)) <= tol for f in DISPUTE_FRACS + PARTIAL_FRACS)]
    if len(frac) == 1:
        return "parcial"
    return ""


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    amt = txn["amount_cents"]
    if not amt:
        return None
    toks = [t for t in refs.tokens(refs.joined(refs.haystack(txn))) if len(t) >= 5]
    if not toks:
        return None
    sh = ctx.shared[NAME]
    credit = amt > 0
    types = _TYPES[credit]
    ents = (txn["entity"],) if credit else PAYMENT_FACTORY.get(txn["entity"], (txn["entity"],))
    cur, day = txn["currency"], txn["booking_date"]
    target = abs(amt)
    # §3 FX_DIFFERENCE: cobro en otra divisa -> partidas en la divisa original y objetivo = importe original del banco
    # (orig_amount); finalize (P3) pone FX_DIFFERENCE en las facturas y deja los abonos sin excepción
    if credit and txn.get("orig_currency") and txn["orig_currency"] != cur and txn.get("orig_amount_cents"):
        cur, target = txn["orig_currency"], abs(txn["orig_amount_cents"])

    def live(c: str) -> bool:
        i = ledger.info[c]
        return (ledger.open.get(c, 0) > 0 and i[2] == cur and i[1] in ents and i[0] in types
                and not (i[4] and i[4] > day))

    pool: set[str] = set()
    for e in ents:
        for t in toks:
            for key in {"s" + t[-KEY_LEN:], "p" + t[:KEY_LEN], "s" + t}:
                hit = sh["idx"].get((e, key), ())
                if len(hit) <= MAX_POOL:
                    pool.update(hit)
    by_party: dict[str, list[str]] = defaultdict(list)
    cited_notes: dict[str, set[str]] = defaultdict(set)     # abonos citados por su propio número
    for c in pool:
        if not live(c):
            continue
        i = ledger.info[c]
        if refs.ref_score(i[5], toks) >= REF_MIN:
            if i[0] == types[0]:
                by_party[i[3]].append(c)
            else:
                cited_notes[i[3]].add(c)
    # §5.9: un abono citado trae su factura (related_document) aunque el texto cite otro número
    for p, ns in cited_notes.items():
        for n in ns:
            for c in sh["doc"].get((p, sh["rel"].get(n, "")), ()):
                if live(c) and ledger.info[c][0] == types[0] and c not in by_party[p]:
                    by_party[p].append(c)
    # candidatos: tercero con alguna factura citada. Una sola factura sin abono solo si el importe no es su saldo
    # (1:1 exacto = text_ref) y hay que completar con otras partidas (concepto cortado)
    cands = {}
    for p, v in by_party.items():
        if not v:
            continue
        nums = {refs.digits(ledger.info[c][5]) for c in v}
        if len(v) >= 2 or cited_notes.get(p) or any(sh["rel"].get(n) in nums for n in sh["notes"].get(p, ())):
            cands[p] = v
        elif ledger.open[v[0]] < target:
            cands[p] = v
    if not cands:
        return None
    tctx = parties.context(txn)
    ok = []
    for pid, cited in cands.items():
        s = parties.score(tctx, pid, ctx)
        if s >= PARTY_MIN:
            ok.append((s, pid, cited))
    if not ok:
        return None
    # varios terceros encajan: gana el de mayor encaje si saca PARTY_GAP al segundo (un número cortado puede ser
    # también de otro cliente con nombre parecido); si no, ambiguo
    ok.sort(key=lambda x: -x[0])
    if len(ok) > 1 and ok[0][0] - ok[1][0] < PARTY_GAP:
        return None
    s, pid, cited = ok[0]
    # una factura citada con saldo == importe es un 1:1 (text_ref): solo se acepta aquí la suma exacta de >= 2
    one_to_one = any(ledger.open[c] == target for v in by_party.values() for c in v)
    mine = [c for c in sh["party"].get(pid, ()) if live(c)]
    vals = {c: ledger.open[c] for c in mine}
    # pares que se anulan (abono que cita su factura por el mismo importe): no los mete el relleno ni la re-búsqueda
    zero: set[str] = set()
    for n in mine:
        if ledger.info[n][0] == types[1]:
            for c in sh["doc"].get((pid, sh["rel"].get(n, "")), ()):
                if c in vals and vals[c] == vals[n]:
                    zero.update((c, n))
    # números cortados con prefijo genérico (clave > MAX_POOL): se buscan dentro de las facturas del tercero, solo si
    # el token casa con UNA partida suya (un prefijo de 5 dígitos puede valer para varias)
    have = set(cited)
    dig = [(c, refs.digits(ledger.info[c][5])) for c in mine if ledger.info[c][0] == types[0]]
    for t in set(toks):
        hit = [c for c, d in dig if _tok_hit(d, t)]
        if len(hit) == 1 and hit[0] not in have and hit[0] not in zero \
                and refs.ref_score(ledger.info[hit[0]][5], toks) >= REF_MIN:
            cited.append(hit[0])
            have.add(hit[0])
    cited.sort(key=ledger._fifo)
    # abonos abiertos del tercero que citan alguna de las facturas citadas o están citados
    cited_digits = {refs.digits(ledger.info[c][5]) for c in cited}
    notes = [n for n in sh["notes"].get(pid, ())
             if live(n) and (sh["rel"].get(n) in cited_digits or n in cited_notes.get(pid, ()))]
    vals.update({c: ledger.open[c] for c in cited + notes})
    sol, how = None, ""
    if len(cited) + len(notes) >= 2:
        sol, how = _pick(vals, cited, notes, target), "suman el importe"
    if sol is None:
        if one_to_one:
            return None
        base = sum(vals[c] for c in cited) - sum(vals[n] for n in notes)
        if base < target and s >= PARTY_FILL and FILL_ON:
            # concepto cortado: el resto, con otras partidas abiertas del tercero (orden FIFO), sin pares que se anulan
            used = set(cited) | set(notes)
            others = sorted((c for c in mine if c not in used and c not in zero), key=ledger._fifo)
            oset, att, attached = set(others), defaultdict(list), set()
            for n in others:
                if ledger.info[n][0] == types[1]:
                    for c in sh["doc"].get((pid, sh["rel"].get(n, "")), ()):
                        if c in oset:
                            att[c].append(n)
                            attached.add(n)
                            break
            units = [([c] + att[c], sum(vals[m] if ledger.info[m][0] == types[0] else -vals[m] for m in [c] + att[c]))
                     for c in others if c not in attached]
            extra = _fill(units, target - base)
            if extra:
                sol, how = cited + notes + extra, f"+{len(extra)} no citadas (concepto cortado)"
        elif base > target and len(cited) >= 2:
            why = _explained(sh, pid, cited, ledger, base - target, max(2, len(cited) + len(notes)))
            if why:
                sol, how = cited + notes, f"diferencia por {why}"
    if not sol:
        return None
    return Proposal(txn["bank_txn_id"], [Alloc(i, ledger.open[i]) for i in sol], confidence=0.9 * min(s, 100) / 100,
                    reason=f"tercero {pid} ({s:.0f}): {len(sol)} partidas, {how}")


if __name__ == "__main__":
    u = [(["a"], 100), (["b", "n"], 150), (["c"], 300)]
    assert _fill(u, 250) == ["a", "b", "n"]                                     # FIFO, abono con su factura
    assert _fill([(["a"], 100), (["b"], 200), (["c"], 300)], 500) == ["b", "c"]  # subconjunto único
    assert _fill([(["a"], 100), (["d"], 200), (["b"], 200), (["c"], 300)], 200) is None   # ambiguo
    assert _pick({"x": 100, "y": 50, "z": 30}, ["x", "y"], ["z"], 120) == ["x", "y", "z"]
    assert _pick({"x": 100, "y": 50}, ["x", "y"], [], 100) is None              # 1:1 -> text_ref
    assert _subset_tol([600, 900, 1200], 1500, 2) and not _subset_tol([600, 900], 1000, 2)
    assert _tok_hit("250100840", "100840") and _tok_hit("250100840", "25010") and not _tok_hit("250100840", "0108")
    print("multi_ref OK")
