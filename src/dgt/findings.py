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
    PRESENT_AT_SOURCE = "present_at_source"
    VALUE_DROPPED = "value_dropped"
    VALUE_APPEARS = "value_appears"


@dataclass(frozen=True)
class ParentStatus:
    node: Node
    status: Status


@dataclass(frozen=True)
class Finding:
    """A node where the column starts or stops, judged against its parents.

    - column_dropped: the node is absent and at least one known parent is present.
    - column_appears: the node is present and no known parent is present.
    - present_at_source: a seed or source where the column is present.

    `depth` is the node's signed level in the traversal that found it.
    """

    cause: Cause
    node: Node
    parents: tuple[ParentStatus, ...]
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
