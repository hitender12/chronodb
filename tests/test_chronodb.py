"""Tests for ChronoDB. Run:  python -m unittest discover -s tests -v"""

import unittest
from datetime import date, timedelta

from chronodb import ChronoDB
from demo import (inventory_count, monthly_total_key, monthly_totals,
                  region_total_key, region_totals)


def build_db() -> tuple[ChronoDB, dict]:
    """Sales: Jan North 100 / South 150, Feb North 200 / South 120,
    Mar North 180 / South 90; plus one unrelated inventory row."""
    db = ChronoDB()
    ids = {}
    for day, region, amount in [
        (date(2026, 1, 15), "North", 100),
        (date(2026, 1, 20), "South", 150),
        (date(2026, 2, 10), "North", 200),
        (date(2026, 2, 18), "South", 120),
        (date(2026, 3, 5), "North", 180),
        (date(2026, 3, 22), "South", 90),
    ]:
        ids[(day, region)] = db.insert(
            "sales", {"region": region, "amount": amount},
            # Point event as a 1-day valid range (see demo.py comment).
            valid_from=day, valid_to=day + timedelta(days=1), actor="loader")
    db.insert("inventory", {"item": "widget", "qty": 500},
              valid_from=date(2026, 1, 1), actor="loader")
    db.register_view("monthly_totals", monthly_totals, ["sales"],
                     compute_key=monthly_total_key,
                     description="sum(amount) per calendar month")
    db.register_view("region_totals", region_totals, ["sales"],
                     compute_key=region_total_key,
                     description="sum(amount) per region")
    db.register_view("inventory_count", inventory_count, ["inventory"],
                     description="sum(qty) per item")
    db.compute_all_views()
    return db, ids


class TestCorrectionPropagates(unittest.TestCase):
    def test_correction_propagates_to_derived_total(self):
        db, ids = build_db()
        self.assertEqual(db.view_value("monthly_totals", "2026-01"), 250)
        self.assertEqual(db.view_value("region_totals", "North"), 480)
        db.correct("sales", ids[(date(2026, 1, 15), "North")],
                   {"amount": 130}, valid_from=date(2026, 1, 15),
                   actor="analyst")
        # Old totals were wrong, new totals are right.
        self.assertEqual(db.view_value("monthly_totals", "2026-01"), 280)
        self.assertEqual(db.view_value("region_totals", "North"), 510)
        # Untouched months/regions did not change.
        self.assertEqual(db.view_value("monthly_totals", "2026-02"), 320)
        self.assertEqual(db.view_value("monthly_totals", "2026-03"), 270)
        self.assertEqual(db.view_value("region_totals", "South"), 360)

    def test_unrelated_view_is_not_recomputed(self):
        db, ids = build_db()
        before = db.views.views["inventory_count"].recompute_count
        self.assertEqual(before, 1)  # computed once by compute_all_views
        db.correct("sales", ids[(date(2026, 1, 15), "North")],
                   {"amount": 130}, valid_from=date(2026, 1, 15),
                   actor="analyst")
        self.assertEqual(db.views.views["inventory_count"].recompute_count, before)
        self.assertNotIn(("inventory_count", "widget"), db.last_recomputed)
        # Only the affected keys were recomputed.
        self.assertEqual(set(db.last_recomputed),
                         {("monthly_totals", "2026-01"), ("region_totals", "North")})

    def test_lineage_lists_exactly_contributing_rows(self):
        db, ids = build_db()
        rec = db.why("monthly_totals", "2026-01")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.value, 250)
        self.assertEqual(rec.source_row_ids,
                         {ids[(date(2026, 1, 15), "North")],
                          ids[(date(2026, 1, 20), "South")]})
        # Feb rows must NOT be in January's lineage.
        self.assertNotIn(ids[(date(2026, 2, 10), "North")], rec.source_row_ids)
        # After the correction, lineage still lists exactly the Jan rows,
        # now with the corrected version.
        db.correct("sales", ids[(date(2026, 1, 15), "North")],
                   {"amount": 130}, valid_from=date(2026, 1, 15),
                   actor="analyst")
        rec2 = db.why("monthly_totals", "2026-01")
        self.assertEqual(rec2.value, 280)
        self.assertEqual(rec2.source_row_ids,
                         {ids[(date(2026, 1, 15), "North")],
                          ids[(date(2026, 1, 20), "South")]})
        corrected = [s for s in rec2.sources
                     if s.row_id == ids[(date(2026, 1, 15), "North")]][0]
        self.assertEqual(corrected.version, 2)

    def test_as_of_returns_pre_and_post_correction_belief(self):
        db, ids = build_db()
        tx_before = db.current_tx
        db.correct("sales", ids[(date(2026, 1, 15), "North")],
                   {"amount": 130}, valid_from=date(2026, 1, 15),
                   actor="analyst")
        tx_after = db.current_tx
        rows_before = db.as_of("sales", date(2026, 1, 15), tx_before)
        rows_after = db.as_of("sales", date(2026, 1, 15), tx_after)
        self.assertEqual(len(rows_before), 1)
        self.assertEqual(rows_before[0]["amount"], 100)
        self.assertEqual(len(rows_after), 1)
        self.assertEqual(rows_after[0]["amount"], 130)
        # Same for the derived view, via point-in-time reconstruction.
        self.assertEqual(
            db.what_did_we_believe("monthly_totals", "2026-01", tx_before), 250)
        self.assertEqual(
            db.what_did_we_believe("monthly_totals", "2026-01", tx_after), 280)

    def test_audit_log_records_correction_with_before_after(self):
        db, ids = build_db()
        row_id = ids[(date(2026, 1, 15), "North")]
        db.correct("sales", row_id, {"amount": 130},
                   valid_from=date(2026, 1, 15), actor="analyst")
        corrections = [e for e in db.audit_log() if e.operation == "correct"]
        self.assertEqual(len(corrections), 1)
        entry = corrections[0]
        self.assertEqual(entry.actor, "analyst")
        self.assertEqual(entry.table, "sales")
        self.assertEqual(entry.row_id, row_id)
        self.assertEqual(entry.before["amount"], 100)
        self.assertEqual(entry.after["amount"], 130)
        # Inserts were audited too, and the log is append-only / ordered.
        self.assertEqual(len(db.audit_log()), 8)  # 7 inserts + 1 correction
        self.assertEqual([e.seq for e in db.audit_log()],
                         list(range(1, 9)))

    def test_correction_preserves_full_history(self):
        db, ids = build_db()
        row_id = ids[(date(2026, 1, 15), "North")]
        tx_before = db.current_tx
        db.correct("sales", row_id, {"amount": 130},
                   valid_from=date(2026, 1, 15), actor="analyst")
        versions = db.history("sales", row_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0].version, 1)
        self.assertEqual(versions[0].data["amount"], 100)
        self.assertEqual(versions[1].version, 2)
        self.assertEqual(versions[1].data["amount"], 130)
        # Old version is still retrievable via as-of at the earlier tx.
        old = db.as_of("sales", date(2026, 1, 15), tx_before)
        self.assertEqual(old[0]["amount"], 100)
        self.assertEqual(old[0]["_version"], 1)

    def test_correcting_unknown_row_raises(self):
        db, _ = build_db()
        with self.assertRaises(KeyError):
            db.correct("sales", "no-such-row", {"amount": 1},
                       valid_from=date(2026, 1, 15), actor="analyst")


if __name__ == "__main__":
    unittest.main()
