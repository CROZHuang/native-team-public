"""Non-model native quota collection owned by the existing notification monitor."""

import os
import logging
from pathlib import Path
import stat
import threading
import time

from .inspection import readonly_snapshot
from .claude_quota import TARGETS as CLAUDE_TARGETS, read_observation
from .quota_capture import atomic_json, pseudonym, read_json
from .quota_native import CodexQuotaClient, capture_directory, publish_runtimes
from .quota_store import QuotaStore, claude_display_sources, shared_claude_account
from .recovery import Recovery
from .schema import has_native_quota
from .store import TeamError


def configuration(state):
    try:
        value = read_json(Path(state) / "quota/config.json", 4096)
    except FileNotFoundError:
        return {"enabled": False}
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("Invalid quota collector configuration")
    threshold = value.get("warning_percent", 10)
    if type(threshold) not in (int, float) or not 0 <= threshold <= 30:
        raise ValueError("Invalid quota warning threshold")
    return {"enabled": value["enabled"], "warning_percent": threshold}


def set_enabled(state, enabled):
    directory = capture_directory(state)
    atomic_json(
        directory / "config.json", {"enabled": bool(enabled), "warning_percent": 10}
    )
    if not enabled:
        atomic_json(
            directory / "runtimes.json",
            {"enabled": False, "updated": time.time(), "runtimes": {}},
        )


def collector_status(state):
    try:
        config = configuration(state)
        status = read_json(Path(state) / "quota/status.json", 8192)
        if not isinstance(status, dict) or any(
            not isinstance(status.get(p, {}), dict) for p in ("codex", "agy", "claude")
        ):
            raise ValueError("Invalid quota collector status")
    except FileNotFoundError:
        status = {}
    except (OSError, ValueError):
        return {"enabled": False, "error": "额度采集配置或状态无法读取。"}
    now = time.time()
    for provider in ("codex", "agy", "claude"):
        value = status.get(provider)
        if value and now - value.get("checked_at", 0) > 600:
            status[provider] = {
                **value,
                "state": "stale",
                "detail": "采集状态长时间未更新；当前连接情况未知。",
            }
    return {"enabled": config.get("enabled", False), **status}


class QuotaCollector:
    """Two bounded I/O workers: slow account reads cannot delay local zero events."""

    def __init__(self, store, *, socket_path=None, client_factory=CodexQuotaClient):
        self.store = store
        self.service = QuotaStore(store)
        self.socket_path = Path(
            socket_path
            or Path.home() / ".codex/app-server-control/app-server-control.sock"
        )
        self.client_factory = client_factory
        self.stopping = threading.Event()
        self.threads = []
        self.status_lock = threading.Lock()
        self.status = {}
        self.seen = {}

    def start(self):
        for name, target in (("local", self._local_loop), ("codex", self._codex_loop)):
            thread = threading.Thread(
                name=f"native-quota-{name}", target=target, daemon=True
            )
            self.threads.append(thread)
            thread.start()

    def close(self, timeout=28):
        self.stopping.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        directory = self.store.state / "quota"
        if directory.is_dir():
            atomic_json(
                directory / "runtimes.json",
                {"enabled": False, "updated": time.time(), "runtimes": {}},
            )

    def _status(self, provider, result, detail, **fields):
        with self.status_lock:
            prior = self.status.get(provider, {})
            self.status[provider] = {
                **prior,
                "state": result,
                "detail": detail,
                "checked_at": time.time(),
                **fields,
            }
            try:
                atomic_json(
                    capture_directory(self.store.state) / "status.json", self.status
                )
            except OSError as error:
                logging.error(
                    "Native quota status could not be saved (%s)", type(error).__name__
                )

    def collect_agy(self):
        with readonly_snapshot(self.store.state) as db:
            if db is None or not has_native_quota(db):
                raise ValueError("Native quota schema not installed")
            runtimes = publish_runtimes(db, self.store.state)
        directory = self.store.state / "quota"
        key = (directory / "identity.key").read_bytes()
        captured = rejected = 0
        for path in sorted(directory.glob("agy-*.json"))[:64]:
            try:
                captured += self._ingest_agy_file(path, runtimes, key)
            except (OSError, ValueError, TypeError, KeyError, TeamError):
                # One damaged/obsolete callback file must not hide another peer's zero.
                rejected += 1
        detail = (
            "接收原生状态栏数据；重复渲染不等于后端刷新。"
            if captured
            else "已安装采集，等待原生窗口的下一次状态更新。"
        )
        if rejected:
            detail += f" 已跳过 {rejected} 份无效快照，其余继续处理。"
        self._status(
            "agy",
            "warning" if rejected else "ok" if captured else "waiting",
            detail,
            associated=len(runtimes),
            captured=captured,
            rejected=rejected,
        )

    def _ingest_agy_file(self, path, runtimes, key):
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise ValueError("Unsafe quota snapshot file")
        value = read_json(path)
        if not isinstance(value, dict):
            raise ValueError("Invalid quota snapshot")
        target = runtimes.get(value.get("runtime"))
        if not target or (value.get("member_id"), value.get("connection")) != (
            target["member_id"],
            target["connection"],
        ):
            return 0
        expected = pseudonym(key, "agy-runtime", value["runtime"])
        if (
            value.get("version") != 1
            or value.get("source_id") != expected
            or path.name != f"agy-{expected}.json"
        ):
            raise ValueError("Quota snapshot association mismatch")
        signature = (
            value.get("observed_at"),
            value.get("received_at"),
            value.get("connection"),
        )
        if self.seen.get(path.name) != signature:
            meta = value["metadata"]
            if not isinstance(meta, dict) or meta.get("provider") != "agy":
                raise ValueError("Unexpected quota source")
            self.service.ingest(
                target["member_id"],
                expected,
                value["connection"],
                meta["session_key"],
                meta["model"],
                meta,
                value["observed_at"],
                value["received_at"],
            )
            self.seen[path.name] = signature
        return 1

    def collect_codex(self):
        with readonly_snapshot(self.store.state) as db:
            if db is None or not has_native_quota(db):
                raise ValueError("Native quota schema not installed")
            members = [
                dict(r)
                for r in db.execute("""SELECT m.id,m.native_id FROM members m WHERE m.provider='codex'
                AND EXISTS(SELECT 1 FROM peer_members pm JOIN peer_teams t ON t.id=pm.team_id
                    WHERE pm.member_id=m.id AND pm.state='active' AND t.state IN ('active','paused')) ORDER BY m.id LIMIT 32""")
            ]
        if not members:
            self._status(
                "codex", "waiting", "没有需要采集的活动 Codex 成员。", associated=0
            )
            return
        directory = capture_directory(self.store.state)
        key = (directory / "identity.key").read_bytes()
        metadata, bindings = self.client_factory(self.socket_path).read(members, key)
        now = time.time()
        # The local socket instance prevents an unrelated daemon replacement from
        # silently inheriting a previous connection association.
        info = self.socket_path.stat()
        connection = pseudonym(
            key, "codex-runtime", f"{self.socket_path}:{info.st_ino}:{info.st_mtime_ns}"
        )
        for item in bindings:
            self.service.ingest(
                item["member_id"],
                "codex-" + item["session_key"],
                connection,
                item["session_key"],
                item["model"],
                metadata,
                now,
                now,
            )
        self._status(
            "codex",
            "ok",
            "已通过原生客户端读取额度；未调用模型。",
            associated=len(bindings),
            last_success=now,
        )

    def collect_claude(self):
        with readonly_snapshot(self.store.state) as db:
            if db is None or not has_native_quota(db):
                raise ValueError("Native quota schema not installed")
            members = db.execute(CLAUDE_TARGETS).fetchall()
        directory = self.store.state / "quota"
        key = (directory / "identity.key").read_bytes()
        captured = rejected = 0
        for member in members:
            digest = pseudonym(key, "claude-session", member["native_id"])
            path = directory / f"claude-{digest}.json"
            try:
                value = read_observation(path, member["id"], digest)
                signature = (member["id"], value["received_at"])
                if self.seen.get(path.name) != signature:
                    self.service.ingest(
                        member["id"],
                        "claude-" + digest,
                        digest,
                        digest,
                        value["model"],
                        value["metadata"],
                        value["observed_at"],
                        value["received_at"],
                    )
                    self.seen[path.name] = signature
                captured += 1
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError, KeyError):
                rejected += 1
        detail = (
            f"{len(members)} 个已登记会话，{captured} 个已收到原生状态栏快照。"
            if members
            else "等待 Claude Code 会话登记。"
        )
        if rejected:
            detail += f"有 {rejected} 份快照未通过校验。"
        with readonly_snapshot(self.store.state) as db:
            account = shared_claude_account(self.store.state, db, now=time.time())
        account_error = (account or {}).get("error")
        if account_error:
            detail += " " + account_error
        self._status(
            "claude",
            "warning" if rejected or account_error else "ok" if captured else "waiting",
            detail,
            associated=len(members),
            captured=captured,
            rejected=rejected,
            shared_account=claude_display_sources(account),
        )

    def _local_loop(self):
        while not self.stopping.is_set():
            try:
                config = configuration(self.store.state)
                if config["enabled"]:
                    try:
                        self.collect_claude()
                    except Exception as error:
                        self._status(
                            "claude",
                            "error",
                            f"状态栏采集失败（{type(error).__name__}）。",
                        )
                    try:
                        self.collect_agy()
                    except Exception as error:
                        self._status(
                            "agy",
                            "error",
                            f"本地采集失败（{type(error).__name__}）；保留旧记录并标明过期。",
                        )
                    # Freshness and latched restrictions advance even when input fails.
                    self.service.reconcile(threshold=config["warning_percent"])
                    Recovery(self.store).reconcile()
            except Exception as error:
                # Raw native payloads and exception messages never enter service logs.
                self._status(
                    "agy",
                    "error",
                    f"本地采集失败（{type(error).__name__}）；保留旧记录并标明过期。",
                )
            self.stopping.wait(5)

    def _codex_loop(self):
        failures = 0
        while not self.stopping.is_set():
            delay = 5
            try:
                config = configuration(self.store.state)
                if config["enabled"]:
                    self.collect_codex()
                    self.service.reconcile(threshold=config["warning_percent"])
                    failures = 0
                    delay = 60
            except Exception as error:
                failures += 1
                delay = min(60 * 2 ** min(failures - 1, 3), 480)
                self._status(
                    "codex",
                    "error",
                    f"原生额度读取失败（{type(error).__name__}）；不会把网络或登录错误判为额度耗尽。",
                )
            self.stopping.wait(delay)
