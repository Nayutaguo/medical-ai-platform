"""Stable domain errors for the governed analysis-history feature."""

from __future__ import annotations


class HistoryError(RuntimeError):
    """Base class for errors that are safe for the HTTP adapter to map."""


class HistoryValidationError(HistoryError):
    """Raised when a history cursor or mutation payload is invalid."""


class HistoryNotFoundError(HistoryError):
    """Raised when an entry is absent from the current membership scope."""


class HistoryVersionConflictError(HistoryError):
    """Raised when optimistic locking detects a stale favorite mutation."""
