"""Run the slow part of the pipeline on several workers.

Classifying a document is a network round trip to a model that takes seconds,
and a vault has hundreds of documents. The work is independent, so it fans out;
everything that touches the vault stays on one thread.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Sequence, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
P = TypeVar("P")
R = TypeVar("R")

# More than this and a single ollama instance is queueing rather than working.
MAX_WORKERS = 16


def resolve_workers(requested: int) -> int:
    """How many workers to actually use."""
    if requested <= 0:
        return 1
    return min(requested, MAX_WORKERS)


def parallel_map(
    work: Callable[[T], R],
    items: Sequence[T] | Iterable[T],
    workers: int = 1,
    on_error: Callable[[T, Exception], R] | None = None,
) -> list[R]:
    """Apply `work` to every item, in order, on `workers` threads.

    Results come back in the order the items went in, whatever order they
    finished in, because the caller writes them to the vault in that order and
    a shuffled run would be maddening to read. One item raising does not take
    the rest down: `on_error` turns it into a result, or it is re-raised if the
    caller has no way to represent a failure.
    """
    items = list(items)
    if not items:
        return []

    workers = resolve_workers(workers)
    if workers == 1 or len(items) == 1:
        return [_run_one(work, item, on_error) for item in items]

    # A thread pool rather than processes: the work is a socket read and a
    # subprocess wait, both of which release the GIL, and threads share the
    # cache and the config without pickling anything.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanvault") as pool:
        return list(pool.map(lambda item: _run_one(work, item, on_error), items))


def pipeline(
    items: Sequence[T],
    prepare: Callable[[T], P],
    finish: Callable[[T, P], R],
    workers: int = 1,
    on_error: Callable[[T, Exception], P] | None = None,
) -> list[R]:
    """Take each item all the way through, without waiting for the others.

    `prepare` is the slow half and runs on the workers; `finish` is the half
    that touches shared state and runs on the calling thread, as soon as that
    item's `prepare` lands rather than after every item has finished. So a
    document is written the moment it is ready, memory holds only what is in
    flight, and a run that dies half way has half its work on disk.

    Results come back in input order even though the work does not, because a
    summary that changes order between runs is hard to read.
    """
    items = list(items)
    if not items:
        return []

    workers = resolve_workers(workers)
    if workers == 1:
        return [finish(item, _run_one(prepare, item, on_error)) for item in items]

    results: list[R | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanvault") as pool:
        futures = {
            pool.submit(_run_one, prepare, item, on_error): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            results[index] = finish(items[index], future.result())
    return [result for result in results if result is not None]


def _run_one(
    work: Callable[[T], R], item: T, on_error: Callable[[T, Exception], R] | None
) -> R:
    try:
        return work(item)
    except Exception as exc:
        if on_error is None:
            raise
        log.warning("worker failed on %s: %s", item, exc)
        return on_error(item, exc)


class Progress:
    """A counter several workers log through, and the evidence that they did.

    Wall-clock against the summed time of the individual documents is the only
    honest answer to "is this actually running in parallel?" - the order of the
    log lines is not, because work is submitted in order whatever happens next.
    """

    def __init__(self, total: int, verb: str = "classifying"):
        self.total = total
        self.verb = verb
        self.started_count = 0
        self.finished = 0
        self.worked_seconds = 0.0
        self.started_at = time.monotonic()
        self._lock = threading.Lock()

    def start(self, label: str) -> float:
        with self._lock:
            self.started_count += 1
            position = self.started_count
        log.info("[%d/%d] %s %s", position, self.total, self.verb, label)
        return time.monotonic()

    def finish(self, label: str, started: float) -> None:
        elapsed = time.monotonic() - started
        with self._lock:
            self.finished += 1
            self.worked_seconds += elapsed
            position = self.finished
        log.info("[%d/%d] done %s in %.1fs", position, self.total, label, elapsed)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def concurrency(self) -> float:
        """Documents actually in flight at once, on average."""
        return self.worked_seconds / self.elapsed if self.elapsed > 0 else 0.0

    def summary(self, workers: int) -> str:
        if not self.finished:
            return "nothing to do"
        average = self.worked_seconds / self.finished
        return (
            f"{self.finished} documents in {self.elapsed:.0f}s "
            f"({average:.1f}s each, {self.concurrency:.1f} at a time "
            f"with {workers} worker{'' if workers == 1 else 's'})"
        )
