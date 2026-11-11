"""Bitemporal store: every row version carries two clocks.

* ``valid_time``  -- when the fact was true in the world.
* ``transaction_time`` -- when we learned / recorded it.

History is never deleted. A correction supersedes the old version
(sets its ``tx_to``) and appends a new version with the same ``row_id``.
``as_of(valid_time, transaction_time)`` therefore answers both
"what was true then?" and "what did we *believe* then, at the time?".

Simplifications (documented, see README / ARCHITECTURE):
* Point valid times and non-overlapping ranges are fully supported.
* A correction supersedes the single latest (still-current) version of
  a ``row_id`` and the new version takes the valid range given to
  ``correct()``. Overlapping ranges belonging to *different* row ids
  are untouched, and a correction whose valid range only partly
  overlaps the old version's range does NOT split the old version
  into before/after fragments -- the old version is superseded whole
  and the new version covers exactly the range passed in. Callers
  correcting a sub-range should pass the full intended range.
* Transaction time is a logical sequence number (1, 2, 3, ...) plus a
  wall-clock timestamp recorded alongside it. ``as_of`` accepts either
  an ``int`` sequence number or a ``datetime`` wall time.
* In-memory only; no persistence, no concurrency control.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class RowVersion:
    """One immutable version of one logical row."""

    row_id: str
    table: str
    data: dict
    valid_from: Any
    valid_to: Any = None  # None == open-ended
    tx_from: int = 0
    tx_to: Optional[int] = None  # None == still current belief
    tx_from_wall: Optional[datetime] = None
    tx_to_wall: Optional[datetime] = None
    version: int = 1

    def is_current(self) -> bool:
        return self.tx_to is None

    def covers_valid(self, valid_time: Any) -> bool:
        if self.valid_from is not None and valid_time < self.valid_from:
            return False
        if self.valid_to is not None and valid_time >= self.valid_to:
            return False
        return True

    def overlaps_valid(self, valid_from: Any, valid_to: Any) -> bool:
        """True if [valid_from, valid_to) overlaps this version's range."""
        if valid_to is not None and self.valid_from is not None and valid_to <= self.valid_from:
            return False
        if self.valid_to is not None and valid_from is not None and self.valid_to <= valid_from:
            return False
        return True

    def visible_at_tx(self, tx_seq: int) -> bool:
        return self.tx_from <= tx_seq and (self.tx_to is None or tx_seq < self.tx_to)

    def to_dict(self) -> dict:
        """Row data plus bitemporal metadata (metadata keys are _-prefixed)."""
        out = copy.deepcopy(self.data)
        out["_row_id"] = self.row_id
        out["_table"] = self.table
        out["_version"] = self.version
        out["_valid_from"] = self.valid_from
        out["_valid_to"] = self.valid_to
        out["_tx_from"] = self.tx_from
        out["_tx_to"] = self.tx_to
        return out


class BitemporalStore:
    """In-memory bitemporal tables."""

    def __init__(self) -> None:
        self._versions: list[RowVersion] = []
        self._by_row: dict[str, list[RowVersion]] = {}
        self._tx_seq: int = 0
        self._tx_wall: dict[int, datetime] = {}

    # -- transaction clock -------------------------------------------------
    @property
    def current_tx(self) -> int:
        """Latest transaction sequence number (0 before any mutation)."""
        return self._tx_seq

    def _next_tx(self) -> tuple[int, datetime]:
        self._tx_seq += 1
        wall = datetime.now(timezone.utc)
        self._tx_wall[self._tx_seq] = wall
        return self._tx_seq, wall

    def _resolve_tx(self, transaction_time: Any) -> int:
        """Resolve an int seq, a datetime wall time, or None (latest)."""
        if transaction_time is None:
            return self._tx_seq
        if isinstance(transaction_time, datetime):
            seq = 0
            for s, wall in self._tx_wall.items():
                if wall <= transaction_time and s > seq:
                    seq = s
            return seq
        return int(transaction_time)

    # -- writes ------------------------------------------------------------
    def insert(self, table: str, row: dict, valid_from: Any,
               valid_to: Any = None) -> str:
        """Insert a new logical row. Returns its row_id.

        If ``row`` contains an ``"id"`` key it is used as the row_id
        (and kept in the data); otherwise a short uuid is generated.
        """
        tx, wall = self._next_tx()
        row_id = str(row.get("id") or uuid.uuid4().hex[:12])
        data = copy.deepcopy(row)
        version = RowVersion(
            row_id=row_id, table=table, data=data,
            valid_from=valid_from, valid_to=valid_to,
            tx_from=tx, tx_to=None, tx_from_wall=wall, version=1,
        )
        self._versions.append(version)
        self._by_row.setdefault(row_id, []).append(version)
        return row_id

    def correct(self, table: str, row_id: str, new_values: dict,
                valid_from: Any = None, valid_to: Any = None,
                *, _valid_to_set: bool = False) -> RowVersion:
        """Supersede the current version of ``row_id``; never deletes history.

        ``new_values`` are merged over the old row's data. If
        ``valid_from`` is None the old version's valid_from is kept.
        ``valid_to``: pass the value explicitly to change it; omitting it
        keeps the old valid_to (see note in module docstring about the
        no-range-splitting simplification).
        Returns the new RowVersion. Raises KeyError if no current
        version of ``row_id`` exists in ``table``.
        """
        current = self.current_version(table, row_id)
        if current is None:
            raise KeyError(f"no current row {row_id!r} in table {table!r}")
        tx, wall = self._next_tx()
        current.tx_to = tx
        current.tx_to_wall = wall
        data = copy.deepcopy(current.data)
        data.update(copy.deepcopy(new_values))
        new_version = RowVersion(
            row_id=row_id, table=table, data=data,
            valid_from=valid_from if valid_from is not None else current.valid_from,
            valid_to=valid_to if _valid_to_set else current.valid_to,
            tx_from=tx, tx_to=None, tx_from_wall=wall,
            version=current.version + 1,
        )
        self._versions.append(new_version)
        self._by_row.setdefault(row_id, []).append(new_version)
        return new_version

    def correct_with_valid_to(self, table: str, row_id: str, new_values: dict,
                              valid_from: Any = None, valid_to: Any = None) -> RowVersion:
        """``correct()`` with an explicitly set ``valid_to`` (even None)."""
        return self.correct(table, row_id, new_values, valid_from,
                            valid_to, _valid_to_set=True)

    # -- reads -------------------------------------------------------------
    def current_version(self, table: str, row_id: str) -> Optional[RowVersion]:
        for v in reversed(self._by_row.get(row_id, [])):
            if v.table == table and v.is_current():
                return v
        return None

    def history(self, table: str, row_id: str) -> list[RowVersion]:
        """All versions of a row, oldest first (full preserved history)."""
        return [v for v in self._by_row.get(row_id, [])
                if v.table == table]

    def as_of(self, table: str, valid_time: Any,
              transaction_time: Any = None) -> list[dict]:
        """Rows believed at ``transaction_time`` for ``valid_time``.

        A version qualifies when its valid range covers ``valid_time``
        and its transaction range covers ``transaction_time``.
        """
        tx = self._resolve_tx(transaction_time)
        return [v.to_dict() for v in self._versions
                if v.table == table and v.visible_at_tx(tx)
                and v.covers_valid(valid_time)]

    def as_of_range(self, table: str, valid_from: Any, valid_to: Any,
                    transaction_time: Any = None) -> list[dict]:
        """Rows believed at ``transaction_time`` whose valid range overlaps
        ``[valid_from, valid_to)``. Used by views that aggregate a period.
        """
        tx = self._resolve_tx(transaction_time)
        return [v.to_dict() for v in self._versions
                if v.table == table and v.visible_at_tx(tx)
                and v.overlaps_valid(valid_from, valid_to)]

    def current_rows(self, table: str) -> list[dict]:
        """Latest belief for every row in ``table`` (any valid time)."""
        return [v.to_dict() for v in self._versions
                if v.table == table and v.is_current()]

    def tables(self) -> list[str]:
        return sorted({v.table for v in self._versions})
