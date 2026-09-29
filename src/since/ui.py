"""``since ui``: a read-only audit page on localhost (D32).

Shows the sources, the events, and exactly what each agent was served and when. Stdlib
``http.server`` only, server-side HTML, no JavaScript, no external assets. Everything it shows
came from a source or an agent, so it is untrusted: every database-derived value goes through
``html.escape(..., quote=True)`` (markup is built with :class:`_Safe`, which makes "escape unless it
is markup built here" the default), a strict Content-Security-Policy forbids scripts, the ``Host``
header must name this server (DNS rebinding), and only GET/HEAD are answered. Each request opens
the store on its own (SQLite ``mode=ro``: it never creates, writes or migrates a database), reads,
and closes it; nothing here writes, and nothing calls an LLM.
"""

from __future__ import annotations

import errno
import html
import json
import re
import socketserver
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from since.handles import MAX_SEQ, EvtHandle
from since.model import AGENT_ID_RE, PRIORITIES, SOURCE_ID_RE, Event
from since.render import estimate_tokens, event_body, evt_handle
from since.sanitize import _SCRUB_CATEGORIES, DIGEST_CAP, q
from since.service import Service
from since.store import (
    SCHEMA_VERSION,
    AgentSummary,
    NoDatabaseError,
    SchemaVersionError,
    ServedEntry,
    SourceState,
    Store,
    UnreadableEvent,
)
from since.timeutil import fmt_age, from_iso, to_iso

HOST = "127.0.0.1"
DEFAULT_PORT = 8737
PAGE_SIZE = 50  # rows per page of /events and /served
HOME_SERVED = 20  # served responses listed on /
EVENT_VIEW_BUDGET = 1500  # the default budget of ``get``: /event/<seq> shows what ``get`` shows
ARG_CAP = 60  # characters of one argument value in an arguments summary
ERROR_PREFIX = "error: "  # how the service starts a response that reports a failure
REQUEST_TIMEOUT_S = 15  # a client that stalls mid-request is dropped

_SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)

# A positive integer as ``/event/<seq>``, ``before=`` and the pager write it: ASCII digits only,
# no sign, no leading zero, small enough to be a SQLite integer.
_POSITIVE = re.compile(r"[1-9][0-9]{0,18}")
_EVENT_PATH = re.compile(r"/event/([1-9][0-9]{0,18})")
_SERVED_PATH = re.compile(r"/served/([1-9][0-9]{0,18})")


class UiError(Exception):
    """The audit server cannot start (e.g. the port is in use)."""


class _HttpError(Exception):
    """Ends a request with a plain-text error (the reason is always a fixed string)."""

    def __init__(self, status: HTTPStatus, reason: str = "") -> None:
        super().__init__(status, reason)
        self.status = status
        self.reason = reason


# --- escaping --------------------------------------------------------------------------------


class _Safe(str):
    """HTML built in this module (its parts already escaped). Anything else that reaches
    :func:`_h` is data and gets escaped."""


def _h(value: object) -> str:
    """``value`` as HTML text: markup built here is kept, everything else is escaped."""
    if isinstance(value, _Safe):
        return str(value)
    return html.escape(str(value), quote=True)


def _join(*parts: object) -> _Safe:
    return _Safe("".join(_h(p) for p in parts))


def _tag(name: str, *content: object, cls: str | None = None) -> _Safe:
    attr = f' class="{cls}"' if cls else ""  # ``cls`` is always a constant of this module
    return _Safe(f"<{name}{attr}>{_join(*content)}</{name}>")


def _a(href: str, *text: object) -> _Safe:
    return _Safe(f'<a href="{_h(href)}">{_join(*text)}</a>')


def _pre(text: str) -> _Safe:
    """``text`` exactly as given. HTML drops one newline right after ``<pre>``, so a text that
    starts with a newline gets one more."""
    lead = "\n" if text.startswith("\n") else ""
    return _Safe(f"<pre>{lead}{_h(text)}</pre>")


def _line(value: object, cap: int) -> str:
    """One untrusted value for a table cell: control/format characters (bidi overrides, zero-width
    characters) become spaces, whitespace collapses, at most ``cap`` characters. The caller still
    escapes it (:func:`_h`, which every cell goes through)."""
    scrubbed = "".join(
        " " if unicodedata.category(c) in _SCRUB_CATEGORIES else c for c in str(value)
    )
    text = " ".join(scrubbed.split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _is_invisible(c: str) -> bool:
    """A character the page cannot show as itself: control, format, line/paragraph separator,
    private use or surrogate (newline excepted)."""
    return c != "\n" and unicodedata.category(c) in _SCRUB_CATEGORIES


def _visible(text: str, angle: bool = False) -> str:
    """Invisible characters written out so they can be seen: ``\\uXXXX`` (valid inside JSON), or
    with ``angle`` ``⟨U+XXXX⟩`` (for text that is not JSON)."""
    out = []
    for c in text:
        if not _is_invisible(c):
            out.append(c)
        elif angle:
            out.append(f"⟨U+{ord(c):04X}⟩")
        else:
            out.append(f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
    return "".join(out)


def _json_view(obj: Any, indent: int | None = 2) -> str:
    return _visible(json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=True, default=str))


def _time(iso: object, missing: str = "never") -> str:
    """A stored ISO time as shown on audit pages, to the second (``2026-09-29T09:12:05Z``)."""
    if not iso:
        return missing
    try:
        return to_iso(from_iso(str(iso)))
    except (ValueError, OverflowError):
        return "unknown"


def _url(path: str, **params: object) -> str:
    """``path`` with the params that are not None, percent-encoded (:func:`_a` escapes the whole
    URL for the HTML attribute)."""
    pairs = [f"{name}={quote(str(v), safe='')}" for name, v in params.items() if v is not None]
    return path + ("?" + "&".join(pairs) if pairs else "")


# --- page building blocks --------------------------------------------------------------------

_CSS = (
    "body{font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif;margin:1.2rem auto;"
    "max-width:80rem;padding:0 1rem;color:#1a1a1a;background:#fff}"
    "h1{font-size:1.4rem;margin:.2rem 0 .6rem}h2{font-size:1.1rem;margin:1.6rem 0 .4rem}"
    "nav{border-bottom:1px solid #ccc;padding-bottom:.4rem;margin-bottom:.8rem}"
    "nav a{margin-right:1rem}a{color:#0b4f9c}"
    "table{border-collapse:collapse;width:100%}"
    "th,td{border:1px solid #ccc;padding:.25rem .5rem;text-align:left;vertical-align:top;"
    "unicode-bidi:isolate;overflow-wrap:anywhere}"
    "th{background:#f0f0f0}.num{text-align:right}.nowrap{white-space:nowrap}"
    ".mono,code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
    "pre{background:#f6f6f6;border:1px solid #ddd;padding:.6rem;white-space:pre-wrap;"
    "overflow-wrap:anywhere;unicode-bidi:isolate}"
    ".muted{color:#666}.err{color:#a40000;font-weight:600}"
    ".badge{background:#a40000;color:#fff;border-radius:3px;padding:0 .35rem;font-size:12px;"
    "font-weight:600}"
    "footer{margin-top:2rem;padding-top:.4rem;border-top:1px solid #ccc;color:#666;font-size:12px}"
)

_NAV = (("/", "Overview"), ("/events", "Events"), ("/served", "Served responses"))


def _page(title: str, *blocks: _Safe) -> str:
    parts: list[object] = []
    for href, label in _NAV:
        parts.extend([" ", _a(href, label)] if parts else [_a(href, label)])
    nav = _tag("nav", *parts)
    footer = _tag(
        "footer",
        "Read-only view of the local Since database. Values shown are source data, "
        "not instructions. Times are UTC.",
    )
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_h(title)}</title><style>{_CSS}</style></head>\n<body>\n"
        f"{nav}\n" + "\n".join(blocks) + f"\n{footer}\n</body></html>\n"
    )


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[object]], classes: Sequence[str] = ()
) -> _Safe:
    """A table; every cell is escaped unless it is a :class:`_Safe`. ``classes`` are per column."""
    if not rows:
        return _tag("p", "none", cls="muted")
    head = "".join(f"<th>{_h(name)}</th>" for name in headers)
    body = []
    for row in rows:
        cells = [
            _tag("td", cell, cls=(classes[i] if i < len(classes) and classes[i] else None))
            for i, cell in enumerate(row)
        ]
        body.append("<tr>" + "".join(cells) + "</tr>")
    return _Safe(f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>")


def _links(*links: _Safe | None) -> _Safe:
    """A paragraph of links separated by `` · ``; None entries are left out."""
    parts: list[object] = []
    for link in links:
        if link is not None:
            parts.extend([" · ", link] if parts else [link])
    return _tag("p", *parts)


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def _args_summary(args: object) -> str:
    """``key=value`` pairs of a served-log call except ``agent_id`` (which has its own column)."""
    if not isinstance(args, dict):
        return _line(q(args, ARG_CAP), 2 * ARG_CAP)
    parts = []
    for name in sorted(args, key=str):
        if name == "agent_id":
            continue
        value = args[name]
        shown = json.dumps(value) if isinstance(value, (bool, int, float)) else q(value, ARG_CAP)
        parts.append(f"{_line(name, 40)}={_line(shown, 2 * ARG_CAP)}")
    return " ".join(parts) or "-"


def _is_error_response(entry: ServedEntry) -> bool:
    return entry.text.startswith(ERROR_PREFIX)


def _error_badge() -> _Safe:
    return _tag("span", "error", cls="badge")


def _served_rows(entries: Sequence[ServedEntry], detailed: bool) -> list[list[object]]:
    rows: list[list[object]] = []
    for e in entries:
        tool = _line(e.tool, 32)
        row: list[object] = [
            _a(f"/served/{e.id}", f"#{e.id}"),
            _time(e.at, "unknown"),
            _a(_url("/served", agent=e.agent_id), _line(e.agent_id, 64)),
            _join(tool, " ", _error_badge()) if _is_error_response(e) else tool,
            _args_summary(e.args),
        ]
        if detailed:
            row += [_line(e.via, 16), f"~{estimate_tokens(e.text)}"]
        rows.append(row)
    return rows


# --- pages -----------------------------------------------------------------------------------


def _heartbeat_text(service: Service, now: datetime) -> tuple[str, bool]:
    """The daemon line (worded like ``status``) and whether it reports a problem."""
    beat = service._heartbeat(now)
    if not beat.present:
        return "daemon not running (no heartbeat)", True
    if beat.stale:
        assert beat.min_schedule_s is not None
        return (
            f"daemon heartbeat stale ({fmt_age(beat.age_s)} ago; "
            f"shortest schedule {fmt_age(beat.min_schedule_s)})",
            True,
        )
    return f"daemon heartbeat {fmt_age(beat.age_s)} ago", False


def _state_cell(state: SourceState) -> _Safe:
    if state.in_error:
        message = (  # D38: the streak start and the latest error can have different causes
            f"error since {_time(state.error_since, 'unknown')}; "
            f"latest {_time(state.last_error_at, 'unknown')}: "
        )
        cell = _tag("span", _line(message + q(state.last_error, DIGEST_CAP), 400), cls="err")
    elif state.last_success_at is None and state.last_error_at is None:
        cell = _tag("span", "never collected", cls="muted")
    else:
        cell = _tag("span", "ok")
    if state.configured:
        return cell
    return _join(cell, " ", _tag("span", "(not in config)", cls="muted"))


def _sources_table(states: Sequence[SourceState]) -> _Safe:
    ordered = sorted(states, key=lambda s: (_priority_rank(s.priority), s.source_id))
    rows = [
        [
            _line(s.priority, 16),
            _a(_url("/events", source=s.source_id), _line(s.source_id, 64)),
            _line(s.type, 32),
            str(s.record_count),
            _time(s.last_success_at),
            _state_cell(s),
        ]
        for s in ordered
    ]
    headers = ("priority", "source", "type", "records", "last success", "state")
    return _table(headers, rows, ("", "", "", "num", "nowrap", ""))


def _agents_table(agents: Sequence[AgentSummary]) -> _Safe:
    rows = [
        [
            _a(_url("/served", agent=a.agent_id), _line(a.agent_id, 64)),
            str(a.cursor),
            _time(a.cursor_updated_at),
            _time(a.last_served_at),
            str(a.served_count),
        ]
        for a in agents
    ]
    headers = ("agent", "cursor", "cursor acked at", "last served", "responses served")
    return _table(headers, rows, ("", "num", "nowrap", "nowrap", "num"))


def _home_page(store: Store, now: datetime) -> str:
    text, problem = _heartbeat_text(Service(store, lambda: now), now)
    status = _tag(
        "p",
        _tag("span", text, cls="err" if problem else None),
        f" · latest event seq {store.max_seq()}",
    )
    recent = _table(
        ("id", "time", "agent", "tool", "arguments"),
        _served_rows(store.list_served(limit=HOME_SERVED), detailed=False),
        ("nowrap", "nowrap", "", "", "mono"),
    )
    return _page(
        "Since audit page",
        _tag("h1", "Since audit page"),
        status,
        _tag("h2", "Sources"),
        _sources_table(store.list_source_states()),
        _tag("h2", "Agents"),
        _agents_table(store.list_agents()),
        _tag("h2", f"Recent responses served (latest {HOME_SERVED})"),
        recent,
        _links(_a("/served", "all served responses"), _a("/events", "all events")),
    )


def _event_line(event: Event | UnreadableEvent, key_labels: Mapping[str, str]) -> str:
    """The digest line body of an event; one unreadable event must not break the page."""
    if isinstance(event, UnreadableEvent):  # its JSON columns cannot even be decoded
        return "? unreadable event"
    try:
        text = event_body(event, key_labels.get(event.source_id, ""), DIGEST_CAP)
    except (ValueError, TypeError, KeyError, AttributeError):
        text = f"? unreadable {event.kind} event"
    return _line(text, 4000)


def _events_page(store: Store, source: str | None, before: int | None) -> str:
    fetched = store.list_events(before=before, source_id=source, limit=PAGE_SIZE + 1)
    events = fetched[:PAGE_SIZE]
    key_labels = {s.source_id: s.key_label for s in store.list_source_states()}
    rows = [
        [
            str(e.seq),
            _time(e.created_at, "unknown"),
            _a(_url("/events", source=e.source_id), _line(e.source_id, 64)),
            _event_line(e, key_labels),
            _a(f"/event/{e.seq}", evt_handle(e.seq)),
        ]
        for e in events
    ]
    title = "Events" if source is None else f"Events of {_line(source, 64)}"
    older = None
    if len(fetched) > PAGE_SIZE:
        older = _a(_url("/events", source=source, before=events[-1].seq), "older events")
    newest = None
    if before is not None:
        newest = _a(_url("/events", source=source), "newest events")
    return _page(
        title,
        _tag("h1", title),
        _tag("p", f"Newest first, {PAGE_SIZE} per page.", cls="muted"),
        _table(
            ("seq", "time", "source", "event", "handle"),
            rows,
            ("num", "nowrap", "", "mono", "nowrap mono"),
        ),
        _links(newest, older, _a("/events", "all sources") if source is not None else None),
    )


def _event_page(store: Store, seq: int, now: datetime) -> str:
    event = store.get_event_or_unreadable(seq)
    if event is None:
        raise _HttpError(HTTPStatus.NOT_FOUND)
    handle = evt_handle(seq)
    stored: dict[str, Any]
    if isinstance(event, UnreadableEvent):
        # The JSON columns cannot be decoded: show the columns as stored, escaped like all data.
        served_text = "(this event's stored data cannot be decoded)"
        stored = {
            "seq": event.seq,
            "source_id": event.source_id,
            "kind": event.kind,
            "record_key": event.record_key,
            "importance": event.importance,
            "created_at": event.created_at,
            "field_changes_json": event.field_changes_json,
            "detail_json": event.detail_json,
        }
    else:
        # The text ``get`` would return; ``_get_evt`` reads only (``get`` writes the served log).
        try:
            served_text = Service(store, lambda: now)._get_evt(EvtHandle(seq), EVENT_VIEW_BUDGET)
        except (ValueError, TypeError, KeyError, AttributeError):  # damaged data: still show it
            served_text = "(this event's stored data cannot be rendered as text)"
        stored = {
            "seq": event.seq,
            "source_id": event.source_id,
            "kind": event.kind,
            "record_key": event.record_key,
            "importance": event.importance,
            "created_at": event.created_at,
            "field_changes": [change.to_dict() for change in event.field_changes],
            "detail": event.detail,
        }
    title = f"Event {seq}"
    return _page(
        title,
        _tag("h1", "Event ", _tag("code", handle)),
        _tag("h2", f"As returned by get({handle})"),
        _pre(served_text),
        _tag("h2", "Stored event data"),
        _pre(_json_view(stored)),
        _links(
            _a(_url("/events", source=event.source_id), f"events of {_line(event.source_id, 64)}"),
            _a("/events", "all events"),
        ),
    )


def _served_page(store: Store, agent: str | None, before: int | None) -> str:
    fetched = store.list_served(agent_id=agent, limit=PAGE_SIZE + 1, before=before)
    entries = fetched[:PAGE_SIZE]
    title = "Served responses" if agent is None else f"Served responses of {_line(agent, 64)}"
    older = None
    if len(fetched) > PAGE_SIZE:
        older = _a(_url("/served", agent=agent, before=entries[-1].id), "older responses")
    newest = None
    if before is not None:
        newest = _a(_url("/served", agent=agent), "newest responses")
    return _page(
        title,
        _tag("h1", title),
        _tag(
            "p", f"Newest first, {PAGE_SIZE} per page. Open one to see the exact text.", cls="muted"
        ),
        _table(
            ("id", "time", "agent", "tool", "arguments", "via", "~tokens"),
            _served_rows(entries, detailed=True),
            ("nowrap", "nowrap", "", "", "mono", "", "num"),
        ),
        _links(newest, older, _a("/served", "all agents") if agent is not None else None),
    )


def _served_entry_page(store: Store, served_id: int) -> str:
    entry = store.get_served(served_id)
    if entry is None:
        raise _HttpError(HTTPStatus.NOT_FOUND)
    fact_rows: list[list[object]] = [
        ["id", str(entry.id)],
        ["agent", _a(_url("/served", agent=entry.agent_id), _line(entry.agent_id, 64))],
        ["tool", _line(entry.tool, 32)],
        ["arguments", _tag("code", _json_view(entry.args, indent=None))],
        ["via", _line(entry.via, 16)],
        ["served at", _time(entry.at, "unknown")],
        ["size", f"{len(entry.text)} characters, ~{estimate_tokens(entry.text)} tokens"],
    ]
    if _is_error_response(entry):
        fact_rows.append(["result", _error_badge()])
    facts = _table(("field", "value"), fact_rows)

    # The text itself stays exact; when it holds characters a browser would not show as
    # themselves (or would act on: bidi overrides), a second view spells them out.
    exact: list[_Safe] = [_pre(entry.text)]
    invisible = sum(1 for c in entry.text if _is_invisible(c))
    if invisible:
        exact.insert(
            0,
            _tag(
                "p",
                f"contains {invisible} invisible character{'' if invisible == 1 else 's'} "
                "(shown as ⟨U+XXXX⟩ in the escaped view below)",
                cls="err",
            ),
        )
        exact += [_tag("h2", "Escaped view"), _pre(_visible(entry.text, angle=True))]
    title = f"Served response #{entry.id}"
    return _page(
        title,
        _tag("h1", title),
        facts,
        _tag("h2", "Exactly what was served"),
        *exact,
        _links(
            _a(_url("/served", agent=entry.agent_id), f"responses of {_line(entry.agent_id, 64)}"),
            _a("/served", "all served responses"),
        ),
    )


def _unavailable_page(exc: NoDatabaseError | SchemaVersionError) -> str:
    """The page (HTTP 503) for a database the audit page cannot read as it is."""
    if isinstance(exc, NoDatabaseError):
        message = f"no database at {exc.path} — run `since daemon` or `since collect` first"
    elif exc.older:
        message = (
            f"database schema v{exc.found} is older than this Since (v{SCHEMA_VERSION}); "
            "run the daemon once to migrate"
        )
    else:
        message = (
            f"database schema v{exc.found} was written by a newer Since "
            f"(this one understands up to v{SCHEMA_VERSION}); upgrade Since"
        )
    return _page(
        "Since audit page",
        _tag("h1", "Since audit page"),
        _tag("p", message, cls="err"),
        _tag("p", "This page only reads the database; it never creates or migrates.", cls="muted"),
    )


# --- routing ---------------------------------------------------------------------------------


def _params(query: str, allowed: Sequence[str]) -> dict[str, str]:
    """The query as a dict. Anything but ``name=value`` pairs of the allowed names, each at most
    once, valid UTF-8 (percent-encoded), is a 400."""
    try:
        pairs = parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, max_num_fields=8, errors="strict"
        )
    except ValueError:
        raise _HttpError(HTTPStatus.BAD_REQUEST, "malformed query") from None
    found: dict[str, str] = {}
    for name, value in pairs:
        if name not in allowed or name in found:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "unexpected or repeated query parameter")
        found[name] = value
    return found


def _id_param(params: Mapping[str, str], name: str) -> int | None:
    value = params.get(name)
    if value is None:
        return None
    if _POSITIVE.fullmatch(value) is None or int(value) > MAX_SEQ:
        raise _HttpError(HTTPStatus.BAD_REQUEST, f"{name} must be a positive integer")
    return int(value)


def _name_param(params: Mapping[str, str], name: str, pattern: re.Pattern[str]) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if pattern.fullmatch(value) is None:
        raise _HttpError(HTTPStatus.BAD_REQUEST, f"invalid {name}")
    return value


def _render(target: str, now: datetime) -> str:
    """The HTML page for a request target, or an :class:`_HttpError`."""
    try:
        parts = urlsplit(target)
    except ValueError:
        raise _HttpError(HTTPStatus.BAD_REQUEST, "malformed request target") from None
    path, query = parts.path, parts.query
    if path == "/":
        _params(query, ())
        with Store.open_readonly() as store:
            return _home_page(store, now)
    if path == "/events":
        params = _params(query, ("source", "before"))
        source = _name_param(params, "source", SOURCE_ID_RE)
        before = _id_param(params, "before")
        with Store.open_readonly() as store:
            return _events_page(store, source, before)
    if path == "/served":
        params = _params(query, ("agent", "before"))
        agent = _name_param(params, "agent", AGENT_ID_RE)
        before = _id_param(params, "before")
        with Store.open_readonly() as store:
            return _served_page(store, agent, before)
    if (m := _EVENT_PATH.fullmatch(path)) is not None:
        _params(query, ())
        if int(m.group(1)) > MAX_SEQ:
            raise _HttpError(HTTPStatus.NOT_FOUND)
        with Store.open_readonly() as store:
            return _event_page(store, int(m.group(1)), now)
    if (m := _SERVED_PATH.fullmatch(path)) is not None:
        _params(query, ())
        if int(m.group(1)) > MAX_SEQ:
            raise _HttpError(HTTPStatus.NOT_FOUND)
        with Store.open_readonly() as store:
            return _served_entry_page(store, int(m.group(1)))
    raise _HttpError(HTTPStatus.NOT_FOUND)


# --- the server ------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _log(text: str) -> None:
    print(f"since ui: {text}", file=sys.stderr, flush=True)


class _Handler(BaseHTTPRequestHandler):
    server: AuditServer
    timeout = REQUEST_TIMEOUT_S

    def version_string(self) -> str:
        return "since-ui"

    def log_message(self, format: str, *args: Any) -> None:  # no access log
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Protocol-level errors of the base class (bad request line, ...) as plain text too."""
        self._text(HTTPStatus(code))

    def __getattr__(self, name: str) -> Callable[[], None]:
        # Every HTTP method but GET and HEAD is answered with 405 (not the base class's 501).
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    # -- methods -----------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def _method_not_allowed(self) -> None:
        if self._host_ok():
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, headers=(("Allow", "GET, HEAD"),))

    def _host_ok(self) -> bool:
        """The Host header must be exactly ``127.0.0.1:<port>`` or ``localhost:<port>``: a page
        served under another name (DNS rebinding) is refused."""
        hosts = self.headers.get_all("Host") or []
        if len(hosts) == 1 and hosts[0] in self.server.allowed_hosts:
            return True
        self._text(HTTPStatus.MISDIRECTED_REQUEST, "unexpected Host header")
        return False

    def _serve(self) -> None:
        if not self._host_ok():
            return
        try:
            page = _render(self.path, self.server.now_fn())
        except _HttpError as exc:
            self._text(exc.status, exc.reason)
            return
        except (NoDatabaseError, SchemaVersionError) as exc:  # a normal page, saying what to do
            body = _unavailable_page(exc).encode("utf-8", "replace")
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, body, "text/html; charset=utf-8")
            return
        except Exception as exc:  # a bug or an unusable database: no details to the client
            _log(f"{self.command} {q(self.path, 120)} failed: {type(exc).__name__}: {q(exc, 200)}")
            self._text(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send(HTTPStatus.OK, page.encode("utf-8", "replace"), "text/html; charset=utf-8")

    # -- responses ---------------------------------------------------------------------------

    def _text(
        self, status: HTTPStatus, reason: str = "", headers: Sequence[tuple[str, str]] = ()
    ) -> None:
        line = f"{status.value} {status.phrase}" + (f": {reason}" if reason else "")
        self._send(status, (line + "\n").encode("utf-8"), "text/plain; charset=utf-8", headers)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        headers: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (*_SECURITY_HEADERS, *headers):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class AuditServer(ThreadingHTTPServer):
    """The audit page server, bound to 127.0.0.1 only (``port`` 0 = any free port)."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket bind a port that is in use; without it a taken
    # port fails as it should. Elsewhere it only lets a restart skip TIME_WAIT.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int = DEFAULT_PORT, now_fn: Callable[[], datetime] = _utcnow) -> None:
        self.now_fn = now_fn
        super().__init__((HOST, port), _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind also does a reverse DNS lookup (slow on some setups); not needed.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):  # the browser went away
            return
        _log(f"error: {type(exc).__name__}: {q(exc, 200)}")


def serve(port: int = DEFAULT_PORT) -> int:
    """Serve until Ctrl-C (exit code 0). Raises :class:`UiError` if the port cannot be bound."""
    try:
        server = AuditServer(port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise UiError(f"port {port} is already in use (try --port 0 for a free port)") from exc
        raise UiError(f"cannot listen on {HOST}:{port}: {exc.strerror or exc}") from exc
    try:
        print(f"Since audit page: http://{HOST}:{server.port}/ (Ctrl-C to stop)", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
    return 0
