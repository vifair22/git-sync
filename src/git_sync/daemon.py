"""Long-running daemon loop.

Runs a fixed set of tasks, each on its own interval, via a single-threaded
polling loop. Sleep between ticks is capped by ``max_idle`` so SIGTERM /
SIGINT interrupts produce a responsive shutdown (signals interrupt
``time.sleep`` on the main thread).

Tasks that raise are logged and isolated; the loop continues to the next
scheduled run. Task runtime is *not* interruptible by the stop flag —
shutdown waits for an in-flight task to finish, which is fine for mirror and
profile passes that can take tens of seconds.
"""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import log

_logger = log.get("git_sync.daemon")


@dataclass
class Task:
    name: str
    interval: timedelta
    run: Callable[[], Any]
    next_due: datetime


def run_loop(
    tasks: list[Task],
    *,
    stop_flag: Callable[[], bool] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    max_idle: timedelta = timedelta(seconds=60),
) -> None:
    stop = stop_flag or (lambda: False)
    if not tasks:
        return
    while not stop():
        current = now()
        for task in tasks:
            if stop():
                return
            if task.next_due <= current:
                _logger.info("task %s: running", task.name)
                try:
                    task.run()
                except Exception as e:  # noqa: BLE001 - per-task isolation
                    _logger.error("task %s raised: %s", task.name, e)
                task.next_due = now() + task.interval
                _logger.info(
                    "task %s: next due at %s",
                    task.name, task.next_due.isoformat(),
                )
        if stop():
            return
        wait = _seconds_until_next(tasks, now(), max_idle)
        _logger.debug("sleeping %.1fs", wait)
        sleep(wait)


def _seconds_until_next(
    tasks: list[Task], current: datetime, max_idle: timedelta,
) -> float:
    earliest = min(t.next_due for t in tasks)
    remaining = (earliest - current).total_seconds()
    return max(0.0, min(remaining, max_idle.total_seconds()))


class SignalStopFlag:
    """Install SIGTERM/SIGINT handlers that flip an internal flag.

    Usable as ``stop_flag`` in :func:`run_loop`; calling the instance returns
    ``True`` once a signal has been received.
    """

    def __init__(self) -> None:
        self._stop = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle)

    def __call__(self) -> bool:
        return self._stop

    def _handle(self, signum: int, _frame: Any) -> None:
        _logger.info("received signal %d, stopping", signum)
        self._stop = True
