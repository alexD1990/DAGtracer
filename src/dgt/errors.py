"""Exception types shared across dgt modules."""


class DgtError(Exception):
    """A tool error: bad input, missing artifacts or a failing dbt call. Maps to exit code 2."""


class ConfigError(DgtError):
    """Invalid CLI options or `.dgt.yml` contents."""


class ArtifactError(DgtError):
    """Missing or unreadable dbt artifacts (manifest.json, catalog.json)."""


class NodeNotFoundError(DgtError):
    """No node in the manifest matches the given name."""


class AmbiguousNodeError(DgtError):
    """More than one node in the manifest matches the given short name."""
