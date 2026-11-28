"""Claude Code subscription observations from the owner's native status line."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .quota_capture import atomic_json, finite, pseudonym, read_json, text, timestamp
from .store import conversation_uuid


SOURCE = "Claude Code native statusLine"
WINDOWS = {"five_hour": "5 小时", "seven_day": "7 天"}
VISIBLE_MEMBERS = """(EXISTS (
    SELECT 1 FROM peer_members p JOIN peer_teams t ON t.id=p.team_id
    WHERE p.member_id=m.id AND p.state='active' AND t.state IN ('active','paused'))
    OR (m.provider='claude' AND NOT EXISTS (
        SELECT 1 FROM peer_members p WHERE p.member_id=m.id)))"""
TARGETS = f"""SELECT m.id,m.native_id FROM members m
    WHERE m.provider='claude' AND {VISIBLE_MEMBERS} AND NOT EXISTS (
        SELECT 1 FROM members other WHERE other.provider='claude'
        AND other.native_id=m.native_id AND other.id!=m.id)"""
MAX_INPUT = 131072


def status_windows(payload):
    raw = payload.get("rate_limits")
    raw = raw if isinstance(raw, dict) else {}
    for bucket in WINDOWS:
        value = raw.get(bucket)
        used = value.get("used_percentage") if isinstance(value, dict) else None
        reset = value.get("resets_at") if isinstance(value, dict) else None
        yield {
            "bucket": bucket,
            "remaining": 100 - used if finite(used) and 0 <= used <= 100 else None,
            "resets_at": timestamp(reset) if finite(reset) else None,
        }


def capture(payload, state, *, now=None):
    """Match the callback UUID before storing anything. A repaint is not a fresh quota reading."""
    stamp = time.time() if now is None else now
    if (
        os.name != "posix"
        or not isinstance(payload, dict)
        or not payload.get("session_id")
    ):
        return
    import fcntl

    session = conversation_uuid("claude", payload["session_id"])
    state = Path(state)
    directory = state / "quota"
    config = read_json(directory / "config.json", 4096)
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return
    # Only this UUID is queried. A locked or unavailable ledger must not delay the renderer.
    uri = (state / "team.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.05)) as db:
        member = db.execute(TARGETS + " AND m.native_id=?", (session,)).fetchone()
    if member is None:
        return
    key = (directory / "identity.key").read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid local quota identity key")
    digest = pseudonym(key, "claude-session", session)
    path = directory / f"claude-{digest}.json"
    # Concurrent repaints cannot replace a newer observation with an older callback.
    fd = os.open(
        path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # A later native repaint can observe a contended update.
        try:
            old = read_observation(path, member[0], digest)
        except (FileNotFoundError, ValueError):
            old = {}
        if stamp < old.get("received_at", 0):
            return
        model = payload.get("model")
        model_id = text(model.get("id")) if isinstance(model, dict) else ""
        history = old.get("last_windows")
        last = {
            k: history[k]
            for k in WINDOWS
            if isinstance(history, dict) and isinstance(history.get(k), dict)
        }
        windows = []
        for window in status_windows(payload):
            bucket, remaining, reset = (
                window["bucket"],
                window["remaining"],
                window["resets_at"],
            )
            if remaining is not None or reset is not None:
                prior = last.get(bucket, {})
                same = (remaining, reset) == (
                    prior.get("remaining"),
                    prior.get("resets_at"),
                )
                previous_time = prior.get("observed_at")
                window["observed_at"] = (
                    previous_time
                    if same and finite(previous_time) and 0 <= previous_time <= stamp
                    else stamp
                )
                last[bucket] = window
            windows.append(window)
        meta = {
            "provider": "claude",
            "source": SOURCE,
            "account_key": None,
            "windows": windows,
        }
        if (
            old.get("metadata") == meta
            and old.get("model") == model_id
            and stamp - old["received_at"] < 15
        ):
            return
        observed = max(
            (w["observed_at"] for w in windows if "observed_at" in w),
            default=old.get("observed_at", stamp),
        )
        atomic_json(
            path,
            {
                "member_id": member[0],
                "session_key": digest,
                "model": model_id,
                "metadata": meta,
                "last_windows": last,
                "observed_at": observed,
                "received_at": stamp,
            },
        )


def forward_statusline(source, destination, state):
    """Forward unchanged bytes to the configured renderer, even on invalid or oversized input."""
    if destination.isatty():
        return
    started = time.time()
    data = bytearray()
    for chunk in iter(lambda: source.read(65536), b""):
        if data is not None:
            if len(data) + len(chunk) <= MAX_INPUT:
                data.extend(chunk)
            else:
                data = None
        if not destination.closed:
            try:
                destination.write(chunk)
                destination.flush()
            except BrokenPipeError:
                try:
                    destination.close()
                except BrokenPipeError:
                    pass
    if not destination.closed:
        destination.close()  # Give the existing renderer EOF before doing any monitoring I/O.
    if data is not None:
        try:
            capture(json.loads(data), state, now=started)
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
        ):
            # Never log the native payload. The monitor exposes the last dated observation.
            pass


def read_private_metadata(path, limit):
    """Read only an owner-private regular file. Refuse redirection before reading its contents."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif Path(path).is_symlink():
        raise ValueError("Claude quota metadata must be a regular file")
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or (
                os.name == "posix"
                and (info.st_uid != os.getuid() or info.st_mode & 0o077)
            )
            or info.st_size > limit
        ):
            raise ValueError("Unsafe Claude quota metadata")
        raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Claude quota metadata exceeds the size limit")
        return json.loads(raw)


def read_observation(path, member_id, digest):
    value = read_private_metadata(path, 16384)
    if (
        not isinstance(value, dict)
        or value.get("session_key") != digest
        or value.get("member_id") != member_id
        or not isinstance(value.get("metadata"), dict)
        or value["metadata"].get("provider") != "claude"
    ):
        raise ValueError("Claude quota observation does not match enrollment")
    return value
