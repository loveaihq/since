"""The daemon: the only long-running writer of events.

``Daemon.tick`` runs every source that is due (schedule elapsed since its last attempt), writes a
heartbeat to the ``meta`` table and prunes expired data at most hourly. ``Daemon.run`` loops over
``tick`` (or, with ``once=True``, runs every source once). The clock and the sleep function are
injected so tests never wait. Nothing here calls an LLM.

Meta keys owned by the daemon: ``daemon_pid``, ``daemon_heartbeat_at``, ``daemon_min_schedule_s``
(read by ``since status`` / the digest header to judge whether the heartbeat is stale) and, via
``Store.prune``, ``last_pruned_at``.
"""

from __future__ import annotations

import os
import re
import signal
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import FrameType
from typing import TextIO

from since.collect import register_sources, run_collection
from since.config import Config, SourceConfig
from since.sources import Collector
from since.store import Store
from since.timeutil import from_iso, to_iso

META_PID = "daemon_pid"
META_HEARTBEAT = "daemon_heartbeat_at"
META_MIN_SCHEDULE = "daemon_min_schedule_s"
META_LAST_PRUNED = "last_pruned_at"

MAX_SLEEP_S = 5  # upper bound of one sleep between ticks
REFUSE_WINDOW_S = 30  # another daemon's heartbeat younger than this blocks start()
PRUNE_INTERVAL_S = 3600

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")

NowFn = Callable[[], datetime]
SleepFn = Callable[[float], None]


class DaemonError(Exception):
    """The daemon cannot start (e.g. another daemon is already running)."""


def _one_line(text: str) -> str:
    """Log-safe text: control characters and line breaks become single spaces."""
    return _CONTROL_RE.sub(" ", text).strip()


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


@contextmanager
def _sigterm_ends_loop() -> Iterator[None]:
    """POSIX: turn SIGTERM into the same stop path as Ctrl-C while the daemon runs (open
    collection transactions roll back). No-op on Windows and outside the main thread."""
    if sys.platform == "win32":
        yield
        return
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)


class Daemon:
    """Scheduler over the sources of ``config``.

    ``collectors`` (source type -> collector) replaces the built-in registry (tests).
    ``log`` receives one line per notable run; ``pid`` identifies this daemon in the meta table.
    """

    def __init__(
        self,
        config: Config,
        store: Store,
        now_fn: NowFn,
        sleep_fn: SleepFn,
        collectors: Mapping[str, Collector] | None = None,
        log: TextIO | None = None,
        pid: int | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._collectors = collectors
        self._log: TextIO = sys.stderr if log is None else log
        self._pid = os.getpid() if pid is None else pid
        self._collectable: list[tuple[SourceConfig, Collector]] | None = None
        # In-memory back-off for runs that raised out of run_collection (a store error): nothing
        # was written, so the stored state would call the source due again on the very next tick.
        self._retry_at: dict[str, datetime] = {}

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        """Register the sources and announce this daemon (pid, shortest schedule, heartbeat).

        Raises :class:`DaemonError` if another pid's heartbeat is younger than 30 seconds; in
        that case nothing is written. Sources whose type has no collector yet are skipped with a
        warning on the log."""
        now = self._now_fn()
        with self._store.transaction():  # check-and-claim is atomic against a racing daemon
            self._refuse_if_other_daemon(now)
            registration = register_sources(self._store, self._config, self._collectors)
            collectable = sorted(registration.collectable, key=lambda item: item[0].id)
            self._store.set_meta(META_PID, self._pid)
            self._store.set_meta(
                META_MIN_SCHEDULE,
                min((cfg.schedule_s for cfg, _ in collectable), default=None),
            )
            self._store.set_meta(META_HEARTBEAT, to_iso(now))
        self._collectable = collectable
        self._retry_at.clear()
        for source_id, reason in registration.skipped:
            self._warn(f"source '{source_id}' skipped: {reason}")
        if not collectable:
            self._warn("no collectable sources configured; only the heartbeat will be written")

    def _refuse_if_other_daemon(self, now: datetime) -> None:
        other = self._store.get_meta(META_PID)
        if other is None or other == str(self._pid):
            return
        heartbeat = self._store.get_meta(META_HEARTBEAT)
        if heartbeat is None:
            return
        try:
            age = (now - from_iso(heartbeat)).total_seconds()
        except ValueError:
            return  # unreadable heartbeat: treat as stale
        if abs(age) < REFUSE_WINDOW_S:
            raise DaemonError(
                f"another daemon is running (pid {other}, heartbeat {max(0, int(age))}s ago)"
            )

    def run(self, once: bool = False) -> int:
        """Start, then serve until stopped (Ctrl-C, or SIGTERM on POSIX). ``once=True`` runs every
        source a single time regardless of schedule and returns. Always returns 0 after a stop;
        errors other than a stop request propagate (after the cleanup)."""
        try:
            with _sigterm_ends_loop():
                self.start()
                if once:
                    self._run_once()
                else:
                    self._loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._clear_meta()
        return 0

    def _run_once(self) -> None:
        now = self._now_fn()
        self._run_sources(self._started(), now)
        self._finish_tick(now)

    def _loop(self) -> None:
        while True:
            self.tick(self._now_fn())
            self._sleep_fn(self._seconds_until_next_due(self._now_fn()))

    def _clear_meta(self) -> None:
        """Remove our heartbeat and pid. Left alone if another daemon owns the meta now (e.g.
        it took over after this process was suspended) or if start() never claimed it."""
        with self._store.transaction():
            if self._store.get_meta(META_PID) == str(self._pid):
                self._store.set_meta(META_HEARTBEAT, None)
                self._store.set_meta(META_PID, None)

    # -- one tick ----------------------------------------------------------------------------

    def tick(self, now: datetime) -> list[str]:
        """Run every due source (ordered by due time, then id), write the heartbeat and prune if
        the last prune is an hour old. Returns the ids that were run (attempted). A source that
        fails is logged and never stops the others."""
        due = [
            (due_at, cfg, collector)
            for cfg, collector in self._started()
            if (due_at := self._due_at(cfg, now)) <= now
        ]
        due.sort(key=lambda item: (item[0], item[1].id))
        ran = self._run_sources([(cfg, collector) for _, cfg, collector in due], now)
        self._finish_tick(now)
        return ran

    def _started(self) -> list[tuple[SourceConfig, Collector]]:
        if self._collectable is None:
            raise DaemonError("daemon not started; call start() first")
        return self._collectable

    def _run_sources(
        self, items: Sequence[tuple[SourceConfig, Collector]], now: datetime
    ) -> list[str]:
        ran: list[str] = []
        for cfg, collector in items:
            ran.append(cfg.id)
            try:
                result = run_collection(self._store, cfg, collector, now)
            except Exception as exc:  # store error etc.; the run was rolled back
                self._retry_at[cfg.id] = now + timedelta(seconds=cfg.schedule_s)
                detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                self._say(now, cfg.id, f"run failed: {detail}")
                continue
            self._retry_at.pop(cfg.id, None)
            if result.error is not None:
                self._say(now, cfg.id, f"collection failed: {result.error}")
            elif result.seqs:
                self._say(now, cfg.id, f"{len(result.seqs)} events")
        return ran

    def _finish_tick(self, now: datetime) -> None:
        self._store.set_meta(META_HEARTBEAT, to_iso(now))
        if self._prune_due(now):
            before = now - timedelta(days=self._config.retention_days)
            self._store.prune(before, now)

    def _prune_due(self, now: datetime) -> bool:
        last = self._store.get_meta(META_LAST_PRUNED)
        if last is None:
            return True
        try:
            return (now - from_iso(last)).total_seconds() >= PRUNE_INTERVAL_S
        except ValueError:
            return True

    # -- scheduling --------------------------------------------------------------------------

    def _due_at(self, cfg: SourceConfig, now: datetime) -> datetime:
        """When the source is next due: last attempt (success or error, from the stored state)
        plus its schedule; ``now`` if it was never attempted."""
        state = self._store.get_source_state(cfg.id)
        attempts = (
            [from_iso(s) for s in (state.last_success_at, state.last_error_at) if s]
            if state is not None
            else []
        )
        due = max(attempts) + timedelta(seconds=cfg.schedule_s) if attempts else now
        retry = self._retry_at.get(cfg.id)
        return retry if retry is not None and retry > due else due

    def _seconds_until_next_due(self, now: datetime) -> float:
        """Sleep length: until the next source is due, at most 5s, never negative."""
        sources = self._started()
        if not sources:
            return float(MAX_SLEEP_S)
        soonest = min(self._due_at(cfg, now) for cfg, _ in sources)
        return max(0.0, min(float(MAX_SLEEP_S), (soonest - now).total_seconds()))

    # -- log ---------------------------------------------------------------------------------

    def _say(self, now: datetime, source_id: str, text: str) -> None:
        print(f"{to_iso(now)} {source_id}: {_one_line(text)}", file=self._log, flush=True)

    def _warn(self, text: str) -> None:
        print(f"warning: {_one_line(text)}", file=self._log, flush=True)
