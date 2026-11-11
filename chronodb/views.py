"""Derived-view engine with lineage-aware, targeted recomputation.

A view is a Python function over the store. Register it with the
tables/columns it reads (``dependencies``). A view function returns a
mapping::

    key -> value | (value, source_row_dicts, steps)

where ``source_row_dicts`` are rows as returned by ``store.as_of*``
(so they carry ``_row_id`` / ``_version`` / ``_valid_from`` /
``_tx_from``) and ``steps`` is a list of human-readable computation
steps. Returning a bare value is allowed; lineage then falls back to
"every row the function read", which the engine cannot attribute
per-key -- per-key sources are the precise form and are what the demo
and tests use.

Targeted propagation
--------------------
The engine caches each view's results plus a :class:`LineageRegistry`.
When a correction lands, ``propagate()`` looks up the corrected row
ids in the reverse lineage index and recomputes *only* the affected
(view, key) pairs:

* If the view registered a ``compute_key(store, key, tx)`` function,
  only that key is recomputed -- the function itself runs for one key.
* Otherwise the view's full ``compute`` function runs again, but only
  for views that have at least one affected key, and only the affected
  keys' cached values/lineage are replaced. Views with no affected
  key are not executed at all (their ``recompute_count`` does not move).

``propagate()`` returns the list of (view, key) pairs recomputed, so
callers can report exactly what a correction changed downstream.
"""

from __future__ import annotations

from typing import Any, Callable

from .lineage import LineageRecord, LineageRegistry, SourceRef
from .store import BitemporalStore


def _source_ref(row: dict) -> SourceRef:
    return SourceRef(
        row_id=row["_row_id"], table=row.get("_table", "?"),
        version=row.get("_version", 0),
        valid_from=row.get("_valid_from"), valid_to=row.get("_valid_to"),
        tx_from=row.get("_tx_from", 0),
    )


def _normalise_result(raw: dict) -> dict[Any, tuple[Any, list[dict], list[str]]]:
    """Normalise a view function's return value to key -> (value, sources, steps)."""
    out: dict[Any, tuple[Any, list[dict], list[str]]] = {}
    for key, item in raw.items():
        if isinstance(item, tuple):
            value = item[0]
            sources = list(item[1]) if len(item) > 1 else []
            steps = list(item[2]) if len(item) > 2 else []
        else:
            value, sources, steps = item, [], []
        out[key] = (value, sources, steps)
    return out


class View:
    def __init__(self, name: str, compute: Callable, dependencies: list[str],
                 compute_key: Callable | None = None,
                 description: str = ""):
        self.name = name
        self.compute = compute
        self.compute_key = compute_key
        self.dependencies = list(dependencies)
        self.description = description or name
        self.recompute_count = 0  # times this view was (re)executed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"View({self.name!r}, deps={self.dependencies}, recomputes={self.recompute_count})"


class ViewEngine:
    def __init__(self, store: BitemporalStore):
        self.store = store
        self.views: dict[str, View] = {}
        self.values: dict[str, dict[Any, Any]] = {}  # view -> key -> value
        self.lineage = LineageRegistry()

    # -- registration / full computation ----------------------------------
    def register(self, name: str, compute: Callable, dependencies: list[str],
                 compute_key: Callable | None = None,
                 description: str = "") -> View:
        """Register a view.

        ``compute(store, tx=None) -> {key: value | (value, sources, steps)}``
        ``compute_key(store, key, tx=None) -> value | (value, sources, steps)``
        (optional; enables true per-key recomputation).
        """
        view = View(name, compute, dependencies, compute_key, description)
        self.views[name] = view
        self.values[name] = {}
        return view

    def compute_all(self, tx: Any = None) -> dict[str, dict[Any, Any]]:
        """Compute every registered view at transaction ``tx`` (latest if None)."""
        tx_seq = self.store._resolve_tx(tx)
        for view in self.views.values():
            self._run_full(view, tx_seq)
        return {name: dict(vals) for name, vals in self.values.items()}

    def _run_full(self, view: View, tx_seq: int) -> None:
        raw = view.compute(self.store, tx_seq)
        view.recompute_count += 1
        for key, (value, sources, steps) in _normalise_result(raw).items():
            self._store_result(view, key, value, sources, steps, tx_seq)

    def _store_result(self, view: View, key: Any, value: Any,
                      sources: list[dict], steps: list[str], tx_seq: int) -> None:
        self.values.setdefault(view.name, {})[key] = value
        self.lineage.record(LineageRecord(
            view_name=view.name, key=key, value=value,
            sources=[_source_ref(r) for r in sources],
            steps=list(steps), definition=view.description,
            computed_tx=tx_seq,
        ))

    def get(self, view_name: str, key: Any) -> Any:
        return self.values[view_name][key]

    def get_all(self, view_name: str) -> dict[Any, Any]:
        return dict(self.values.get(view_name, {}))

    # -- targeted propagation ---------------------------------------------
    def propagate(self, corrected_row_ids: set[str], tx: Any = None) -> list[tuple[str, Any]]:
        """Recompute only derived values whose lineage includes a corrected row.

        Returns the sorted list of (view_name, key) pairs recomputed.
        Views with no affected key are not executed at all.
        """
        tx_seq = self.store._resolve_tx(tx)
        affected = self.lineage.affected_by(set(corrected_row_ids))
        if not affected:
            return []
        by_view: dict[str, list[Any]] = {}
        for view_name, key in affected:
            by_view.setdefault(view_name, []).append(key)
        recomputed: list[tuple[str, Any]] = []
        for view_name in sorted(by_view):
            view = self.views[view_name]
            keys = sorted(by_view[view_name], key=repr)
            if view.compute_key is not None:
                view.recompute_count += 1
                for key in keys:
                    result = view.compute_key(self.store, key, tx_seq)
                    value, sources, steps = _normalise_result({key: result})[key]
                    self._store_result(view, key, value, sources, steps, tx_seq)
                    recomputed.append((view_name, key))
            else:
                # Full-function fallback: run once per affected view, but
                # replace only the affected keys in the cache.
                raw = view.compute(self.store, tx_seq)
                view.recompute_count += 1
                normalised = _normalise_result(raw)
                for key in keys:
                    if key in normalised:
                        value, sources, steps = normalised[key]
                        self._store_result(view, key, value, sources, steps, tx_seq)
                        recomputed.append((view_name, key))
        return sorted(recomputed)

    def propagate_insert(self, table: str, tx: Any = None) -> list[tuple[str, Any]]:
        """A brand-new row has no prior lineage, so it cannot be found
        via the reverse index. Recompute every view that depends on its
        table (full recompute of those views only). Returns recomputed keys.
        """
        tx_seq = self.store._resolve_tx(tx)
        recomputed: list[tuple[str, Any]] = []
        for view in self.views.values():
            if table in view.dependencies:
                before = set(self.values.get(view.name, {}))
                self._run_full(view, tx_seq)
                for key in sorted(self.values[view.name], key=repr):
                    recomputed.append((view.name, key))
        return sorted(recomputed)

    # -- lineage / point-in-time ------------------------------------------
    def why(self, view_name: str, key: Any) -> LineageRecord | None:
        """Lineage record for a derived value, or None if unknown."""
        return self.lineage.get(view_name, key)

    def what_did_we_believe(self, view_name: str, key: Any,
                            transaction_time: Any) -> Any:
        """The derived value as believed at ``transaction_time``.

        Recomputes from as-of rows at that transaction time. Does not
        mutate the live cache, lineage, or recompute counts.
        Raises KeyError if the view/key did not exist then.
        """
        view = self.views[view_name]
        tx_seq = self.store._resolve_tx(transaction_time)
        if view.compute_key is not None:
            result = view.compute_key(self.store, key, tx_seq)
            return _normalise_result({key: result})[key][0]
        raw = view.compute(self.store, tx_seq)
        normalised = _normalise_result(raw)
        if key not in normalised:
            raise KeyError(f"{view_name}[{key!r}] did not exist at tx {tx_seq}")
        return normalised[key][0]
