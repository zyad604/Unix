# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Run callables on Blender's main thread from worker threads.

``bpy`` is not thread-safe, and MCP requests arrive on HTTP worker threads. A worker
calls ``submit(fn)`` and blocks; the main thread calls ``pump()`` from a
``bpy.app.timers`` callback (or a test loop) and runs queued jobs inside a time
budget so the UI stays responsive. No ``bpy`` import here.

A job whose function returns a generator is run a slice at a time: each ``pump`` advances
it until its budget is spent, then gives the main thread back to Blender, which redraws
and answers the user before the next slice. While it runs, other jobs wait in the queue,
so a tool that is half-way through (render settings changed, a temporary camera in the
scene) is never interleaved with another one. The generator yields where it may be paused;
``yield WAIT`` means it has nothing to do until later (a render job running on its own
thread) and ends the slice at once.
"""

from __future__ import annotations

import collections
import queue
import threading
import time
from types import GeneratorType
from typing import Any, Callable, Deque, Generator, List, Optional, Tuple

# A job that holds the main thread longer than this is a stall: the window froze for it.
STALL_S = 0.1
# Yielded by a sliced job that is waiting (for a render job, a file): end this slice now.
WAIT = "wait"


_slicing = [0]       # > 0 while the dispatcher is advancing a sliced job (see in_slices)


def in_slices() -> bool:
    """Is the code running now a slice of a sliced job, which the main thread leaves between slices? Only
    then may it start work that finishes on Blender's own (a render job) and wait for it with WAIT: run to
    the end at once (``drain``), the main thread would be waiting on itself."""
    return _slicing[0] > 0


class DispatchTimeout(Exception):
    """The main thread did not pick the job up in time."""


class Steps:
    """A generator run a slice at a time. ``advance`` runs it until ``deadline`` (at least one
    step), until it yields ``WAIT``, or until it returns; the return value is ``result``."""

    def __init__(self, gen: Generator) -> None:
        self.gen = gen
        self.result: Any = None
        self.done = False
        self.waiting = False

    def advance(self, deadline: float) -> bool:
        while True:
            try:
                signal = next(self.gen)
            except StopIteration as stop:
                self.result, self.done = stop.value, True
                return True
            self.waiting = signal == WAIT
            if self.waiting or time.monotonic() >= deadline:
                return False

    def close(self) -> None:
        self.gen.close()


def drain(gen: Generator) -> Any:
    """Run a sliced job to the end at once (on the main thread, when nothing else can pump:
    background mode, tests, or a caller that needs the answer before it can go on)."""
    steps = Steps(gen)
    outer, _slicing[0] = _slicing[0], 0
    try:
        while not steps.advance(float("inf")):
            if steps.waiting:
                time.sleep(0.02)
    finally:
        _slicing[0] = outer
    return steps.result


class _Job:
    __slots__ = ("fn", "done", "result", "error", "cancelled", "label", "steps", "thread", "started")

    def __init__(self, fn: Callable[[], Any], label: str = ""):
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.cancelled = False
        self.label = label or getattr(fn, "label", "") or getattr(fn, "__qualname__", "job")
        self.steps: Optional[Steps] = None
        self.thread = threading.get_ident()
        self.started = 0.0


class MainThreadDispatcher:
    def __init__(self) -> None:
        self._jobs: "queue.Queue[_Job]" = queue.Queue()
        self._main_ident = threading.get_ident()
        self._active: Optional[_Job] = None       # the sliced job that has the main thread
        # Main-thread stalls, newest last: (what, seconds, wall-clock time). A slice of a sliced
        # job counts on its own, so a long job that gives the thread back is not a stall.
        self.stalls: Deque[Tuple[str, float, float]] = collections.deque(maxlen=200)

    def bind_main_thread(self) -> None:
        """Call once from the main thread (add-on register)."""
        self._main_ident = threading.get_ident()

    def submit(self, fn: Callable[[], Any], timeout: float = 300.0) -> Any:
        """Run ``fn`` on the main thread and return its result (re-raising its error).
        If ``fn`` returns a generator, it is run in slices and its return value is the result."""
        if threading.get_ident() == self._main_ident:
            out = fn()
            return drain(out) if isinstance(out, GeneratorType) else out
        job = _Job(fn)
        self._jobs.put(job)
        if not job.done.wait(timeout):
            job.cancelled = True  # the pump skips it, or closes it between two slices
            raise DispatchTimeout(f"Blender's main thread was busy for {timeout:.0f}s")
        if job.error is not None:
            raise job.error
        return job.result

    def spawn(self, gen: Generator, label: str = "") -> _Job:
        """Queue a sliced job nobody waits for (work after a turn). Set ``cancelled`` on the
        returned job to stop it at its next slice; its ``finally`` blocks still run."""
        job = _Job(lambda: gen, label or "background job")
        self._jobs.put(job)
        return job

    def pump(self, budget_s: float = 0.05) -> int:
        """Main thread: run queued jobs until the budget is spent. Returns jobs run (slices count).

        At least one job (or one step of a sliced job) always runs, even if it alone exceeds
        the budget. While a sliced job is unfinished, nothing else is started."""
        deadline = time.monotonic() + budget_s
        ran = 0
        while True:
            job = self._active
            if job is None:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if job.cancelled:
                    job.done.set()
                    continue
                job.started = time.monotonic()
                try:
                    out = job.fn()
                except BaseException as exc:  # delivered to the waiting worker
                    job.error = exc
                    out = None
                ran += 1
                if job.error is None and isinstance(out, GeneratorType):
                    job.steps, self._active = Steps(out), job
                else:
                    job.result = out
                    self._finished(job, job.started)
            if self._active is not None:
                ran += 1
                if not self._slice(job, deadline):
                    break
            if time.monotonic() >= deadline:
                break
        return ran

    def _slice(self, job: _Job, deadline: float) -> bool:
        """Advance the active sliced job. Returns True when it is over."""
        began = time.monotonic()
        _slicing[0] += 1
        try:
            if job.cancelled:
                job.steps.close()
                over = True
            else:
                over = job.steps.advance(deadline)
                if over:
                    job.result = job.steps.result
        except BaseException as exc:
            job.error, over = exc, True
        finally:
            _slicing[0] -= 1
        took = time.monotonic() - began
        if took > STALL_S:
            self._stall(job.label, took)
        if over:
            self._active = None
            job.done.set()
            if job.error is not None and job.thread == self._main_ident:
                print(f"[Unix] {job.label} failed: {job.error!r}")     # nobody waits for a spawned job
        return over

    def _finished(self, job: _Job, began: float) -> None:
        took = time.monotonic() - began
        if took > STALL_S:
            self._stall(job.label, took)
        job.done.set()

    def _stall(self, label: str, seconds: float) -> None:
        self.stalls.append((label, seconds, time.time()))
        if seconds > 2.0:
            print(f"[Unix] the main thread was busy for {seconds:.1f}s: {label}")

    def worst_stalls(self, since: float = 0.0, count: int = 10) -> List[Tuple[str, float, float]]:
        return sorted((s for s in self.stalls if s[2] >= since), key=lambda s: -s[1])[:count]

    @property
    def waiting(self) -> bool:
        """A sliced job is waiting on work outside the main thread (a render job on the same GPU)."""
        active = self._active
        return active is not None and active.steps is not None and active.steps.waiting

    @property
    def pending(self) -> int:
        """Work waiting for the main thread: queued jobs, plus a sliced job that is not just
        waiting on something else (the timer should come back soon for its next slice)."""
        active = self._active
        return self._jobs.qsize() + (1 if active is not None and not active.steps.waiting else 0)
