"""Diff engine: kinds, track_fields, long text, char stats."""

from __future__ import annotations

import pytest

from since.diff import LONG_TEXT_CHARS, Draft, diff, text_change_stats
from since.model import FieldChange, Record


def rec(key: str, **fields) -> Record:
    return Record.make(key, fields)


def snap(*records: Record) -> dict[str, Record]:
    return {r.key: r for r in records}


LONG_A = "a" * 201
LONG_B = "b" * 250


# --- kinds -------------------------------------------------------------------------------------


def test_empty_inputs_no_drafts():
    assert diff({}, [], None) == []
    assert diff(snap(rec("a", x=1)), [rec("a", x=1)], None) == []


def test_added_without_track_fields_has_no_changes():
    (d,) = diff({}, [rec("k", status="Open", n=1)], None)
    assert d == Draft("added", "k", [], {"status": "Open", "n": 1})


def test_added_with_track_fields_lists_tracked_in_config_order():
    (d,) = diff({}, [rec("k", a=1, status="Open", eta="soon")], ["status", "eta", "missing"])
    assert d.kind == "added"
    assert d.changes == [FieldChange("status", None, "Open"), FieldChange("eta", None, "soon")]
    assert d.fields == {"a": 1, "status": "Open", "eta": "soon"}  # all fields, not just tracked


def test_modified_all_fields_sorted_union_missing_is_none():
    old = snap(rec("k", b=1, c="x"))
    (d,) = diff(old, [rec("k", a=5, b=2, c="x")], None)
    assert d.kind == "modified"
    assert d.changes == [FieldChange("a", None, 5), FieldChange("b", 1, 2)]
    assert d.fields == {"a": 5, "b": 2, "c": "x"}


def test_modified_field_removed_from_record_reports_none_as_new():
    (d,) = diff(snap(rec("k", a=1, b=2)), [rec("k", a=1)], None)
    assert d.changes == [FieldChange("b", 2, None)]


def test_modified_with_track_fields_uses_config_order():
    old = snap(rec("k", status="Open", eta="1", noise=1))
    new = [rec("k", status="Closed", eta="2", noise=2)]
    (d,) = diff(old, new, ["eta", "status"])
    assert d.changes == [FieldChange("eta", "1", "2"), FieldChange("status", "Open", "Closed")]
    assert d.fields == {"status": "Closed", "eta": "2", "noise": 2}


def test_untracked_only_change_produces_no_event():
    old = snap(rec("k", status="Open", noise=1))
    assert diff(old, [rec("k", status="Open", noise=2)], ["status"]) == []


def test_tracked_field_absent_on_both_sides_is_no_change():
    old = snap(rec("k", a=1))
    assert diff(old, [rec("k", a=2)], ["status"]) == []


def test_identical_hash_skipped_even_when_fields_dict_objects_differ():
    old = snap(rec("k", a=1, b=2))
    assert diff(old, [rec("k", b=2, a=1)], None) == []


def test_removed_carries_old_fields_and_no_changes():
    old = snap(rec("k", status="Open", n=1))
    (d,) = diff(old, [], ["status"])
    assert d == Draft("removed", "k", [], {"status": "Open", "n": 1})


def test_mixed_kinds_sorted_by_key():
    old = snap(rec("d", v=1), rec("b", v=1), rec("a", v=1), rec("x", v=1))
    new = [rec("e", v=1), rec("b", v=2), rec("a", v=1), rec("c", v=1), rec("x", v=1)]
    drafts = diff(old, new, None)
    assert [(d.key, d.kind) for d in drafts] == [
        ("b", "modified"),
        ("c", "added"),
        ("d", "removed"),
        ("e", "added"),
    ]


def test_input_order_does_not_change_output():
    old = snap(rec("a", v=1), rec("b", v=1))
    new = [rec("z", v=1), rec("a", v=2), rec("m", v=1)]
    assert diff(old, new, None) == diff(old, list(reversed(new)), None)


def test_drafts_do_not_alias_record_fields():
    r = rec("k", v=1)
    (d,) = diff({}, [r], None)
    d.fields["v"] = 99
    assert r.fields["v"] == 1


@pytest.mark.parametrize(
    ("old_v", "new_v"),
    [(1, True), (1, 1.0), (0, False), ("1", 1), (None, ""), (None, 0)],
)
def test_type_changes_count_as_changes(old_v, new_v):
    (d,) = diff(snap(rec("k", v=old_v)), [rec("k", v=new_v)], ["v"])
    assert d.changes == [FieldChange("v", old_v, new_v)]


def test_nan_is_not_a_change():
    nan = float("nan")
    old = snap(rec("k", v=nan, other=1))
    assert diff(old, [rec("k", v=nan, other=2)], ["v"]) == []


def test_duplicate_track_fields_are_collapsed():
    (d,) = diff(snap(rec("k", v=1)), [rec("k", v=2)], ["v", "v"])
    assert d.changes == [FieldChange("v", 1, 2)]


# --- long text ---------------------------------------------------------------------------------


def test_threshold_is_strictly_greater_than_200():
    assert LONG_TEXT_CHARS == 200
    short = "s" * 200
    (d,) = diff(snap(rec("k", body="")), [rec("k", body=short)], None)
    assert d.changes == [FieldChange("body", "", short)]  # 200 chars: still shown
    (d,) = diff(snap(rec("k", body="")), [rec("k", body="s" * 201)], None)
    assert d.changes == [FieldChange("body", None, None, 201, 0)]  # 201: char counts only


def test_long_text_modified_never_stores_values():
    old = snap(rec("k", body=LONG_A, title="t"))
    (d,) = diff(old, [rec("k", body=LONG_B, title="t")], None)
    (c,) = d.changes
    assert (c.field, c.old, c.new) == ("body", None, None)
    assert (c.added_chars, c.removed_chars) == (250, 201)
    assert LONG_A not in repr(d.changes) and LONG_B not in repr(d.changes)
    assert d.fields["body"] == LONG_B  # the draft's fields are the full new record


def test_long_old_short_new_is_long_change():
    (d,) = diff(snap(rec("k", body=LONG_A)), [rec("k", body="Closed")], None)
    (c,) = d.changes
    assert (c.old, c.new) == (None, None)
    assert (c.added_chars, c.removed_chars) == (6, 201)


def test_short_old_long_new_is_long_change():
    (d,) = diff(snap(rec("k", body="hi")), [rec("k", body=LONG_B)], None)
    (c,) = d.changes
    assert (c.old, c.new) == (None, None)
    assert (c.added_chars, c.removed_chars) == (250, 2)


def test_long_text_replaced_by_non_str_counts_missing_side_as_empty():
    (d,) = diff(snap(rec("k", body=LONG_A)), [rec("k", body=None)], None)
    assert d.changes == [FieldChange("body", None, None, 0, 201)]


def test_added_long_text_counts_full_length_when_tracked():
    (d,) = diff({}, [rec("k", body=LONG_B, status="Open")], ["body", "status"])
    assert d.changes == [
        FieldChange("body", None, None, 250, 0),
        FieldChange("status", None, "Open"),
    ]


def test_added_long_text_not_tracked_stores_nothing():
    (d,) = diff({}, [rec("k", body=LONG_B)], None)
    assert d.changes == []


def test_long_text_in_untracked_field_change_is_ignored():
    old = snap(rec("k", body=LONG_A, status="Open"))
    assert diff(old, [rec("k", body=LONG_B, status="Open")], ["status"]) == []


def test_long_and_short_changes_together():
    old = snap(rec("k", body=LONG_A, status="Open"))
    (d,) = diff(old, [rec("k", body=LONG_B, status="Closed")], None)
    assert d.changes == [
        FieldChange("body", None, None, 250, 201),
        FieldChange("status", "Open", "Closed"),
    ]


def test_long_non_str_never_triggers_long_rule():
    (d,) = diff(snap(rec("k", n=1)), [rec("k", n=10**300)], None)
    assert d.changes == [FieldChange("n", 1, 10**300)]


# --- text_change_stats -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("", "", (0, 0)),
        ("same\ntext\n", "same\ntext\n", (0, 0)),
        (None, "hello", (5, 0)),
        ("hello", None, (0, 5)),
        (5, "hello", (5, 0)),  # non-str side counts as ""
        ("hello", 5, (0, 5)),
        ("a\nb\nc\n", "a\nb\nc\nd\ne\n", (4, 0)),  # pure insert: full lines
        ("a\nb\nc\n", "a\n", (0, 4)),  # pure delete: full lines
        ("a\nb\nc\n", "a\nB\nc\n", (1, 1)),  # replace refined at char level
        ("x" * 100 + "A", "x" * 100 + "B", (1, 1)),  # single-line replace refined
        ("hello world", "hello brave world", (6, 0)),
        ("abc", "xyz", (3, 3)),
    ],
)
def test_text_change_stats_known_inputs(old, new, expected):
    assert text_change_stats(old, new) == expected


def test_stats_replace_chunk_over_5000_counts_full_lines():
    old = "x" * 3000
    new = "y" * 2001  # combined 5001 > 5000
    assert text_change_stats(old, new) == (2001, 3000)
    # same shape but a shared tail would refine if the chunk were small enough
    old2 = "x" * 2999 + "A"
    new2 = "x" * 2999 + "B"  # combined 6000 > 5000 -> full line lengths, not (1, 1)
    assert text_change_stats(old2, new2) == (3000, 3000)


def test_stats_replace_chunk_exactly_5000_is_refined():
    old = "x" * 2499 + "A"
    new = "x" * 2499 + "B"  # combined exactly 5000 -> refined
    assert text_change_stats(old, new) == (1, 1)


def test_stats_only_changed_line_is_counted_in_long_text():
    lines = [f"line {i}\n" for i in range(50)]
    old = "".join(lines)
    changed = list(lines)
    changed[10] = "line TEN\n"
    # "line 10" -> "line TEN": +"TEN" -"10"
    assert text_change_stats(old, "".join(changed)) == (3, 2)


def test_stats_keepends_makes_newline_changes_visible():
    assert text_change_stats("a\nb", "a\nb\n") == (1, 0)
    assert text_change_stats("a\r\nb\r\n", "a\nb\n") == (0, 2)


def test_stats_are_deterministic():
    old = "one\ntwo\nthree\nfour\n" * 20
    new = "one\n2\nthree\nfour\nfive\n" * 20
    assert text_change_stats(old, new) == text_change_stats(old, new)
