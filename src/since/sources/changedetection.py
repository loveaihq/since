"""``changedetection`` source: every watch of a changedetection.io instance is one record.

Options (``SourceConfig.options``)::

    url: http://localhost:5000     # required: base URL of the instance (http or https)
    api_key_env: CD_API_KEY        # optional: env var holding the API key, sent as ``x-api-key``
    tag: Suppliers                 # optional: only watches with this tag (tag *name*)
    fetch_text: true               # default true: add the latest snapshot text as field ``text``
    timeout_s: 30                  # default 30, 1-300: per network operation

Uses the changedetection.io REST API (checked against its OpenAPI spec)::

    GET {url}/api/v1/watch[?tag=<name>]   -> {uuid: {url, title, last_changed, last_error, ...}}
    GET {url}/api/v1/watch/{uuid}/history/latest   -> latest snapshot as text/plain, 404 = none

Key = watch uuid. Fields: ``url`` (the raw watch URL), ``title`` (the watch title, else its
page_title, else its URL; D22), ``last_changed`` (ISO UTC, ``None`` when unknown/never),
``last_error`` (string, ``""`` when there is none) and ``text`` (latest snapshot, only when
``fetch_text`` and there is one; a watch without history simply has no ``text``). ``last_checked``
and ``viewed`` are deliberately not fields: they change without the watched page changing and would
flood the digest (D22).

Safety: the API key is read from the environment at collect time only, never appears in a message
(everything that leaves ``collect`` is scrubbed of it), and redirects are never followed so the key
cannot be forwarded to another host. Error messages carry the request *path* only, never the query
string. Snapshot text is cut at ``MAX_TEXT_BYTES`` and a list response larger than
``MAX_LIST_BYTES`` is an error, so a misbehaving server cannot exhaust memory. Nothing here calls an
LLM; stdlib ``urllib`` only.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from since.config import ConfigError, SourceConfig
from since.model import Record, Scalar
from since.sources import CollectError
from since.timeutil import to_iso

MAX_LIST_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024

DEFAULT_TIMEOUT_S = 30
_MIN_TIMEOUT_S = 1
_MAX_TIMEOUT_S = 300

_ALLOWED_OPTIONS = ("url", "api_key_env", "tag", "fetch_text", "timeout_s")
_REDACTED = "***"
_LIST_PATH = "/api/v1/watch"


# -- options -------------------------------------------------------------------------------------


def _bad(cfg: SourceConfig, key: str, message: str) -> ConfigError:
    return ConfigError(f"source '{cfg.id}': key '{key}': {message}")


def _check_url(cfg: SourceConfig, value: Any) -> str:
    """The base URL, without a trailing ``/``. No I/O."""
    if not isinstance(value, str) or not value.strip():
        raise _bad(cfg, "url", "required, must be a non-empty string")
    url = value.strip()
    if any(c.isspace() or not c.isprintable() for c in url):
        raise _bad(cfg, "url", "must not contain whitespace or control characters")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        raise _bad(cfg, "url", "is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _bad(cfg, "url", "must be an http:// or https:// URL with a host")
    if "@" in parts.netloc:
        raise _bad(cfg, "url", "must not contain credentials (use api_key_env)")
    if parts.query or parts.fragment:
        raise _bad(cfg, "url", "must be the base URL of the instance, without ? or #")
    return url.rstrip("/")


def _parse_options(cfg: SourceConfig) -> tuple[str, str | None, str | None, bool, int]:
    """Check ``cfg.options`` (no I/O). Returns ``(base_url, api_key_env, tag, fetch_text,
    timeout_s)``; ``ConfigError`` naming the offending key."""
    options = cfg.options
    for name in sorted(options, key=str):
        if name not in _ALLOWED_OPTIONS:
            raise _bad(
                cfg,
                str(name),
                "unknown option for a changedetection source "
                f"(allowed: {', '.join(_ALLOWED_OPTIONS)})",
            )
    base_url = _check_url(cfg, options.get("url"))

    api_key_env: str | None = None
    if "api_key_env" in options:
        api_key_env = options["api_key_env"]
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            raise _bad(cfg, "api_key_env", "must be a non-empty string (an env var name)")
    tag: str | None = None
    if "tag" in options:
        tag = options["tag"]
        if not isinstance(tag, str) or not tag.strip():
            raise _bad(cfg, "tag", "must be a non-empty string (a tag name)")
    fetch_text = options.get("fetch_text", True)
    if not isinstance(fetch_text, bool):
        raise _bad(cfg, "fetch_text", "must be true or false")
    timeout_s = options.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, int)
        or not _MIN_TIMEOUT_S <= timeout_s <= _MAX_TIMEOUT_S
    ):
        raise _bad(
            cfg, "timeout_s", f"must be an integer from {_MIN_TIMEOUT_S} to {_MAX_TIMEOUT_S}"
        )
    return base_url, api_key_env, tag, fetch_text, timeout_s


# -- HTTP ----------------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would forward ``x-api-key`` to the redirect target."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _scrub(message: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            message = message.replace(secret, _REDACTED)
    return message


def _reason_text(exc: BaseException) -> str:
    """A short, single-line reason for a network failure."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    text = str(exc).strip() or type(exc).__name__
    return " ".join(text.split())


class _HttpStatus(Exception):
    """A non-auth HTTP error status. ``collect`` turns it into a ``CollectError``; the snapshot
    fetch treats 404 as "no history"."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"HTTP {status} from {path}")
        self.status = status


class _Api:
    """GET requests against one changedetection.io instance."""

    def __init__(self, base_url: str, api_key: str | None, timeout_s: int) -> None:
        self._base = base_url
        self._api_key = api_key
        self._timeout = timeout_s
        self._secrets = [api_key] if api_key else []
        self._opener = urllib.request.build_opener(_NoRedirect)

    def scrub(self, message: str) -> str:
        return _scrub(message, self._secrets)

    def get(
        self, path: str, query: Mapping[str, str] | None, max_bytes: int, *, truncate: bool
    ) -> tuple[bytes, str]:
        """GET ``path`` (+ ``query``); returns ``(body, charset)``. ``max_bytes`` bounds the body:
        larger is cut when ``truncate`` else an error. Raises ``CollectError`` (message: path only,
        never the query string or the API key) and ``_HttpStatus`` for a status >= 300 the caller
        may want to handle (404)."""
        url = self._base + path
        if query:
            url += "?" + urlencode(query, quote_via=quote)
        headers = {"User-Agent": "since", "Accept": "*/*"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(max_bytes + 1)
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise CollectError("API key rejected") from None
            raise _HttpStatus(status, path) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            reason = _reason_text(exc)
            if reason == "timed out":
                raise CollectError(f"request to {path} timed out after {self._timeout}s") from None
            raise CollectError(self.scrub(f"connection failed: {reason}")) from None
        if len(body) > max_bytes:
            if not truncate:
                raise CollectError(f"response from {path} is too large")
            body = body[:max_bytes]
        return body, charset


# -- fields --------------------------------------------------------------------------------------


def _iso_or_none(value: Any) -> str | None:
    """Epoch seconds -> ISO UTC; ``0``, negatives, missing or unusable values -> ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return to_iso(datetime.fromtimestamp(value, UTC))
    except (OverflowError, OSError, ValueError):
        return None


def _fields(entry: Mapping[str, Any]) -> dict[str, Scalar]:
    raw_url = entry.get("url")
    url = raw_url if isinstance(raw_url, str) else ""
    # label: the watch's own title, else the page's HTML title, else the URL
    names = (entry.get("title"), entry.get("page_title"))
    title = next((t for t in names if isinstance(t, str) and t.strip()), url)
    last_error = entry.get("last_error")
    return {
        "url": url,
        "title": title,
        "last_changed": _iso_or_none(entry.get("last_changed")),
        "last_error": last_error if isinstance(last_error, str) else "",
    }


def _decode_text(body: bytes, charset: str) -> str:
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# -- collector -----------------------------------------------------------------------------------


class ChangedetectionCollector:
    type_name = "changedetection"

    def validate(self, cfg: SourceConfig) -> None:
        """Check the options. No I/O: the env var is read and the instance contacted only in
        ``collect``."""
        _parse_options(cfg)

    def key_label(self, cfg: SourceConfig) -> str:
        return ""

    def default_title_fields(self, cfg: SourceConfig) -> list[str]:
        return ["title"]

    def collect(self, cfg: SourceConfig) -> list[Record]:
        try:
            base_url, api_key_env, tag, fetch_text, timeout_s = _parse_options(cfg)
        except ConfigError as exc:
            raise CollectError(str(exc)) from None

        api_key: str | None = None
        if api_key_env is not None:
            api_key = os.environ.get(api_key_env, "").strip()
            if not api_key:
                raise CollectError(f"environment variable {api_key_env} is not set (or is empty)")
            if not (api_key.isascii() and api_key.isprintable()):
                # http.client would echo the value in its own error: refuse it up front.
                raise CollectError(f"environment variable {api_key_env} holds an invalid API key")
        api = _Api(base_url, api_key, timeout_s)
        try:
            return self._collect(api, tag, fetch_text)
        except CollectError as exc:
            raise CollectError(api.scrub(str(exc))) from None
        except Exception as exc:
            raise CollectError(api.scrub(f"{type(exc).__name__}: {exc}")) from None

    def _collect(self, api: _Api, tag: str | None, fetch_text: bool) -> list[Record]:
        entries = self._list_watches(api, tag)
        records: list[Record] = []
        for uuid in sorted(entries):
            fields = _fields(entries[uuid])
            if fetch_text:
                text = self._latest_text(api, uuid)
                if text is not None:
                    fields["text"] = text
            records.append(Record.make(uuid, fields))
        return records

    def _list_watches(self, api: _Api, tag: str | None) -> dict[str, Mapping[str, Any]]:
        try:
            body, _charset = api.get(
                _LIST_PATH, {"tag": tag} if tag else None, MAX_LIST_BYTES, truncate=False
            )
        except _HttpStatus as exc:
            raise CollectError(str(exc)) from None
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:  # JSONDecodeError and UnicodeDecodeError; never echo the content
            raise CollectError(f"invalid JSON from {_LIST_PATH}") from None
        if not isinstance(data, dict):
            raise CollectError(f"unexpected response from {_LIST_PATH}: expected a JSON object")
        for uuid, entry in data.items():
            if not isinstance(uuid, str) or not uuid:
                raise CollectError(f"unexpected response from {_LIST_PATH}: invalid watch id")
            if not isinstance(entry, dict):
                raise CollectError(f"unexpected response from {_LIST_PATH}: watch is not an object")
        return data

    def _latest_text(self, api: _Api, uuid: str) -> str | None:
        """The latest snapshot text, or ``None`` when the watch has no history (404)."""
        path = f"{_LIST_PATH}/{quote(uuid, safe='')}/history/latest"
        try:
            body, charset = api.get(path, None, MAX_TEXT_BYTES, truncate=True)
        except _HttpStatus as exc:
            if exc.status == 404:
                return None
            raise CollectError(str(exc)) from None
        return _decode_text(body, charset)
