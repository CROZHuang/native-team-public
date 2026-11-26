"""Keep durable native Codex wake-up independent of MCP connection lifetime."""

import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time
import uuid

from .file_locks import acquire_exclusive_nowait
from .notifications import Dispatcher, is_transient_sqlite_lock
from .outbox import Outbox
from .quota_capture import atomic_json
from .store import Store, TeamError
from .quota_collector import QuotaCollector

HEARTBEAT = "monitor-heartbeat.json"
HEARTBEAT_INTERVAL = 30
HEARTBEAT_STALE = 120
RESTART_BASE = 5
RESTART_MAX = 300
RESTART_RESET = 600
INSTANCE_PREFIX = "monitor-instance-"
PROBE_LOCK = "monitor-probe.lock"
PROBE_ATTEMPTS = 20
PROBE_DELAY = 0.01


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


def _open_existing(path):
    return Path(path).open("r+b" if os.name == "nt" else "r")


def instance_held(path):
    """True while a monitor lifetime holds this companion lock, False once free.

    Probes are serialized through monitor-probe.lock, which only readers take,
    so one reader's momentary probe can never be mistaken for the writer by
    another. If the probe slot is busy or missing, or the probe itself fails,
    the answer is None, which is never treated as ownership.
    """
    try:
        slot = _open_existing(Path(path).parent / PROBE_LOCK)
    except OSError:
        return None
    with slot:
        for attempt in range(PROBE_ATTEMPTS):
            try:
                acquire_exclusive_nowait(slot)
                break
            except BlockingIOError:
                time.sleep(PROBE_DELAY)
            except OSError:
                return None
        else:
            return None
        try:
            with _open_existing(path) as stream:
                try:
                    acquire_exclusive_nowait(stream)
                    return False
                except BlockingIOError:
                    return True
        except FileNotFoundError:
            return False
        except OSError:
            return None


class Instance:
    """A companion lock held only inside one monitor's monitor.lock lifetime.

    A heartbeat names its instance; readers trust the beat only while that
    companion is still held, so a beat can never outlive the lock that wrote it.
    """

    def __init__(self, state):
        # Readers open the probe slot but never create it; the writer provides it.
        Path(state, PROBE_LOCK).open("a").close()
        self.id = uuid.uuid4().hex
        self.path = Path(state) / f"{INSTANCE_PREFIX}{self.id}.lock"
        self.stream = self.path.open("a")
        try:
            acquire_exclusive_nowait(self.stream)
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        self.stream.close()
        try:
            self.path.unlink()
        except OSError:
            # A reader may have it open on Windows; an unheld leftover is harmless.
            pass


def sweep_instances(state, keep):
    """Remove companions of earlier lifetimes that no process holds."""
    for path in Path(state).glob(f"{INSTANCE_PREFIX}*.lock"):
        if path != keep and instance_held(path) is False:
            try:
                path.unlink()
            except OSError:
                pass


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _count(value, minimum=0):
    return type(value) is int and value >= minimum


def _short_text(value):
    return isinstance(value, str) and 0 < len(value) <= 100


def _valid_heartbeat(value):
    if not isinstance(value, dict) or not _count(value.get("version"), 1) or value["version"] != 1:
        return False
    if not (_number(value.get("updated")) and _number(value.get("started_at"))):
        return False
    if not _count(value.get("scan_retries", 0)):
        return False
    instance = value.get("instance")
    if not (isinstance(instance, str) and len(instance) == 32 and all(c in "0123456789abcdef" for c in instance)):
        return False
    backoff = value.get("backoff")
    return isinstance(backoff, list) and len(backoff) <= 1000 and all(
        isinstance(entry, dict)
        and _short_text(entry.get("member"))
        and _count(entry.get("restarts"), 1)
        and _count(entry.get("due_in"))
        and _short_text(entry.get("error"))
        for entry in backoff
    )


def heartbeat(state, now=None):
    """Read the monitor heartbeat without trusting it more than it proves.

    A missing, unreadable, malformed or older-format file is never evidence of a
    hang. A well-formed beat only speaks for the current lock holder while its
    instance companion lock is still held; a beat from an earlier lifetime, or a
    legacy holder without companions, leaves readers on the lock alone.
    """
    path = Path(state) / HEARTBEAT
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("Heartbeat exceeds its size limit")
        value = json.loads(raw)
        if not _valid_heartbeat(value):
            raise ValueError("Malformed heartbeat")
    except FileNotFoundError:
        return {"state": "missing", "attributed": False}
    except (OSError, ValueError):
        return {"state": "unreadable", "attributed": False}
    now = time.time() if now is None else now
    age = max(0.0, now - value["updated"])
    companion = Path(state) / f"{INSTANCE_PREFIX}{value['instance']}.lock"
    return {
        "state": "stale" if age > HEARTBEAT_STALE else "fresh",
        "attributed": instance_held(companion) is True,
        "age": age,
        "backoff": len(value["backoff"]),
        "scan_retries": value.get("scan_retries", 0),
    }


def summary(state, now=None):
    """running() keeps its True/False/None contract; status refines a held lock."""
    state = Path(state)
    active = running(state)
    result = {"running": active, "status": "unknown", "heartbeat_age": None, "backoff": 0}
    if active is False:
        result["status"] = "stopped"
    elif active is True:
        beat = heartbeat(state, now)
        result["status"] = "running"
        # Only a beat from the current holder refines the lock; anything else is ambiguous.
        if beat["attributed"]:
            result["heartbeat_age"] = round(beat["age"])
            result["backoff"] = beat["backoff"]
            if beat["state"] == "stale":
                result["status"] = "unresponsive"
            elif beat["backoff"] or beat["scan_retries"]:
                result["status"] = "degraded"
    return result


class WorkerPool:
    """One Dispatcher per active recipient; a stopped one restarts with backoff.

    A failing recipient never stops delivery to the others. Pause, removal and
    shutdown cancel a pending restart. A restarted Dispatcher recovers its own
    unfinished sends as uncertain before sending anything new.
    """

    def __init__(self, factory):
        self.factory = factory
        self.workers = {}
        self.pending = {}
        self.restarts = {}

    def reconcile(self, active, now):
        changed = False
        for actor in list(self.workers):
            worker, started = self.workers[actor]
            if actor not in active:
                # Pause or removal ends this recipient's restart history as well.
                del self.workers[actor]
                self.restarts.pop(actor, None)
                worker.close()
                changed = True
            elif not worker.thread.is_alive():
                del self.workers[actor]
                count = 1 if now - started >= RESTART_RESET else self.restarts.get(actor, 0) + 1
                self.restarts[actor] = count
                delay = min(RESTART_BASE * 2 ** (count - 1), RESTART_MAX)
                error = getattr(worker, "failure", None) or "stopped"
                self.pending[actor] = {"restarts": count, "due": now + delay, "error": error}
                logging.warning(
                    "Native Team notification worker stopped (%s); restart %d in %d s",
                    error,
                    count,
                    delay,
                )
                changed = True
        for actor in list(self.pending):
            if actor not in active:
                del self.pending[actor]
                self.restarts.pop(actor, None)
                changed = True
        for actor in active:
            if actor in self.workers:
                continue
            entry = self.pending.get(actor)
            if entry and now < entry["due"]:
                continue
            if self.pending.pop(actor, None):
                changed = True
            worker = self.factory(actor)
            worker.start()
            self.workers[actor] = (worker, now)
        return changed

    def backoff(self, now):
        return [
            {
                "member": actor,
                "restarts": entry["restarts"],
                "due_in": max(0, round(entry["due"] - now)),
                "error": entry["error"],
            }
            for actor, entry in sorted(self.pending.items())
        ]

    def close(self, deadline):
        self.pending.clear()
        for worker, _ in self.workers.values():
            worker.stopping.set()
        for worker, _ in self.workers.values():
            worker.close(timeout=max(0, deadline - time.monotonic()))


class Heartbeat:
    """Publish liveness for readers; a write failure never stops delivery."""

    def __init__(self, state, instance, interval=HEARTBEAT_INTERVAL):
        self.path = Path(state) / HEARTBEAT
        self.instance = instance
        self.interval = interval
        self.started = time.time()
        self.written = None
        self.warned = False

    def beat(self, pool, scan_retries=0, force=False):
        if self.instance is None:
            return False  # lock-only mode: an unattributable beat would prove nothing
        now = time.time()
        if not force and self.written is not None and now - self.written < self.interval:
            return False
        value = {
            "version": 1,
            "instance": self.instance,
            "started_at": self.started,
            "updated": now,
            "workers": len(pool.workers),
            "backoff": pool.backoff(time.monotonic()),
            "scan_retries": scan_retries,
        }
        try:
            atomic_json(self.path, value)
        except (OSError, ValueError) as error:
            if not self.warned:
                logging.warning(
                    "Native Team monitor heartbeat could not be written (%s); delivery continues",
                    type(error).__name__,
                )
                self.warned = True
            return False
        self.written, self.warned = now, False
        return True


def scan_recipients(outbox, stopping, failures):
    """Return (recipients, failures). A transient lock is retried, never fatal."""
    try:
        return outbox.recipients(), 0
    except Exception as error:
        if not is_transient_sqlite_lock(error):
            raise
        failures += 1
        if failures in (1, 10) or failures % 60 == 0:
            logging.warning(
                "Native Team monitor retrying recipient scan after transient SQLite lock (%d)",
                failures,
            )
        stopping.wait(min(2 ** min(failures - 1, 3), 10))
        return None, failures


def serve(state, executable):
    store = Store(state)
    outbox = Outbox(store)
    stopping = threading.Event()
    pool = WorkerPool(lambda actor: Dispatcher(outbox, actor, executable))
    quota = QuotaCollector(store)
    with (store.state / "monitor.lock").open("a") as lock:
        try:
            acquire_exclusive_nowait(lock)
        except BlockingIOError:
            raise TeamError(
                "A Native Team notification monitor is already running"
            ) from None
        # The companion is taken after, and released before, monitor.lock. It only
        # serves diagnostics: if it cannot be created, delivery runs lock-only.
        instance = None
        try:
            instance = Instance(store.state)
        except OSError as error:
            logging.warning(
                "Native Team monitor heartbeat companion unavailable (%s); delivery continues with lock-only status",
                type(error).__name__,
            )
        try:
            if instance is not None:
                try:
                    sweep_instances(store.state, instance.path)
                except OSError:
                    pass
            pulse = Heartbeat(store.state, instance.id if instance else None)
            pulse.beat(pool, force=True)
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stopping.set())
            try:
                quota.start()
                failures = 0
                while not stopping.is_set():
                    pulse.beat(pool, failures)
                    if any(not thread.is_alive() for thread in quota.threads):
                        raise TeamError(
                            "A native quota worker stopped; inspect service diagnostics"
                        )
                    active, failures = scan_recipients(outbox, stopping, failures)
                    if active is None:
                        continue
                    if pool.reconcile(active, time.monotonic()):
                        pulse.beat(pool, failures, force=True)
                    stopping.wait(1)
            finally:
                # All workers stop concurrently; the service has a 35-second stop budget.
                deadline = time.monotonic() + 30
                quota.stopping.set()
                for worker, _ in pool.workers.values():
                    worker.stopping.set()
                quota.close()
                pool.close(deadline)
        finally:
            if instance is not None:
                instance.close()
