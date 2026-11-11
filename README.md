# ChronoDB

**A database where correcting the past recomputes the future.**

Real data is corrected retroactively all the time — a sales figure was
mis-keyed, a sensor reading was recalibrated, a price was backdated.
Ordinary databases handle this badly: history is either treated as
immutable (so the correction can never be recorded) or silently
overwritten (so nobody can tell what was believed, when, or why a
report changed). ChronoDB does neither.

## The novel idea

**Bitemporal storage + lineage-aware recomputation.**

1. Every row version carries two clocks: *valid time* (when the fact
   was true in the world) and *transaction time* (when we learned it).
   A correction never deletes history — it supersedes one version and
   appends a new one, so any past belief can be reconstructed exactly.
2. Every derived value records the exact source rows (and versions) it
   was computed from. When a correction lands, ChronoDB looks up the
   corrected row in that reverse lineage index and recomputes **only**
   the derived values that actually depend on it. Unrelated views are
   not touched at all — verified by test, not by assertion in prose.
3. `why(view, key)` explains any derived value: its definition, the
   computation steps, and the source rows with their versions, valid
   times and transaction times.

## Quickstart

Requires Python 3.10+ (developed on 3.12). Standard library only —
no installs, no network.

```bash
python demo.py
python -m unittest discover -s tests -v
```

```python
from datetime import date
from chronodb import ChronoDB

db = ChronoDB()
row = db.insert("sales", {"region": "North", "amount": 100},
                valid_from=date(2026, 1, 15),
                valid_to=date(2026, 1, 16), actor="loader")

db.register_view("monthly_totals", monthly_totals, ["sales"],
                 compute_key=monthly_total_key,
                 description="sum(amount) per calendar month")
db.compute_all_views()
print(db.view_value("monthly_totals", "2026-01"))   # 250 (with Jan South 150)

tx_before = db.current_tx
db.correct("sales", row, {"amount": 130},
           valid_from=date(2026, 1, 15), actor="analyst")
print(db.view_value("monthly_totals", "2026-01"))   # 280 -- recomputed
print(db.last_recomputed)  # [('monthly_totals', '2026-01'), ...]
print(db.why("monthly_totals", "2026-01").explain())
print(db.what_did_we_believe("monthly_totals", "2026-01", tx_before))  # 250
print([e.describe() for e in db.audit_log()])
```

See `demo.py` for the full worked example (view definitions included)
and `ARCHITECTURE.md` for the design.

## What genuinely works

- Bitemporal `insert` / `correct` / `as_of` (point valid times and
  non-overlapping ranges), with full history preserved and retrievable
  via `history()` or `as_of(..., earlier_tx)`.
- Targeted propagation: a correction recomputes only lineage-affected
  (view, key) pairs; per-key views recompute just that key, other
  views run once per affected view and only affected keys are replaced,
  unrelated views are not executed at all.
- `why()` lineage with exact contributing source rows (versions
  included), `what_did_we_believe()` point-in-time reconstruction.
- Append-only audit log (seq, timestamp, actor, before/after).
- Demo + 7 unittest tests, all passing (verified by running them).

## Honest status / limitations

- **In-memory only.** No persistence, no disk format, no recovery.
- **No concurrency.** Single-threaded use; no locking or transactions.
- **No range splitting.** A correction supersedes the whole current
  version of a row and the new version covers exactly the range passed
  to `correct()`. Correcting only part of a valid range does not split
  the old version into before/after fragments (see `ARCHITECTURE.md`).
- **Open-ended valid ranges are literal.** `valid_to=None` means "true
  from `valid_from` onwards", so a point event stored that way overlaps
  every later period. Model point events as 1-day ranges (as the demo
  does) if you want them in exactly one period.
- **Insert propagation is dependency-based, not lineage-based.** A
  brand-new row has no prior lineage, so inserts recompute all views
  depending on that table; only corrections get lineage-targeted
  propagation.
- **Views are Python functions**, not a query language; there is no
  SQL, no indexing beyond the lineage reverse index, and no query
  optimiser. Performance is O(rows) per recomputed key.
- **Audit log is in-memory** and not tamper-evident (no hashing /
  signatures).

## Evaluation plan

1. **Correctness:** the test suite covers propagation, non-recomputation
   of unrelated views, exact lineage, as-of beliefs, audit before/after,
   and history preservation. Extend it with: a correction to a Feb row
   (Jan/Feb totals must move independently), two corrections to the same
   row (v1/v2/v3 all retrievable), and a view with no `compute_key`
   (fallback path).
2. **Targeting:** instrument `recompute_count` per view (already
   exposed) while correcting rows in a larger synthetic dataset
   (e.g. 12 months x 10 regions) and assert recomputed keys stay
   proportional to the correction's lineage, not to the dataset size.
3. **As-of fidelity:** random sequences of inserts/corrections, then
   for random (valid_time, tx) pairs compare `as_of` against a naive
   replay of the audit log.
4. **Next steps if this grows:** persistence (append-only log file +
   snapshot), proper range splitting on correction, a SQL-ish view
   language, and concurrency control.

## Project layout

```
chronodb/
  __init__.py   public API (ChronoDB, BitemporalStore, ViewEngine, ...)
  store.py      bitemporal store (RowVersion, insert/correct/as_of)
  views.py      derived-view engine, targeted propagation
  lineage.py    lineage records + reverse index
  audit.py      append-only audit log
  db.py         ChronoDB facade tying the four together
demo.py         runnable end-to-end demo
tests/
  test_chronodb.py
ARCHITECTURE.md
LICENSE (MIT)
```
