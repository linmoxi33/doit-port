"""Exception hierarchy for doit-port.

Every error raised by this package derives from :class:`PortError` so callers
can distinguish "the portability layer failed" from "the user's task failed".
"""

from __future__ import annotations


class PortError(Exception):
    """Base class for all doit-port failures."""


class ConfigError(PortError):
    """Configuration file is missing, malformed or self-contradictory."""


class UnsupportedTask(PortError):
    """The task cannot be made portable, and we refuse to pretend otherwise.

    Raised for tasks without ``targets`` (nothing to restore), tasks whose
    targets are directories, and tasks produced by ``yield`` sub-task
    generators where we cannot attribute outputs deterministically.
    """


class ReceiptCorrupt(PortError):
    """A receipt file exists but does not satisfy the schema or its digests."""


class StoreCorrupt(PortError):
    """A content-addressed object is missing or does not match its key."""


class BundleError(PortError):
    """An exported bundle is incomplete, tampered with, or unreadable."""
