"""Native quota input adapters; no credentials, inference or terminal driving."""

import hashlib
import json
import os
from pathlib import Path
import time

from .quota_capture import (
    agy_runtime,
    atomic_json,
    finite,
    process_info,
    pseudonym,
    text,
    timestamp,
)


def capture_directory(state):
    directory = Path(state) / "quota"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    key = directory / "identity.key"
    try:
        fd = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as output:
            output.write(os.urandom(32))
    if len(key.read_bytes()) != 32:
        raise ValueError("Invalid local quota identity key; preserve it for inspection")
    return directory


def enrolled_agy_runtimes(db, state, proc=Path("/proc")):
    """Correlate an enrolled MCP connection's actual flock owner and CLI ancestor.

    Inspect only OS process metadata. Never read cmdline, environ or conversations.
    Multiple aliases under one native runtime are ambiguous and remain unbound.
    """
    rows = db.execute("""SELECT m.id,m.alias,m.project FROM members m WHERE m.provider='agy'
        AND EXISTS(SELECT 1 FROM peer_members pm JOIN peer_teams t ON t.id=pm.team_id
            WHERE pm.member_id=m.id AND pm.state='active' AND t.state IN ('active','paused'))""").fetchall()
    locks = {}
    for line in (proc / "locks").read_text().splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
            continue
        try:
            major, minor, inode = parts[5].split(":")
            locks[(int(major, 16), int(minor, 16), int(inode))] = int(parts[4])
        except ValueError:
            continue
    candidates = {}
    for row in rows:
        digest = hashlib.sha256(
            f"{row['project']}\0{row['alias']}".encode()
        ).hexdigest()
        try:
            stat = (Path(state) / f"agy-{digest}.lock").stat()
        except FileNotFoundError:
            continue
        pid = locks.get((os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino))
        info = process_info(pid, proc) if pid else None
        runtime = agy_runtime(pid, proc) if info else None
        if runtime:
            candidates.setdefault(runtime, []).append(
                {
                    "member_id": row["id"],
                    "connection": f"{runtime}:{pid}:{info['start']}",
                }
            )
    return {key: items[0] for key, items in candidates.items() if len(items) == 1}


def publish_runtimes(db, state, *, enabled=True, now=None):
    directory = capture_directory(state)
    runtimes = enrolled_agy_runtimes(db, state) if enabled else {}
    atomic_json(
        directory / "runtimes.json",
        {
            "enabled": enabled,
            "updated": time.time() if now is None else now,
            "runtimes": runtimes,
        },
    )
    return runtimes


def codex_metadata(result, key):
    account = text(result.get("accountId"), 256)
    groups = result.get("rateLimitsByLimitId")
    if not isinstance(groups, dict):
        item = result.get("rateLimits")
        groups = (
            {item.get("limitId") or "codex": item} if isinstance(item, dict) else {}
        )
    if len(groups) > 32:
        raise ValueError("Too many native quota groups")
    windows = []
    for group, item in sorted(groups.items()):
        if not text(group) or not isinstance(item, dict):
            continue
        for name in ("primary", "secondary"):
            window = item.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            windows.append(
                {
                    "bucket": f"{group}/{name}",
                    "group": group,
                    "model": text(item.get("normalModelSlug")),
                    "remaining": max(0, 100 - used)
                    if finite(used) and used >= 0
                    else None,
                    "resets_at": timestamp(window.get("resetsAt")),
                    "duration_minutes": window.get("windowDurationMins")
                    if type(window.get("windowDurationMins")) is int
                    and window["windowDurationMins"] > 0
                    else None,
                    "blocked": bool(text(item.get("rateLimitReachedType")))
                    or item.get("spendControlReached") is True,
                    "disabled": False,
                }
            )
    allowed = result.get("ordinaryUsageAllowed")
    return {
        "provider": "codex",
        "source": "Codex native account/rateLimits/read",
        "account_key": pseudonym(key, "codex-account", account) if account else None,
        "ordinary_allowed": allowed if type(allowed) is bool else None,
        "windows": windows,
    }


class NativeQuotaError(ValueError):
    """A safe diagnostic code, never an upstream response containing account data."""


class CodexQuotaClient:
    def __init__(self, socket_path, *, timeout=20):
        self.socket_path, self.timeout = str(socket_path), timeout

    def read(self, members, key):
        """Query quota and metadata for enrolled IDs only. Never enumerate threads."""
        from websockets.sync.client import unix_connect

        deadline = time.monotonic() + self.timeout
        with unix_connect(
            self.socket_path,
            open_timeout=5,
            close_timeout=1,
            max_size=1048576,
            max_queue=8,
        ) as connection:
            sequence = 0

            def call(method, params):
                nonlocal sequence
                sequence += 1
                connection.send(
                    json.dumps({"id": sequence, "method": method, "params": params})
                )
                for _ in range(100):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Native quota request timed out")
                    message = json.loads(connection.recv(timeout=remaining))
                    if message.get("id") == sequence:
                        if "error" in message:
                            raise NativeQuotaError(
                                f"Native request failed ({message['error'].get('code')})"
                            )
                        return message["result"]
                raise NativeQuotaError("Too many unrelated native notifications")

            call(
                "initialize",
                {"clientInfo": {"name": "native_team_quota", "version": "0.1.0"}},
            )
            connection.send(json.dumps({"method": "initialized"}))
            result = codex_metadata(
                call("account/rateLimits/read", {"excludeResetCreditDetails": True}),
                key,
            )
            bindings = []
            # A metadata-only read proves this known ID belongs to this native server.
            # Preview, paths, turns and all other thread data are discarded immediately.
            for member in members[:32]:
                try:
                    thread = call(
                        "thread/read",
                        {"threadId": member["native_id"], "includeTurns": False},
                    )["thread"]
                except NativeQuotaError:
                    continue
                if (
                    thread.get("id") != member["native_id"]
                    or thread.get("modelProvider") != "openai"
                ):
                    continue
                bindings.append(
                    {
                        "member_id": member["id"],
                        "model": text(thread.get("model")),
                        "session_key": pseudonym(
                            key, "codex-session", member["native_id"]
                        ),
                    }
                )
            return result, bindings
