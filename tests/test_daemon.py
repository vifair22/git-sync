"""Tests for the scheduling loop."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from git_sync.daemon import SignalStopFlag, Task, _seconds_until_next, run_loop


class _Clock:
    """Deterministic clock. advance() moves time forward."""

    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def _start_time() -> datetime:
    return datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_runs_task_when_due_then_reschedules():
    calls: list[int] = []

    def step():
        calls.append(1)

    clock = _Clock(_start_time())
    stop_state = {"iters": 0}

    task = Task(
        name="t",
        interval=timedelta(hours=1),
        run=step,
        next_due=clock.t,  # due immediately
    )

    def sleep(seconds):
        # Advance the clock by the slept duration and stop after 2 iterations.
        clock.advance(seconds)
        stop_state["iters"] += 1

    def stop():
        return stop_state["iters"] >= 2

    run_loop(
        [task],
        stop_flag=stop,
        now=clock.now,
        sleep=sleep,
        max_idle=timedelta(seconds=60),
    )

    # Task ran once (first iteration); second iteration sleeps until next_due.
    assert calls == [1]
    assert task.next_due == _start_time() + timedelta(hours=1)


def test_task_not_run_before_due():
    calls: list[int] = []
    clock = _Clock(_start_time())

    task = Task(
        name="t",
        interval=timedelta(hours=1),
        run=lambda: calls.append(1),
        next_due=clock.t + timedelta(minutes=30),
    )

    stop_state = {"iters": 0}

    def sleep(seconds):
        clock.advance(seconds)
        stop_state["iters"] += 1

    run_loop(
        [task], stop_flag=lambda: stop_state["iters"] >= 1,
        now=clock.now, sleep=sleep,
        max_idle=timedelta(seconds=60),
    )

    assert calls == []


def test_task_runs_again_after_interval_elapses():
    calls: list[int] = []
    clock = _Clock(_start_time())
    stop_state = {"iters": 0}

    task = Task(
        name="t",
        interval=timedelta(minutes=30),
        run=lambda: calls.append(1),
        next_due=clock.t,
    )

    def sleep(seconds):
        # Big fake "sleep" to cross the next interval each iteration.
        clock.advance(max(seconds, 60 * 30))
        stop_state["iters"] += 1

    run_loop(
        [task], stop_flag=lambda: stop_state["iters"] >= 3,
        now=clock.now, sleep=sleep,
        max_idle=timedelta(minutes=30),
    )

    assert len(calls) >= 2


def test_task_error_is_isolated_and_rescheduled():
    calls: list[int] = []
    clock = _Clock(_start_time())
    stop_state = {"iters": 0}

    def flaky():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    task = Task(
        name="flaky",
        interval=timedelta(seconds=10),
        run=flaky,
        next_due=clock.t,
    )

    def sleep(seconds):
        clock.advance(max(seconds, 10))
        stop_state["iters"] += 1

    run_loop(
        [task], stop_flag=lambda: stop_state["iters"] >= 2,
        now=clock.now, sleep=sleep,
        max_idle=timedelta(seconds=10),
    )

    assert calls == [1, 2]  # first errored, loop continued, second ran


def test_stop_flag_prevents_loop_entry():
    calls: list[int] = []

    task = Task(
        name="t", interval=timedelta(hours=1),
        run=lambda: calls.append(1), next_due=_start_time(),
    )
    run_loop([task], stop_flag=lambda: True)

    assert calls == []


def test_empty_tasks_returns_immediately():
    run_loop([])  # no sleep called, no error


def test_multiple_tasks_run_in_order_when_all_due():
    calls: list[str] = []
    clock = _Clock(_start_time())

    a = Task(
        name="a", interval=timedelta(hours=1),
        run=lambda: calls.append("a"), next_due=clock.t,
    )
    b = Task(
        name="b", interval=timedelta(hours=2),
        run=lambda: calls.append("b"), next_due=clock.t,
    )

    stop_state = {"iters": 0}

    def sleep(_):
        stop_state["iters"] += 1

    run_loop(
        [a, b], stop_flag=lambda: stop_state["iters"] >= 1,
        now=clock.now, sleep=sleep,
    )

    assert calls == ["a", "b"]


def test_sleep_capped_by_max_idle():
    clock = _Clock(_start_time())
    task = Task(
        name="t", interval=timedelta(hours=24),
        run=lambda: None,
        next_due=clock.t + timedelta(hours=10),  # 10h away
    )

    wait = _seconds_until_next(
        [task], clock.now(), max_idle=timedelta(seconds=60),
    )
    assert wait == 60.0  # capped


def test_sleep_returns_remaining_when_below_cap():
    clock = _Clock(_start_time())
    task = Task(
        name="t", interval=timedelta(hours=1),
        run=lambda: None,
        next_due=clock.t + timedelta(seconds=30),
    )

    wait = _seconds_until_next(
        [task], clock.now(), max_idle=timedelta(seconds=60),
    )
    assert wait == 30.0


def test_sleep_never_negative():
    clock = _Clock(_start_time())
    task = Task(
        name="t", interval=timedelta(hours=1),
        run=lambda: None,
        next_due=clock.t - timedelta(hours=1),  # overdue
    )

    wait = _seconds_until_next(
        [task], clock.now(), max_idle=timedelta(seconds=60),
    )
    assert wait == 0.0


def test_signal_stop_flag(monkeypatch):
    installed = []

    def fake_signal(sig, handler):
        installed.append((sig, handler))

    monkeypatch.setattr("signal.signal", fake_signal)

    flag = SignalStopFlag()
    assert flag() is False

    # Simulate a signal firing.
    handler = installed[0][1]
    handler(15, None)  # SIGTERM
    assert flag() is True
