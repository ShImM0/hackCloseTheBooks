"""Pruebas de docs_remit (P2): evidencia por payment_ref, asignación aviso<->movimiento, filas de importes y
deducciones sin motivo. Uso: uv run python -m pytest tests/test_docs_remit.py (o uv run python tests/test_docs_remit.py)."""
import datetime as dt

from bankrec.resolvers import docs_remit as R
from bankrec.resolvers import docs_remit_parse as P


def test_ref_level():
    t = {"text": "DIRECT CREDIT RESORT PRIME PTY PMT ADVICE 2545300", "remittance_info": "PMT ADVICE 2545300633"}
    assert R._ref_level("2545300633", t) == 2                       # entero en remittance_info
    t = {"text": "DIRECT CREDIT RESORT PRIME PTY PMT ADVICE 2545300"}
    assert R._ref_level("2545300633", t) == 1                       # cortado al final del campo
    t = {"text": "TRANSFERENCIAS VAZQUEZ INSTALACIONES | VAZQUEZ IN REMESA PAGOS 060125 2517802"}
    assert R._ref_level("2517802341", t) == 1                       # cortado al final de un trozo N43
    t = {"text": "TRANSFER INMEDIATA | 90767898-IGLESIAS | 2501087 | 82118 N/PEDIDO"}
    assert R._ref_level("250108782118", t) == 2                     # partido por ' | '
    assert R._ref_level("250108782118", {"text": "PAGO FACTURA FV25-0001234"}) == 0
    assert R._ref_level("12345", {"text": "12345"}) == 0             # demasiado corto para ser evidencia


def test_assign_ties_and_evidence():
    # (evidencia, nombre, |días|, aviso, movimiento)
    m = R._assign([(2, 40, 1, 0, "T1"), (1, 90, 0, 1, "T1"), (1, 90, 2, 1, "T2")])
    assert m == {"T1": 0, "T2": 1}                                  # el ref gana al nombre; el aviso 1 va a T2
    m = R._assign([(1, 80, 1, 0, "T1"), (1, 78, 1, 1, "T1")])
    assert m == {}                                                  # empate sin evidencia que lo rompa
    m = R._assign([(1, 80, 1, 0, "T1"), (1, 80, 3, 0, "T2")])
    assert m == {}                                                  # un aviso con dos movimientos iguales


def test_amounts_rows():
    assert P._amounts(["2,463.78", "-739.13", "1,724.65"]) == (246378, -73913, 172465)
    assert P._amounts(["41,703.70", "37,533.33"]) == (4170370, -417037, 3753333)
    rows = P._rows_from_lines(["NF0125007817 18/04/2025 41,703.70 37,533.33"], False)
    assert rows == [("NF0125007817", dt.date(2025, 4, 18), 4170370, 0, "", 3753333, "")]   # sin deducción impresa
    rows = P._rows_from_lines(["FV24-0120500 02/01/2025 2,463.78 -739.13 SHORT DELIVERY 1,724.65"], False)
    assert rows[0][3:6] == (-73913, "SHORT DELIVERY", 172465)


def test_deduction_without_reason():
    b = dt.date(2025, 10, 10)
    # (emisión, días de pronto pago, bps de descuento, total, país)
    info = {"D": (dt.date(2025, 10, 1), 15, 100, 100000, "PT"), "W": (dt.date(2025, 9, 1), None, None, 100000, "BR"),
            "S": (dt.date(2025, 9, 1), None, None, 100000, "FR"), "L": (dt.date(2025, 8, 1), 15, 100, 100000, "PT")}
    row = lambda i, paid: {"item_id": i, "gross": 100000, "paid": paid, "ded": paid - 100000, "reason": ""}
    assert R._deduction(row("D", 99000), b, info) == R.Alloc("D", 100000, "SHORT_DISCOUNT")
    assert R._deduction(row("L", 99000), b, info) == R.Alloc("L", 99000, "PARTIAL")     # descuento fuera de plazo
    assert R._deduction(row("W", 90000), b, info) == R.Alloc("W", 100000, "SHORT_WITHHOLDING")
    assert R._deduction(row("S", 70000), b, info) == R.Alloc("S", 70000, "SHORT_DISPUTE")
    # pagado < bruto sin deducción ni motivo (a cuenta): se deja a exceptions.finalize
    al = R._allocs([{"item_id": "S", "gross": 100000, "paid": 50000, "ded": 0, "reason": ""}], b, info)
    assert al == [R.Alloc("S", 50000, "")]


if __name__ == "__main__":
    for f in (test_ref_level, test_assign_ties_and_evidence, test_amounts_rows, test_deduction_without_reason):
        f()
    print("ok")
