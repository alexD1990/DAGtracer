"""Data types for traversal results and status-change findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from dgt.graph import Direction
from dgt.project import Node


class Status(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class UnknownReason(str, Enum):
    EPHEMERAL = "ephemeral"
    NOT_IN_CATALOG = "not_in_catalog"


class Cause(str, Enum):
    COLUMN_DROPPED = "column_dropped"
    COLUMN_APPEARS = "column_appears"
    VALUE_DROPPED = "value_dropped"
    VALUE_APPEARS = "value_appears"


@dataclass(frozen=True)
class Finding:
    """A DAG edge across which status changes.

    `upstream` and `downstream` are in data-flow order (parent, child),
    independent of the traversal direction that found the edge. `depth` is the
    signed level of the node where the traversal observed the change.
    """

    cause: Cause
    upstream: Node
    downstream: Node
    direction: Direction
    depth: int
    value: str | None = None


@dataclass(frozen=True)
class Visit:
    """One node reached by a traversal.

    `effective_status` is the node's own status, or for unknown nodes the
    status inherited from the node it was reached through. `stopped` marks
    nodes whose neighbors were not explored because the status changed.
    """

    node: Node
    depth: int
    status: Status
    effective_status: Status
    via: str | None
    stopped: bool = False
    unknown_reason: UnknownReason | None = None


@dataclass
class Trace:
    direction: Direction
    visits: list[Visit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


@dataclass
class Report:
    start: Node
    column: str
    start_status: Status
    full_trace: bool
    traces: list[Trace] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
