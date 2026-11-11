# ChronoDB — Architecture

## Components

```
                    ┌──────────────────┐
                    │    ChronoDB      │  chronodb/db.py  (facade)
                    │  insert/correct  │
                    │  as_of / why /   │
                    │  what_did_we_    │
                    │  believe / audit │
                    └──────┬───┬───┬───┘
              ┌────────────┘   │   └────────────┐
              ▼                ▼                ▼
   ┌─────────────────┐ ┌──────────────┐ ┌──────────────┐
   │ BitemporalStore │ │  ViewEngine  │ │   AuditLog   │
   │ chronodb/       │ │ chronodb/    │ │ chronodb/    │
   │ store.py        │ │ views.py     │ │ audit.py     │
   │ RowVersion list │ │ view cache + │ │ append-only  │
   │ tx clock (seq + │ │ recompute    │ │ entries      │
   │ wall time)      │ │ counts       │ │              │
   └─────────────────┘ └──────┬───────┘ └──────────────┘
                              │ uses
                              ▼
                       ┌──────────────┐
                       │ Lineage-     │
                       │ Registry     │
                       │ chronodb/    │
                       │ lineage.py   │
                       │ (view,key) ->│
                       │ record;      │
                       │ reverse index│
                       │ row -> keys  │
                       └──────────────┘
```

## Data model

A logical row is a chain of `RowVersion`s sharing one `row_id`:

| field | meaning |
|---|---|
| `data` | the row's fields (a dict, deep-copied on write) |
| `valid_from` / `valid_to` | when the fact was true in the world (`valid_to=None` = open-ended) |
| `tx_from` / `tx_to` | when we believed it (`tx_to=None` = still believed) |
| `tx_from_wall` / `tx_to_wall` | wall-clock timestamps alongside the tx sequence |
| `version` | 1, 2, 3, … within the row's chain |

Transaction time is a logical sequence number (1, 2, 3, …) assigned
by the store, plus a wall timestamp per sequence number. `as_of`
accepts either an `int` sequence or a `datetime` wall time (resolved
to the greatest sequence whose wall time is ≤ the argument), or
`None` for "latest". A version is visible at `(valid_time, tx)` iff
`valid_from ≤ valid_time < valid_to` and `tx_from ≤ tx < tx_to`.

## Write path

`ChronoDB.correct(table, row_id, new_values, valid_from, valid_to, actor)`:

1. Look up the current version of the row (`tx_to is None`); raise
   `KeyError` if none.
2. Close it: `tx_to = new_tx`. Append a new version (`version + 1`)
   with merged data and the given (or inherited) valid range.
3. Append an audit entry: actor, operation, table, row id, before/after
   data, tx.
4. Propagate: `ViewEngine.propagate({row_id}, tx)`.

`insert` is the same minus step 1/2, except a new row has no prior
lineage, so propagation is dependency-based instead (see below).

## View engine and targeted propagation

A view is registered with:

- `compute(store, tx) -> {key: value | (value, sources, steps)}` — the
  full computation. `sources` are rows as returned by `as_of*` (they
  carry `_row_id`, `_version`, `_valid_from`, `_tx_from`), so lineage
  is exact per key. A bare value is allowed, with lineage falling back
  to "no recorded sources".
- optionally `compute_key(store, key, tx)` — computes a single key.
  This is what makes propagation genuinely per-key.
- `dependencies`: the tables it reads (used for insert propagation).

The engine caches `values[view][key]` and a `LineageRegistry`:
`(view, key) -> LineageRecord`, plus the reverse index
`row_id -> {(view, key)}` derived from the records.

On correction, `propagate(corrected_row_ids)`:

1. `affected = lineage.affected_by(corrected_row_ids)` — set lookup,
   no scan of view definitions beyond the registry.
2. Group affected keys by view. For each affected view:
   - with `compute_key`: call it once per affected key, replace only
     those keys in the cache/lineage.
   - without `compute_key`: run `compute` once, replace only the
     affected keys (other keys keep their cached value/lineage even
     though the function re-ran — the function-level rerun is the
     documented fallback; see Limitations).
3. Views with no affected key are never executed; their
   `recompute_count` does not move. `propagate` returns the sorted
   list of recomputed `(view, key)` pairs, surfaced as
   `ChronoDB.last_recomputed`.

On insert, `propagate_insert(table)` recomputes every view whose
`dependencies` include the table, because a new row cannot be found
via the reverse lineage index (it had no lineage before it existed).

## Lineage and point-in-time reconstruction

`why(view, key)` returns the cached `LineageRecord`: view definition
string, computation steps, and the `SourceRef`s (row id, table,
version, valid range, recording tx) that produced the value.

`what_did_we_believe(view, key, transaction_time)` recomputes the
value from as-of rows at that tx — via `compute_key` if present,
otherwise via the full `compute` filtered to the key — without
touching the live cache, lineage, or recompute counts.

## Limitations (by design, for v0.1)

- **No range splitting.** `correct()` supersedes the whole current
  version. If the correction's valid range only partly overlaps the
  old version's range, the old version is still superseded whole and
  the new version covers exactly the range passed in; no
  before/after fragments are created. Callers should pass the full
  intended range. A future version would split the old version into
  up to two surviving fragments plus the corrected middle.
- **No persistence, no concurrency.** In-memory lists; single-threaded.
  The tx sequence is the only ordering; there is no locking.
- **Insert propagation is coarse** (dependency-based, not lineage-based)
  for the reason above.
- **Views without `compute_key`** rerun their full function on any
  correction affecting them, even though only affected keys are
  replaced in the cache.
- **No validation of valid ranges** (e.g. `valid_from > valid_to` is
  not rejected) and no schema enforcement on row dicts.
- **Audit log is not tamper-evident** (no hash chain) and is lost
  with the process, like everything else.
