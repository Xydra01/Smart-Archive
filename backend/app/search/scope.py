"""Resolved query scope: what subset of sources a query should search.

`QueryScope` is a small, immutable value object produced once at the API
boundary and handed to the retrieval layer. It captures a single, deliberate
distinction that the rest of the system depends on:

    selection is None   -> no selection was made: search the WHOLE archive.
    selection == frozenset()  -> an explicit EMPTY selection: search NOTHING
                                  (zero results). This is NOT a whole-archive
                                  fallback — an empty group or a selection whose
                                  members are all unindexed lands here.
    selection == {...}  -> restrict retrieval to these ``source_path`` values.

Keeping ``None`` (whole archive) distinct from an empty ``frozenset`` (zero
results) is what lets the retrieval layer tell "search everything" apart from
"search nothing." A set is used so duplicate ``source_path`` values collapse by
construction — selection is inherently a set, not a list.

This module intentionally imports nothing from ``main.py`` or ``hybrid.py`` so
both can depend on it without creating an import cycle.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class QueryScope:
    """An immutable, resolved scope for a single query.

    Attributes:
        selection: ``None`` for the whole archive; an empty ``frozenset`` for an
            explicit empty scope (zero results); otherwise the set of
            ``source_path`` values retrieval is restricted to.
    """

    selection: frozenset[str] | None

    @classmethod
    def whole_archive(cls) -> "QueryScope":
        """Scope covering the whole archive (no selection)."""
        return cls(selection=None)

    @classmethod
    def of(cls, sources: Iterable[str]) -> "QueryScope":
        """Scope restricted to ``sources``; duplicates collapse by construction."""
        return cls(selection=frozenset(sources))

    @property
    def is_unscoped(self) -> bool:
        """True when this scope covers the whole archive (no selection)."""
        return self.selection is None
