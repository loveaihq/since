"""A small threaded IMAP4rev1 server for the ``imap`` source tests (127.0.0.1, ephemeral port).

Mailboxes are plain in-memory objects that tests mutate between collections. Supported commands:
CAPABILITY, NOOP, LOGIN, EXAMINE, UID SEARCH SINCE <date>, UID FETCH (UID, FLAGS, INTERNALDATE,
RFC822.SIZE and ``BODY[.PEEK][HEADER.FIELDS (...)]`` answered as a literal), LOGOUT. Everything
else, including SELECT/STORE/EXPUNGE, is answered ``BAD`` and listed in ``bad_commands``.
``commands`` records every command received (LOGIN with the credentials redacted) so tests can
assert the client stays read-only.

The fake is deliberately independent of the code under test: it has its own modified UTF-7
decoder and its own FETCH response writer.
"""

from __future__ import annotations

import base64
import re
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_AUTO = "\0auto"  # sentinel: "generate this header value"

_REDACTED = "***"

DEFAULT_INTERNALDATE = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def decode_mutf7(name: str) -> str:
    """IMAP modified UTF-7 -> text (RFC 3501 section 5.1.3)."""
    out: list[str] = []
    i = 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.index("-", i)
        chunk = name[i + 1 : end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            out.append(base64.b64decode(b64).decode("utf-16-be"))
        i = end + 1
    return "".join(out)


def make_header(
    *,
    message_id: str | None,
    subject: str | None,
    from_: str | None,
    to: str | None,
    date: str | None,
) -> bytes:
    """A raw header block (CRLF lines, terminated by an empty line). ``None`` omits the header;
    the values are written as given, so tests can pass RFC 2047 encoded words."""
    lines = []
    for name, value in (
        ("Message-ID", message_id),
        ("From", from_),
        ("To", to),
        ("Subject", subject),
        ("Date", date),
    ):
        if value is not None:
            lines.append(f"{name}: {value}")
    lines.append("X-Other: not requested by the client")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


@dataclass
class FakeMessage:
    uid: int
    header: bytes
    flags: list[str] = field(default_factory=list)
    internaldate: datetime = DEFAULT_INTERNALDATE
    size: int | None = None  # RFC822.SIZE; default: len(header) + a pretend body

    @property
    def rfc822_size(self) -> int:
        return self.size if self.size is not None else len(self.header) + 100


@dataclass
class FakeMailbox:
    uidvalidity: int = 1000
    messages: list[FakeMessage] = field(default_factory=list)  # ascending UID


class FakeImapServer:
    """``with FakeImapServer() as server:`` -> ``server.host`` / ``server.port`` are ready."""

    def __init__(self, username: str = "alice@example.test", password: str = "s3cret-pw") -> None:
        self.username = username
        self.password = password
        self.mailboxes: dict[str, FakeMailbox] = {}
        self.commands: list[str] = []  # raw commands without tag, LOGIN redacted
        self.bad_commands: list[str] = []  # commands answered BAD
        self.connections = 0
        self.login_error_text = "Invalid credentials"  # tests may make it echo secrets
        self.silent = False  # accept connections but never send a greeting (timeouts)
        self.literal_first = False  # put the header literal before UID/FLAGS/... in FETCH
        self.drop_on: str | None = None  # e.g. "UID FETCH": close the socket instead of answering
        self.lock = threading.Lock()
        self._stopped = threading.Event()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------------------

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> FakeImapServer:
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> FakeImapServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- mailbox helpers for tests ---------------------------------------------------------------

    def add_mailbox(self, name: str, uidvalidity: int = 1000) -> FakeMailbox:
        with self.lock:
            return self.mailboxes.setdefault(name, FakeMailbox(uidvalidity))

    def add_message(
        self,
        folder: str,
        *,
        uid: int | None = None,
        message_id: str | None = _AUTO,
        subject: str | None = "Hello",
        from_: str | None = "Alice <alice@example.test>",
        to: str | None = "bob@example.test",
        date: str | None = _AUTO,
        flags: tuple[str, ...] = (),
        internaldate: datetime = DEFAULT_INTERNALDATE,
        raw_header: bytes | None = None,
        size: int | None = None,
    ) -> FakeMessage:
        """Append a message (a new UID by default). ``message_id`` / ``date`` default to generated
        values; pass ``None`` to leave the header out. ``raw_header`` replaces the whole block."""
        mailbox = self.add_mailbox(folder)
        with self.lock:
            if uid is None:
                uid = max((m.uid for m in mailbox.messages), default=0) + 1
            if message_id == _AUTO:
                message_id = f"<m{uid}@example.test>"
            if date == _AUTO:
                date = format_datetime(internaldate)
            header = raw_header
            if header is None:
                header = make_header(
                    message_id=message_id, subject=subject, from_=from_, to=to, date=date
                )
            message = FakeMessage(uid, header, list(flags), internaldate, size)
            mailbox.messages.append(message)
            mailbox.messages.sort(key=lambda m: m.uid)
            return message

    def find(self, folder: str, uid: int) -> FakeMessage:
        with self.lock:
            return next(m for m in self.mailboxes[folder].messages if m.uid == uid)

    def set_flags(self, folder: str, uid: int, *flags: str) -> None:
        message = self.find(folder, uid)
        with self.lock:
            message.flags = list(flags)

    def delete_message(self, folder: str, uid: int) -> None:
        with self.lock:
            mailbox = self.mailboxes[folder]
            mailbox.messages = [m for m in mailbox.messages if m.uid != uid]

    # -- command log -----------------------------------------------------------------------------

    def command_names(self) -> list[str]:
        """``["LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"]``-style names, in order."""
        names = []
        for command in self.commands:
            words = command.split()
            name = words[0].upper()
            if name == "UID" and len(words) > 1:
                name = f"UID {words[1].upper()}"
            names.append(name)
        return names

    def commands_named(self, name: str) -> list[str]:
        return [c for c, n in zip(self.commands, self.command_names(), strict=True) if n == name]

    def _record(self, command: str) -> None:
        with self.lock:
            self.commands.append(command)

    def _record_bad(self, command: str) -> None:
        with self.lock:
            self.bad_commands.append(command)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    fake: FakeImapServer


_ARG_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')
_SEARCH_SINCE_RE = re.compile(r"^SINCE (\d{1,2})-([A-Za-z]{3})-(\d{4})$", re.IGNORECASE)
_HEADER_FIELDS_RE = re.compile(
    r"BODY(?P<peek>\.PEEK)?\[HEADER\.FIELDS \((?P<names>[^)]*)\)\]", re.IGNORECASE
)


def _split_args(text: str) -> list[str]:
    """Atoms and quoted strings (backslash escapes resolved)."""
    args = []
    for quoted, atom in _ARG_RE.findall(text):
        args.append(re.sub(r"\\(.)", r"\1", quoted) if not atom else atom)
    return args


def _uid_set(text: str, highest: int) -> set[int]:
    uids: set[int] = set()
    for part in text.split(","):
        lo_text, _, hi_text = part.partition(":")
        lo = highest if lo_text == "*" else int(lo_text)
        hi = lo if not hi_text else (highest if hi_text == "*" else int(hi_text))
        uids.update(range(min(lo, hi), max(lo, hi) + 1))
    return uids


def _filter_header(raw: bytes, names: set[str]) -> bytes:
    """Only the requested header fields (folded lines kept together) plus the empty line."""
    fields: list[bytes] = []
    for line in raw.replace(b"\r\n", b"\n").split(b"\n"):
        if not line:
            break
        if line[:1] in (b" ", b"\t") and fields:
            fields[-1] += b"\r\n" + line
        else:
            fields.append(line)
    kept = [f for f in fields if f.split(b":", 1)[0].decode("ascii", "replace").upper() in names]
    return b"".join(f + b"\r\n" for f in kept) + b"\r\n"


def _internaldate_text(moment: datetime) -> str:
    utc = moment.astimezone(UTC)
    return f"{utc.day:02d}-{_MONTHS[utc.month - 1]}-{utc.year:04d} {utc:%H:%M:%S} +0000"


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def handle(self) -> None:
        fake = self.server.fake
        with fake.lock:
            fake.connections += 1
        if fake.silent:
            fake._stopped.wait(timeout=30)
            return
        self._send("* OK IMAP4rev1 fake server ready")
        authenticated = False
        selected: FakeMailbox | None = None
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            tag, _, rest = line.partition(" ")
            name, _, args = rest.partition(" ")
            name = name.upper()
            recorded = f"LOGIN {_REDACTED}" if name == "LOGIN" else rest
            fake._record(recorded)
            if fake.drop_on is not None and recorded.upper().startswith(fake.drop_on):
                return  # close the connection without an answer
            if name == "CAPABILITY":
                self._send("* CAPABILITY IMAP4rev1")
                self._send(f"{tag} OK CAPABILITY completed")
            elif name == "NOOP":
                self._send(f"{tag} OK NOOP completed")
            elif name == "LOGOUT":
                self._send("* BYE fake server logging out")
                self._send(f"{tag} OK LOGOUT completed")
                return
            elif name == "LOGIN":
                given = _split_args(args)
                if given == [fake.username, fake.password]:
                    authenticated = True
                    self._send(f"{tag} OK LOGIN completed")
                else:
                    self._send(f"{tag} NO [AUTHENTICATIONFAILED] {fake.login_error_text}")
            elif name == "EXAMINE" and authenticated:
                selected = self._examine(tag, args)
            elif name == "UID" and selected is not None:
                self._uid(tag, args, selected)
            else:
                fake._record_bad(recorded)
                self._send(f"{tag} BAD command not recognized or not allowed here")

    # -- commands --------------------------------------------------------------------------------

    def _examine(self, tag: str, args: str) -> FakeMailbox | None:
        fake = self.server.fake
        given = _split_args(args)
        with fake.lock:
            mailbox = fake.mailboxes.get(decode_mutf7(given[0])) if len(given) == 1 else None
            count = len(mailbox.messages) if mailbox else 0
        if mailbox is None:
            self._send(f"{tag} NO [NONEXISTENT] Mailbox does not exist")
            return None
        self._send(f"* {count} EXISTS")
        self._send("* 0 RECENT")
        self._send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self._send(f"* OK [UIDVALIDITY {mailbox.uidvalidity}] UIDs valid")
        self._send(f"{tag} OK [READ-ONLY] EXAMINE completed")
        return mailbox

    def _uid(self, tag: str, args: str, mailbox: FakeMailbox) -> None:
        fake = self.server.fake
        sub, _, sub_args = args.partition(" ")
        sub = sub.upper()
        with fake.lock:
            messages = list(mailbox.messages)
        if sub == "SEARCH":
            match = _SEARCH_SINCE_RE.match(sub_args.strip())
            if match is None:
                fake._record_bad(f"UID {args}")
                self._send(f"{tag} BAD only SEARCH SINCE is supported")
                return
            month = [m.lower() for m in _MONTHS].index(match.group(2).lower()) + 1
            since = datetime(int(match.group(3)), month, int(match.group(1))).date()
            found = [m.uid for m in messages if m.internaldate.astimezone(UTC).date() >= since]
            self._send("* SEARCH" + "".join(f" {uid}" for uid in found))
            self._send(f"{tag} OK SEARCH completed")
        elif sub == "FETCH":
            self._fetch(tag, sub_args, messages)
        else:
            fake._record_bad(f"UID {args}")
            self._send(f"{tag} BAD unsupported UID command")

    def _fetch(self, tag: str, args: str, messages: list[FakeMessage]) -> None:
        fake = self.server.fake
        set_text, _, items = args.partition(" ")
        header_match = _HEADER_FIELDS_RE.search(items)
        try:
            wanted = _uid_set(set_text, max((m.uid for m in messages), default=0))
        except ValueError:
            wanted = set()
        upper = items.upper()
        for seq, message in enumerate(messages, start=1):
            if message.uid not in wanted:
                continue
            if "BODY[" in upper and (header_match is None or header_match.group("peek") is None):
                with fake.lock:  # a real server marks the mail read on a non-PEEK body fetch
                    if "\\Seen" not in message.flags:
                        message.flags.append("\\Seen")
            meta = [f"UID {message.uid}"]
            if "FLAGS" in upper:
                meta.append(f"FLAGS ({' '.join(message.flags)})")
            if "INTERNALDATE" in upper:
                meta.append(f'INTERNALDATE "{_internaldate_text(message.internaldate)}"')
            if "RFC822.SIZE" in upper:
                meta.append(f"RFC822.SIZE {message.rfc822_size}")
            if header_match is None:
                self._send(f"* {seq} FETCH ({' '.join(meta)})")
                continue
            names = header_match.group("names").upper().split()
            literal = _filter_header(message.header, set(names))
            section = f"BODY[HEADER.FIELDS ({' '.join(names)})] {{{len(literal)}}}\r\n".encode()
            if fake.literal_first:
                body = b"* %d FETCH (" % seq + section + literal + b" " + " ".join(meta).encode()
            else:
                body = b"* %d FETCH (" % seq + " ".join(meta).encode() + b" " + section + literal
            self.wfile.write(body + b")\r\n")
        self._send(f"{tag} OK FETCH completed")

    def _send(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8") + b"\r\n")
