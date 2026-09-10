"""Run the slow part of the pipeline on several workers.

Classifying a document is a network round trip to a model that takes seconds,
and a vault has hundreds of documents. The work is independent, so it fans out;
everything that touches the vault stays on one thread.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Sequence, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
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
    """A counter several workers can log through without interleaving badly."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self._lock = threading.Lock()

    def start(self, label: str) -> None:
        with self._lock:
            self.done += 1
            position = self.done
        log.info("[%d/%d] classifying %s", position, self.total, label)
