"""The database proxy's own thread pool. It rebuilds itself after shutdown
(so a second app lifespan never meets a pool that refuses work), and it knows
how many of its threads are really busy - including one still finishing a
query whose request already timed out - so new requests can be turned away
straight away instead of queueing behind them."""

import logging
import threading
from concurrent.futures import Executor, Future, ThreadPoolExecutor

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import Disabled

logger = logging.getLogger(LOGGER_NAME)


class RenewableExecutor(Executor):
    def __init__(self, max_workers: int) -> None:
        self._max_workers = max_workers
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()   # submit() and done callbacks run on different threads
        self._in_flight = 0

    def has_room(self) -> bool:
        try:
            with self._lock:
                return self._in_flight < self._max_workers
        except Exception:
            logger.warning("database proxy capacity check failed")
            return False

    def submit(self, fn, /, *args, **kwargs) -> Future:
        try:
            with self._lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="codelab-db")
                pool = self._pool
                self._in_flight += 1
            try:
                future = pool.submit(fn, *args, **kwargs)
            except BaseException:
                self._finished(None)
                raise
            future.add_done_callback(self._finished)
            return future
        except RuntimeError as exc:
            logger.error("database proxy thread pool refused work")
            raise Disabled(temporary=True) from exc

    def _finished(self, future) -> None:
        try:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)
        except Exception:
            logger.warning("database proxy thread accounting failed")

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        try:
            with self._lock:
                pool, self._pool = self._pool, None
            if pool is not None:
                pool.shutdown(wait=wait, cancel_futures=cancel_futures)
        except Exception:
            logger.warning("database proxy thread pool shutdown failed", exc_info=True)
