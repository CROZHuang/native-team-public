"""Offer bounded recovery to an already-running, native-owned read-only watch.

The monitor records an offer; only agy's native background completion can wake a model.
Neither reset time nor an offer releases availability, claims work or proves execution.
"""

from contextlib import closing

try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
from pathlib import Path
import time

from .availability import read_availability
from .peers import PeerStore
from .quota_capture import process_info
from .quota_native import enrolled_agy_runtimes
from .quota_store import FRESH_SECONDS, RECORDER, applicable
from .schema import has_recovery
from .startup import console_entry

RESET_GRACE = 90
RECEIPT_GRACE = 120


def process_instance(pid=None, proc=Path("/proc")):
    # The native sandbox may have a PID namespace. /proc/self is authoritative even
    # when /proc is mounted from a different namespace than os.getpid(). Compare
    # the innermost PID + namespace inode + start time, which the host can also read.
    base = proc / (str(pid) if pid is not None else "self")
    try:
        stat = (base / "stat").read_text()
        started = stat[stat.rindex(")") + 2 :].split()[19]
        inner = next(
            line.split()[-1]
            for line in (base / "status").read_text().splitlines()
            if line.startswith("NSpid:")
        )
        namespace = (base / "ns/pid").stat().st_ino
        boot = (proc / "sys/kernel/random/boot_id").read_text().strip()
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return f"{boot}:{namespace}:{inner}:{started}"


def agy_lock_held(state, project, alias):
    """Return True if an agy alias lock is actively held, False if unlocked, None if unreadable."""
    if fcntl is None:
        return None
    digest = hashlib.sha256(f"{project}\0{alias}".encode()).hexdigest()
    path = Path(state) / f"agy-{digest}.lock"
    try:
        with path.open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return None


def agy_watches(db, state, proc=Path("/proc")):
    """Inspect command options only in descendants of enrolled agy processes.

    No terminal, environment, provider database or conversation content is read.
    A helper started by Codex or an unrelated shell cannot stand in for a native watch.
    """
    runtimes = enrolled_agy_runtimes(db, state, proc)
    if not runtimes:
        return {"recovery": {}, "inbox": {}, "visible": set()}
    roots = {
        int(key.split(":")[1]): value["member_id"] for key, value in runtimes.items()
    }
    members = {
        r["id"]: r
        for r in db.execute("SELECT id,alias,project FROM members WHERE provider='agy'")
    }
    executable = console_entry().resolve()
    infos = {}
    paths = [p for p in proc.iterdir() if p.name.isdigit()]
    for path in paths:
        info = process_info(int(path.name), proc)
        if info:
            infos[info["pid"]] = info
    result = {"recovery": {}, "inbox": {}}
    for pid, info in infos.items():
        if not info["exe"].startswith("python"):
            continue
        parent = info["parent"]
        for _ in range(24):
            if parent in roots or parent not in infos:
                break
            parent = infos[parent]["parent"]
        actor = roots.get(parent)
        if not actor:
            continue
        try:
            with (proc / str(pid) / "cmdline").open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                continue
            args = raw.rstrip(b"\0").decode().split("\0")
            # The installed Python console entry point (or a symlink to it, e.g.
            # ~/.local/bin/native-team) preserves exact argparse options.
            if len(args) < 3 or Path(args[1]).resolve() != executable:
                continue
            args = args[2:]
            options = {}
            if args and args[0] == "--state":
                options["--state"] = args[1]
                args = args[2:]
            if not args or args[0] != "wait":
                continue
            args = args[1:]
            if len(args) % 2 or len(set(args[::2])) != len(args) // 2:
                continue
            options.update(zip(args[::2], args[1::2]))
            mode = options.get("--mode", "all")
            if (
                "--project" not in options
                or mode not in {"recovery", "actionable", "all"}
                or options.get("--alias") != members[actor]["alias"]
            ):
                continue
            if (
                str(Path(options.get("--project", "")).resolve())
                != members[actor]["project"]
            ):
                continue
            if (
                Path(
                    options.get("--state", Path.home() / ".local/state/native-team")
                ).resolve()
                != Path(state).resolve()
            ):
                continue
            instance = process_instance(pid, proc)
            if instance:
                bucket = "recovery" if mode == "recovery" else "inbox"
                result[bucket].setdefault(actor, []).append(instance)
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
    return {
        "recovery": {
            actor: sorted(instances) for actor, instances in result["recovery"].items()
        },
        "inbox": {
            actor: sorted(instances) for actor, instances in result["inbox"].items()
        },
        "visible": set(roots.values()),
    }


def native_watches(db, state, proc=Path("/proc")):
    """Discover live --mode recovery watches inside enrolled agy runtimes."""
    return agy_watches(db, state, proc)["recovery"]


def inbox_watches(db, state, proc=Path("/proc")):
    """Discover live --mode actionable/all watches inside enrolled agy runtimes."""
    return agy_watches(db, state, proc)["inbox"]


def read_recovery(db, actor):
    if not has_recovery(db):
        return None
    row = db.execute(
        "SELECT * FROM native_quota_recovery WHERE member_id=?", (actor,)
    ).fetchone()
    return dict(row) if row else None


def recovery_signal(db, actor, availability, instance):
    """Read-only predicate; process identity prevents duplicate/PID-reuse consumption."""
    value = read_recovery(db, actor)
    if (
        not value
        or value["state"] not in {"offered", "unconfirmed"}
        or value["target"] != instance
    ):
        return None
    if availability["status"] == "cooldown" and availability["recorded_by"] != RECORDER:
        return None
    if not db.execute(
        """SELECT 1 FROM peer_members p JOIN peer_teams t ON t.id=p.team_id
        WHERE p.member_id=? AND p.state='active' AND t.state='active' LIMIT 1""",
        (actor,),
    ).fetchone():
        return None
    return value


def _incident(db, member, policy):
    """Use all recorded blocking windows, not just the latest partial refresh."""
    driver = db.execute(
        "SELECT * FROM native_quota_sources WHERE source_id=?",
        (policy["blocking_source"],),
    ).fetchone()
    if not driver or driver["association"] != "verified":
        return "", None
    own = db.execute(
        "SELECT * FROM native_quota_sources WHERE member_id=?", (member["id"],)
    ).fetchone()
    if (
        not own
        or own["association"] != "verified"
        or (
            driver["member_id"] != member["id"]
            and (not own["account_key"] or own["account_key"] != driver["account_key"])
        )
    ):
        return "", None
    windows = {
        w["bucket"]: w
        for w in json.loads(driver["snapshot"])["windows"]
        if applicable(w, "agy", own["model"])
    }
    blocking = json.loads(policy["blocking_windows"])
    resets = [windows.get(bucket, {}).get("resets_at") for bucket in blocking]
    reset = max(resets) if resets and all(r is not None for r in resets) else None
    key = hashlib.sha256(
        json.dumps(
            [driver["source_id"], own["account_key"], blocking, reset], sort_keys=True
        ).encode()
    ).hexdigest()
    return key, reset + RESET_GRACE if reset is not None else None


class Recovery:
    def __init__(self, store):
        self.store = store

    def reconcile(self, *, now=None, watches=None):
        stamp = time.time() if now is None else now
        if watches is None:
            with closing(self.store.connect()) as db:
                db.execute("PRAGMA query_only=ON")
                watches = native_watches(db, self.store.state)
        with self.store.transaction() as db:
            if not has_recovery(db):
                return
            members = db.execute("""SELECT m.* FROM members m WHERE m.provider='agy' AND EXISTS(
                SELECT 1 FROM peer_members p JOIN peer_teams t ON t.id=p.team_id WHERE p.member_id=m.id
                AND p.state='active' AND t.state IN ('active','paused'))""").fetchall()
            for member in members:
                self._reconcile_member(db, member, watches.get(member["id"], []), stamp)

    def _reconcile_member(self, db, member, watches, stamp):
        actor = member["id"]
        old = read_recovery(db, actor)
        policy = db.execute(
            "SELECT * FROM native_quota_policy WHERE member_id=?", (actor,)
        ).fetchone()
        availability = read_availability(db, actor, now=stamp)
        active = db.execute(
            """SELECT t.id FROM peer_members p JOIN peer_teams t ON t.id=p.team_id
            WHERE p.member_id=? AND p.state='active' AND t.state='active'""",
            (actor,),
        ).fetchall()
        value = (
            dict(old)
            if old
            else dict(
                member_id=actor,
                incident="",
                due_at=None,
                target=None,
                offered_at=None,
                received_at=None,
                confirmed_at=None,
                state="unarmed",
                detail="",
                watch_count=0,
                updated=stamp,
            )
        )
        value["watch_count"] = len(watches)
        automatic = (
            availability["status"] == "cooldown"
            and availability["recorded_by"] == RECORDER
        )
        valid_incident = True
        reset_known = True
        if automatic and policy and policy["posture"] == "cooldown":
            incident, due = _incident(db, member, policy)
            valid_incident = bool(incident)
            reset_known = due is not None
            # A partial native refresh must not erase an already-spent attempt. Nor
            # may an older/reset-backwards snapshot reopen a previously attempted time.
            spent_later = (
                value["offered_at"] is not None
                and value["due_at"] is not None
                and due is not None
                and due <= value["due_at"]
            )
            if (
                incident
                and due is not None
                and incident != value["incident"]
                and not spent_later
            ):
                value.update(
                    incident=incident,
                    due_at=due,
                    target=None,
                    offered_at=None,
                    received_at=None,
                    confirmed_at=None,
                )
        verified = bool(
            value["incident"]
            and policy
            and policy["posture"] in {"normal", "conserve"}
            and 0 <= stamp - policy["evidence_at"] < FRESH_SECONDS
        )
        if verified and value["confirmed_at"] is None:
            value["confirmed_at"] = stamp
        if value["offered_at"] is not None:
            receipt = db.execute(
                """SELECT MIN(d.received_at) FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id
                WHERE d.recipient=? AND m.request_key=? AND d.received_at IS NOT NULL""",
                (actor, "native-recovery:" + value["incident"]),
            ).fetchone()[0]
            value["received_at"] = receipt
        if not active:
            value.update(state="paused", detail="团队已暂停；不发起恢复尝试。")
        elif availability["status"] == "cooldown" and not automatic:
            value.update(
                state="explicit_hold",
                detail="保留人工或原生会话设置的冷却；不自动覆盖。",
            )
        elif automatic and not valid_incident:
            value.update(
                state="unknown_reset",
                detail="额度来源关联不完整；不使用旧来源的恢复时间。",
            )
        elif automatic and not reset_known:
            value.update(
                state="unknown_reset",
                detail="重置时间不完整；保留等待，不猜测恢复时间。",
            )
        elif automatic and value["received_at"] is not None:
            value.update(
                state="received",
                detail="原生会话已读取恢复通知；继续执行与额度确认分别核验。",
            )
        elif value["offered_at"] is not None and value["received_at"] is None:
            uncertain = stamp - value["offered_at"] >= RECEIPT_GRACE
            value.update(
                state="unconfirmed" if uncertain else "offered",
                detail="恢复信号已发出，尚无原生回执；同一重置不重复触发。"
                if uncertain
                else "已向原生恢复等待任务提供一次信号，等待原生回执。",
            )
        elif not watches:
            value.update(
                state="unarmed",
                detail="未发现原生恢复等待任务；窗口打开本身不能保证自动续跑。",
            )
        elif automatic and value["due_at"] is None:
            value.update(
                state="unknown_reset",
                detail="重置时间不完整；保留等待，不猜测恢复时间。",
            )
        elif automatic and stamp < value["due_at"]:
            value.update(
                state="waiting_reset",
                detail="原生恢复等待任务已就绪，等待已记录的重置时间。",
            )
        elif (automatic or verified) and value["offered_at"] is None:
            value.update(
                state="offered",
                target=watches[0],
                offered_at=stamp,
                detail="已向原生恢复等待任务提供一次信号，等待原生回执。",
            )
            for team in active:
                PeerStore(self.store)._publish(
                    db,
                    actor,
                    team["id"],
                    "quota",
                    "Native Team 自动恢复通知（程序信号，不是模型报告）：本会话的原生恢复等待任务获准进行一次恢复尝试。"
                    "重置时间不等于额度已恢复。若你已由原生客户端重新执行，请读取并确认团队上下文，核对原生额度或本次实际可执行状态，"
                    "用自己的 team_availability 记录恢复依据，再从检查点继续原授权任务。保留暂停、权限与任务归属；重新建立独立的 --mode recovery 等待任务。",
                    "native-recovery:" + value["incident"],
                    {actor},
                )
        else:
            value.update(
                state="armed",
                detail="原生恢复等待任务已就绪；当前没有自动冷却需要恢复。",
            )
        if old and all(old[k] == value[k] for k in value if k != "updated"):
            return
        value["updated"] = stamp
        columns = list(value)
        db.execute(
            f"INSERT INTO native_quota_recovery({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
            "ON CONFLICT(member_id) DO UPDATE SET "
            + ",".join(f"{k}=excluded.{k}" for k in columns if k != "member_id"),
            [value[k] for k in columns],
        )
