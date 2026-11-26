"""Keep durable native Codex wake-up independent of MCP connection lifetime."""

import logging
import os
import signal
import threading
import time

from .file_locks import acquire_exclusive_nowait
from .notifications import Dispatcher, is_transient_sqlite_lock
from .outbox import Outbox
from .store import Store, TeamError
from .quota_collector import QuotaCollector


def running(state):
    path = state / "monitor.lock"
    if not path.exists():
        return False
    mode = "r+b" if os.name == "nt" else "r"
    try:
        with path.open(mode) as lock:
            try:
                acquire_exclusive_nowait(lock)
                return False
            except BlockingIOError:
                return True
    except OSError:
        return None


def serve(state, executable):
    store = Store(state)
    outbox = Outbox(store)
    stopping = threading.Event()
    workers = {}
    quota = QuotaCollector(store)
    with (store.state / "monitor.lock").open("a") as lock:
        try:
            acquire_exclusive_nowait(lock)
        except BlockingIOError:
            raise TeamError(
                "A Native Team notification monitor is already running"
            ) from None
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stopping.set())
        try:
            quota.start()
            lock_failures = 0
            while not stopping.is_set():
                if any(not thread.is_alive() for thread in quota.threads):
                    raise TeamError(
                        "A native quota worker stopped; inspect service diagnostics"
                    )
                try:
                    active = outbox.recipients()
                    lock_failures = 0
                except Exception as error:
                    if is_transient_sqlite_lock(error) and lock_failures < 10:
                        lock_failures += 1
                        logging.warning(
                            "Native Team monitor retrying recipient scan after transient SQLite lock (%d/10)",
                            lock_failures,
                        )
                        stopping.wait(min(2 ** min(lock_failures - 1, 3), 10))
                        continue
                    raise
                for actor in active:
                    if actor not in workers:
                        workers[actor] = Dispatcher(outbox, actor, executable)
                        workers[actor].start()
                    elif not workers[actor].thread.is_alive():
                        raise TeamError(
                            "A notification worker stopped; inspect the service log before recovery"
                        )
                for actor in set(workers) - active:
                    workers.pop(actor).close()
                stopping.wait(1)
        finally:
            # All workers stop concurrently; the service has a 35-second stop budget.
            deadline = time.monotonic() + 30
            quota.stopping.set()
            for worker in workers.values():
                worker.stopping.set()
            quota.close()
            for worker in workers.values():
                worker.close(timeout=max(0, deadline - time.monotonic()))
