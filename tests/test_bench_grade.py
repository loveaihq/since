"""Answer extraction and grading of the benchmark (``bench/grade.py``, M3 T4). No model calls."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

import bench.world as world_mod
from bench.grade import (
    ARM_A_LIMITS,
    UNREADABLE_KIND,
    Grade,
    arm_a_observable,
    arm_a_observable_in,
    extract_items,
    grade,
    normalise,
)
from bench.world import NOW, build_world

PLANTED = [
    ("email", "91000019"),
    ("email", "700033"),
    ("po", "4500108"),
    ("portal", "62000069"),
    ("system", "portal-layout"),
    ("system", "portal-login"),
]
OBSERVABLE = [item for item in PLANTED if item != ("portal", "62000069")]
# what arm A's tools reach: no portal order, no portal-layout
OBSERVABLE_A = [
    ("email", "91000019"),
    ("email", "700033"),
    ("po", "4500108"),
    ("system", "portal-login"),
]


def block(items: list[dict[str, object]]) -> str:
    return "```json\n" + json.dumps({"items": items}) + "\n```"


def entry(kind: str, ref: object) -> dict[str, object]:
    return {"kind": kind, "ref": ref}


# -- extract_items -------------------------------------------------------------------------------


def test_extract_plain_block() -> None:
    text = block([entry("email", "91000019"), entry("po", "4500108")])
    got = extract_items(text)
    assert got.items == [("email", "91000019"), ("po", "4500108")]
    assert (got.malformed, got.note) == (False, "")


def test_extract_ignores_prose_and_takes_the_last_json_block() -> None:
    text = (
        "Here is my thinking.\n```json\n"
        + json.dumps({"items": [entry("po", "1")]})
        + "\n```\nOn reflection:\n```text\nnot json\n```\nFinal answer:\n"
        + block([entry("email", "2"), entry("email", "3")])
        + "\n"
    )
    assert extract_items(text).items == [("email", "2"), ("email", "3")]


def test_extract_tolerates_case_crlf_untagged_fence_and_one_line_block() -> None:
    body = json.dumps({"items": [entry("po", "4500108")]})
    for text in (
        f"```JSON\n{body}\n```",
        f"```json\r\n{body}\r\n```",
        f"```\n{body}\n```",
        f"```json {body}```",
        f"Answer: ```json\n{body}```",
    ):
        got = extract_items(text)
        assert got.items == [("po", "4500108")], text
        assert not got.malformed


def test_extract_bare_json_object_when_there_is_no_fence() -> None:
    text = 'Done. {"items": [{"kind": "po", "ref": "4500108"}]} Thanks.'
    got = extract_items(text)
    assert got.items == [("po", "4500108")]
    assert not got.malformed


def test_extract_empty_items_is_a_valid_answer() -> None:
    got = extract_items(block([]))
    assert (got.items, got.malformed) == ([], False)


def test_extract_malformed_gives_no_items_and_a_flag() -> None:
    for text in (
        '```json\n{"items": [{"kind": "po", "ref": "1"},]}\n```',  # trailing comma
        '```json\n{"items": \n```',  # cut off
        '```json\n{"things": []}\n```',  # no items list
        '```json\n{"items": "none"}\n```',
        "I found nothing to report.",
        "",
    ):
        got = extract_items(text)
        assert got.items == [], text
        assert got.malformed, text
        assert got.note, text


def test_extract_is_literal_about_the_last_block() -> None:
    """A valid block followed by a broken one is a broken answer, not a lucky one."""
    text = block([entry("po", "1")]) + "\n```json\n{oops\n```"
    got = extract_items(text)
    assert got.items == [] and got.malformed


def test_extract_keeps_unreadable_entries_as_reported_items() -> None:
    text = block([entry("po", 4500108), "4500111", {"kind": "po"}, entry("po", None)])
    got = extract_items(text)
    assert got.items[0] == ("po", "4500108")  # an integer ref is read
    assert [kind for kind, _ in got.items[1:]] == [UNREADABLE_KIND] * 3
    assert not got.malformed


# -- normalise -----------------------------------------------------------------------------------


def test_normalise_numeric_refs() -> None:
    for ref in ("4500108", " 4500108 ", "PO 4500108", "po-4500108", "#4500108", "PO#4500108."):
        assert normalise("po", ref) == ("po", "4500108"), ref
    assert normalise("Email", "ASN 91000019") == ("email", "91000019")
    assert normalise("PORTAL", "Order 62000069") == ("portal", "62000069")


def test_normalise_keeps_ambiguous_refs_whole() -> None:
    assert normalise("po", "4500108 and 4500111") == ("po", "4500108 AND 4500111")
    assert normalise("po", "abc") == ("po", "ABC")


def test_normalise_system_refs() -> None:
    for ref in ("portal-login", "PORTAL-LOGIN", "portal_login", "Portal Login", " portal-login "):
        assert normalise("system", ref) == ("system", "PORTAL-LOGIN"), ref
    assert normalise("system", "portal-layout") == ("system", "PORTAL-LAYOUT")


# -- grade ---------------------------------------------------------------------------------------


def test_grade_perfect_answer() -> None:
    got = grade(PLANTED, PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert (got.recall, got.recall_observable, got.precision) == (1.0, 1.0, 1.0)
    assert got.recall_observable_a == 1.0
    assert got.fp == [] and got.fn == []
    assert sorted(got.tp) == sorted(PLANTED)  # as written in the answer key
    assert (got.reported, got.duplicates) == (6, 0)


def test_grade_partial_answer() -> None:
    items = [
        ("email", "91000019"),
        ("po", "4500108"),
        ("system", "portal-login"),
        ("po", "4500999"),  # not planted
    ]
    got = grade(items, PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert got.recall == 3 / 6
    assert got.recall_observable == 3 / 5
    assert got.recall_observable_a == 3 / 4  # found 3 of the 4 items A can reach (not 700033)
    assert got.precision == 3 / 4
    assert got.fp == [("po", "4500999")]
    assert got.fn == [("email", "700033"), ("portal", "62000069"), ("system", "portal-layout")]


def test_grade_recall_on_observable_only_counts_observable_items() -> None:
    got = grade([("portal", "62000069")], PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert got.recall == 1 / 6
    assert got.recall_observable == 0.0  # the one item found is not observable
    assert got.recall_observable_a == 0.0  # A cannot see portal orders either
    assert got.precision == 1.0


def test_grade_recall_on_arm_a_items_ignores_what_a_cannot_reach() -> None:
    # portal-layout is observable by Since but not by A: it counts for one denominator only
    got = grade([("system", "portal-layout")], PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert got.recall == 1 / 6
    assert got.recall_observable == 1 / 5
    assert got.recall_observable_a == 0.0
    every = grade(OBSERVABLE_A, PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert (every.recall_observable_a, every.recall_observable) == (1.0, 4 / 5)


def test_grade_counts_duplicates_once() -> None:
    items = [("po", "4500108"), ("po", "PO 4500108"), ("PO", "#4500108"), ("po", "4500999")]
    got = grade(items, PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert got.tp == [("po", "4500108")]
    assert got.fp == [("po", "4500999")]
    assert (got.reported, got.duplicates) == (2, 2)
    assert got.precision == 1 / 2


def test_grade_unknown_kinds_are_false_positives() -> None:
    items = [("purchase_order", "4500108"), ("invoice", "700033"), (UNREADABLE_KIND, "x")]
    got = grade(items, PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert got.tp == []
    assert len(got.fp) == 3 and {kind for kind, _ in got.fp} == {
        "purchase_order",
        "invoice",
        UNREADABLE_KIND,
    }
    assert (got.recall, got.precision) == (0.0, 0.0)


def test_grade_the_right_number_with_the_wrong_kind_is_wrong() -> None:
    got = grade([("email", "4500108")], PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert got.tp == [] and got.fp == [("email", "4500108")]
    assert ("po", "4500108") in got.fn


def test_grade_nothing_reported() -> None:
    got = grade([], PLANTED, OBSERVABLE, OBSERVABLE_A)
    assert (got.recall, got.recall_observable, got.precision) == (0.0, 0.0, 0.0)
    assert got.recall_observable_a == 0.0
    assert len(got.fn) == len(PLANTED) and got.reported == 0


def test_grade_normalises_the_answer_key_too() -> None:
    got = grade(
        [("system", "portal_login"), ("email", "PO 91000019")], PLANTED, OBSERVABLE, OBSERVABLE_A
    )
    assert got.tp == [("email", "91000019"), ("system", "portal-login")]


def test_grade_observable_outside_the_key_is_ignored() -> None:
    got = grade(PLANTED, PLANTED, [("po", "0000001")], [("po", "0000001")])
    assert got.recall_observable == 1.0  # nothing observable to find: vacuously complete
    assert got.recall_observable_a == 1.0


def test_grade_to_dict_is_json_serialisable() -> None:
    got = grade([("po", "4500108"), ("po", "9")], PLANTED, OBSERVABLE, OBSERVABLE_A)
    data = json.loads(json.dumps(got.to_dict()))
    assert data["tp"] == [["po", "4500108"]] and data["fp"] == [["po", "9"]]
    assert data["recall_observable_a"] == 1 / 4
    assert isinstance(got, Grade)


def test_grading_the_answer_key_of_the_real_world_is_perfect() -> None:
    world = build_world()
    text = block([entry(kind, ref) for kind, ref in world.planted])
    extraction = extract_items(text)
    got = grade(extraction.items, world.planted, world.planted, arm_a_observable_in(world))
    assert (got.recall, got.precision) == (1.0, 1.0)
    assert len(got.tp) == len(world.planted)


# -- what arm A's tools can reach ----------------------------------------------------------------


def test_arm_a_cannot_see_portal_orders_or_the_layout_change_behind_the_login() -> None:
    got = arm_a_observable(PLANTED, portal_blind=True)
    assert got == OBSERVABLE_A  # order of the answer key kept
    assert ("portal", "62000069") not in got and ("system", "portal-layout") not in got
    assert ("system", "portal-login") in got  # the login page itself is what A sees
    assert arm_a_observable(PLANTED) == got  # blind is the default: the world's portal is


def test_arm_a_sees_everything_when_the_portal_can_be_read() -> None:
    assert arm_a_observable(PLANTED, portal_blind=False) == PLANTED
    assert arm_a_observable([], portal_blind=True) == []


def test_arm_a_observable_of_the_real_world_is_computed_from_its_answer_key() -> None:
    world = build_world()
    reachable = arm_a_observable_in(world)
    planted = list(world.planted)
    missing = [item for item in planted if item not in reachable]
    assert set(reachable) <= set(planted)
    # exactly the portal orders and portal-layout are out of reach
    assert {kind for kind, _ in missing} == {"portal", "system"}
    assert [ref for kind, ref in missing if kind == "system"] == ["portal-layout"]
    assert all(kind in ("email", "po") or ref == "portal-login" for kind, ref in reachable)
    portal_items = sum(1 for kind, _ in planted if kind == "portal")
    assert len(reachable) == len(planted) - portal_items - 1
    assert (len(reachable), len(planted)) == (13, 18)  # the ceiling the report states
    assert "portal order change" in ARM_A_LIMITS and "portal-layout" in ARM_A_LIMITS


def test_arm_a_observable_follows_the_world_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the login had not expired by now, the page A fetches would be readable: A could then see
    the portal orders and the new layout, so nothing is out of its reach."""
    monkeypatch.setattr(world_mod, "LOGIN_EXPIRY_AT", NOW + timedelta(days=1))
    world: Any = build_world()
    assert not world.state_at(NOW).portal.login_expired
    assert ("system", "portal-login") not in world.planted
    assert arm_a_observable_in(world) == list(world.planted)
