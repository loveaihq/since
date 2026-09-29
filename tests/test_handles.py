"""Handle parsing: strictness and round-trips with the formatters in ``since.render``."""

from __future__ import annotations

import pytest

from since.handles import (
    MAX_SEQ,
    BatchHandle,
    EvtHandle,
    HandleError,
    RecHandle,
    format_handle,
    parse,
)
from since.render import batch_handle, evt_handle, rec_handle

# --- valid handles -------------------------------------------------------------------------------


def test_parse_evt() -> None:
    assert parse("since://evt/42") == EvtHandle(42)
    assert parse(evt_handle(1)) == EvtHandle(1)
    assert parse(f"since://evt/{MAX_SEQ}") == EvtHandle(MAX_SEQ)


def test_parse_rec_plain_and_decoded() -> None:
    assert parse("since://rec/po-table/4500123") == RecHandle("po-table", "4500123")
    assert parse("since://rec/docs/reports/q3%20final|v2.csv") == RecHandle(
        "docs", "reports/q3 final|v2.csv"
    )
    assert parse("since://rec/docs/%E2%82%AC.txt") == RecHandle("docs", "€.txt")


def test_parse_batch_variants() -> None:
    assert parse("since://batch/41-45") == BatchHandle(41, 45)
    assert parse("since://batch/41-45?source=docs") == BatchHandle(41, 45, "docs")
    assert parse("since://batch/41-45?source=docs&after=43") == BatchHandle(41, 45, "docs", 43)
    assert parse("since://batch/7-7") == BatchHandle(7, 7)
    # Parameter order is free; ``after`` without ``source`` is fine.
    assert parse("since://batch/41-45?after=43&source=docs") == BatchHandle(41, 45, "docs", 43)
    assert parse("since://batch/41-45?after=43") == BatchHandle(41, 45, None, 43)


# --- round trips with the render functions -------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "4500123",
        "reports/q3.csv",
        "a b",
        "a|b",
        "reports/q3 final|v2.csv",
        "100%",
        "50%25",
        "what?#&=+",
        "<msg-1@mail.example>",
        "日本語/ファイル.txt",
        "€",
        "tab\tnew\nline",
        'quote"back\\slash',
        "trailing/",
        "/leading",
    ],
)
def test_rec_round_trip(key: str) -> None:
    handle = rec_handle("docs", key)
    assert parse(handle) == RecHandle("docs", key)
    assert format_handle(RecHandle("docs", key)) == handle


@pytest.mark.parametrize(
    "handle",
    [
        BatchHandle(1, 1),
        BatchHandle(41, 45, "docs"),
        BatchHandle(41, 45, "docs", 43),
        BatchHandle(41, 45, None, 43),
        BatchHandle(10, 99999, "inbox-2", 1234),
    ],
)
def test_batch_round_trip(handle: BatchHandle) -> None:
    text = batch_handle(handle.lo, handle.hi, handle.source, handle.after)
    assert parse(text) == handle
    assert format_handle(handle) == text


def test_evt_round_trip() -> None:
    assert format_handle(EvtHandle(42)) == "since://evt/42"
    assert parse(format_handle(EvtHandle(42))) == EvtHandle(42)


# --- rejected handles ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "42",
        "since://",
        "since://evt",
        "since://evt/",
        "since://evt/0",
        "since://evt/042",
        "since://evt/-1",
        "since://evt/+1",
        "since://evt/1.5",
        "since://evt/abc",
        "since://evt/42 ",
        " since://evt/42",
        "since://evt/42\n",
        "since://evt/42/",
        "since://evt/42?x=1",
        "since://evt/٤٢",  # non-ASCII digits
        f"since://evt/{MAX_SEQ + 1}",
        "since://evt/" + "9" * 5000,
        "SINCE://evt/42",
        "since:/evt/42",
        "since://event/42",
        "http://evt/42",
        "since://unknown/1",
    ],
)
def test_rejects_bad_evt_and_scheme(text: str) -> None:
    with pytest.raises(HandleError):
        parse(text)


@pytest.mark.parametrize(
    "text",
    [
        "since://rec/",
        "since://rec/po-table",
        "since://rec/po-table/",  # empty key
        "since://rec//key",  # empty source id
        "since://rec/Po-Table/key",  # uppercase source id
        "since://rec/-bad/key",
        "since://rec/has space/key",
        "since://rec/" + "a" * 65 + "/key",
        "since://rec/docs/%ff",  # invalid UTF-8
        "since://rec/docs/%E2%82",  # truncated UTF-8 sequence
        "since://rec/src\n/key",
    ],
)
def test_rejects_bad_rec(text: str) -> None:
    with pytest.raises(HandleError):
        parse(text)


@pytest.mark.parametrize(
    "text",
    [
        "since://batch/",
        "since://batch/41",
        "since://batch/41-",
        "since://batch/-45",
        "since://batch/45-41",  # lo > hi
        "since://batch/0-5",
        "since://batch/1-0",
        "since://batch/01-5",
        "since://batch/a-b",
        "since://batch/1-2-3",
        "since://batch/1-2?",
        "since://batch/1-2?source=",
        "since://batch/1-2?source",
        "since://batch/1-2?source=Docs",  # invalid source id
        "since://batch/1-2?source=a/b",
        "since://batch/1-2?source=docs&source=inbox",  # duplicate
        "since://batch/1-2?after=1&after=2",
        "since://batch/1-2?after=0",
        "since://batch/1-2?after=-1",
        "since://batch/1-2?after=x",
        "since://batch/1-2?foo=bar",  # unknown parameter
        "since://batch/1-2?source=docs&foo=bar",
        "since://batch/1-2?source=docs&",
        "since://batch/1-2?source=docs&&after=1",
        "since://batch/1-2#frag",
        "since://batch/1-2?source=docs\n",
        f"since://batch/1-{MAX_SEQ + 1}",
        f"since://batch/1-2?after={MAX_SEQ + 1}",
    ],
)
def test_rejects_bad_batch(text: str) -> None:
    with pytest.raises(HandleError):
        parse(text)


def test_rejects_non_string() -> None:
    for value in (None, 42, b"since://evt/1", ["since://evt/1"]):
        with pytest.raises(HandleError):
            parse(value)  # type: ignore[arg-type]


def test_handle_error_is_a_value_error() -> None:
    assert issubclass(HandleError, ValueError)
