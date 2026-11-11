"""ChronoDB facade: one object tying store + views + lineage + audit together."""

from __future__ import annotations

from typing import Any, Callable

from .audit import AuditEntry, AuditLog
from .lineage import LineageRecord
from .store import BitemporalStore, RowVersion
from .views import ViewEngine

_UNSET = object()


class ChronoDB:
    """A database where correcting the past recomputes the future."""

    def __init__(self) -> None:
        self.store = BitemporalStore()
        self.views = ViewEngine(self.store)
        self.audit = AuditLog()
        self.last_recomputed: list[tuple[str, Any]] = []

    # -- bitemporal writes --------------------------------------------------
    def insert(self, table: str, row: dict, valid_from: Any,
               valid_to: Any = None, actor: str = "system") -> str:
        row_id = self.store.insert(table, row, valid_from, valid_to)
        version = self.store.current_version(table, row_id)
        assert version is not None
        self.audit.append(actor, "insert", table, row_id, None,
                          dict(version.data), valid_from, valid_to,
                          version.tx_from)
        # A new row may belong to an already-computed view key.
        if self.views.views:
            self.last_recomputed = self.views.propagate_insert(table, version.tx_from)
        return row_id

    def correct(self, table: str, row_id: str, new_values: dict,
                valid_from: Any = None, valid_to: Any = _UNSET,
                actor: str = "system") -> RowVersion:
        """Correct a past fact. Supersedes history (never deletes it) and
        recomputes only the derived values whose lineage includes the row.
        """
        before_version = self.store.current_version(table, row_id)
        if before_version is None:
            raise KeyError(f"no current row {row_id!r} in table {table!r}")
        before = dict(before_version.data)
        if valid_to is _UNSET:
            new_version = self.store.correct(table, row_id, new_values, valid_from)
        else:
            new_version = self.store.correct_with_valid_to(
                table, row_id, new_values, valid_from, valid_to)
        self.audit.append(actor, "correct", table, row_id, before,
                          dict(new_version.data),
                          new_version.valid_from, new_version.valid_to,
                          new_version.tx_from)
        self.last_recomputed = self.views.propagate(
            {row_id}, new_version.tx_from)
        return new_version

    # -- reads ---------------------------------------------------------------
    def as_of(self, table: str, valid_time: Any,
              transaction_time: Any = None) -> list[dict]:
        return self.store.as_of(table, valid_time, transaction_time)

    def history(self, table: str, row_id: str) -> list[RowVersion]:
        return self.store.history(table, row_id)

    @property
    def current_tx(self) -> int:
        return self.store.current_tx

    # -- views ----------------------------------------------------------------
    def register_view(self, name: str, compute: Callable, dependencies: list[str],
                      compute_key: Callable | None = None,
                      description: str = ""):
        return self.views.register(name, compute, dependencies,
                                   compute_key, description)

    def compute_all_views(self, tx: Any = None):
        return self.views.compute_all(tx)

    def view_value(self, view_name: str, key: Any) -> Any:
        return self.views.get(view_name, key)

    def view_values(self, view_name: str) -> dict[Any, Any]:
        return self.views.get_all(view_name)

    # -- lineage / point-in-time ----------------------------------------------
    def why(self, view_name: str, key: Any) -> LineageRecord | None:
        return self.views.why(view_name, key)

    def what_did_we_believe(self, view_name: str, key: Any,
                            transaction_time: Any) -> Any:
        return self.views.what_did_we_believe(view_name, key, transaction_time)

    # -- audit ------------------------------------------------------------------
    def audit_log(self) -> list[AuditEntry]:
        return self.audit.entries()

    def audit_for_row(self, row_id: str) -> list[AuditEntry]:
        return self.audit.for_row(row_id)
