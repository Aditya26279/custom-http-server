"""A fixed-size worker thread pool with a bounded task queue.

Built directly on ``threading.Condition`` + ``collections.deque`` rather than
``concurrent.futures`` / ``queue.Queue`` so the mechanics are visible.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

log = logging.getLogger("rawhttp.pool")


class PoolClosed(RuntimeError):
    pass


class ThreadPool:
    def __init__(self, num_workers: int = 8, max_queue: int = 0, name: str = "worker"):
        """
        :param num_workers: number of long-lived worker threads.
        :param max_queue: maximum number of pending tasks; 0 means unbounded.
            When the queue is full :meth:`submit` returns False so the caller
            can shed load (the server answers 503).
        """
        if num_workers < 1:
            raise ValueError("num_workers must be >= 1")
        self.num_workers = num_workers
        self.max_queue = max_queue
        self._tasks: deque[tuple[Callable[..., Any], tuple, dict]] = deque()
        self._cond = threading.Condition()
        self._shutdown = False
        self._active = 0
        self._completed = 0
        self._threads = [
            threading.Thread(target=self._worker, name=f"{name}-{i}", daemon=True)
            for i in range(num_workers)
        ]
        for t in self._threads:
            t.start()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Queue ``fn(*args, **kwargs)``. Returns False if the queue is full."""
        with self._cond:
            if self._shutdown:
                raise PoolClosed("cannot submit to a pool that is shut down")
            if self.max_queue and len(self._tasks) >= self.max_queue:
                return False
            self._tasks.append((fn, args, kwargs))
            self._cond.notify()
            return True

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._tasks and not self._shutdown:
                    self._cond.wait()
                if not self._tasks:  # shutdown requested and queue drained
                    return
                fn, args, kwargs = self._tasks.popleft()
                self._active += 1
            try:
                fn(*args, **kwargs)
            except Exception:  # a task must never kill its worker
                log.exception("unhandled exception in pool task")
            finally:
                with self._cond:
                    self._active -= 1
                    self._completed += 1

    def shutdown(self, wait: bool = True, timeout: float | None = None) -> None:
        """Stop accepting work; workers drain remaining tasks and exit."""
        with self._cond:
            self._shutdown = True
            self._cond.notify_all()
        if wait:
            # One overall deadline; joining each thread with the full timeout
            # could otherwise take num_workers * timeout.
            deadline = None if timeout is None else time.monotonic() + timeout
            for t in self._threads:
                t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))

    def stats(self) -> dict[str, int]:
        with self._cond:
            return {
                "workers": self.num_workers,
                "active": self._active,
                "queued": len(self._tasks),
                "completed": self._completed,
            }
