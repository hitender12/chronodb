"""Append-only audit trail.

Every insert and correction is one :class:`AuditEntry` with a sequence
number, wall-clock timestamp, actor, operation, table, row id, and the
before/after row data. Entries are never mutated or deleted; the log
only grows.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    timestamp: datetime
    actor: str
    operation: str  # "insert" | "correct"
    table: str
    row_id: str
    before: Optional[dict]
    after: dict
    valid_from: Any
    valid_to: Any = None
    tx: int = 0

    def describe(self) -> str:
        return (
            f"#{self.seq} [{self.timestamp.isoformat()}] {self.actor} "
            f"{self.operation} {self.table}:{self.row_id} (tx {self.tx}) "
            f"before={self.before} after={self.after}"
        )


class AuditLog:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def append(self, actor: str, operation: str, table: str, row_id: str,
               before: Optional[dict], after: dict,
               valid_from: Any, valid_to: Any, tx: int) -> AuditEntry:
        entry = AuditEntry(
            seq=len(self._entries) + 1,
            timestamp=datetime.now(timezone.utc),
            actor=actor, operation=operation, table=table, row_id=row_id,
            before=copy.deepcopy(before) if before is not None else None,
            after=copy.deepcopy(after),
            valid_from=valid_from, valid_to=valid_to, tx=tx,
        )
        self._entries.append(entry)
        return entry

    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def for_row(self, row_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.row_id == row_id]

    def __len__(self) -> int:
        return len(self._entries)
