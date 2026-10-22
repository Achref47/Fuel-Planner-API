"""Counts the external HTTP requests a single API call actually makes (for diagnostics)."""

from __future__ import annotations

from collections import Counter
from contextvars import ContextVar

_current: ContextVar[Counter | None] = ContextVar("fuelplanner_external_calls", default=None)


def start() -> Counter:
    """Begin counting for the current request context."""
    counter: Counter = Counter()
    _current.set(counter)
    return counter


def record(kind: str) -> None:
    counter = _current.get()
    if counter is not None:
        counter[kind] += 1
