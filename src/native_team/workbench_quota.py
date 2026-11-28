"""Local client readings for the workbench, independent of membership and scheduling."""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from .claude_quota import MAX_INPUT, read_private_metadata, status_windows
from .file_locks import acquire_exclusive_nowait
from .quota_capture import agy_metadata, atomic_json, finite, pseudonym, text
from .quota_native import capture_directory, codex_metadata
from .quota_store import FRESH_SECONDS, applicable


def _snapshot_path(state, provider):
    return Path(state) / "quota" / f"workbench-{provider}.json"


def _read_history(path):
    try:
        value = read_private_metadata(path, 524288)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("sessions"), dict):
        raise ValueError("Invalid quota history")
    return value["sessions"]


def capture_statusline(state, provider, payload, stamp):
    if provider not in ("agy", "claude") or not isinstance(payload, dict):
        return
    session = text(payload.get("conversation_id") or payload.get("session_id"), 160)
    if not session:
        return
    state = Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = capture_directory(state)
    key = (directory / "identity.key").read_bytes()
    model = payload.get("model")
    model = (
        text(model.get("id") or model.get("display_name"))
        if isinstance(model, dict)
        else ""
    )
    if provider == "agy":
        native = agy_metadata(payload, key)
        source = native["account_key"] or native["session_key"]
        windows = native["windows"]
    else:
        source = pseudonym(key, "claude-session", session)
        windows = list(status_windows(payload))
    path = _snapshot_path(state, provider)
    lock = path.with_suffix(".lock")
    if lock.is_symlink():
        return
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as stream:
        acquire_exclusive_nowait(stream)
        sessions = _read_history(path)
        previous = sessions.get(source, {})
        if stamp < previous.get("received_at", 0):
            return
        history = previous.get("history", {})
        for window in windows:
            bucket = window["bucket"]
            value = {
                k: window[k]
                for k in ("remaining", "amount", "resets_at", "disabled")
                if k in window
            }
            old = history.get(bucket, {})
            if all(
                window.get(k) is None for k in ("remaining", "amount", "resets_at")
            ) and not window.get("disabled"):
                continue
            observed = (
                old.get("observed_at", stamp) if old.get("value") == value else stamp
            )
            history[bucket] = {"value": value, "observed_at": observed}
            window["observed_at"] = observed
        if (
            windows == previous.get("windows")
            and model == previous.get("model")
            and stamp - previous["received_at"] < 15
        ):
            return
        history = dict(
            sorted(
                history.items(), key=lambda pair: pair[1]["observed_at"], reverse=True
            )[:64]
        )
        sessions[source] = {
            "received_at": stamp,
            "model": model,
            "windows": windows,
            "history": history,
        }
        # Native status lines can represent several accounts or sessions over time.
        # Keep each bucket's observation time across switches without keeping raw identities.
        recent = sorted(
            sessions.items(), key=lambda pair: pair[1]["received_at"], reverse=True
        )[:8]
        atomic_json(path, {"sessions": dict(recent)})


def statusline_stream(source, state, provider, renderer):
    """Forward original bytes to the existing renderer, including oversized or invalid payloads."""
    stamp = time.time()
    child = (
        subprocess.Popen(
            renderer,
            stdin=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if renderer
        else None
    )
    data = bytearray()
    try:
        for chunk in iter(lambda: source.read(65536), b""):
            if data is not None:
                if len(data) + len(chunk) <= MAX_INPUT:
                    data.extend(chunk)
                else:
                    data = None
            if child and not child.stdin.closed:
                try:
                    child.stdin.write(chunk)
                except BrokenPipeError:
                    try:
                        child.stdin.close()
                    except BrokenPipeError:
                        pass
    finally:
        if child:
            try:
                child.stdin.close()
            except BrokenPipeError:
                pass
            child.wait()
    if data:
        try:
            capture_statusline(state, provider, json.loads(data), stamp)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass  # A missing reading must never interrupt the user's native status line.
    return child.returncode if child else 0


def read_codex_windows(command=None, timeout=10):
    """Read the CLI account's rate-limit metadata over its native stdio protocol."""
    if command is None:
        binary = (
            (shutil.which("codex.exe") or shutil.which("codex.cmd"))
            if os.name == "nt"
            else shutil.which("codex")
        )
        if not binary:
            raise FileNotFoundError("Codex CLI unavailable")
        command = [binary, "app-server", "--listen", "stdio://"]
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=Path.home(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    replies = queue.Queue()
    deadline = time.monotonic() + timeout

    def receive():
        try:
            for _ in range(64):
                line = child.stdout.readline(262145)
                if not line or len(line) > 262144:
                    break
                reply = json.loads(line)
                if isinstance(reply, dict) and reply.get("id") in (1, 2):
                    replies.put(reply)
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            replies.put(None)

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()

    def send(message):
        child.stdin.write(json.dumps(message).encode() + b"\n")
        child.stdin.flush()

    def request(seq, method, params):
        send({"id": seq, "method": method, "params": params})
        try:
            reply = replies.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("Quota query timed out") from None
        if (
            not reply
            or reply.get("id") != seq
            or not isinstance(reply.get("result"), dict)
        ):
            raise ValueError("Native quota unavailable")
        return reply["result"]

    try:
        request(
            1,
            "initialize",
            {"clientInfo": {"name": "native_team_workbench", "version": "0.1.0"}},
        )
        send({"method": "initialized"})
        result = request(
            2, "account/rateLimits/read", {"excludeResetCreditDetails": True}
        )
        return codex_metadata(result, os.urandom(32))["windows"]
    finally:
        try:
            child.stdin.close()
        except BrokenPipeError:
            pass
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        reader.join(timeout=1)
        child.stdout.close()


def project_windows(provider, model, windows, now):
    result = []
    for window in windows:
        bucket = text(window.get("bucket"))
        if not bucket:
            continue
        observed = window.get("observed_at")
        reset = window.get("resets_at")
        result.append(
            {
                "bucket": bucket,
                "remaining": window.get("remaining")
                if finite(window.get("remaining"))
                else None,
                "amount": window.get("amount")
                if finite(window.get("amount"))
                else None,
                "resets_at": reset if finite(reset) else None,
                "observed_at": observed if finite(observed) else None,
                "disabled": window.get("disabled") is True,
                "applies": applicable(window, provider, model),
                "stale": not finite(observed)
                or not 0 <= now - observed < FRESH_SECONDS
                or (finite(reset) and reset <= now),
            }
        )
    return result


class WorkbenchQuota:
    def __init__(self, state, codex_reader=read_codex_windows):
        self.state = state
        self.codex_reader = codex_reader
        self.lock = threading.Lock()
        self.checked = None
        self.windows = []
        self.status = "idle"

    def read(self, refresh=False):
        # Coalesce concurrent requests and throttle repeated clicks across browser tabs.
        with self.lock:
            if refresh and (
                self.checked is None or time.monotonic() - self.checked >= 15
            ):
                try:
                    windows = self.codex_reader()
                    stamp = time.time()
                    self.windows = [dict(w, observed_at=stamp) for w in windows]
                    self.status = "ok" if windows else "empty"
                except (OSError, ValueError, TypeError, KeyError, RecursionError):
                    self.status = "unavailable"
                self.checked = time.monotonic()
            now = time.time()
            codex = project_windows("codex", "", self.windows, now)
            if self.status == "unavailable":
                for window in codex:
                    window["stale"] = True
            providers = {"codex": {"status": self.status, "windows": codex}}
        for provider in ("agy", "claude"):
            try:
                history = _read_history(_snapshot_path(self.state, provider))
                latest = max(
                    history.values(), key=lambda s: s["received_at"], default={}
                )
                model = text(latest.get("model"))
                windows = project_windows(
                    provider, model, latest.get("windows", []), now
                )
                providers[provider] = {
                    "status": "ok" if windows else "empty",
                    "model": model,
                    "windows": windows,
                }
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                providers[provider] = {"status": "unavailable", "windows": []}
        return {"read_at": now, "providers": providers}
