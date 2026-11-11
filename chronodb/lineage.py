"""Lineage: every derived value records exactly what produced it.

A :class:`LineageRecord` is the answer to "why is this value what it is?":
the view definition that computed it, the source rows (with their
versions, valid times and transaction times) it was computed from, and
the computation steps in plain language. The registry also maintains
the reverse index -- source row id -> {(view, key)} -- that lets a
correction find the derived values it affects without a full recompute.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceRef:
    """One contributing source row version."""

    row_id: str
    table: str
    version: int
    valid_from: Any
    valid_to: Any = None
    tx_from: int = 0

    def describe(self) -> str:
        vt = f"valid {self.valid_from}" + (f" to {self.valid_to}" if self.valid_to is not None else " onwards")
        return f"{self.table}:{self.row_id} v{self.version} ({vt}, recorded at tx {self.tx_from})"


@dataclass
class LineageRecord:
    view_name: str
    key: Any
    value: Any
    sources: list[SourceRef] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    definition: str = ""
    computed_tx: int = 0

    @property
    def source_row_ids(self) -> set[str]:
        return {s.row_id for s in self.sources}

    def explain(self) -> str:
        lines = [
            f"why {self.view_name}[{self.key!r}] == {self.value!r}:",
            f"  definition: {self.definition or self.view_name}",
            "  computation:",
        ]
        lines += [f"    - {step}" for step in self.steps] or ["    - (no steps recorded)"]
        lines.append(f"  derived from {len(self.sources)} source row(s):")
        lines += [f"    - {s.describe()}" for s in self.sources] or ["    - (no sources)"]
        lines.append(f"  computed at transaction {self.computed_tx}")
        return "\n".join(lines)


class LineageRegistry:
    """(view_name, key) -> LineageRecord, plus the reverse index."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, Any], LineageRecord] = {}

    def record(self, record: LineageRecord) -> None:
        self._records[(record.view_name, record.key)] = record

    def get(self, view_name: str, key: Any) -> LineageRecord | None:
        return self._records.get((view_name, key))

    def affected_by(self, row_ids: set[str]) -> set[tuple[str, Any]]:
        """All (view, key) pairs whose lineage includes any of ``row_ids``.

        This is the targeted-propagation lookup: a correction touches
        exactly these derived values, nothing else.
        """
        affected: set[tuple[str, Any]] = set()
        for (view_name, key), rec in self._records.items():
            if rec.source_row_ids & row_ids:
                affected.add((view_name, key))
        return affected

    def all_keys(self, view_name: str) -> list[Any]:
        return [k for (v, k) in self._records if v == view_name]

    def snapshot(self) -> dict[tuple[str, Any], LineageRecord]:
        return copy.deepcopy(self._records)
