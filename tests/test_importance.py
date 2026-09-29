"""Importance scoring: weights and highlight rules."""

from __future__ import annotations

import pytest

from since.config import HighlightRule
from since.importance import score
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SCHEMA_CHANGED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
    FieldChange,
)


def eq(field: str, value: str, bonus: int = 10) -> HighlightRule:
    return HighlightRule(field, "equals", value, bonus)


def has(field: str, value: str, bonus: int = 10) -> HighlightRule:
    return HighlightRule(field, "contains", value, bonus)


def to(field: str, value: str, bonus: int = 10) -> HighlightRule:
    return HighlightRule(field, "changed_to", value, bonus)


# --- weights -----------------------------------------------------------------------------------

PRIORITY_W = {"high": 3, "normal": 2, "low": 1}
KIND_W = {
    KIND_SOURCE_ERROR: 5,
    KIND_SCHEMA_CHANGED: 5,
    KIND_MODIFIED: 4,
    KIND_REMOVED: 4,
    KIND_ADDED: 3,
    KIND_BASELINE: 1,
    KIND_SOURCE_RECOVERED: 1,
}


@pytest.mark.parametrize("priority", PRIORITY_W)
@pytest.mark.parametrize("kind", KIND_W)
def test_base_score_is_priority_times_kind(priority, kind):
    assert score(priority, kind, [], {}, []) == PRIORITY_W[priority] * KIND_W[kind]


def test_spec_examples():
    assert score("high", KIND_MODIFIED, [FieldChange("s", "a", "b")], {"s": "b"}, []) == 12
    assert score("normal", KIND_ADDED, [], {}, []) == 6
    assert score("normal", KIND_BASELINE, [], {}, []) == 2
    assert score("high", KIND_SOURCE_ERROR, [], {}, []) == 15


def test_ordering_of_kinds_within_a_priority():
    s = {k: score("normal", k, [], {}, []) for k in KIND_W}
    assert s[KIND_SOURCE_ERROR] == s[KIND_SCHEMA_CHANGED] > s[KIND_MODIFIED] == s[KIND_REMOVED]
    assert s[KIND_MODIFIED] > s[KIND_ADDED] > s[KIND_BASELINE] == s[KIND_SOURCE_RECOVERED]


def test_unknown_priority_or_kind_raises():
    with pytest.raises(KeyError):
        score("urgent", KIND_ADDED, [], {}, [])
    with pytest.raises(KeyError):
        score("high", "exploded", [], {}, [])


def test_score_is_int():
    assert isinstance(score("low", KIND_ADDED, [], {}, []), int)


# --- equals ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "rule_value", "hit"),
    [
        ("Cancelled", "Cancelled", True),
        ("cancelled", "Cancelled", False),  # equals is case-sensitive
        ("Cancelled ", "Cancelled", False),
        ("Cancelled", "Cancel", False),
        (5, "5", True),  # str(value)
        (5.0, "5.0", True),
        (True, "True", True),
        ("", "", True),
        (None, "None", False),  # None never matches, not even "None"
        (None, "", False),
    ],
)
def test_equals(value, rule_value, hit):
    got = score("high", KIND_ADDED, [], {"status": value}, [eq("status", rule_value)])
    assert got == 3 * 3 + (10 if hit else 0)


def test_equals_missing_field_never_matches():
    assert score("high", KIND_ADDED, [], {"other": "x"}, [eq("status", "x")]) == 9
    assert score("high", KIND_ADDED, [], {}, [eq("status", "")]) == 9


def test_equals_applies_to_added_modified_removed():
    fields = {"status": "Cancelled"}
    rules = [eq("status", "Cancelled")]
    for kind, weight in ((KIND_ADDED, 3), (KIND_MODIFIED, 4), (KIND_REMOVED, 4)):
        assert score("normal", kind, [], fields, rules) == 2 * weight + 10


def test_equals_on_removed_uses_last_known_fields():
    assert score("low", KIND_REMOVED, [], {"status": "Open"}, [eq("status", "Open", 7)]) == 4 + 7


# --- contains ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "needle", "hit"),
    [
        ("Re: DJ ASN Rejection", "rejection", True),  # case-insensitive
        ("Re: DJ ASN rejection", "REJECTION", True),
        ("Re: DJ ASN rejection", "reject", True),
        ("Re: DJ ASN rejection", "accepted", False),
        ("Straße", "STRASSE", True),  # casefold
        (12345, "234", True),  # str(value)
        (None, "None", False),  # None never matches
        (None, "", False),
    ],
)
def test_contains(value, needle, hit):
    assert score("normal", KIND_ADDED, [], {"subject": value}, [has("subject", needle)]) == (
        6 + (10 if hit else 0)
    )


def test_contains_missing_field_never_matches():
    assert score("normal", KIND_ADDED, [], {"other": "urgent"}, [has("subject", "urgent")]) == 6


# --- changed_to --------------------------------------------------------------------------------


def test_changed_to_matches_new_value_of_that_field():
    changes = [FieldChange("status", "Open", "Cancelled")]
    fields = {"status": "Cancelled"}
    assert score("high", KIND_MODIFIED, changes, fields, [to("status", "Cancelled")]) == 12 + 10


def test_changed_to_is_exact_and_case_sensitive():
    changes = [FieldChange("status", "Open", "Cancelled")]
    fields = {"status": "Cancelled"}
    for v in ("cancelled", "Cancel", "Cancelled!"):
        assert score("high", KIND_MODIFIED, changes, fields, [to("status", v)]) == 12


def test_changed_to_requires_field_among_changes():
    # status equals the value now, but it did not change in this event
    changes = [FieldChange("eta", "1", "2")]
    fields = {"status": "Cancelled", "eta": "2"}
    assert score("high", KIND_MODIFIED, changes, fields, [to("status", "Cancelled")]) == 12


def test_changed_to_uses_change_new_not_current_fields():
    changes = [FieldChange("status", "Open", "Pending")]
    fields = {"status": "Cancelled"}  # inconsistent on purpose: the change value decides
    assert score("high", KIND_MODIFIED, changes, fields, [to("status", "Cancelled")]) == 12
    assert score("high", KIND_MODIFIED, changes, fields, [to("status", "Pending")]) == 22


def test_changed_to_compares_str_of_new_value():
    changes = [FieldChange("n", 1, 5)]
    assert score("low", KIND_MODIFIED, changes, {"n": 5}, [to("n", "5")]) == 4 + 10


def test_changed_to_none_new_value_never_matches():
    changes = [FieldChange("status", "Open", None)]
    rules = [to("status", "None"), to("status", "")]
    assert score("high", KIND_MODIFIED, changes, {}, rules) == 12


def test_changed_to_long_text_uses_new_record_field_value():
    changes = [FieldChange("body", None, None, 9, 300)]
    fields = {"body": "Cancelled"}
    assert score("high", KIND_MODIFIED, changes, fields, [to("body", "Cancelled")]) == 12 + 10
    assert score("high", KIND_MODIFIED, changes, fields, [to("body", "Open")]) == 12


def test_changed_to_long_text_new_value_none_or_missing_never_matches():
    changes = [FieldChange("body", None, None, 0, 300)]
    assert score("high", KIND_MODIFIED, changes, {"body": None}, [to("body", "None")]) == 12
    assert score("high", KIND_MODIFIED, changes, {}, [to("body", "")]) == 12


@pytest.mark.parametrize("kind", [KIND_ADDED, KIND_REMOVED])
def test_changed_to_only_applies_to_modified(kind):
    changes = [FieldChange("status", None, "Cancelled")]
    fields = {"status": "Cancelled"}
    base = 2 * (3 if kind == KIND_ADDED else 4)
    assert score("normal", kind, changes, fields, [to("status", "Cancelled")]) == base


# --- multiple rules / non-record kinds ---------------------------------------------------------


def test_multiple_matching_rules_sum_their_bonuses():
    changes = [FieldChange("status", "Open", "Cancelled")]
    fields = {"status": "Cancelled", "note": "urgent: call supplier"}
    rules = [
        to("status", "Cancelled", 10),
        eq("status", "Cancelled", 5),
        has("note", "URGENT", 3),
        has("note", "missing", 100),  # no match
    ]
    assert score("high", KIND_MODIFIED, changes, fields, rules) == 12 + 10 + 5 + 3


def test_duplicate_rules_each_count():
    rules = [eq("s", "x"), eq("s", "x")]
    assert score("low", KIND_ADDED, [], {"s": "x"}, rules) == 3 + 20


def test_custom_bonus_and_negative_bonus_are_summed_as_given():
    assert score("normal", KIND_ADDED, [], {"s": "x"}, [eq("s", "x", 1)]) == 7
    assert score("normal", KIND_ADDED, [], {"s": "x"}, [eq("s", "x", -2)]) == 4


@pytest.mark.parametrize(
    "kind", [KIND_BASELINE, KIND_SCHEMA_CHANGED, KIND_SOURCE_ERROR, KIND_SOURCE_RECOVERED]
)
def test_rules_ignored_for_non_record_kinds(kind):
    rules = [eq("status", "Cancelled"), has("status", "cancel"), to("status", "Cancelled")]
    changes = [FieldChange("status", "Open", "Cancelled")]
    fields = {"status": "Cancelled"}
    assert score("high", kind, changes, fields, rules) == 3 * KIND_W[kind]


def test_no_rules_no_bonus():
    assert score("high", KIND_MODIFIED, [FieldChange("s", 1, 2)], {"s": 2}, []) == 12
