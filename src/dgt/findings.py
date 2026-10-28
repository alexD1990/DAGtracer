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
    QUERY_ERROR = "query_error"


class Cause(str, Enum):
    COLUMN_DROPPED = "column_dropped"
    COLUMN_APPEARS = "column_appears"
    PRESENT_AT_SOURCE = "present_at_source"
    COLUMN_ABSENT_AT_SOURCE = "column_absent_at_source"
    VALUE_DROPPED = "value_dropped"
    VALUE_APPEARS = "value_appears"
    VALUE_PRESENT_AT_SOURCE = "value_present_at_source"
    VALUE_ABSENT_AT_SOURCE = "value_absent_at_source"


@dataclass(frozen=True)
class ParentStatus:
    """A parent as seen when judging a child.

    Unknown parents are transparent: their own parents stand in for them, and
    `via` lists the unknown nodes passed through, nearest the child first. An
    unknown parent with no parents of its own is kept with status unknown.
    `values` holds per-value statuses in value mode.
    """

    node: Node
    status: Status
    via: tuple[Node, ...] = ()
    values: dict[str, Status] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """A node where the column or values start or stop, judged against its parents.

    Column level, which takes precedence over values:
    - column_dropped: the node lacks the column and a known parent has it.
    - column_appears: the node has the column and no known parent has it.
    - present_at_source: a seed or source with the column (column mode).
    - column_absent_at_source: a seed or source without the column, reached
      along a branch where the column was absent all the way.

    Value level, per value, where the node has the column:
    - value_dropped: absent in the node, present in a known parent.
    - value_appears: present in the node, present in no known parent.
    - value_present_at_source: present in a seed or source.
    - value_absent_at_source: absent in a seed or source, reached along a
      branch where the value was absent all the way.

    The *_at_source findings make "nowhere upstream" explicit rather than an
    empty result.

    `values` maps the affected values to their status in this node (value
    mode only); for column_appears these are all tracked values. `depth` is the
    node's signed level in the traversal that found it. A finding that depends
    on an unknown node is `uncertain`; `candidates` then lists the nodes where
    the change may actually happen, this node first.
    """

    cause: Cause
    node: Node
    parents: tuple[ParentStatus, ...]
    direction: Direction
    depth: int
    uncertain: bool = False
    candidates: tuple[Node, ...] = ()
    values: dict[str, Status] = field(default_factory=dict)


@dataclass(frozen=True)
class Visit:
    """One node reached by a traversal.

    `status` is the column status and `values` the per-value statuses (value
    mode). `stopped` marks nodes whose neighbors were not explored because
    the column, or every tracked value, changed status there.
    """

    node: Node
    depth: int
    status: Status
    via: str | None
    stopped: bool = False
    unknown_reason: UnknownReason | None = None
    column_type: str | None = None
    values: dict[str, Status] = field(default_factory=dict)
    detail: str | None = None


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
    values: tuple[str, ...] = ()
    start_values: dict[str, Status] = field(default_factory=dict)
    column_type: str | None = None
    traces: list[Trace] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
