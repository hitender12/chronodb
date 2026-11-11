"""ChronoDB — a database where correcting the past recomputes the future.

Bitemporal storage plus lineage-aware recomputation: a correction to a
past record automatically propagates through derived views, with full
lineage for every derived value.
"""

from .audit import AuditEntry, AuditLog
from .db import ChronoDB
from .lineage import LineageRecord, LineageRegistry, SourceRef
from .store import BitemporalStore, RowVersion
from .views import View, ViewEngine

__all__ = [
    "ChronoDB",
    "BitemporalStore",
    "RowVersion",
    "ViewEngine",
    "View",
    "LineageRecord",
    "LineageRegistry",
    "SourceRef",
    "AuditEntry",
    "AuditLog",
]

__version__ = "0.1.0"
