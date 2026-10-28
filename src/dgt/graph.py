"""DAG adjacency built from the manifest's `parent_map` and `child_map`."""

from __future__ import annotations

from enum import Enum
from typing import Any


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"


# Node types a traversal may step into, per direction.
UPSTREAM_TYPES = frozenset({"model", "snapshot", "seed", "source"})
DOWNSTREAM_TYPES = frozenset({"model", "snapshot"})
# Upstream traversal ends at these; they have no data-bearing parents.
TERMINAL_UPSTREAM_TYPES = frozenset({"seed", "source"})


class DagGraph:
    def __init__(self, manifest: dict[str, Any]) -> None:
        self._parent_map: dict[str, list[str]] = manifest.get("parent_map", {})
        self._child_map: dict[str, list[str]] = manifest.get("child_map", {})
        self._types: dict[str, str] = {
            uid: raw["resource_type"]
            for section in ("nodes", "sources", "exposures", "metrics", "semantic_models")
            for uid, raw in manifest.get(section, {}).items()
        }

    def resource_type(self, unique_id: str) -> str | None:
        return self._types.get(unique_id)

    def neighbors(self, unique_id: str, direction: Direction) -> list[str]:
        """Parents (UP) or children (DOWN) a traversal may step into, sorted by unique_id."""
        if direction is Direction.UP:
            if self.resource_type(unique_id) in TERMINAL_UPSTREAM_TYPES:
                return []
            candidates, allowed = self._parent_map.get(unique_id, []), UPSTREAM_TYPES
        else:
            candidates, allowed = self._child_map.get(unique_id, []), DOWNSTREAM_TYPES
        return sorted(uid for uid in set(candidates) if self.resource_type(uid) in allowed)
