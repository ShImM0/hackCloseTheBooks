"""Regresiones de categorías y preparación de envíos sin red. Dueño: P4."""
from datetime import date
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import polars as pl

from bankrec import config, parties
from bankrec.resolvers import nonitem, nonitem_unmatched, router
from bankrec.validate import submission_rows, unknown_item_ids, validate

spec = importlib.util.spec_from_file_location('submit_p4', config.REPO_ROOT / 'scripts' / 'submit.py')
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)
audit_spec = importlib.util.spec_from_file_location('p4_audit', config.REPO_ROOT / 'scripts' / 'p4_category_audit.py')
audit = importlib.util.module_from_spec(audit_spec)
audit_spec.loader.exec_module(audit)


def txn(text='', amount=10000, code='04', tid='BT-2025-0000001', day=1):
    return dict(bank_txn_id=tid, text=text, amount_cents=amount, bank_code=code,
                account_id='ES01-A', currency='EUR', booking_date=date(2025, 6, day))


class Categories(unittest.TestCase):
    def test_specific_rules_and_ambiguous_payment(self):
        for t, category in [
            (txn('LIQUIDACIÓN INTERESES', -100, '17'), 'INTEREST_EXPENSE'),
            (txn('LIQUIDACION DE INTERESES, COMISIONES Y GASTOS', -100, '17'), 'BANK_FEES'),
            (txn('COMISION DEVOLUCION RECIBO', -100), 'BANK_FEES'),
            (txn('NÓMINAS 06/2025', -100), 'PAYROLL'),
            (txn('CASH POOL ZERO BALANCING', -100, '99'), 'CASH_POOL'),
            (txn('PROFORMA PF25-1'), 'CUSTOMER_ADVANCE'),
            (txn('REFUND DUPLICATE PAYMENT RE2501', -100), 'CUSTOMER_REFUND'),
        ]:
            with self.subTest(category=category):
                self.assertEqual(nonitem.classify(t)[0], category)
        for text in ['TRANSFER', 'PAGO PEDIDO', 'PAGO FACTURA FV25-0400572', 'DEVOLUCION RECIBO']:
            self.assertIsNone(nonitem.classify(txn(text)))

    def test_generalization_and_conflicting_signals(self):
        # Nombres, códigos, importes y años ajenos al sample no cambian la semántica.
        for code in ['UNKNOWN-NEW-BANK', '17', 'PMNT/RCDT/ESCT', '']:
            self.assertEqual(nonitem.classify(txn('CASH POOL NUEVA FILIAL 2031', -987654, code))[0], 'CASH_POOL')
            self.assertIsNone(nonitem.classify(txn('TRANSFERENCIA CLIENTE NUEVO', 999, code)))
        for text in ['TRASPASO PROPIO', 'TRASPASO ENTRE CUENTAS ES00',
                     'PAGO FACTURA COMISION SERVICIOS', 'INVOICE PAYROLL SERVICES',
                     'PAGO FACTURA A DEDUCIR ANTICIPO PROFORMA', 'INTEREST CONSULTING LTD']:
            self.assertIsNone(nonitem.classify(txn(text, -100, 'NCHG')))
        self.assertIsNone(nonitem.classify(txn('ABONO PROVEEDOR POR NOTA DE CREDITO')))
        self.assertIsNone(nonitem.classify(txn('PROFORMA ANTICIPO', -100)))

    def test_unique_reversal_pair_and_router(self):
        original = txn('COMISION', -120, '02')
        reversal = txn('ANULACION OPERACION INDEBIDA N 123 COMISION', 120, '98', 'BT-2025-0000002', 2)
        ctx = SimpleNamespace(txns=pl.DataFrame([original, reversal]), shared={})
        nonitem.prepare(ctx)
        self.assertEqual(nonitem.resolve(original, ctx, None).category, 'BANK_REVERSAL')
        router.prepare(ctx)
        self.assertEqual(ctx.shared['router'][reversal['bank_txn_id']], 'nonitem')
        other = {**original, 'bank_txn_id': 'BT-2025-0000003'}
        ctx.txns = pl.DataFrame([original, reversal, other])
        nonitem.prepare(ctx)
        self.assertFalse(ctx.shared['nonitem'])


    def test_audited_patterns_and_pool_accounts(self):
        # Patrones auditados en 2023 y 2024 (reports/P4_HISTORY.md); números y entidades ficticios.
        for t, category in [
            (txn('CASH CONCENTRATION XX01 2031', 5000, 'NEWCODE'), 'CASH_POOL'),
            (txn('AMORTIZACION POLIZA CREDITO 77', -100, '05'), 'LOAN_REPAYMENT'),
            (txn('PRES.ZZ09-12', -100, 'NLDP'), 'LOAN_REPAYMENT'),
            (txn('DISPOSICION POLIZA CREDITO 77', 100, '99'), 'LOAN_DRAWDOWN'),
            (txn('VENTA DIVISA USD 1.1 OP 9 9', 100, '13'), 'FX_DEAL'),
            (txn('COMPRA DIVISA GBP 1.1 OP 9 9', -100, '13'), 'FX_DEAL'),
            (txn('LIQUIDACION CTA.CTO', -100, 'NCHG'), 'BANK_FEES'),
            (txn('TAX AUTHORITY', -100, 'NMSC'), 'TAX_PAYMENT'),
            (txn('TGSS. COTIZACION 202501', -100, '15'), 'SOCIAL_SECURITY'),
            (txn('SEGUROS MULTIRRIESGO', -100, 'NDDT'), 'INSURANCE'),
            (txn('DEVOLUCION PRESTAMO FILIAL NUEVA GMBH', 100, '04'), 'INTERCOMPANY'),
        ]:
            with self.subTest(text=t['text']):
                self.assertEqual(nonitem.classify(t)[0], category)
        # Traspaso propio: CASH_POOL solo en cuentas del cash pooling (maestro bank_accounts).
        own = txn('TRASPASO PROPIO', -100, '99')
        self.assertIsNone(nonitem.classify(own))
        self.assertEqual(nonitem.classify(own, frozenset({'ES01-A'}))[0], 'CASH_POOL')
        self.assertEqual(nonitem.classify(txn('TRASPASO A CTA 1234', -100), frozenset({'ES01-A'}))[0],
                         'INTERNAL_TRANSFER')
        accounts = pl.DataFrame({'account_id': ['A', 'B', 'C'],
                                 'purpose': ['Cabecera cash pooling / Pagos', 'Operativa / participante cash pool', 'Cobros']})
        self.assertEqual(nonitem.pool_accounts(SimpleNamespace(master=lambda name: accounts)), frozenset({'A', 'B'}))
        self.assertEqual(nonitem.pool_accounts(SimpleNamespace()), frozenset())


WEB = json.loads((Path(__file__).parent / 'p4_web_examples.json').read_text(encoding='utf-8'))['examples']
KIND = {'AR_INVOICE': 'ARI', 'AR_CREDIT_NOTE': 'ARC', 'AP_INVOICE': 'API', 'AP_CREDIT_NOTE': 'APC'}


def web_txn(e):
    b = dict(e['bank'])
    b['amount_cents'] = round(float(b['amount']) * 100)
    b['booking_date'] = date.fromisoformat(b['booking_date'])
    return b


CUST_SCHEMA = {'customer_id': pl.String, 'entity': pl.String, 'name': pl.String, 'trade_name': pl.String,
               'iban': pl.String, 'parent_customer_id': pl.String}
SUP_SCHEMA = {'supplier_id': pl.String, 'entity': pl.String, 'name': pl.String, 'iban': pl.String}
ITEM_SCHEMA = {'item_id': pl.String, 'item_type': pl.String, 'entity': pl.String, 'party_id': pl.String,
               'party_name': pl.String, 'document_number': pl.String, 'issue_date': pl.Date, 'open_cents': pl.Int64}


def unmatched_ctx(items, customers=(), suppliers=()):
    """Contexto real en miniatura: maestros + partidas, con parties.prepare y nonitem_unmatched.prepare de verdad."""
    masters = {'customers': pl.DataFrame(list(customers), schema=CUST_SCHEMA),
               'suppliers': pl.DataFrame(list(suppliers), schema=SUP_SCHEMA)}
    ctx = SimpleNamespace(items=pl.DataFrame(list(items), schema=ITEM_SCHEMA), shared={}, master=masters.__getitem__)
    nonitem_unmatched.prepare(ctx)
    open_ids = set(ctx.items.filter(pl.col('open_cents') > 0)['item_id'])
    return ctx, SimpleNamespace(is_open=lambda iid: iid in open_ids)


def expected(e):
    """(category, exception) de la fila sin partida del CSV esperado, o None si el movimiento casa partidas."""
    first = e['csv'].split('\n')[1].split(',')
    return None if first[1] else (first[3], first[4])


class WebExamples(unittest.TestCase):
    """Los 36 ejemplos resueltos de https://hackathon.usetalky.com/ejemplos (TRAIN, uno por segmento)."""

    def test_nonitem_only_claims_nonitem_examples(self):
        for e in WEB:
            with self.subTest(segment=e['segment']):
                got = nonitem.classify(web_txn(e))
                exp = expected(e)
                if exp and exp[0] != 'UNIDENTIFIED':
                    self.assertEqual(got[0], exp[0])
                else:
                    self.assertIsNone(got)

    def ctx_for(self, e):
        """Partidas candidatas del ejemplo abiertas; sus terceros como únicos clientes/proveedores del maestro."""
        ids, items, cust, sup = {}, [], [], []
        for i in e['items']:
            typ = KIND[i['kind']]
            side = 'C' if typ.startswith('AR') else 'S'
            if (side, i['party']) not in ids:
                ids[(side, i['party'])] = pid = f"{side}{i['entity']}{len(ids):06d}"
                (cust if side == 'C' else sup).append(
                    {'customer_id': pid, 'entity': i['entity'], 'name': i['party'], 'trade_name': None, 'iban': None,
                     'parent_customer_id': None} if side == 'C' else
                    {'supplier_id': pid, 'entity': i['entity'], 'name': i['party'], 'iban': None})
            items.append({'item_id': i['item_id'], 'item_type': typ, 'entity': i['entity'],
                          'party_id': ids[(side, i['party'])], 'party_name': i['party'], 'document_number': i['number'],
                          'issue_date': date.fromisoformat(i['issue_date']),
                          'open_cents': abs(round(float(i['total']) * 100))})
        return unmatched_ctx(items, cust, sup)

    def test_unmatched_keeps_unidentified_only_without_open_items(self):
        seen = set()
        for e in WEB:
            t = web_txn(e)
            if t['amount_cents'] <= 0:
                continue
            ctx, ledger = self.ctx_for(e)
            p = nonitem_unmatched.resolve(t, ctx, ledger)
            with self.subTest(segment=e['segment']):
                if expected(e) == ('UNIDENTIFIED', 'UNIDENTIFIED'):
                    self.assertEqual((p.category, p.exception), ('UNIDENTIFIED', 'UNIDENTIFIED'))
                    seen.add(e['segment'])
                elif e['segment'] in {'ar_1to1', 'ar_1toN_text', 'ar_dd_batch', 'ar_card_settlement',
                                      'ar_overpayment', 'ar_third_party_parent'}:
                    # número citado, lote agregado o nombre distintivo: existe partida, no es UNIDENTIFIED
                    self.assertEqual((p.category, p.exception), (nonitem_unmatched.PENDING, ''))
        self.assertEqual(seen, {'unidentified', 'unidentified_lookalike'})

    def test_unmatched_ignores_debits(self):
        e = next(e for e in WEB if e['segment'] == 'nonitem_bank_fees')
        ctx, ledger = self.ctx_for(e)
        self.assertIsNone(nonitem_unmatched.resolve(web_txn(e), ctx, ledger))


def cand(**kw):
    c = {'pid': 'CES01000001', 'sc': 100, 'w': 2, 'emp': True, 'ab': 1, 'aa': 1, 'n': 'FERRETERIA PUIG', 'cpr': None}
    c.update(kw)
    return c


def feats(**kw):
    f = {'lote': '', 'reemb': False, 'cp': False, 'iban': [], 'ref': None, 'cli': [], 'pro': []}
    f.update(kw)
    return f


class UnmatchedDecisions(unittest.TestCase):
    """Reglas de nonitem_unmatched sobre señales sintéticas (nombres e ids ficticios)."""

    def decide(self, credit=True, **kw):
        return nonitem_unmatched.decide(feats(**kw), credit)

    def test_unidentified_only_without_any_tie_and_always_both_fields(self):
        cat, exc, why = self.decide()
        self.assertEqual((cat, exc), ('UNIDENTIFIED', 'UNIDENTIFIED'))
        self.assertIn('POLICIES §3', why)

    def test_company_two_words_identifies_but_person_needs_three(self):
        self.assertEqual(self.decide(cli=[cand()])[:2], (nonitem_unmatched.PENDING, ''))
        cat, exc, why = self.decide(cli=[cand(emp=False, n='JESSICA WHITE')])
        self.assertEqual((cat, exc), ('UNIDENTIFIED', 'UNIDENTIFIED'))
        self.assertIn('particular de 2 palabras', why)
        self.assertEqual(self.decide(cli=[cand(emp=False, w=3, n='RAQUEL GOMEZ GARRIDO')])[0], nonitem_unmatched.PENDING)

    def test_counterparty_name_must_match_whole_name(self):
        cat, _, why = self.decide(cli=[cand(cpr=73, n='POOL BENNETT')])
        self.assertEqual(cat, 'UNIDENTIFIED')
        self.assertIn('la contraparte es otro nombre', why)
        self.assertEqual(self.decide(cli=[cand(cpr=100)])[0], nonitem_unmatched.PENDING)

    def test_name_without_open_items_or_weak_match_is_not_enough(self):
        self.assertIn('sin partidas abiertas', self.decide(cli=[cand(aa=0, ab=0)])[2])
        self.assertIn('parecido 85', self.decide(cli=[cand(sc=85)])[2])

    def test_batch_iban_and_cited_number_tie_to_items(self):
        self.assertEqual(self.decide(lote='REMESA')[0], nonitem_unmatched.PENDING)
        self.assertEqual(self.decide(iban=[{'pid': 'CES01000001', 'ab': 1, 'aa': 2}])[0], nonitem_unmatched.PENDING)
        self.assertEqual(self.decide(iban=[{'pid': 'CES01000001', 'ab': 0, 'aa': 0}])[0], 'UNIDENTIFIED')
        ref = {'iid': 'ARI-2031-0000001', 'rs': 100, 'antes': True, 'pf': 0, 't': 'ARI'}
        self.assertEqual(self.decide(ref=ref)[0], nonitem_unmatched.PENDING)

    def test_supplier_refund_only_without_open_credit_note(self):
        sup = cand(pid='SES01000001', aa=0, ab=0, api=True, n='QUIMICA NUEVA')
        self.assertEqual(self.decide(pro=[sup])[:2], ('SUPPLIER_REFUND', ''))
        self.assertEqual(self.decide(reemb=True, pro=[{**sup, 'api': False}])[0], 'SUPPLIER_REFUND')
        self.assertEqual(self.decide(pro=[{**sup, 'api': False}])[0], 'UNIDENTIFIED')
        # con abono APC abierto el reembolso liquida la APC: partida probable, no categoría
        self.assertEqual(self.decide(reemb=True, pro=[{**sup, 'aa': 1, 'ab': 1}])[0], nonitem_unmatched.PENDING)

    def test_customer_refund_debits(self):
        self.assertEqual(self.decide(False, cli=[cand(aa=0, ab=0)])[:2], ('CUSTOMER_REFUND', ''))
        self.assertIsNone(self.decide(False, cli=[cand(aa=1)]))                       # abono ARC abierto: partida
        self.assertIsNone(self.decide(False, cli=[cand(aa=0)], pro=[cand(pid='SES01000002')]))  # también proveedor
        self.assertIsNone(self.decide(False))

    def test_real_prepare_and_resolve_with_two_word_company(self):
        items = [{'item_id': 'ARI-2031-0000001', 'item_type': 'ARI', 'entity': 'XX01', 'party_id': 'CXX01000001',
                  'party_name': 'Ferretería Nube S.L.', 'document_number': 'ZZ31-0000001',
                  'issue_date': date(2031, 1, 5), 'open_cents': 12345}]
        cust = [{'customer_id': 'CXX01000001', 'entity': 'XX01', 'name': 'Ferretería Nube S.L.', 'trade_name': None,
                 'iban': None, 'parent_customer_id': None}]
        ctx, ledger = unmatched_ctx(items, cust)
        t = dict(bank_txn_id='BT-2031-0000001', entity='XX01', text='TRANSFERENCIA DE FERRETERIA NUBE S.L.',
                 amount_cents=50000, booking_date=date(2031, 2, 1), ref2='', remittance_info='', counterparty_name='')
        p = nonitem_unmatched.resolve(t, ctx, ledger)
        self.assertEqual((p.category, p.exception), (nonitem_unmatched.PENDING, ''))
        self.assertTrue(json.loads(p.reason.split(' | ', 1)[1])['cli'][0]['emp'])
        t2 = {**t, 'text': 'TRANSFERENCIA DE MARIA SOL NUEVA', 'bank_txn_id': 'BT-2031-0000002'}
        p2 = nonitem_unmatched.resolve(t2, ctx, ledger)
        self.assertEqual((p2.category, p2.exception), ('UNIDENTIFIED', 'UNIDENTIFIED'))

    def test_company_detection(self):
        for raw, exp in [('Porto Alvor Jardins S.A.', True), ('Pool & Spa Bennett Hermanos Co.', True),
                         ('Plastics Wattle Creek Pty Ltd', True), ('Raquel Gómez Garrido', False), ('Jessica White', False)]:
            self.assertEqual(nonitem_unmatched.is_company(raw), exp, raw)


class Registro(unittest.TestCase):
    def test_unidentified_and_category_sign_warnings(self):
        from bankrec.validate import registro_warnings
        row = lambda t, cat, exc='': {'bank_txn_id': t, 'item_id': '', 'amount': '', 'category': cat, 'exception': exc}
        signs = {'A': 1, 'B': -1, 'C': -1, 'D': 1, 'E': 1}
        ok = [row('A', 'UNIDENTIFIED', 'UNIDENTIFIED'), row('B', 'PAYROLL'), row('D', 'SUPPLIER_REFUND')]
        self.assertEqual(registro_warnings(ok, signs), [])
        bad = registro_warnings([row('A', 'UNIDENTIFIED'), row('C', 'UNIDENTIFIED', 'UNIDENTIFIED'),
                                 row('D', 'PAYROLL'), row('E', 'BANK_FEES'), row('B', 'CUSTOMER_ADVANCE')], signs)
        self.assertEqual(len(bad), 3)
        self.assertIn('1 filas con UNIDENTIFIED solo en category', bad[0])
        self.assertIn('1 cargos marcados UNIDENTIFIED', bad[1])
        self.assertIn('PAYROLL en abono 1', bad[2])
        self.assertIn('CUSTOMER_ADVANCE en cargo 1', bad[2])


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ids = ['BT-2025-0000001']
        self.header = 'bank_txn_id,item_id,amount,category,exception\n'

    def check(self, body, **kwargs):
        p = self.root / 'sub.csv'
        p.write_text(self.header + body, encoding='utf-8')
        return validate(p, 'sample', self.ids, **kwargs)

    def test_official_rejections_and_coverage(self):
        for body in ['', 'BT-2025-9999999,,,BANK_FEES,\n',
                     'BT-2025-0000001,,,BANK_FEES,\nBT-2025-0000001,,,PAYROLL,\n',
                     'BT-2025-0000001,ARI-2025-0000001,-1,,\n',
                     'BT-2025-0000001,invalid,1,,\n',
                     'BT-2025-0000001,,,BOGUS,\n',
                     'BT-2025-0000001,ARI-2025-0000001,1,,\nBT-2025-0000001,,,BANK_FEES,\n']:
            with self.subTest(body=body):
                self.assertTrue(self.check(body)[0])

    def test_unknown_item_warns_and_decimal_style(self):
        errors, warnings = self.check('BT-2025-0000001,ARI-2025-0000001,1.234,,\n', known_items=set())
        self.assertFalse(errors)
        self.assertEqual(len(warnings), 2)

    def test_automatic_unknown_check_uses_prior_items_without_golden(self):
        prior = 'ARI-2024-0000001'
        fake = 'ARI-2030-0000001'
        def table(folder, name):
            self.assertNotEqual(name, 'golden_allocations')
            return pl.DataFrame({'item_id': [prior]}) if (folder, name) == ('train', 'ar_invoices') else None
        with patch('bankrec.validate.load.table', side_effect=table):
            self.assertEqual(unknown_item_ids('final', {prior, fake}), {fake})
            errors, warnings = self.check(f'BT-2025-0000001,{fake},1.00,,\n', check_known_items=True)
            self.assertFalse(errors)
            self.assertEqual(warnings, ['1 item_id desconocidos (FP seguros)'])

    def test_final_categories_come_from_updated_masters(self):
        masters = self.root / 'final' / 'masters_final'
        masters.mkdir(parents=True)
        (masters / 'categories.csv').write_text('category,description,gl_account\nFINAL_CATEGORY,Nueva,1\n', encoding='utf-8')
        p = self.root / 'final.csv'
        p.write_text(self.header + 'BT-2025-0000001,,,FINAL_CATEGORY,\n', encoding='utf-8')
        with patch.object(config, 'data_dir', return_value=self.root):
            self.assertEqual(validate(p, 'final', self.ids), ([], []))

    def test_server_csv_variants(self):
        p = self.root / 'sub.csv.gz'
        raw = '\ufeff EXCEPTION ; CATEGORY ; AMOUNT ; ITEM_ID ; BANK_TXN_ID\r\n ; ; 1,00 ; ARI-2025-0000001 ; BT-2025-0000001\r\n'
        p.write_bytes(gzip.compress(raw.encode('utf-8')))
        self.assertEqual(validate(p, 'sample', self.ids), ([], []))
        self.assertEqual(submission_rows(p.read_bytes())[0]['amount'], '1,00')

    def test_archive_hash_manifest_and_no_archive_on_rejection(self):
        (self.root / 'docs').mkdir()
        p = self.root / 'source.csv'
        p.write_text(self.header + 'BT-2025-0000001,,,BANK_FEES,\n', encoding='utf-8')
        with patch.object(submit, 'phase_txn_ids', return_value=self.ids), \
             patch.object(config, 'REPO_ROOT', self.root), \
             patch.object(config, 'OUT_DIR', self.root / 'out'):
            result = submit.prepare_submission(p, 'live')
            self.assertFalse(result['errors'])
            archive = Path(result['archive'])
            self.assertEqual(archive.read_bytes(), p.read_bytes())
            self.assertEqual(result['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertTrue(archive.with_suffix('.csv.json').exists())
            self.assertIn('NO ENVIADO', (self.root / 'docs' / 'SCOREBOARD.md').read_text(encoding='utf-8'))
            before = set((self.root / 'out' / 'live').iterdir())
            p.write_text(self.header, encoding='utf-8')
            self.assertTrue(submit.prepare_submission(p, 'live')['errors'])
            self.assertEqual(before, set((self.root / 'out' / 'live').iterdir()))


class Audit(unittest.TestCase):
    def test_metrics_count_intercepted_invoices_and_abstentions(self):
        frame = pl.DataFrame({'predicted': ['BANK_FEES', 'BANK_FEES', '', 'PAYROLL'],
                              'category': ['BANK_FEES', '', 'CASH_POOL', 'BANK_FEES']})
        m = audit.summarize(frame)
        self.assertEqual(m['predicted'], 3)
        self.assertEqual(m['correct'], 1)
        self.assertEqual(m['intercepted_item_payments'], 1)
        self.assertEqual(m['false_categories'], 2)
        self.assertAlmostEqual(m['precision'], 1/3)
        self.assertAlmostEqual(m['recall'], 1/3)


if __name__ == '__main__':
    unittest.main()
