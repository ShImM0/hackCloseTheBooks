"""Mide solo exceptions.finalize usando el golden como resolver perfecto. Dueño: P3 (herramienta de desarrollo).

    uv run python -m bankrec.exceptions_oracle [--phase val|sample] [--months N] [--dump N] [--segment S]

Para cada movimiento con partidas en el golden se construye una Proposal con esas partidas y como importe el
saldo abierto (lo que daría un resolver que no sabe de excepciones), se llama a finalize y se compara fila a
fila con el golden (excepción e importe con la tolerancia del scorer). El ledger evoluciona con el golden, así
que un fallo no se arrastra. Salida: matriz de confusión por excepción, precisión por segmento, acierto por
movimiento y los N fallos más frecuentes (--dump) con el contexto del movimiento.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

import polars as pl

from bankrec import exceptions, pdf_facts, pdf_text, phase as phase_mod
from bankrec.contract import Alloc, Proposal
from bankrec.ledger import Ledger


def _cash_share(ctx, it, amt, exc, explicit, ledger) -> int:
    """Importe que daría el resolver: el saldo abierto; con --explicit, el efectivo real del aviso (neto del descuento o
    de la retención en origen) para las filas SHORT_DISCOUNT / SHORT_WITHHOLDING."""
    if explicit and exc == "PARTIAL":
        return amt
    if explicit and exc in ("SHORT_DISCOUNT", "SHORT_WITHHOLDING"):
        st = exceptions._state(ctx)
        info = st.items[it]
        total = info[4]
        if exc == "SHORT_DISCOUNT":
            return total - (total * st.terms[info[5]][0] + 5000) // 10000
        r = exceptions.WH_BPS.get(st.country.get(info[1]), (125,))
        # el golden no dice qué tasa: se prueba la menor y se corrige con la mayor si no cuadra en la fase real
        return total - (total * r[0] + 5000) // 10000
    return ledger.open.get(it, 0)


def run(phase_name: str, months: int | None, segment: str | None, dump: int, lock: set[str] = frozenset(), explicit: bool = False) -> None:
    ctx = phase_mod.build(phase_name, months)
    pdf_text.prepare(ctx)
    pdf_facts.prepare(ctx)
    gold = ctx.golden.with_columns(pl.col("exception").fill_null("")).filter(pl.col("item_id").is_not_null())
    by_txn: dict[str, list] = defaultdict(list)
    for r in gold.select("bank_txn_id", "item_id", "amount_cents", "exception", "segment").iter_rows():
        by_txn[r[0]].append(r[1:])
    ledger = Ledger(ctx.items)
    conf: Counter = Counter()
    seg_n: Counter = Counter()
    seg_ok: Counter = Counter()
    fails = []
    n_txn = n_txn_ok = 0
    for txn in ctx.txns.iter_rows(named=True):
        g = by_txn.get(txn["bank_txn_id"])
        if not g:
            continue
        seg = g[0][3]
        if segment and seg != segment:
            for it, amt, exc, _ in g:
                ledger.apply(Proposal("", [Alloc(it, amt, exc)]))
            continue
        # filas que un resolver ya fijaría (--lock): se pasan con su importe y excepción del golden
        prop = Proposal(txn["bank_txn_id"], [Alloc(it, amt, exc) if exc in lock else Alloc(it, _cash_share(ctx, it, amt, exc, explicit, ledger), "")
                                             for it, amt, exc, _ in g])
        before = {it: ledger.open.get(it, 0) for it, _, _, _ in g}
        try:
            out = exceptions.finalize(prop, txn, ctx, ledger) or prop
        except Exception:
            import traceback
            traceback.print_exc()
            out = prop
        pred = {a.item_id: a for a in out.allocs}
        ok_all = True
        for it, amt, exc, _ in g:
            a = pred[it]
            amt_ok = abs(a.amount_cents - amt) <= max(1, 0.005 * amt)
            conf[(exc or "-", a.exception or "-")] += 1
            if a.exception != exc or not amt_ok:
                ok_all = False
        n_txn += 1
        n_txn_ok += ok_all
        seg_n[seg] += 1
        seg_ok[seg] += ok_all
        if not ok_all and len(fails) < 200000:
            fails.append((seg, txn, g, out.allocs, before))
        ledger.apply(Proposal("", [Alloc(it, amt, exc) for it, amt, exc, _ in g]))

    print(f"movimientos con partidas: {n_txn}  acierto exacto (excepción+importe de todas las filas): {n_txn_ok / n_txn:.4f}")
    classes = sorted({k[0] for k in conf} | {k[1] for k in conf})
    f1s = []
    print(f"{'clase':20s} {'gold':>8s} {'pred':>8s} {'TP':>8s} {'P':>6s} {'R':>6s} {'F1':>6s}")
    for c in classes:
        g_n = sum(v for k, v in conf.items() if k[0] == c)
        p_n = sum(v for k, v in conf.items() if k[1] == c)
        tp = conf[(c, c)]
        p, r = tp / p_n if p_n else 0, tp / g_n if g_n else 0
        f1 = 2 * p * r / (p + r) if p + r else 0
        if g_n:
            f1s.append(f1)
        print(f"{c:20s} {g_n:8d} {p_n:8d} {tp:8d} {p:6.3f} {r:6.3f} {f1:6.3f}")
    print(f"macro-F1 (clases en golden): {sum(f1s) / len(f1s):.4f}")
    print("confusiones principales (golden -> predicho):")
    for (a, b), v in conf.most_common(40):
        if a != b:
            print(f"  {a:20s} -> {b:20s} {v}")
    print("segmentos (movimientos fallados):")
    for s, n in sorted(seg_n.items(), key=lambda x: -(x[1] - seg_ok[x[0]]))[:25]:
        print(f"  {s:30s} {n:7d} fallos {n - seg_ok[s]:6d}  acierto {seg_ok[s] / n:.3f}")
    if dump:
        shown = Counter()
        for seg, txn, g, allocs, before in fails:
            if shown[seg] >= max(1, dump // 10) or sum(shown.values()) >= dump:
                continue
            shown[seg] += 1
            print(f"--- {seg} {txn['bank_txn_id']} {txn['booking_date']} cash={txn['amount_cents']} {txn['currency']} "
                  f"orig={txn.get('orig_amount')}{txn.get('orig_currency') or ''}")
            pred = {a.item_id: a for a in allocs}
            bad = [r for r in g if pred[r[0]].exception != r[2] or abs(pred[r[0]].amount_cents - r[1]) > max(1, 0.005 * r[1])]
            print(f"   ({len(g)} filas, {len(bad)} mal)")
            for it, amt, exc, _ in bad[:6]:
                info = ctx.shared["exceptions"].items.get(it)
                a = pred[it]
                print(f"   {it} open={before[it]} total={info[4]} {info[1]} {info[5]} | gold {amt} {exc or '-'} | pred {a.amount_cents} {a.exception or '-'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="val", choices=["val", "sample"])
    ap.add_argument("--months", type=int, default=None)
    ap.add_argument("--segment", default=None)
    ap.add_argument("--dump", type=int, default=0)
    ap.add_argument("--explicit", action="store_true", help="el resolver da el efectivo real en SHORT_DISCOUNT/WITHHOLDING")
    ap.add_argument("--lock", default="", help="excepciones del golden que se pasan ya fijadas, p. ej. SHORT_DISPUTE,RETURNED")
    a = ap.parse_args()
    run(a.phase, a.months, a.segment, a.dump, {x for x in a.lock.split(",") if x}, a.explicit)
