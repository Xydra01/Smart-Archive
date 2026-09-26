"""Tests for the bibliography ordering comparator (Feature: citation-formatting).

Covers task 6.2. Property tests use Hypothesis at >=100 examples and are tagged
with the property they validate. The comparator is a pure function.
"""

from __future__ import annotations

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.references.ordering import order_bibliography, sort_key

# ---------------------------------------------------------------------------
# Strategies: items are (leading_element | None, text) pairs.
# ---------------------------------------------------------------------------
leading = st.one_of(st.none(), st.text(max_size=12))
text = st.text(max_size=16)
items_st = st.lists(st.tuples(leading, text), max_size=12)


def _key(item):
    return sort_key(item[0], item[1])


# ---------------------------------------------------------------------------
# Task 6.2 / Property 17 — total order with the documented key
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=200)
@given(items=items_st)
def test_ordering_is_a_total_order(items):
    """Feature: citation-formatting, Property 17: Bibliography ordering is a
    total order with the documented key."""
    ordered = order_bibliography(items, key=lambda x: (x[0], x[1]))

    # Sorting is a permutation of the input (same multiset of items). Compare
    # via the total sort_key since raw items mix None and str and are not
    # directly orderable.
    assert sorted(ordered, key=_key) == sorted(items, key=_key)

    # Idempotent: sorting twice equals sorting once.
    twice = order_bibliography(ordered, key=lambda x: (x[0], x[1]))
    assert twice == ordered

    # Non-decreasing under the key.
    keys = [_key(i) for i in ordered]
    assert keys == sorted(keys)

    # No-leading entries all come after entries that have a leading element.
    def _has_leading(item):
        le = item[0]
        return le is not None and le.strip() != ""

    flags = [0 if _has_leading(i) else 1 for i in ordered]
    assert flags == sorted(flags)  # all 0s then all 1s


@hyp_settings(max_examples=200)
@given(a=st.tuples(leading, text), b=st.tuples(leading, text))
def test_key_is_a_consistent_total_preorder(a, b):
    """Feature: citation-formatting, Property 17: for any pair, exactly one of
    key(a) < key(b), > , or == holds (total, comparable)."""
    ka, kb = _key(a), _key(b)
    lt = ka < kb
    gt = ka > kb
    eq = ka == kb
    # Exactly one of the three relations holds.
    assert (lt + gt + eq) == 1


@hyp_settings(max_examples=100)
@given(
    word=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
    body=text,
)
def test_primary_key_is_case_insensitive(word, body):
    """Feature: citation-formatting, Property 17: the primary key is
    case-insensitive on the leading element."""
    lower = sort_key(word, body)
    upper = sort_key(word.upper(), body)
    # Same primary key (flag + folded leading) regardless of case.
    assert lower[0] == upper[0]
    assert lower[1] == upper[1]


def test_diacritic_insensitive_shares_primary_key():
    """Feature: citation-formatting, Property 17: with diacritic_insensitive,
    accented and unaccented leading elements share the primary key."""
    accented = sort_key("Éluard", "text", diacritic_insensitive=True)
    plain = sort_key("Eluard", "text", diacritic_insensitive=True)
    assert accented[1] == plain[1]

    # Without the flag they differ (control).
    accented_cs = sort_key("Éluard", "text")
    plain_cs = sort_key("Eluard", "text")
    assert accented_cs[1] != plain_cs[1]


def test_no_leading_entries_sort_last():
    """Feature: citation-formatting, Property 17: entries with no leading element
    sort after every entry that has one."""
    items = [
        (None, "zzz no leading"),
        ("Beta", "b text"),
        ("", "blank leading counts as none"),
        ("Alpha", "a text"),
    ]
    ordered = order_bibliography(items, key=lambda x: (x[0], x[1]))
    # The two with leading elements come first, sorted; the None/blank come last.
    assert ordered[0] == ("Alpha", "a text")
    assert ordered[1] == ("Beta", "b text")
    assert set(ordered[2:]) == {
        (None, "zzz no leading"),
        ("", "blank leading counts as none"),
    }
