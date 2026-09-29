"""Tests for model types, weight tables, time helpers and cli skeleton."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from since import cli, model
from since.timeutil import fmt_age, fmt_minute, from_iso, parse_schedule, to_iso

# --- Record ----------------------------------------------------------------------------------


def test_hash_is_stable_across_dict_key_order():
    a = model.Record.make("k", {"status": "Open", "eta": "2026-10-01", "n": 3})
    b = model.Record.make("k", {"n": 3, "eta": "2026-10-01", "status": "Open"})
    assert a.content_hash == b.content_hash
    assert a == b


def test_hash_changes_with_content_and_is_sha256_hex():
    a = model.Record.make("k", {"status": "Open"})
    b = model.Record.make("k", {"status": "Closed"})
    assert a.content_hash != b.content_hash
    assert len(a.content_hash) == 64
    int(a.content_hash, 16)


def test_hash_distinguishes_value_types():
    assert (
        model.Record.make("k", {"n": 1}).content_hash
        != model.Record.make("k", {"n": "1"}).content_hash
    )


def test_hash_is_canonical_json_sha256():
    import hashlib

    fields = {"b": "é", "a": None}
    expected = hashlib.sha256('{"a":null,"b":"é"}'.encode()).hexdigest()
    assert model.Record.make("k", fields).content_hash == expected


def test_record_make_keeps_key_and_fields():
    r = model.Record.make("notes/todo.md", {"size": 10})
    assert (r.key, r.fields) == ("notes/todo.md", {"size": 10})


# --- FieldChange -----------------------------------------------------------------------------


def test_field_change_round_trip_plain():
    fc = model.FieldChange("status", "Open", "Cancelled")
    d = fc.to_dict()
    assert d == {"field": "status", "old": "Open", "new": "Cancelled"}
    assert model.FieldChange.from_dict(json.loads(json.dumps(d))) == fc


def test_field_change_round_trip_long_text_includes_char_counts():
    fc = model.FieldChange("text", None, None, added_chars=12, removed_chars=3)
    d = fc.to_dict()
    assert d == {"field": "text", "old": None, "new": None, "added_chars": 12, "removed_chars": 3}
    assert model.FieldChange.from_dict(json.loads(json.dumps(d))) == fc


def test_field_change_omits_none_char_counts():
    d = model.FieldChange("n", 1, 2).to_dict()
    assert "added_chars" not in d
    assert "removed_chars" not in d
    d = model.FieldChange("n", 1, 2, added_chars=0).to_dict()
    assert d["added_chars"] == 0  # zero is a real count, not omitted
    assert "removed_chars" not in d


def test_field_change_from_dict_tolerates_missing_old_new():
    fc = model.FieldChange.from_dict({"field": "x"})
    assert (fc.old, fc.new, fc.added_chars, fc.removed_chars) == (None, None, None, None)


# --- Event / tables --------------------------------------------------------------------------


def test_event_holds_fields():
    ev = model.Event(
        seq=7,
        source_id="po-table",
        kind=model.KIND_MODIFIED,
        record_key="4500123",
        field_changes=[model.FieldChange("status", "Open", "Cancelled")],
        importance=22,
        detail={},
        created_at="2026-09-29T09:12:05Z",
    )
    assert ev.seq == 7
    assert ev.kind == "modified"
    assert ev.field_changes[0].new == "Cancelled"


def test_priority_weights():
    assert model.PRIORITY_WEIGHT == {"high": 3, "normal": 2, "low": 1}
    assert set(model.PRIORITIES) == set(model.PRIORITY_WEIGHT)
    assert model.PRIORITIES == ("high", "normal", "low")


def test_kind_weights():
    assert model.KIND_WEIGHT == {
        "source_error": 5,
        "schema_changed": 5,
        "modified": 4,
        "removed": 4,
        "added": 3,
        "baseline": 1,
        "source_recovered": 1,
    }
    assert set(model.KINDS) == set(model.KIND_WEIGHT)
    assert model.HIGHLIGHT_BONUS_DEFAULT == 10


def test_id_patterns():
    assert model.SOURCE_ID_RE.match("po-table")
    assert model.SOURCE_ID_RE.match("a")
    assert model.SOURCE_ID_RE.match("a" * 64)
    assert not model.SOURCE_ID_RE.match("a" * 65)
    for bad in ("", "-a", "_a", "PO", "a b", "a/b", "a.b"):
        assert not model.SOURCE_ID_RE.match(bad), bad
    assert model.AGENT_ID_RE.match("Claude.Code_1-x")
    assert not model.AGENT_ID_RE.match("")
    assert not model.AGENT_ID_RE.match("a b")
    assert not model.AGENT_ID_RE.match("x" * 65)


# --- timeutil --------------------------------------------------------------------------------


def test_to_iso_and_from_iso_round_trip():
    dt = datetime(2026, 9, 29, 9, 12, 5, tzinfo=UTC)
    assert to_iso(dt) == "2026-09-29T09:12:05Z"
    assert from_iso("2026-09-29T09:12:05Z") == dt
    assert from_iso(to_iso(dt)).tzinfo is not None


def test_to_iso_converts_offsets_to_utc_and_drops_micros():
    dt = datetime(2026, 9, 29, 17, 12, 5, 999999, tzinfo=timezone(timedelta(hours=8)))
    assert to_iso(dt) == "2026-09-29T09:12:05Z"


def test_from_iso_accepts_offsets_and_minute_form():
    assert from_iso("2026-09-29T17:12:05+08:00") == datetime(2026, 9, 29, 9, 12, 5, tzinfo=UTC)
    assert from_iso("2026-09-29T09:12Z") == datetime(2026, 9, 29, 9, 12, tzinfo=UTC)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 9, 29, 9, 12))
    with pytest.raises(ValueError):
        fmt_minute(datetime(2026, 9, 29, 9, 12))
    with pytest.raises(ValueError):
        from_iso("2026-09-29T09:12:05")


def test_fmt_minute():
    dt = datetime(2026, 9, 29, 9, 12, 59, tzinfo=UTC)
    assert fmt_minute(dt) == "2026-09-29T09:12Z"
    east = datetime(2026, 9, 29, 1, 12, tzinfo=timezone(timedelta(hours=8)))
    assert fmt_minute(east) == "2026-09-28T17:12Z"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (12, "12s"),
        (12.9, "12s"),
        (59, "59s"),
        (60, "1m"),
        (47 * 60, "47m"),
        (3599, "59m"),
        (3600, "1h"),
        (47 * 3600 + 3599, "47h"),
        (48 * 3600 - 1, "47h"),
        (48 * 3600, "2d"),
        (5 * 86400 + 100, "5d"),
        (-5, "0s"),
    ],
)
def test_fmt_age(seconds, expected):
    assert fmt_age(seconds) == expected


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("every 15m", 900),
        ("every 10s", 10),
        ("every 2h", 7200),
        ("every 1d", 86400),
        ("every 90s", 90),
        ("  Every 5m ", 300),
        ("every 15 m", 900),
    ],
)
def test_parse_schedule_ok(text, seconds):
    assert parse_schedule(text) == seconds


@pytest.mark.parametrize(
    "text",
    ["every 9s", "every 0m", "15m", "every m", "every 15", "every 15w", "every -5m", "", None],
)
def test_parse_schedule_rejects(text):
    with pytest.raises(ValueError):
        parse_schedule(text)


# --- cli skeleton ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["daemon"],
        ["daemon", "--once"],
        ["mcp"],
        ["collect", "po-table"],
        ["digest"],
        ["digest", "--agent", "a", "--budget", "300", "--source", "docs"],
        ["get", "since://evt/1", "--budget", "500", "--agent", "a"],
        ["ack", "5", "--agent", "a"],
        ["status"],
    ],
)
def test_cli_subcommands_are_stubs(argv, capsys):
    assert cli.main(argv) == 2
    assert "not implemented" in capsys.readouterr().err


def test_cli_requires_a_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_cli_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("daemon", "mcp", "collect", "digest", "get", "ack", "status"):
        assert name in out
