"""``imap`` source: every mail of the last N days in some folders is one record (headers only).

Config keys (unknown keys are rejected)::

    host: imap.example.com
    port: 993                     # default 993 for ssl, else 143
    security: ssl                 # ssl (default) | starttls | none (localhost, 127.0.0.1, ::1 only)
    username: me@example.com      # printable ASCII
    password_env: SINCE_IMAP_PW   # name of the env var holding the password (D9: never YAML)
    folders: [INBOX]              # default; names are quoted and sent as IMAP modified UTF-7
    since_days: 14                # 1-365, default 14
    max_messages: 500             # 1-5000 per folder, default 500; see "The cap" below

Read-only by construction (D20): the collector only ever sends CAPABILITY (imaplib does), LOGIN,
EXAMINE, ``UID SEARCH SINCE``, ``UID FETCH ... BODY.PEEK[HEADER.FIELDS (...)]`` and LOGOUT. It never
sends SELECT, STORE, COPY, MOVE, EXPUNGE or APPEND, and never fetches a body part without PEEK.
Only stdlib ``imaplib`` / ``email`` are used.

Record: key = Message-ID (stripped), or ``uid:<folder>/<uidvalidity>/<uid>`` when the mail has none;
fields ``subject``, ``from`` (first address, ``Name <addr>``), ``to`` (first 3 addresses), ``date``
(Date header as ISO UTC, else INTERNALDATE), ``received`` (INTERNALDATE as ISO UTC: the moment the
server took the mail in, and what ``SEARCH SINCE`` filters on; the Date header only when the
server sent no usable INTERNALDATE), ``folder``, ``seen``, ``flagged``, ``answered``, ``size``.
A mail that shows up under the same Message-ID in several folders (Gmail labels) is kept once:
first by folder order in the config, then by ascending UID.

The window (D20/D21 revised). The result carries ``Window("received", <start>, scope_field="folder",
starts={folder: <start of that folder>})`` (``<start>``, for a mail of a folder that is no longer
configured, is the start for ``since_days``): a mail of the previous snapshot that is absent from
the result and was *received* before its folder's start has aged out and leaves the snapshot
without a ``removed`` event; a mail absent although received on or after that start is a real
removal. It is the ``received`` time, not the Date header, that is compared, because that is what
the server filters on: ``SEARCH SINCE`` compares the *date* of INTERNALDATE in the server's own
time zone, which Since cannot know. So each folder's start is 00:00 UTC of its SINCE day **plus
one day**, a margin that covers every time zone. The price: a mail that is deleted at the source
while it was received on the SINCE day or earlier (the last day at the far end of the window)
leaves the snapshot silently instead of as ``removed``.

The cap (``max_messages``, per folder). The newest mails are never silently dropped from a window
that claims to cover them, so the SINCE date is shortened instead of cutting the result: when
``SEARCH SINCE`` for ``since_days`` matches more than ``max_messages`` mails, the number of days is
bisected (1 .. ``since_days``, with further ``UID SEARCH SINCE`` calls) to the largest day count
that matches at most ``max_messages`` mails; that folder is collected for that many days only and
its window start is that (shorter) SINCE day plus one day. Mails older than that age out silently.
Only when even one day matches more than ``max_messages`` mails does it keep the newest
``max_messages`` UIDs of that day and set the folder's start to the oldest kept ``received`` plus
one day (this assumes UIDs grow with the received time; a folder that violates this may show a
mail that fell out of the cap as removed).

Default ``track_fields`` (D26): ``folder``, ``flagged``, ``answered``. A mail merely being read
(``seen``) makes no event; a ``track_fields`` option in the config replaces the default. Default
title fields (D17, D29): ``subject``, ``from``, ``received``.

Errors reaching the stored ``source_error`` never contain the password: the messages are fixed
texts (``login failed for <username>``, raised as ``LoginRequired``, D24) or scrubbed reasons of
the underlying exception. A folder that EXAMINE rejects fails the whole run (silently skipping it
would make its mails look removed).
"""

from __future__ import annotations

import base64
import email
import email.message
import email.policy
import email.utils
import imaplib
import os
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from since.config import ConfigError, SourceConfig
from since.model import Record, Scalar
from since.sources import CollectError, CollectOutput, LoginRequired, Window
from since.timeutil import to_iso

_ALLOWED_OPTIONS = (
    "host",
    "port",
    "security",
    "username",
    "password_env",
    "folders",
    "since_days",
    "max_messages",
)
SECURITY_MODES = ("ssl", "starttls", "none")
# Plain text passwords never go over a network: `none` is for a local bridge/test server only.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

DEFAULT_SINCE_DAYS = 14
DEFAULT_MAX_MESSAGES = 500
MAX_SINCE_DAYS = 365
MAX_MAX_MESSAGES = 5000
BATCH_SIZE = 100
# Servers apply SEARCH SINCE to the INTERNALDATE date in their own time zone: window starts get a
# day of margin (see the module docstring).
_MARGIN = timedelta(days=1)
TIMEOUT_S = 30  # socket timeout of every IMAP operation, seconds
MAX_TO_ADDRESSES = 3

FETCH_ITEMS = (
    "(UID FLAGS INTERNALDATE RFC822.SIZE "
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM TO DATE)])"
)

# IMAP month names are English whatever the locale (``strftime("%b")`` is not).
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_REASON_CAP = 200
_REDACTED = "***"
_DATE_MIN_YEAR, _DATE_MAX_YEAR = 1970, 9998  # Date headers outside this are not trusted


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- modified UTF-7 (RFC 3501 section 5.1.3) -----------------------------------------------------


def mutf7_encode(name: str) -> str:
    """Folder name -> IMAP modified UTF-7: printable ASCII stands for itself (``&`` becomes
    ``&-``); every other run of characters becomes ``&`` + base64 of its UTF-16BE bytes (``/``
    written as ``,``, no padding) + ``-``. ``台北`` -> ``&U,BTFw-``. Raises ``UnicodeEncodeError``
    for lone surrogates."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            raw = "".join(run).encode("utf-16-be")
            out.append(
                "&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-"
            )
            run.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run.append(ch)
    flush()
    return "".join(out)


def _quote(text: str) -> str:
    """An IMAP quoted string. Callers pass printable ASCII only (modified UTF-7 output, a checked
    username), so escaping ``\\`` and ``"`` is all there is to do."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- settings ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Settings:
    host: str
    port: int
    security: str
    username: str
    password_env: str
    folders: list[str]
    since_days: int
    max_messages: int


def _int_option(
    where: str, options: dict[str, Any], name: str, default: int, lo: int, hi: int
) -> int:
    value = options[name] if name in options else default
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{where}: key '{name}': must be an integer from {lo} to {hi}")
    return value


def _has_control(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def _str_option(where: str, options: dict[str, Any], name: str, what: str) -> str:
    value = options.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: key '{name}': required, must be a non-empty string ({what})")
    return value


def _settings(cfg: SourceConfig) -> _Settings:
    """Parse and check ``cfg.options`` (no I/O); raises ``ConfigError``."""
    where = f"source '{cfg.id}'"
    options = cfg.options
    for name in sorted(options, key=str):
        if name not in _ALLOWED_OPTIONS:
            raise ConfigError(
                f"{where}: key '{name}': unknown option for an imap source "
                f"(allowed: {', '.join(_ALLOWED_OPTIONS)})"
            )

    host = _str_option(where, options, "host", "a host name or IP address").strip()
    if any(c.isspace() for c in host) or _has_control(host):
        raise ConfigError(f"{where}: key 'host': must not contain whitespace or control characters")

    security = options.get("security", "ssl")
    if security not in SECURITY_MODES:
        raise ConfigError(
            f"{where}: key 'security': must be one of {', '.join(SECURITY_MODES)} "
            f"(got {security!r})"
        )
    if security == "none" and host.lower() not in _LOCAL_HOSTS:
        raise ConfigError(
            f"{where}: key 'security': 'none' sends the password in plain text and is only "
            "allowed for localhost, 127.0.0.1 or ::1; use ssl or starttls"
        )
    port = _int_option(where, options, "port", 993 if security == "ssl" else 143, 1, 65535)

    username = _str_option(where, options, "username", "the login name")
    if not all(0x20 <= ord(c) <= 0x7E for c in username):
        raise ConfigError(f"{where}: key 'username': must be printable ASCII")
    password_env = _str_option(
        where, options, "password_env", "name of the env var with the password"
    )

    folders = options["folders"] if "folders" in options else ["INBOX"]
    if (
        not isinstance(folders, list)
        or not folders
        or not all(isinstance(f, str) and f for f in folders)
    ):
        raise ConfigError(f"{where}: key 'folders': must be a non-empty list of folder names")
    for folder in folders:
        try:
            mutf7_encode(folder)
        except UnicodeEncodeError:
            raise ConfigError(f"{where}: key 'folders': a folder name is not valid text") from None

    since_days = _int_option(where, options, "since_days", DEFAULT_SINCE_DAYS, 1, MAX_SINCE_DAYS)
    max_messages = _int_option(
        where, options, "max_messages", DEFAULT_MAX_MESSAGES, 1, MAX_MAX_MESSAGES
    )
    return _Settings(
        host, port, security, username, password_env, list(folders), since_days, max_messages
    )


# -- the collector -------------------------------------------------------------------------------


class ImapCollector:
    type_name = "imap"

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        """``now_fn`` returns the aware current time (UTC clock by default); tests inject one."""
        self._now_fn = now_fn if now_fn is not None else _utc_now

    def validate(self, cfg: SourceConfig) -> None:
        """Check the options. No I/O: the env var is read and the server contacted only in
        ``collect``."""
        _settings(cfg)

    def key_label(self, cfg: SourceConfig) -> str:
        return ""

    def default_title_fields(self, cfg: SourceConfig) -> list[str]:
        return ["subject", "from", "received"]

    def default_track_fields(self, cfg: SourceConfig) -> list[str]:
        """A mail merely being read (``seen``) is not news and must not outrank new mail (D26);
        ``flagged`` / ``answered`` and a move to another folder are. ``track_fields`` in the
        config replaces this."""
        return ["folder", "flagged", "answered"]

    def collect(self, cfg: SourceConfig) -> CollectOutput:
        try:
            s = _settings(cfg)
        except ConfigError as exc:
            raise CollectError(str(exc)) from None
        password = _read_password(s)
        now = self._now_fn()
        secrets = _secrets(password)

        conn: imaplib.IMAP4 | None = None
        try:
            conn = _connect(s, secrets)
            _login(conn, s, password, secrets)
            records, starts = _collect_folders(conn, s, now)
        except CollectError:
            raise
        except Exception as exc:
            raise CollectError(f"IMAP error: {_reason(exc, secrets)}") from None
        finally:
            _logout(conn)
        records.sort(key=lambda r: r.key)
        start = _since_start(now, s.since_days) + _MARGIN
        window = Window(
            "received",
            to_iso(start),
            scope_field="folder",
            starts={folder: to_iso(folder_start) for folder, folder_start in starts.items()},
        )
        return CollectOutput(records, window=window)


def _read_password(s: _Settings) -> str:
    password = os.environ.get(s.password_env)
    if not password:
        raise CollectError(f"environment variable {s.password_env} is not set (or is empty)")
    if _has_control(password) or not password.isascii():
        raise CollectError(
            f"environment variable {s.password_env} holds a password that IMAP LOGIN cannot "
            "send (control or non-ASCII characters)"
        )
    return password


def _since_start(now: datetime, days: int) -> datetime:
    """00:00 UTC of the day ``days`` before ``now``: the SINCE day and the window start."""
    day = (now.astimezone(UTC) - timedelta(days=days)).date()
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _imap_date(moment: datetime) -> str:
    """``15-Sep-2026`` (English month, whatever the locale)."""
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year:04d}"


# -- errors that must not leak the password ------------------------------------------------------


def _secrets(password: str) -> list[str]:
    """The password as sent (raw and IMAP-quoted): scrubbed from every reason text."""
    return [s for s in dict.fromkeys([password, _quote(password)[1:-1]]) if s]


def _reason(exc: BaseException, secrets: list[str]) -> str:
    """A short, single-line reason for an exception: first line of its message (or its type name),
    with the password replaced by ``***``, capped."""
    verify_message = getattr(exc, "verify_message", None)
    if isinstance(exc, ssl.SSLCertVerificationError) and verify_message:
        text = f"certificate verification failed: {verify_message}"
    else:
        lines = str(exc).strip().splitlines()
        text = lines[0].strip() if lines else ""
    if not text:
        text = type(exc).__name__
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return text[:_REASON_CAP]


# -- protocol ------------------------------------------------------------------------------------


def _connect(s: _Settings, secrets: list[str]) -> imaplib.IMAP4:
    """Open the connection per security mode (ssl: implicit TLS; starttls: upgrade before LOGIN;
    none: plain, localhost only). The default SSL context verifies certificate and host name."""
    try:
        if s.security == "ssl":
            return imaplib.IMAP4_SSL(
                s.host, s.port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S
            )
        conn = imaplib.IMAP4(s.host, s.port, timeout=TIMEOUT_S)
        if s.security == "starttls":
            try:
                conn.starttls(ssl_context=ssl.create_default_context())
            except BaseException:
                _logout(conn)
                raise
        return conn
    except (OSError, imaplib.IMAP4.error) as exc:
        raise CollectError(
            f"cannot connect to {s.host}:{s.port}: {_reason(exc, secrets)}"
        ) from None


def _login(conn: imaplib.IMAP4, s: _Settings, password: str, secrets: list[str]) -> None:
    try:
        # imaplib quotes the password but sends the user name as it is: quote it ourselves.
        conn.login(_quote(s.username), password)
    except imaplib.IMAP4.abort as exc:
        raise CollectError(f"connection lost during login: {_reason(exc, secrets)}") from None
    except imaplib.IMAP4.error:
        # Never echo the server's text: it is untrusted and could quote what we sent.
        hint = f"check the app password in {s.password_env}"
        raise LoginRequired(f"login failed for {s.username}", hint) from None


def _logout(conn: imaplib.IMAP4 | None) -> None:
    if conn is None:
        return
    try:
        conn.logout()
    except Exception:
        try:
            conn.shutdown()
        except Exception:
            pass


def _examine(conn: imaplib.IMAP4, folder: str) -> str:
    """EXAMINE (never SELECT) a folder and return its UIDVALIDITY (digits)."""
    try:
        typ, _data = conn.select(_quote(mutf7_encode(folder)), readonly=True)
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        raise CollectError(f'cannot open folder "{folder}"') from None
    if typ != "OK":
        raise CollectError(f'cannot open folder "{folder}"')
    _typ, values = conn.response("UIDVALIDITY")
    for value in values or []:
        match = re.match(rb"\s*(\d+)", value) if isinstance(value, bytes) else None
        if match:
            return match.group(1).decode("ascii")
    return "0"


def _search(conn: imaplib.IMAP4, folder: str, since_arg: str) -> list[int]:
    """UIDs of the mails with an internal date on or after the SINCE day, ascending."""
    typ, data = conn.uid("SEARCH", "SINCE", since_arg)
    if typ != "OK":
        raise CollectError(f'search failed in folder "{folder}"')
    uids: set[int] = set()
    for chunk in data or []:
        if isinstance(chunk, bytes):
            uids.update(int(t) for t in chunk.split() if t.isdigit())
    return sorted(uids)


@dataclass(frozen=True)
class _Pick:
    """The UIDs of one folder to fetch: ascending, from a ``SEARCH SINCE`` of ``days`` days;
    ``capped`` when even one day matched more than ``max_messages``: only the newest were kept."""

    uids: list[int]
    days: int
    capped: bool


def _pick_uids(conn: imaplib.IMAP4, folder: str, s: _Settings, now: datetime) -> _Pick:
    """The UIDs to fetch from the selected folder: everything ``SEARCH SINCE`` finds for
    ``since_days`` if that is at most ``max_messages``; else those of the largest day count
    (bisected with further searches) that stays within the cap; else the newest ``max_messages``
    of one day."""
    found: dict[int, list[int]] = {}

    def search(days: int) -> list[int]:
        if days not in found:
            found[days] = _search(conn, folder, _imap_date(_since_start(now, days)))
        return found[days]

    if len(search(s.since_days)) <= s.max_messages:
        return _Pick(found[s.since_days], s.since_days, False)
    # The match count grows with the day count: lo (0 days: nothing) fits the cap, hi does not.
    lo, hi = 0, s.since_days
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if len(search(mid)) <= s.max_messages:
            lo = mid
        else:
            hi = mid
    if lo > 0:
        return _Pick(found[lo], lo, False)
    return _Pick(found[hi][-s.max_messages :], hi, True)  # hi is 1: even one day is over the cap


def _folder_start(now: datetime, pick: _Pick, fetched: dict[int, _Fetched]) -> datetime:
    """The window start of a folder: its SINCE day plus a day; when only the newest of one day were
    kept, the oldest of them plus a day."""
    start = _since_start(now, pick.days) + _MARGIN
    if pick.capped:
        received = [item.internaldate for item in fetched.values() if item.internaldate]
        if received:
            try:
                start = min(received).astimezone(UTC) + _MARGIN
            except (ValueError, OverflowError):
                pass
    return start


def _collect_folders(
    conn: imaplib.IMAP4, s: _Settings, now: datetime
) -> tuple[list[Record], dict[str, datetime]]:
    """The records of all folders and the window start of each folder."""
    records: list[Record] = []
    starts: dict[str, datetime] = {}
    seen: set[str] = set()
    for folder in s.folders:
        uidvalidity = _examine(conn, folder)
        pick = _pick_uids(conn, folder, s, now)
        fetched: dict[int, _Fetched] = {}
        for i in range(0, len(pick.uids), BATCH_SIZE):
            for item in _fetch(conn, folder, pick.uids[i : i + BATCH_SIZE]):
                fetched.setdefault(item.uid, item)
        starts[folder] = _folder_start(now, pick, fetched)
        for uid in sorted(fetched):  # (folder order, UID ascending): the first duplicate wins
            key, fields = _mail_fields(folder, uidvalidity, fetched[uid])
            if key not in seen:
                seen.add(key)
                records.append(Record.make(key, fields))
    return records, starts


# -- FETCH responses -----------------------------------------------------------------------------

_FETCH_START_RE = re.compile(rb"^\s*\d+ \(")
_FLAGS_RE = re.compile(r"\bFLAGS \(([^)]*)\)")
_UID_RE = re.compile(r"\bUID (\d+)")
_SIZE_RE = re.compile(r"\bRFC822\.SIZE (\d+)")
_INTERNALDATE_RE = re.compile(r'\bINTERNALDATE "([^"]*)"')
_INTERNALDATE_VALUE_RE = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})$"
)


@dataclass(frozen=True)
class _Fetched:
    uid: int
    flags: frozenset[str]  # lower case, e.g. "\\seen"
    internaldate: datetime | None
    size: int
    header: bytes


def _fetch(conn: imaplib.IMAP4, folder: str, uids: list[int]) -> list[_Fetched]:
    typ, data = conn.uid("FETCH", ",".join(str(u) for u in uids), FETCH_ITEMS)
    if typ != "OK":
        raise CollectError(f'fetch failed in folder "{folder}"')
    wanted = set(uids)
    return [item for item in _parse_fetch(data or []) if item.uid in wanted]


def _parse_fetch(data: list[Any]) -> list[_Fetched]:
    """Turn imaplib's FETCH data into records. imaplib delivers each response as text (or a
    ``(text, literal)`` tuple) followed by the trailer after the literal; a new response starts
    where the text begins ``<seq> (``. The order of the items inside a response is the server's
    choice, so the metadata is searched in the joined text, never by position."""
    groups: list[tuple[list[bytes], bytes]] = []  # (text parts, header literal)
    for item in data:
        if isinstance(item, tuple):
            text, literal = item[0], item[1]
        elif isinstance(item, bytes):
            text, literal = item, None
        else:
            continue
        if not isinstance(text, bytes):
            continue
        if _FETCH_START_RE.match(text) or not groups:
            groups.append(([], b""))
        parts, header = groups[-1]
        parts.append(text)
        if isinstance(literal, bytes) and not header:
            groups[-1] = (parts, literal)
    return [item for item in (_fetched(parts, header) for parts, header in groups) if item]


def _fetched(parts: list[bytes], header: bytes) -> _Fetched | None:
    text = b" ".join(parts).decode("ascii", "replace")
    flags_match = _FLAGS_RE.search(text)
    flags = frozenset(flags_match.group(1).lower().split()) if flags_match else frozenset()
    if flags_match:  # a flag is an arbitrary atom: keep it away from the other searches
        text = text[: flags_match.start()] + " " + text[flags_match.end() :]
    uid_match = _UID_RE.search(text)
    if uid_match is None:  # e.g. an unsolicited `* 3 FETCH (FLAGS (...))`
        return None
    size_match = _SIZE_RE.search(text)
    date_match = _INTERNALDATE_RE.search(text)
    return _Fetched(
        uid=int(uid_match.group(1)),
        flags=flags,
        internaldate=_parse_internaldate(date_match.group(1)) if date_match else None,
        size=int(size_match.group(1)) if size_match else 0,
        header=header,
    )


def _parse_internaldate(value: str) -> datetime | None:
    """``01-Sep-2026 09:12:05 +0200`` (day may be space padded) -> aware datetime; no locale."""
    match = _INTERNALDATE_VALUE_RE.match(value)
    if match is None:
        return None
    day, month, year, hour, minute, second, sign, off_h, off_m = match.groups()
    try:
        month_no = [m.lower() for m in _MONTHS].index(month.lower()) + 1
        offset = timedelta(hours=int(off_h), minutes=int(off_m))
        zone = timezone(offset if sign == "+" else -offset)
        return datetime(
            int(year), month_no, int(day), int(hour), int(minute), int(second), tzinfo=zone
        )
    except ValueError:
        return None


# -- one mail ------------------------------------------------------------------------------------


def _clean(text: str) -> str:
    """Make header text storable. ``email`` keeps undecoded 8-bit header bytes as lone surrogates
    (``surrogateescape``): raw UTF-8 is recovered, anything else becomes U+FFFD."""
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:  # a lone surrogate that is not an escaped byte
        return text.encode("utf-8", "replace").decode("utf-8")


def _header_text(msg: email.message.Message, name: str) -> str:
    try:
        value = msg[name]
        return "" if value is None else _clean(str(value)).strip()
    except Exception:  # a malformed header must never fail the run
        return ""


def _format_address(address: Any) -> str:
    name = _clean(address.display_name or "").strip()
    spec = _clean(address.addr_spec or "").strip()
    if spec == "<>":
        spec = ""
    if name and spec:
        return f"{name} <{spec}>"
    return spec or name


def _addresses(msg: email.message.Message, name: str, limit: int) -> str:
    """The first ``limit`` addresses of a header as ``Name <addr>`` joined by ``, ``; when none
    can be parsed (e.g. ``undisclosed-recipients:;``) the plain header text."""
    try:
        header = msg[name]
        found = [_format_address(a) for a in header.addresses] if header is not None else []
    except Exception:
        found = []
    found = [a for a in found if a]
    if not found:
        return _header_text(msg, name)
    return ", ".join(found[:limit])


def _internaldate_iso(internaldate: datetime | None) -> str | None:
    """INTERNALDATE as ISO UTC; ``None`` when there is none (or it is out of range)."""
    if internaldate is not None:
        try:
            return to_iso(internaldate)
        except (ValueError, OverflowError):
            pass
    return None


def _date_field(msg: email.message.Message, internaldate: datetime | None) -> str | None:
    """The Date header as ISO UTC; missing, unparseable or implausible -> INTERNALDATE."""
    text = _header_text(msg, "Date")
    if text:
        try:
            moment = email.utils.parsedate_to_datetime(text)
            if moment.tzinfo is None:  # `-0000`: no zone information, UTC by convention
                moment = moment.replace(tzinfo=UTC)
            moment = moment.astimezone(UTC)
            if _DATE_MIN_YEAR <= moment.year <= _DATE_MAX_YEAR:
                return to_iso(moment)
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    return _internaldate_iso(internaldate)


def _mail_fields(folder: str, uidvalidity: str, item: _Fetched) -> tuple[str, dict[str, Scalar]]:
    """(key, fields) of one fetched mail."""
    msg = email.message_from_bytes(item.header, policy=email.policy.default)
    key = _header_text(msg, "Message-ID") or f"uid:{folder}/{uidvalidity}/{item.uid}"
    date = _date_field(msg, item.internaldate)
    received = _internaldate_iso(item.internaldate)
    fields: dict[str, Scalar] = {
        "subject": _header_text(msg, "Subject"),
        "from": _addresses(msg, "From", 1),
        "to": _addresses(msg, "To", MAX_TO_ADDRESSES),
        "date": date,
        "received": received if received is not None else date,
        "folder": folder,
        "seen": "\\seen" in item.flags,
        "flagged": "\\flagged" in item.flags,
        "answered": "\\answered" in item.flags,
        "size": item.size,
    }
    return key, fields
