"""Tests for enrichment.nicknames — the nickname equivalence table used by
the wrong-person audit to suppress Matt/Matthew-type false positives."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pytest

from enrichment.nicknames import are_nickname_equivalent, canonical_first_names


# ─────────────────────────── basic round-trips ───────────────────────────


@pytest.mark.parametrize(
    "nickname, canonical",
    [
        ("Matt", "Matthew"),
        ("matt", "matthew"),
        ("MATT", "MATTHEW"),
        ("Rob", "Robert"),
        ("Bob", "Robert"),
        ("Bobby", "Robert"),
        ("Mike", "Michael"),
        ("Chris", "Christopher"),
        ("Dave", "David"),
        ("Dan", "Daniel"),
        ("Jim", "James"),
        ("Joe", "Joseph"),
        ("Tom", "Thomas"),
        ("Tim", "Timothy"),
        ("Will", "William"),
        ("Bill", "William"),
        ("Steve", "Steven"),
        ("Steve", "Stephen"),
        ("Mark", "Marcus"),
        ("Kate", "Katherine"),
        ("Cathy", "Catherine"),
        ("Jen", "Jennifer"),
        ("Beth", "Elizabeth"),
        ("Liz", "Elizabeth"),
        ("Sue", "Susan"),
        ("Pat", "Patricia"),
        ("Maggie", "Margaret"),
        ("Peggy", "Margaret"),
        ("Mo", "Mohammed"),
        ("Mohammad", "Muhammad"),
        ("Abhi", "Abhishek"),
        ("Raj", "Rajiv"),
        ("Sam", "Samuel"),
        ("Sam", "Samantha"),
        ("Alex", "Alexander"),
        ("Alex", "Alexandra"),
        ("Nick", "Nicholas"),
        ("Tony", "Anthony"),
        ("Ed", "Edward"),
        ("Eddie", "Edward"),
        ("Ted", "Theodore"),
        ("Ted", "Edward"),
        ("Rick", "Richard"),
        ("Dick", "Richard"),
    ],
)
def test_nickname_is_equivalent(nickname, canonical):
    assert are_nickname_equivalent(nickname, canonical), (
        f"{nickname!r} should be equivalent to {canonical!r}"
    )
    assert are_nickname_equivalent(canonical, nickname), "must be symmetric"


def test_canonical_contains_self():
    assert "matt" in canonical_first_names("Matt")
    assert "matthew" in canonical_first_names("Matt")
    assert "matthew" in canonical_first_names("Matthew")


def test_unknown_name_returns_singleton():
    result = canonical_first_names("Obadiah")
    assert result == {"obadiah"}


def test_empty_returns_empty_set():
    assert canonical_first_names("") == set()
    assert canonical_first_names("   ") == set()


# ─────────────────── non-equivalences (must NOT match) ───────────────────


@pytest.mark.parametrize(
    "a, b",
    [
        ("Matt", "Mark"),           # different canonical roots
        ("John", "Joe"),            # easily confused but not equivalent
        ("Paul", "Peter"),
        ("Dan", "Don"),
        ("David", "Daniel"),
        ("Mike", "Matt"),
        ("Sarah", "Susan"),
        ("Karen", "Kate"),
        ("Obadiah", "Nathaniel"),   # both unknown
    ],
)
def test_non_equivalent_names(a, b):
    assert not are_nickname_equivalent(a, b), (
        f"{a!r} and {b!r} should NOT be nickname-equivalent"
    )


# ──────────────────── normalization / edge cases ─────────────────────────


def test_case_insensitive():
    assert are_nickname_equivalent("matt", "MATTHEW")
    assert are_nickname_equivalent("KATE", "katherine")


def test_whitespace_trimmed():
    assert are_nickname_equivalent("  matt  ", "matthew")


def test_accent_stripped():
    # "José" is not in the table, but accents shouldn't break normalization.
    # It just round-trips to itself (singleton).
    assert "jose" in canonical_first_names("José")


def test_symmetry_for_all_groups():
    """Any two members of the same canonical group must be equivalent to
    each other, in both directions."""
    samples = [
        ("matt", "matthew"),
        ("matthew", "matt"),
        ("bob", "robert"),
        ("robert", "bob"),
        ("kate", "katherine"),
        ("katherine", "kate"),
        ("catherine", "kate"),
        ("mo", "mohammad"),
    ]
    for a, b in samples:
        assert are_nickname_equivalent(a, b)
        assert are_nickname_equivalent(b, a)
