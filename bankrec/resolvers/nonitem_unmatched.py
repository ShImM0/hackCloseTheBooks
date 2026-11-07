"""Último paso de la cascada: movimientos que ningún resolver supo casar. Dueño: P4.

Es el único sitio donde P4 decide UNIDENTIFIED (el fallback de pipeline.py solo ve lo que este paso deja pasar).

Abonos. POLICIES §3: UNIDENTIFIED es un cobro para el que NO EXISTE partida abierta (tercero desconocido,
beneficiario equivocado, ingreso en efectivo…). Que la cascada no encuentre la partida no basta: cada UNIDENTIFIED de
más es un falso positivo de esa clase en el macro-F1. Por orden:
1. Abono agregado (tarjetas/TPV, remesa, lockbox, cheques): liquida los pedidos o cheques de sus ficheros
   (POLICIES §4) → CUSTOMER_ADVANCE (pendiente de aplicar). 100 % con partidas en 2023 y 2024.
2. Reembolso de proveedor (POLICIES §4): el ordenante es un proveedor sin abono APC abierto y el texto es de
   reembolso o cita una factura suya → SUPPLIER_REFUND (si tuviera APC abierta, el reembolso liquidaría la APC).
3. Existe partida probable → CUSTOMER_ADVANCE sin excepción (cobro identificado pendiente de aplicar, cuenta
   438000 «anticipos de clientes»): IBAN de un tercero con partidas abiertas; número impreso de una partida abierta
   citado (≥ 6 dígitos, sin prefijo o cortado: POLICIES §5.5); o nombre del ordenante ≈ cliente/proveedor con
   partidas abiertas (bankrec.parties: nombre actual, comercial, impreso en facturas o de la matriz, §5.1).
   El nombre solo cuenta si identifica: empresa (forma social en el maestro) con ≥ 2 palabras o particular con
   ≥ 3; los no identificados suelen traer «NOMBRE APELLIDO» de particulares que coinciden con clientes. Si el banco
   trae el nombre de la contraparte, debe coincidir entero (un nombre contenido en otro más largo es otra empresa:
   «POOL BENNETT» dentro de «POOL & SPA BENNETT HERMANOS CO.»).
4. Nada de lo anterior → UNIDENTIFIED (category y exception). El audit dice qué candidato se descartó y por qué.

Cargos: solo CUSTOMER_REFUND (POLICIES §4: devolución a un cliente) si el beneficiario es un cliente, ningún
proveedor encaja con el nombre y el cliente no tiene abonos ARC abiertos (eso sería una partida). El resto → None
(fallback de pipeline.py, BANK_FEES).

Medido en val (2025-H1, reports/P4_QA.md): UNIDENTIFIED F1 0,72 → 0,86 (P 0,62 → 0,91). Umbrales redondos y
estables (±0,03 puntos entre vecinos). No lee golden, segmentos ni importes concretos. El audit guarda
«motivo | señales JSON» para poder ajustar reglas sin reejecutar.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from rapidfuzz import fuzz
from unidecode import unidecode

from bankrec import parties, refs
from bankrec.contract import Proposal
from bankrec.resolvers.nonitem import normalized

NAME = "nonitem_unmatched"
PENDING = "CUSTOMER_ADVANCE"   # categoría de un cobro identificado sin partida aplicada
NAME_MIN = 90                  # partial_ratio nombre del tercero vs texto
FULL_MIN = 90                  # ratio nombre del tercero vs nombre de la contraparte (si el banco lo trae)
CAND_MIN = 85                  # candidatos de nombre que se registran en el audit
NAME_LEN = 8                   # nombres normalizados más cortos no deciden (siglas, iniciales)
COMPANY_WORDS = 2              # empresa: forma social + 2 palabras identifica
PERSON_WORDS = 3               # particular: hace falta nombre y dos apellidos (o más)
KEY_LEN = 6                    # dígitos de las claves de número impreso (como text_ref)
MAX_KEY = 300                  # claves de nombre o número demasiado genéricas se ignoran
BATCH = re.compile(r"\bLOCKBOX\b|\bDEPOSIT \d+ ITEMS?\b|\bCARD SETTLEMENT\b|\bSETTLEMENT\b|\bREMESA\b|\bTPV\b"
                   r"|\bACQUIR|\bADQUIRENTE\b|\bCHEQUES?\b|\bCHQ\b|\bCHECKS?\b")
REFUND = re.compile(r"\bREEMBOLSO\b|\bDEVOLUCION\b|\bREFUND\b|\bRIMBORSO\b|\bREMBOURSEMENT\b|\bERSTATTUNG\b"
                    r"|\bDUPLICAD[OA]\b|\bDUPLICATE\b")
# Formas sociales inequívocas: un nombre con alguna es de una empresa, no de un particular.
LEGAL_FORMS = frozenset("SL SLU SA SAU SCOOP SRL SRLS SNC SAS SARL EURL SASU GMBH MBH KG AG UG OHG LTD LIMITED PLC "
                        "LLP PTY LLC INC CORP CORPORATION BV NV LDA LTDA EIRELI SPA SPZOO CO".split())


def _bigrams(text: str) -> set[str]:
    w = text.split()
    return {f"{a} {b}" for a, b in zip(w, w[1:])}


def _name_keys(name: str, df: Counter) -> list[str]:
    """Los dos pares de palabras seguidas menos frecuentes del nombre: los nombres sintéticos repiten vocabulario
    ("POOL BUILDERS", "SUN PRAIRIE HILLS") y una sola palabra es demasiado genérica; dos claves toleran cortes."""
    return sorted(_bigrams(name), key=lambda b: (df[b], b))[:2]


def is_company(raw: str | None) -> bool:
    """'Porto Alvor Jardins S.A.' -> True; 'Raquel Gómez Garrido' -> False."""
    words = re.sub(r"[^A-Z0-9 ]+", " ", unidecode(raw or "").upper().replace(".", "")).split()
    return any(w in LEGAL_FORMS for w in words) or any(f"{a}{b}" in LEGAL_FORMS for a, b in zip(words, words[1:]))


def prepare(ctx) -> None:
    """Índices por sociedad: partidas con saldo (ARI/APC para abonos, ARC para cargos), nombres de todos sus
    terceros (bigramas raros) y números impresos (dígitos → item_id). Maestros vía ctx.master (FINAL: masters_final)."""
    parties.prepare(ctx)
    names = ctx.shared["parties"]["names"]
    items = ctx.items
    raw: dict[str, set[str]] = defaultdict(set)
    ent_of: dict[str, set[str]] = defaultdict(set)
    for cid, ent, n, tn in ctx.master("customers").select("customer_id", "entity", "name", "trade_name").iter_rows():
        ent_of[cid].add(ent)
        raw[cid].update(x for x in (n, tn) if x)
    for sid, ent, n in ctx.master("suppliers").select("supplier_id", "entity", "name").iter_rows():
        ent_of[sid].add(ent)
        if n:
            raw[sid].add(n)
    for pid, ent, pn in items.select("party_id", "entity", "party_name").unique().iter_rows():
        if pid:
            ent_of[pid].add(ent)
            if pn:
                raw[pid].add(pn)
    company = {pid: any(is_company(n) for n in ns) for pid, ns in raw.items()}
    items_of: dict[str, list[tuple[str, str, object]]] = defaultdict(list)
    ref_idx: dict[tuple, list[str]] = defaultdict(list)
    info: dict[str, tuple] = {}
    api_of: dict[str, list[str]] = defaultdict(list)
    for iid, typ, ent, pid, dn, issued, op in items.select("item_id", "item_type", "entity", "party_id",
                                                          "document_number", "issue_date", "open_cents").iter_rows():
        info[iid] = (typ, pid, dn, issued)
        if typ == "API" and dn:
            api_of[pid].append(dn)   # facturas del proveedor (también cerradas): las cita su reembolso
        if op <= 0 or typ not in ("ARI", "APC", "ARC"):
            continue
        items_of[pid].append((iid, typ, issued))
        if typ == "ARC":
            continue
        d = refs.digits(dn)
        if len(d) >= KEY_LEN:
            ref_idx[(ent, "s" + d[-KEY_LEN:])].append(iid)
            ref_idx[(ent, "p" + d[:KEY_LEN])].append(iid)
    name_idx: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    by_ent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pid, ents in ent_of.items():
        for n in names.get(pid, ()):
            if len(n) >= NAME_LEN and len(n.split()) >= 2:
                for ent in ents:
                    by_ent[ent].append((n, pid))
    for ent, pairs in by_ent.items():
        df = Counter(b for n, _ in pairs for b in _bigrams(n))
        for n, pid in pairs:
            for k in _name_keys(n, df):
                name_idx[(ent, k)].append((n, pid))
    ctx.shared[NAME] = {"items_of": items_of, "ref": ref_idx, "name": name_idx, "company": company,
                        "info": info, "api_of": api_of}


def _open(S, pid, ledger, day, types) -> tuple[int, int]:
    """(partidas abiertas emitidas hasta el día del movimiento, abiertas en total) del tercero, de los tipos dados."""
    before = total = 0
    for iid, typ, issued in S["items_of"].get(pid, ()):
        if typ in types and ledger.is_open(iid):
            total += 1
            if issued is None or issued <= day:
                before += 1
    return before, total


def _candidates(txn, tctx, ctx, prefix: str) -> list[tuple[str, int, str]]:
    """Terceros cuyo nombre aparece en el movimiento, con prefijo de id (C<sociedad> clientes, S<sociedad>
    proveedores): (party, score, nombre que mejor encaja). Une parties.find (IBAN, contraparte exacta, palabras
    raras) y el índice de bigramas (nombres de vocabulario común, que parties.find no busca)."""
    S = ctx.shared[NAME]
    names = ctx.shared["parties"]["names"]
    hay = tctx["hay"]
    pids = {p for p, _ in parties.find(tctx, ctx, prefix)}
    for b in _bigrams(hay):
        cands = S["name"].get((txn["entity"], b), ())
        if len(cands) <= MAX_KEY:
            pids.update(p for _, p in cands if p.startswith(prefix))
    out = []
    for pid in pids:
        best, bn = 0, ""
        for n in names.get(pid, ()):
            if len(n) >= NAME_LEN and len(n.split()) >= 2:
                s = fuzz.partial_ratio(n, hay)
                if s > best or (s == best and len(n) > len(bn)):
                    best, bn = s, n
        if best >= CAND_MIN:
            out.append((pid, round(best), bn))
    return sorted(out, key=lambda x: (-x[1], -len(x[2].split()), x[0]))


def features(txn: dict, ctx, ledger) -> dict:
    """Señales del movimiento; se guardan en el audit además de decidir."""
    S = ctx.shared[NAME]
    ent, day = txn["entity"], txn["booking_date"]
    text = normalized(" ".join(txn.get(k) or "" for k in ("text", "remittance_info")))
    tctx = parties.context(txn)
    credit = txn["amount_cents"] > 0
    f: dict = {"lote": (m.group(0) if (m := BATCH.search(text)) else ""), "reemb": bool(REFUND.search(text)),
               "cp": bool(tctx["cp"])}
    f["iban"] = []
    for ib in tctx["ibans"]:
        for pid in sorted(ctx.shared["parties"]["iban"].get(ib, ())):
            ab, aa = _open(S, pid, ledger, day, ("ARI", "APC") if credit else ("ARC", "API"))
            f["iban"].append({"pid": pid, "ab": ab, "aa": aa})
    best = None
    toks = [t for t in refs.tokens(refs.haystack(txn)) if len(t) >= KEY_LEN]
    for tok in toks:
        for key in ("s" + tok[-KEY_LEN:], "p" + tok[:KEY_LEN]):
            hits = S["ref"].get((ent, key), ())
            if len(hits) > MAX_KEY:
                continue
            for iid in hits:
                if not ledger.is_open(iid):
                    continue
                typ, pid, dn, issued = S["info"][iid]
                cand = {"iid": iid, "rs": refs.ref_score(dn, toks), "antes": issued is None or issued <= day,
                        "pf": round(parties.score(tctx, pid, ctx)), "t": typ}
                k = (cand["antes"], cand["rs"], cand["pf"])
                if best is None or k > best[0]:
                    best = (k, cand)
    f["ref"] = best[1] if best else None
    for prefix, key in (("C" + ent, "cli"), ("S" + ent, "pro")):
        f[key] = []
        for pid, sc, n in _candidates(txn, tctx, ctx, prefix)[:3]:
            types = (("ARI",) if key == "cli" else ("APC",)) if credit else (("ARC",) if key == "cli" else ("API",))
            ab, aa = _open(S, pid, ledger, day, types)
            c = {"pid": pid, "sc": sc, "w": len(n.split()), "emp": S["company"].get(pid, False), "ab": ab, "aa": aa,
                 "n": n, "cpr": round(fuzz.ratio(tctx["cp"], n)) if tctx["cp"] else None}
            if key == "pro":
                c["api"] = bool(toks) and any(refs.ref_score(dn, toks) >= 85 for dn in S["api_of"].get(pid, ()))
            f[key].append(c)
    return f


def _name_ok(c: dict) -> bool:
    """El nombre aparece en el texto y, si el banco trae la contraparte, coincide con ella entera."""
    return c["sc"] >= NAME_MIN and (c["cpr"] is None or c["cpr"] >= FULL_MIN)


def _identifies(c: dict) -> bool:
    return c["w"] >= PERSON_WORDS or (c["emp"] and c["w"] >= COMPANY_WORDS)


def _why_not(c: dict) -> str:
    """Por qué un candidato de nombre no basta (para el audit de UNIDENTIFIED)."""
    if c["sc"] < NAME_MIN:
        return f"parecido {c['sc']} < {NAME_MIN}"
    if c["cpr"] is not None and c["cpr"] < FULL_MIN:
        return f"la contraparte es otro nombre (coincidencia {c['cpr']})"
    if not c["aa"]:
        return "sin partidas abiertas"
    return f"{'empresa' if c['emp'] else 'particular'} de {c['w']} palabras: no identifica"


def decide(f: dict, credit: bool) -> tuple[str, str, str] | None:
    """(category, exception, motivo) o None (cargo sin decisión → fallback)."""
    if not credit:
        cli = [c for c in f["cli"] if _name_ok(c) and c["w"] >= COMPANY_WORDS]
        pro = [c for c in f["pro"] if c["sc"] >= NAME_MIN]
        if cli and not pro and not any(c["aa"] for c in cli):
            c = cli[0]
            return ("CUSTOMER_REFUND", "", f"CUSTOMER_REFUND (POLICIES §4): pago a cliente {c['pid']} ({c['n']}) sin abono "
                                         "ARC abierto ni proveedor con ese nombre")
        return None
    if f["lote"]:
        return (PENDING, "", f"abono agregado sin casar ({f['lote']}): liquida pedidos o cheques de sus ficheros, "
                             "no es UNIDENTIFIED (POLICIES §4)")
    for c in f["pro"]:
        if _name_ok(c) and not c["aa"] and (f["reemb"] or c["api"]):
            return ("SUPPLIER_REFUND", "", f"SUPPLIER_REFUND (POLICIES §4): abono del proveedor {c['pid']} ({c['n']}) sin "
                                         f"abono APC abierto; {'cita una factura suya' if c['api'] else 'texto de reembolso'}")
    for x in f["iban"]:
        if x["aa"]:
            return (PENDING, "", f"no casado; IBAN de {x['pid']} con {x['aa']} partidas abiertas")
    if f["ref"]:
        return (PENDING, "", f"no casado; el texto cita el número de {f['ref']['iid']} (abierta)")
    for c in f["cli"] + f["pro"]:
        if _name_ok(c) and c["aa"] and _identifies(c):
            return (PENDING, "", f"no casado; ordenante ≈ {c['n']} ({c['pid']}, {'empresa' if c['emp'] else 'particular'}, "
                                 f"{c['w']} palabras) con {c['aa']} partidas abiertas")
    rejected = (f["cli"] + f["pro"])[:1]
    tail = f"; descartado {rejected[0]['n']} ({rejected[0]['pid']}): {_why_not(rejected[0])}" if rejected else ""
    return ("UNIDENTIFIED", "UNIDENTIFIED", "UNIDENTIFIED (POLICIES §3): ningún IBAN, número citado ni nombre "
                                           f"distintivo lo ata a una partida abierta{tail}")


def resolve(txn: dict, ctx, ledger) -> Proposal | None:
    credit = txn["amount_cents"] > 0
    f = features(txn, ctx, ledger)
    d = decide(f, credit)
    if d is None:
        return None
    cat, exc, why = d
    conf = {"UNIDENTIFIED": 0.5, PENDING: 0.1}.get(cat, 0.6)
    blob = json.dumps(f, ensure_ascii=False, separators=(",", ":"), default=str)
    return Proposal(txn["bank_txn_id"], category=cat, exception=exc, confidence=conf, resolver=NAME,
                    reason=f"{why} | {blob}")
