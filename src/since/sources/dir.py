"""``dir`` source: every file under a directory is one record, keyed by its relative POSIX path.

Options (``SourceConfig.options``):

- ``path`` (required): root directory. ``~`` is expanded; the result must be absolute, because a
  relative path would resolve differently in the daemon and in ``since collect`` (different working
  directories) and cause mass false added/removed events.
- ``include``: glob patterns, default ``["**/*"]``; a file is collected if it matches any of them...
- ``exclude``: ...and none of these (default ``[]``). A directory that an exclude pattern ending in
  ``/**`` covers entirely (``.git/**``, ``**/build/**``) is not descended into at all, so
  nothing below it is read or can fail the run.
- ``max_text_bytes``: files up to this size that are valid UTF-8 without NUL bytes are stored as
  ``text``; every other file is stored as ``sha256`` (hex). Default 65536.

Glob patterns are matched (case-sensitively, on every platform) against the file's path relative to
the root, written with ``/``. ``*`` and ``?`` never match ``/``; ``[abc]`` / ``[a-c]`` / ``[!abc]``
character classes work; ``**`` as a whole path segment matches any number of directories, including
none (so ``**/*.md`` matches ``a.md`` and ``x/y/a.md``, and ``docs/**`` matches everything below
``docs``). ``*`` also matches leading dots (dotfiles are ordinary files here), and ``\\`` is an
ordinary character, not an escape.

Fields: ``size`` (bytes) plus ``text`` or ``sha256``. There is deliberately no mtime, so merely
touching a file never creates an event.

Symlinked files are read like any other file; symlinked directories are not followed. Files that are
not regular files (FIFOs, sockets, devices) are ignored, as are files whose relative path cannot be
encoded as UTF-8 (they could never be stored).

Unreadable things (D5): a directory that cannot be listed, or a missing/non-directory root, fails
the whole run with a ``CollectError`` (its contents are unknown; skipping it would look like a
removal). A *file* that exists but cannot be opened or read (permission denied, sharing violation
because another program holds it open, ...) is reported in ``CollectOutput.unavailable``: the runner
keeps its last known record, so it neither produces an event nor blocks the other files. A file that
vanishes between listing and reading (``FileNotFoundError``, also a dangling symlink) is simply not
included: it really is gone. Nothing here ever puts file content in an error message.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from since.config import ConfigError, SourceConfig
from since.model import Record, Scalar
from since.sources import CollectError, CollectOutput

DEFAULT_INCLUDE = ("**/*",)
DEFAULT_MAX_TEXT_BYTES = 65536

_KNOWN_OPTIONS = ("path", "include", "exclude", "max_text_bytes")

# Read size when hashing / buffering a file.
_CHUNK = 1 << 20


# -- glob matching -------------------------------------------------------------------------------


def _translate_segment(seg: str) -> str:
    """Regex for one path segment (no ``/`` inside): ``*``, ``?``, ``[...]``, literals."""
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        i += 1
        if c == "*":
            while i < n and seg[i] == "*":  # "**" inside a segment is just "*"
                i += 1
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i
            if j < n and seg[j] == "!":
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:  # no closing bracket: a literal "["
                out.append(r"\[")
                continue
            stuff = seg[i:j]
            i = j + 1
            negate = stuff.startswith("!")
            if negate:
                stuff = stuff[1:]
            stuff = re.sub(r"[\\\[\]&~|^]", lambda m: "\\" + m.group(), stuff)
            # A negated class must not match "/" (it would span path segments).
            out.append("[" + ("^/" if negate else "") + stuff + "]")
        else:
            out.append(re.escape(c))
    return "".join(out)


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a glob (see the module docstring) to a regex for ``fullmatch`` on a relative
    POSIX path. ``ValueError`` if the pattern is not valid (e.g. a reversed range ``[z-a]``)."""
    segments = pattern.split("/")
    parts: list[str] = []
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")  # zero or more directories
            continue
        parts.append(_translate_segment(seg))
        if not last:
            parts.append("/")
    try:
        return re.compile("".join(parts), re.DOTALL)
    except re.error as exc:
        raise ValueError(f"invalid glob pattern {pattern!r}: {exc}") from None


def glob_match(pattern: str, path: str) -> bool:
    """True if the relative POSIX ``path`` matches ``pattern``."""
    return compile_glob(pattern).fullmatch(path) is not None


# -- options -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Options:
    path: str  # as configured (used in messages)
    root: Path  # ``path`` with ``~`` expanded; absolute
    include: list[re.Pattern[str]]
    exclude: list[re.Pattern[str]]
    prune: list[re.Pattern[str]]  # directories that ``exclude`` covers entirely
    max_text_bytes: int


def _bad(cfg: SourceConfig, key: str, message: str) -> ConfigError:
    return ConfigError(f"source '{cfg.id}': key '{key}': {message}")


def _patterns(
    cfg: SourceConfig, key: str, default: tuple[str, ...]
) -> list[tuple[str, re.Pattern[str]]]:
    """The validated pattern list of ``key`` as ``(pattern, compiled)`` pairs."""
    raw = cfg.options[key] if key in cfg.options else list(default)
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise _bad(cfg, key, "must be a list of glob patterns (strings)")
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for pattern in raw:
        try:
            compiled.append((pattern, compile_glob(pattern)))
        except ValueError as exc:
            raise _bad(cfg, key, str(exc)) from None
    return compiled


def _prune_patterns(exclude: list[tuple[str, re.Pattern[str]]]) -> list[re.Pattern[str]]:
    """Patterns that match the relative path of a directory whose whole subtree is excluded.

    ``P/**`` excludes every file below any directory that ``P`` matches (and a bare ``**``
    excludes everything below the root), so those directories need not be walked."""
    prune: list[re.Pattern[str]] = []
    for pattern, compiled in exclude:
        if pattern == "**":
            prune.append(compiled)  # matches every directory
        elif pattern.endswith("/**"):
            prune.append(compile_glob(pattern[: -len("/**")]))
    return prune


def _parse_options(cfg: SourceConfig) -> _Options:
    """Check ``cfg.options`` (no I/O); ``ConfigError`` naming the offending key."""
    for key in cfg.options:
        if key not in _KNOWN_OPTIONS:
            accepted = ", ".join(_KNOWN_OPTIONS)
            raise _bad(cfg, str(key), f"unknown option (dir sources accept: {accepted})")
    path = cfg.options.get("path")
    if not isinstance(path, str) or not path.strip():
        raise _bad(cfg, "path", "required, must be a non-empty string")
    try:
        root = Path(path).expanduser()
    except RuntimeError as exc:  # "~user" that does not exist
        raise _bad(cfg, "path", f"cannot expand '~': {exc}") from None
    if not root.is_absolute():
        raise _bad(cfg, "path", "must be an absolute path (or start with ~)")
    include = _patterns(cfg, "include", DEFAULT_INCLUDE)
    exclude = _patterns(cfg, "exclude", ())
    max_text = cfg.options.get("max_text_bytes", DEFAULT_MAX_TEXT_BYTES)
    if isinstance(max_text, bool) or not isinstance(max_text, int) or max_text < 0:
        raise _bad(cfg, "max_text_bytes", "must be an integer >= 0")
    return _Options(
        path=path,
        root=root,
        include=[c for _, c in include],
        exclude=[c for _, c in exclude],
        prune=_prune_patterns(exclude),
        max_text_bytes=max_text,
    )


# -- reading -------------------------------------------------------------------------------------


def _read_fields(path: str, max_text_bytes: int) -> dict[str, Scalar]:
    """Read one file (the only place file content is read; may raise ``OSError``).

    Files of at most ``max_text_bytes`` that are valid UTF-8 without NUL bytes give ``text``
    (decoded as is, newlines untouched); everything else gives ``sha256`` of the raw bytes."""
    size = 0
    buf = bytearray()
    hasher = None  # created once the file turns out to be larger than max_text_bytes
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            size += len(chunk)
            if hasher is not None:
                hasher.update(chunk)
                continue
            buf += chunk
            if len(buf) > max_text_bytes:
                hasher = hashlib.sha256(buf)
                buf = bytearray()
    if hasher is not None:
        return {"size": size, "sha256": hasher.hexdigest()}
    if b"\x00" not in buf:
        try:
            return {"size": size, "text": buf.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    return {"size": size, "sha256": hashlib.sha256(buf).hexdigest()}


def _encodable(name: str) -> bool:
    """False for names with lone surrogates (undecodable bytes): they could never be stored."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


class DirCollector:
    type_name = "dir"

    def validate(self, cfg: SourceConfig) -> None:
        _parse_options(cfg)

    def key_label(self, cfg: SourceConfig) -> str:
        return ""

    def collect(self, cfg: SourceConfig) -> CollectOutput:
        opts = _parse_options(cfg)
        root = opts.root
        if not root.is_dir():
            # Lead with the cause: digests cap messages at 120 chars and the path can be long.
            if root.exists():
                raise CollectError(f'root is not a directory: "{opts.path}"')
            raise CollectError(f'root directory not found: "{opts.path}"')

        def walk_error(exc: OSError) -> None:
            # os.walk would silently skip an unreadable directory; its files would then look
            # removed, so fail the run instead.
            try:
                rel = Path(str(exc.filename)).relative_to(root).as_posix()
            except ValueError:
                rel = "."
            where = "the root directory" if rel == "." else f'directory "{rel}"'
            raise CollectError(f"cannot list {where}: {_reason(exc)}") from exc

        records: list[Record] = []
        unavailable: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=walk_error):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            prefix = "" if rel_dir == "." else rel_dir + "/"
            # Walk only into directories that can be stored and are not excluded entirely.
            dirnames[:] = sorted(
                d
                for d in dirnames
                if _encodable(d) and not any(p.fullmatch(prefix + d) for p in opts.prune)
            )
            for name in sorted(filenames):
                rel = prefix + name
                if not _encodable(rel):
                    continue
                if not any(p.fullmatch(rel) for p in opts.include):
                    continue
                if any(p.fullmatch(rel) for p in opts.exclude):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if not stat.S_ISREG(os.stat(full).st_mode):  # follows symlinks
                        continue
                    fields = _read_fields(full, opts.max_text_bytes)
                except FileNotFoundError:
                    continue  # vanished after the listing (or a dangling symlink): really gone
                except OSError:
                    # Present but unreadable right now (locked, no permission, ...): keep its
                    # last known record instead of failing the run or reporting a removal.
                    unavailable.append(rel)
                    continue
                records.append(Record.make(rel, fields))
        records.sort(key=lambda r: r.key)
        return CollectOutput(records, sorted(unavailable))
