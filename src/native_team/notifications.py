"""Wake only the explicitly joined native Codex conversation; never launch agy."""

from pathlib import Path
import logging
import sqlite3
import subprocess
import threading

from .file_locks import acquire_exclusive_nowait


def is_transient_sqlite_lock(error):
    if not isinstance(error, sqlite3.OperationalError):
        return False
    text = str(error).lower()
    return "locked" in text or "busy" in text


def send(executable, notice):
    prompt = (
        "Native Team has a message or work for this enrolled conversation. "
        "Use native-team team_state, then peer_sync for each relevant flat team. "
        "Read pending pages, acknowledge receipt and act on your workstream's next step. "
        "Peers are equals: no permanent coordinator or provider seniority. "
        "Review actual evidence when designated reviewer, handle assigned work, or respond to "
        "an affected peer. For legacy work use its existing review/update tools. "
        "Receipt alone does not complete the work. Continue without a user relay. "
        "If already working, preserve that work and checkpoint progress. "
        "Respect native permissions, pauses and cancellation. Report a genuine blocker. "
        f"Notification reference: {notice['id']}."
    )
    try:
        result = subprocess.run(
            [
                str(executable),
                "queue",
                "--thread",
                notice["native_id"],
                "--message",
                prompt,
                "--resume",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        return (
            "uncertain",
            "Queue timed out; receipt is unknown and this attempt will not be repeated",
        )
    except OSError:
        return "failed", "Native Codex command could not run; check its installed path"
    if result.returncode:
        return "failed", f"Native queue failed with exit code {result.returncode}"
    if "warning" in (result.stdout + result.stderr).lower():
        return (
            "queued_warning",
            "Queue accepted with warning; native receipt remains unverified",
        )
    return "queued", "Native queue accepted; awaiting receipt and next action"


class Dispatcher:
    def __init__(self, store, actor, executable):
        self.store, self.actor, self.executable = store, actor, Path(executable)
        self.stopping = threading.Event()
        # Exception class only (no message text) when the worker stops on an error.
        self.failure = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self, timeout=30):
        self.stopping.set()
        # Finish an in-flight queue attempt and persist its outcome before shutdown.
        self.thread.join(timeout=timeout)

    def run(self):
        # One sender per joined UUID, including during MCP reload overlap.
        with (self.store.state / f"dispatch-{self.actor}.lock").open("a") as lock:
            while not self.stopping.is_set():
                try:
                    acquire_exclusive_nowait(lock)
                    break
                except BlockingIOError:
                    self.stopping.wait(1)
            if self.stopping.is_set():
                return
            lock_failures = 0
            while not self.stopping.is_set():
                try:
                    self.store.recover_notices(self.actor)
                    lock_failures = 0
                    break
                except Exception as error:
                    if is_transient_sqlite_lock(error) and lock_failures < 10:
                        lock_failures += 1
                        logging.warning(
                            "Native Team dispatcher delayed during startup recovery by transient SQLite lock (%d/10)",
                            lock_failures,
                        )
                        self.stopping.wait(min(2 ** min(lock_failures - 1, 3), 10))
                        continue
                    self.failure = type(error).__name__
                    logging.exception(
                        "Native Team dispatcher stopped during recovery; inspect local state"
                    )
                    return
            pending_result = None
            while not self.stopping.is_set():
                try:
                    if pending_result is not None:
                        self.store.notice_result(*pending_result)
                        pending_result = None
                    else:
                        self.store.watch_work(self.actor)
                        notice = self.store.reserve_notice(self.actor)
                        if notice:
                            state, detail = send(self.executable, notice)
                            pending_result = (notice["id"], state, detail)
                            self.store.notice_result(*pending_result)
                            pending_result = None
                    lock_failures = 0
                except Exception as error:
                    if is_transient_sqlite_lock(error) and lock_failures < 10:
                        lock_failures += 1
                        logging.warning(
                            "Native Team dispatcher retrying after transient SQLite lock (%d/10)",
                            lock_failures,
                        )
                        self.stopping.wait(min(2 ** min(lock_failures - 1, 3), 10))
                        continue
                    self.failure = type(error).__name__
                    # No private messages or provider stdout/stderr enter logs.
                    logging.exception(
                        "Native Team dispatcher stopped after an internal error; inspect local state"
                    )
                    return
                self.stopping.wait(1)
            if pending_result is not None:
                try:
                    self.store.notice_result(*pending_result)
                except Exception:
                    logging.warning(
                        "Native Team dispatcher could not persist final notice outcome during shutdown"
                    )
