"""Regresiones sintéticas: ambigüedad, divisas y separación temporal sin depender de IDs de TRAIN."""
import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import polars as pl

from bankrec.ledger import Ledger
from bankrec.resolvers import multi_ref_fill, ranker, subset_sum


DAY = dt.date(2030, 6, 1)


def item(iid, amount, party="payer", currency="EUR", typ="ARI", issued=None, entity="NEW"):
    return dict(item_id=iid, item_type=typ, entity=entity, currency=currency, party_id=party,
                issue_date=issued or DAY - dt.timedelta(days=30), due_date=DAY,
                document_number="INV-" + iid, po="", total_cents=amount, open_cents=amount)


def context(rows):
    items = pl.DataFrame(rows)
    ledger = Ledger(items)
    by_party = {}
    for row in rows:
        by_party.setdefault(row["party_id"], []).append(row["item_id"])
    ctx = SimpleNamespace(shared={subset_sum.NAME: {"by_party": by_party},
                                  multi_ref_fill.NAME: by_party})
    return ctx, ledger


def txn(amount=100):
    return dict(bank_txn_id="synthetic", amount_cents=amount, currency="EUR", entity="NEW",
                booking_date=DAY, text="PAYMENT", counterparty_name="Example Customer")


class ReferenceCompletion(unittest.TestCase):
    def test_full_candidate_pool_required(self):
        rows = [item("123456", 40), item("654321", 60)]
        rows += [item(str(700000 + i), 1000 + i) for i in range(multi_ref_fill.MAX_FREE)]
        ctx, ledger = context(rows)
        ctx.shared["text_ref"] = {("NEW", "s123456"): ["123456"]}
        with patch.object(multi_ref_fill.parties, "score", return_value=100):
            self.assertIsNone(multi_ref_fill.resolve(dict(txn(), text="INV-123456"), ctx, ledger))

    def test_shared_party_does_not_cross_entities(self):
        ctx, ledger = context([item("123456", 40), item("654321", 60, entity="OTHER")])
        ctx.shared["text_ref"] = {("NEW", "s123456"): ["123456"]}
        with patch.object(multi_ref_fill.parties, "score", return_value=100):
            self.assertIsNone(multi_ref_fill.resolve(dict(txn(), text="INV-123456"), ctx, ledger))


class RankerGeneralization(unittest.TestCase):
    def test_reference_and_iban_cannot_admit_unsupported_currency(self):
        rows = [item("123456", 100), item("223456", 100, currency="USD"),
                item("323456", 100, currency="GBP"), item("423456", 100, entity="OTHER")]
        idx = ranker._Index(pl.DataFrame(rows))
        transaction = dict(txn(), counterparty_account="account")
        ibmap = {"ACCOUNT": {"payer"}}
        self.assertEqual(ranker._cands(transaction, idx, ibmap, lambda _: 100, ["223456"]), ["123456"])
        transaction.update(orig_currency="USD", orig_amount_cents=100)
        self.assertEqual(set(ranker._cands(transaction, idx, ibmap, lambda _: 100, ["223456"])),
                         {"123456", "223456"})

    def test_features_are_invariant_to_amount_scale(self):
        matrices = []
        for scale in (1, 100):
            rows = [item("123456", 100 * scale), item("234567", 130 * scale)]
            idx = ranker._Index(pl.DataFrame(rows))
            with patch.object(ranker.parties, "score", return_value=90):
                matrices.append(ranker._feats(txn(100 * scale), ["123456", "234567"], idx,
                                             lambda iid: idx.info[iid][-1], {"hay_raw": ""}, [], None))
        np.testing.assert_allclose(*matrices)

    def test_temporal_holdout_keeps_all_candidates_together(self):
        import lightgbm as lgb

        hi = dt.date(2029, 1, 1)
        split = hi - dt.timedelta(days=ranker.VALID_DAYS)
        dates = np.array([(split - dt.timedelta(days=1)).toordinal()] * 2 + [split.toordinal()] * 2)
        X = np.arange(4 * len(ranker.FEATS)).reshape(4, -1)
        y = np.array([0, 1, 1, 0])
        with patch.object(lgb, "LGBMClassifier") as factory:
            ranker._fit_temporal(X, y, dates, hi)
            args, kwargs = factory.return_value.fit.call_args
            np.testing.assert_array_equal(args[0], X[:2])
            np.testing.assert_array_equal(kwargs["eval_set"][0][0], X[2:])
            self.assertTrue(kwargs["callbacks"])
        with self.assertRaisesRegex(ValueError, "fuera del histórico"):
            ranker._fit_temporal(X, y, np.array([hi.toordinal()] * 4), hi)

    def test_insufficient_history_does_not_train_on_holdout(self):
        hi = dt.date(2029, 1, 1)
        with self.assertRaisesRegex(ValueError, "insuficiente"):
            ranker._fit_temporal(np.zeros((2, len(ranker.FEATS))), np.array([0, 1]),
                                 np.array([(hi - dt.timedelta(days=1)).toordinal()] * 2), hi)


if __name__ == "__main__":
    unittest.main()
