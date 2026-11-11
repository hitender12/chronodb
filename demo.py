"""ChronoDB demo: correct one past sales figure, watch the future recompute.

Run:  python demo.py
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from chronodb import ChronoDB

SALES = [
    # (date, region, amount) -- 3 months x 2 regions
    (date(2026, 1, 15), "North", 100),
    (date(2026, 1, 20), "South", 150),
    (date(2026, 2, 10), "North", 200),
    (date(2026, 2, 18), "South", 120),
    (date(2026, 3, 5),  "North", 180),
    (date(2026, 3, 22), "South", 90),
]


def month_bounds(key: str) -> tuple[date, date]:
    year, month = int(key[:4]), int(key[5:7])
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start, end


def monthly_total_key(store, key, tx=None):
    start, end = month_bounds(key)
    rows = store.as_of_range("sales", start, end, tx)
    total = sum(r["amount"] for r in rows)
    steps = [f"{r['region']} sale on {r['_valid_from']}: +{r['amount']}" for r in rows]
    steps.append(f"sum = {total}")
    return total, rows, steps


def monthly_totals(store, tx=None):
    return {f"2026-0{m}": monthly_total_key(store, f"2026-0{m}", tx)
            for m in (1, 2, 3)}


def region_total_key(store, key, tx=None):
    rows = [r for r in store.as_of_range("sales", date(2026, 1, 1), date(2026, 4, 1), tx)
            if r["region"] == key]
    total = sum(r["amount"] for r in rows)
    steps = [f"{r['_valid_from']} sale: +{r['amount']}" for r in rows]
    steps.append(f"sum = {total}")
    return total, rows, steps


def region_totals(store, tx=None):
    return {region: region_total_key(store, region, tx) for region in ("North", "South")}


def inventory_count(store, tx=None):
    rows = store.as_of_range("inventory", date(2026, 1, 1), date(2027, 1, 1), tx)
    out = {}
    for item in sorted({r["item"] for r in rows}):
        item_rows = [r for r in rows if r["item"] == item]
        out[item] = (sum(r["qty"] for r in item_rows), item_rows,
                     [f"{r['item']}: +{r['qty']}" for r in item_rows])
    return out


def main() -> None:
    db = ChronoDB()

    print("== 1. Load sales (3 months x 2 regions) + inventory ==")
    ids = {}
    for day, region, amount in SALES:
        # A sale is a point event: model it as a 1-day valid range so it
        # belongs to exactly one month (an open-ended valid_to would mean
        # "true from this day onwards" and overlap every later month).
        row_id = db.insert("sales", {"region": region, "amount": amount},
                           valid_from=day, valid_to=day + timedelta(days=1),
                           actor="loader")
        ids[(day, region)] = row_id
        print(f"  inserted sales:{row_id}  {day} {region} amount={amount}")
    db.insert("inventory", {"item": "widget", "qty": 500},
              valid_from=date(2026, 1, 1), actor="loader")

    db.register_view("monthly_totals", monthly_totals, ["sales"],
                     compute_key=monthly_total_key,
                     description="sum(amount) of sales rows per calendar month")
    db.register_view("region_totals", region_totals, ["sales"],
                     compute_key=region_total_key,
                     description="sum(amount) of sales rows per region")
    db.register_view("inventory_count", inventory_count, ["inventory"],
                     description="sum(qty) of inventory rows per item")
    db.compute_all_views()
    print("\nMonthly totals (before correction):", db.view_values("monthly_totals"))
    print("Region totals  (before correction):", db.view_values("region_totals"))

    tx_before = db.current_tx
    jan_north = ids[(date(2026, 1, 15), "North")]

    print("\n== 2. Correct the past: 15 Jan North sale was 100, actually 130 ==")
    db.correct("sales", jan_north, {"amount": 130},
               valid_from=date(2026, 1, 15), actor="analyst")
    print("Monthly totals (after correction): ", db.view_values("monthly_totals"))
    print("Region totals  (after correction): ", db.view_values("region_totals"))
    print("Views/keys recomputed by the correction:", db.last_recomputed)
    print("Recompute counts: monthly_totals =",
          db.views.views["monthly_totals"].recompute_count,
          "| region_totals =",
          db.views.views["region_totals"].recompute_count,
          "| inventory_count (unrelated) =",
          db.views.views["inventory_count"].recompute_count)

    print("\n== 3. Lineage: why is the January total 280? ==")
    print(db.why("monthly_totals", "2026-01").explain())

    print("\n== 4. Point-in-time belief: what did we believe at tx",
          f"{tx_before} (before the correction)? ==")
    print("  monthly_totals[2026-01] then:",
          db.what_did_we_believe("monthly_totals", "2026-01", tx_before))
    print("  monthly_totals[2026-01] now: ",
          db.what_did_we_believe("monthly_totals", "2026-01", db.current_tx))
    print("  as_of sales on 2026-01-15 at tx", tx_before, ":",
          db.as_of("sales", date(2026, 1, 15), tx_before))
    print("  as_of sales on 2026-01-15 at tx", db.current_tx, ":",
          db.as_of("sales", date(2026, 1, 15), db.current_tx))

    print("\n== 5. Audit trail (append-only) ==")
    for entry in db.audit_log():
        print(" ", entry.describe())
    print("\nHistory of the corrected row (both versions preserved):")
    for v in db.history("sales", jan_north):
        print(f"  v{v.version} amount={v.data['amount']} valid={v.valid_from} "
              f"tx {v.tx_from}..{v.tx_to}")


if __name__ == "__main__":
    main()
