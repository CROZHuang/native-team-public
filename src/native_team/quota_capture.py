"""Small, stdlib-only agy statusline side effect: persist allowlisted metadata.

The native CLI supplies stdin to its existing renderer. This module never reads a
transcript, credential, terminal, or provider endpoint, and never invokes a model.
"""

from datetime import datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time


def atomic_json(path, value):
    """Publish a whole private snapshot; readers never observe a partial write."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".quota-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path, limit=131072):
    with Path(path).open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Quota snapshot exceeds its size limit")
    return json.loads(raw)


def process_info(pid, proc=Path("/proc")):
    try:
        path = proc / str(pid)
        stat = (path / "stat").read_text()
        fields = stat[stat.rindex(")") + 2 :].split()
        return {
            "pid": int(pid),
            "parent": int(fields[1]),
            "start": fields[19],
            "exe": Path(os.readlink(path / "exe")).name,
        }
    except (OSError, ValueError, IndexError):
        return None


def agy_runtime(pid=None, proc=Path("/proc")):
    """An OS process instance, not a reusable PID or a guessed window title."""
    pid = os.getpid() if pid is None else pid
    found = None
    for _ in range(24):
        info = process_info(pid, proc)
        if info is None:
            break
        # Native updates rename the still-running inode, then may unlink it.
        # /proc/.../exe retains that name; comm is truncated and is not identity.
        executable = info["exe"].removesuffix(" (deleted)")
        if re.fullmatch(r"agy(?:\.\d+\.old)?", executable):
            found = info
        if info["parent"] <= 1 or info["parent"] == pid:
            break
        pid = info["parent"]
    if found is None:
        return None
    boot = (proc / "sys/kernel/random/boot_id").read_text().strip()
    return f"{boot}:{found['pid']}:{found['start']}"


def pseudonym(key, kind, value):
    return hmac.new(key, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def text(value, limit=120):
    return (
        value.strip()
        if isinstance(value, str) and 0 < len(value.strip()) <= limit
        else ""
    )


def timestamp(value):
    try:
        if finite(value) and value >= 0:
            datetime.fromtimestamp(value)
            return float(value)
        if isinstance(value, str):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.utcoffset() is not None and date.timestamp() >= 0:
                return date.timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def agy_metadata(payload, key):
    """Copy only documented quota fields; account/session labels become local hashes."""
    if not isinstance(payload, dict):
        raise ValueError("Expected native statusline object")
    session = text(payload.get("conversation_id") or payload.get("session_id"), 160)
    if not session:
        raise ValueError("Native conversation not yet selected")
    email = text(payload.get("email"), 254).casefold()
    model = payload.get("model")
    model = (
        text(model.get("id") or model.get("display_name"))
        if isinstance(model, dict)
        else ""
    )
    quotas = payload.get("quota")
    if not isinstance(quotas, dict) or len(quotas) > 32:
        raise ValueError("Quota field missing or too large")
    windows = []
    for bucket, item in sorted(quotas.items()):
        if not text(bucket) or not isinstance(item, dict):
            continue
        fraction, amount = item.get("remaining_fraction"), item.get("remaining_amount")
        windows.append(
            {
                "bucket": bucket,
                "remaining": fraction * 100
                if finite(fraction) and 0 <= fraction <= 1
                else None,
                "amount": amount if finite(amount) and amount >= 0 else None,
                "resets_at": timestamp(item.get("reset_time")),
                "disabled": item.get("disabled") is True,
            }
        )
    return {
        "provider": "agy",
        "source": "agy native statusline",
        "account_key": pseudonym(key, "agy-account", email) if email else None,
        "session_key": pseudonym(key, "agy-session", session),
        "model": model,
        "windows": windows,
    }


def capture_agy(payload, state=None, *, runtime=None, now=None):
    """Called by the existing renderer. No capture until the operator enables it.

    The collector publishes verified live MCP process associations. A callback
    cannot enroll another conversation or infer an alias from its title/path.
    Unchanged renders retain their original observation time: UI redraw is not a
    fresh backend query. Received time is a separate, throttled transport signal.
    """
    state = Path(state or Path.home() / ".local/state/native-team") / "quota"
    try:
        manifest = read_json(state / "runtimes.json")
        config = read_json(state / "config.json", 4096)
    except FileNotFoundError:
        return "disabled"
    stamp = time.time() if now is None else now
    if (
        config.get("enabled") is not True
        or not manifest.get("enabled")
        or stamp - manifest.get("updated", 0) > 90
    ):
        return "disabled"
    runtime = agy_runtime() if runtime is None else runtime
    target = manifest.get("runtimes", {}).get(runtime)
    if not target:
        return "unassociated"
    key = (state / "identity.key").read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid local quota identity key")
    metadata = agy_metadata(payload, key)
    source_id = pseudonym(key, "agy-runtime", runtime)
    path = state / f"agy-{source_id}.json"
    try:
        old = read_json(path)
    except (FileNotFoundError, ValueError):
        old = {}
    # Ignore changing countdowns, conversation text and UI state when dating a value.
    same = (
        old.get("metadata") == metadata
        and old.get("connection") == target["connection"]
    )
    if same and stamp - old.get("received_at", 0) < 15:
        return "unchanged"
    atomic_json(
        path,
        {
            "version": 1,
            "source_id": source_id,
            "runtime": runtime,
            "connection": target["connection"],
            "member_id": target["member_id"],
            "observed_at": old["observed_at"] if same else stamp,
            "received_at": stamp,
            "metadata": metadata,
        },
    )
    return "captured"
