"""Keep durable native Codex wake-up independent of MCP connection lifetime."""

import signal
import threading
import time

from .file_locks import acquire_exclusive_nowait
from .notifications import Dispatcher
from .outbox import Outbox
from .store import Store, TeamError
from .quota_collector import QuotaCollector


def running(state):
    path = state / "monitor.lock"
    if not path.exists():
        return False
    with path.open("r") as lock:
        try:
            acquire_exclusive_nowait(lock)
            return False
        except BlockingIOError:
            return True


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
            while not stopping.is_set():
                if any(not thread.is_alive() for thread in quota.threads):
                    raise TeamError(
                        "A native quota worker stopped; inspect service diagnostics"
                    )
                active = outbox.recipients()
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
