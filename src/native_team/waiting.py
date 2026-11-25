"""Read-only ledger wait, run by the native client's background-command tool.

This does not invoke a model, enqueue a prompt, enroll a session or own a native schedule.
The native client decides whether process completion wakes its conversation.
"""

from contextlib import closing
import ctypes
import errno
import math
import os
from pathlib import Path
import select
import sqlite3
import sys
import time

from .schema import available
from .store import TeamError, conversation_uuid
from .availability import parse_resume_after, read_availability
from .recovery import RECEIPT_GRACE, process_instance, read_recovery, recovery_signal


def pending(path, project, alias, mode="all"):
    state = pending_state(path, project, alias, mode)
    return (
        [] if mode != "recovery" and state["availability"]["held"] else state["reasons"]
    )


def pending_state(path, project, alias, mode):
    if mode not in {"all", "actionable", "recovery"}:
        raise TeamError("Wait mode must be all, actionable or recovery")
    try:
        return _pending(path, project, alias, mode)
    except sqlite3.Error as error:
        raise TeamError(
            "Native sandbox could not read the ledger consistently; inspect sandbox/WAL access before re-arming, or use a user-authorized native schedule"
        ) from error


def _pending(path, project, alias, mode):
    with closing(
        sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
            isolation_level=None,
        )
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        member = db.execute(
            "SELECT id,provider,native_id FROM members WHERE project=? AND alias=?",
            (project, alias),
        ).fetchone()
        if member is None or member["provider"] not in {"agy", "claude"}:
            raise TeamError(
                "Read-only wait needs this project's enrolled agy or Claude Code alias"
            )
        provider = member["provider"]
        if provider == "claude":
            if mode == "recovery":
                raise TeamError(
                    "Recovery mode requires agy's native recovery watch; use actionable for Claude"
                )
            inherited = os.environ.get("CLAUDE_CODE_SESSION_ID")
            if (
                inherited is not None
                and conversation_uuid(provider, inherited) != member["native_id"]
            ):
                raise TeamError("This alias belongs to a different Claude Code session")
        actor = member["id"]
        availability = read_availability(db, actor)
        if mode == "recovery":
            recovery = read_recovery(db, actor)
            signal = recovery_signal(db, actor, availability, process_instance())
            return {
                "provider": provider,
                "reasons": ["quota_recovery_attempt"] if signal else [],
                "availability": availability,
                "effective_mode": mode,
                "defer_until": recovery["due_at"]
                if recovery and recovery["state"] == "waiting_reset"
                else None,
                "recovery": {
                    "incident": signal["incident"],
                    "quota_confirmed": signal["confirmed_at"] is not None,
                }
                if signal
                else None,
            }
        if availability["posture"] == "conserve":
            mode = "actionable"
        reasons = []
        if db.execute(
            "SELECT 1 FROM assignments a JOIN work w ON w.id=a.work_id WHERE a.owner=? AND a.state IN ('ready','working') AND w.state='active' LIMIT 1",
            (actor,),
        ).fetchone():
            reasons.append("legacy_assignment")
        if available(db):
            if db.execute(
                "SELECT 1 FROM peer_messages m JOIN peer_deliveries d ON d.message_id=m.id JOIN peer_members p ON p.team_id=m.team_id AND p.member_id=d.recipient WHERE d.recipient=? AND d.acked_at IS NULL AND m.kind='lifecycle' AND p.state='active' LIMIT 1",
                (actor,),
            ).fetchone():
                reasons.append("team_lifecycle")
            for membership in db.execute(
                "SELECT p.team_id,p.acked_epoch,t.epoch FROM peer_members p JOIN peer_teams t ON t.id=p.team_id WHERE p.member_id=? AND p.state='active' AND t.state='active'",
                (actor,),
            ):
                team = membership["team_id"]
                if mode == "actionable" and membership["acked_epoch"] == 0:
                    reasons.append("team_activation")
                if mode == "all" and (
                    membership["acked_epoch"] != membership["epoch"]
                    or db.execute(
                        "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.recipient=? AND d.acked_at IS NULL LIMIT 1",
                        (team, actor),
                    ).fetchone()
                ):
                    reasons.append("team_context")
                elif (
                    mode == "actionable"
                    and db.execute(
                        "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.recipient=? AND d.acked_at IS NULL AND m.kind IN ('direct','handoff','blocker','user_instruction') LIMIT 1"
                        if provider == "agy"
                        else "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.recipient=? AND d.acked_at IS NULL AND m.kind IN ('direct','handoff','blocker','user_instruction','quota') LIMIT 1",
                        (team, actor),
                    ).fetchone()
                ):
                    reasons.append("directed_context")
                if mode == "actionable":
                    if db.execute(
                        "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.recipient=? AND d.acked_at IS NULL AND m.kind='decision' LIMIT 1",
                        (team, actor),
                    ).fetchone():
                        reasons.append("decision_update")
                    # Internal task notifications have durable keys tied to structured history.
                    # Match those records rather than parsing the human-facing message body.
                    if db.execute(
                        "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id JOIN peer_tasks t ON t.team_id=m.team_id JOIN peer_task_history h ON h.task_id=t.id AND m.request_key='task:'||h.task_id||':'||h.revision WHERE m.team_id=? AND d.recipient=? AND d.acked_at IS NULL AND m.kind='task' AND h.kind!='accept' LIMIT 1",
                        (team, actor),
                    ).fetchone():
                        reasons.append("task_transition")
                    if db.execute(
                        "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.recipient=? AND d.acked_at IS NULL AND m.kind='membership' AND EXISTS(SELECT 1 FROM peer_tasks t WHERE t.team_id=m.team_id AND t.state NOT IN ('accepted','cancelled') AND (t.handoff_needed=1 OR t.reviewer_handoff_needed=1)) LIMIT 1",
                        (team, actor),
                    ).fetchone():
                        reasons.append("handoff_context")
                if db.execute(
                    "SELECT 1 FROM peer_tasks WHERE team_id=? AND ((owner=? AND state IN ('ready','working')) OR (reviewer=? AND state='review')) LIMIT 1",
                    (team, actor, actor),
                ).fetchone():
                    reasons.append("peer_task")
                if db.execute(
                    "SELECT 1 FROM peer_votes v JOIN peer_decisions d ON d.id=v.decision_id WHERE d.team_id=? AND v.member_id=? AND v.response IS NULL AND d.state IN ('proposed','disputed') LIMIT 1",
                    (team, actor),
                ).fetchone():
                    reasons.append("decision_response")
        defer_until = None
        if provider == "agy":
            recovery = read_recovery(db, actor)
            if (
                recovery
                and recovery["state"] == "offered"
                and recovery["offered_at"] is not None
                and recovery["received_at"] is None
            ):
                receipt = db.execute(
                    """SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id
                    WHERE d.recipient=? AND m.request_key=? AND d.received_at IS NOT NULL LIMIT 1""",
                    (actor, "native-recovery:" + recovery["incident"]),
                ).fetchone()
                if not receipt:
                    defer_until = recovery["offered_at"] + RECEIPT_GRACE
        return {
            "provider": provider,
            "reasons": sorted(set(reasons)),
            "availability": availability,
            "effective_mode": mode,
            "defer_until": defer_until,
        }


def _open_watch(state):
    if sys.platform != "linux":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    libc.inotify_init1.argtypes, libc.inotify_init1.restype = (
        [ctypes.c_int],
        ctypes.c_int,
    )
    libc.inotify_add_watch.argtypes, libc.inotify_add_watch.restype = (
        [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32],
        ctypes.c_int,
    )
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(
            ctypes.get_errno(), "Could not create native read-only event watch"
        )
    try:
        # Watch BEFORE inspecting the predicate, so a commit between inspection and sleep
        # cannot disappear. Directory watch also observes WAL creation/replacement.
        mask = (
            0x00000002 | 0x00000008 | 0x00000080 | 0x00000100 | 0x00000400 | 0x00000800
        )
        if libc.inotify_add_watch(fd, os.fsencode(state), mask) < 0:
            raise OSError(ctypes.get_errno(), "Native sandbox cannot watch this ledger")
        return fd
    except BaseException:
        os.close(fd)
        raise


def wait(
    state, project, alias, timeout=3600, *, mode="all", coalesce=0, not_before=None
):
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise TeamError("Wait deadline must be 1–86400 seconds")
    if mode not in {"all", "actionable", "recovery"}:
        raise TeamError("Wait mode must be all, actionable or recovery")
    if (
        type(coalesce) not in {int, float}
        or not math.isfinite(coalesce)
        or not 0 <= coalesce <= 30
    ):
        raise TeamError("Coalescing must be 0–30 finite seconds")
    not_before = parse_resume_after(not_before)
    state, project = (
        Path(state).expanduser().resolve(),
        str(Path(project).expanduser().resolve()),
    )
    path = state / "team.sqlite3"
    if not path.is_file():
        raise TeamError(
            "Native Team ledger is not visible in this native sandbox; do not bypass permissions. Use an authorized native inbox schedule instead."
        )
    if mode == "recovery" and sys.platform != "linux":
        raise TeamError(
            "Quota recovery requires Linux process evidence. Use --mode actionable for inbox work."
        )
    try:
        fd = _open_watch(state)
        mechanism = {"wait_mode": "inotify" if fd is not None else "poll"}
    except OSError as error:
        # All native clients share the user's watch allowance. Resource exhaustion
        # must not strand an idle peer; permission and path errors remain failures.
        if error.errno not in {errno.EMFILE, errno.ENFILE, errno.ENOSPC}:
            raise
        fd = None
        mechanism = {"wait_mode": "poll", "watch_errno": error.errno}
    try:
        deadline = time.monotonic() + timeout
        was_deferred = False
        quiet_until = ready_limit = None
        while True:
            snapshot = pending_state(path, project, alias, mode)
            availability = snapshot["availability"]
            gate = max(
                not_before or 0,
                availability["resume_after"] or 0
                if availability["status"] == "cooldown"
                else 0,
            )
            gate = max(gate, snapshot.get("defer_until") or 0)
            now, monotonic = time.time(), time.monotonic()
            if gate > now:
                # The bounded idle window follows an explicitly known reset. Expiring it
                # before that time would itself wake a quota-limited native conversation.
                deadline = monotonic + gate - now + timeout
            elif was_deferred:
                # Expiry or an explicit correction starts a fresh bounded idle window;
                # retaining a removed/shortened reset would keep an idle helper too long.
                deadline = monotonic + timeout
            was_deferred = gate > now
            unknown_reset = (
                availability["status"] == "cooldown"
                and availability["resume_after"] is None
                and mode != "recovery"
            )
            # Re-evaluate time here: a reset may pass between the snapshot and this loop.
            reasons = snapshot["reasons"] if not unknown_reset and now >= gate else []
            if reasons and quiet_until is None:
                quiet_until = monotonic + coalesce
                ready_limit = monotonic + min(30, coalesce * 4)
            if not reasons:
                quiet_until = ready_limit = None
            if reasons and monotonic >= min(quiet_until, ready_limit, deadline):
                return {
                    **mechanism,
                    "provider": snapshot["provider"],
                    "mode": mode,
                    "effective_mode": snapshot["effective_mode"],
                    "ready": True,
                    "reasons": reasons,
                    "next_step": "Read native team_state and peer_sync; this wait did not mark anything received. A recovery attempt does not clear a quota hold; verify native execution before recording ready.",
                    **(
                        {"recovery": snapshot["recovery"]} if mode == "recovery" else {}
                    ),
                }
            remaining = deadline - monotonic
            if remaining <= 0:
                return {
                    **mechanism,
                    "provider": snapshot["provider"],
                    "mode": mode,
                    "effective_mode": snapshot["effective_mode"],
                    "ready": False,
                    "reason": "deadline",
                    "next_step": "Re-arm quietly only while the user-authorized native conversation remains active",
                }
            # Recheck wall time after suspend or clock adjustment without waking a model.
            delay = min(remaining, gate - now, 60) if gate > now else remaining
            if quiet_until is not None:
                delay = min(delay, max(0, min(quiet_until, ready_limit) - monotonic))
            if fd is None:
                # This is local read-only waiting, not a scheduled model turn.
                time.sleep(min(1.0, delay))
            elif select.select([fd], [], [], delay)[0]:
                try:
                    os.read(fd, 65536)
                except BlockingIOError:
                    pass
                if quiet_until is not None:
                    # Unrelated filesystem traffic cannot postpone work indefinitely.
                    quiet_until = min(time.monotonic() + coalesce, ready_limit)
    finally:
        if fd is not None:
            os.close(fd)
