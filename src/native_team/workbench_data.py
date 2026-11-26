"""Read projections for the local workbench. No Store, enrollment or receipts."""

import json
import math
from pathlib import Path
import time

from .change_manifest import latest
from . import checkouts
from .inspection import readonly_snapshot
from .monitor import running as monitor_running
from .schema import available, has_availability
from .store import TeamError
from .availability import member_progress
from .workbench_workflow import workflow_history
from .quota_collector import collector_status
from .quota_store import shared_claude_account, claude_display_sources

PAGE_SIZE = 50
SECTIONS = {
    "tasks": "peer_tasks",
    "activity": "peer_messages",
    "decisions": "peer_decisions",
}


class SchemaError(TeamError):
    """A readable ledger needs an explicit schema upgrade, not an implicit UI write."""


def _schema(db):
    if db is not None and not available(db):
        raise SchemaError(
            "工作台无法读取此版本的团队记录；请先检查 Native Team 安装与迁移状态。"
        )


def _monitor_summary(state, db, team_id=None):
    active = monitor_running(Path(state).expanduser().resolve())
    if db is None:
        return {
            "running": active,
            "active_codex": 0,
            "pending_codex": 0,
            "all_active_codex": 0,
            "all_pending_codex": 0,
        }
    all_active = db.execute(
        """SELECT count(DISTINCT pm.member_id) FROM peer_members pm
        JOIN peer_teams t ON t.id=pm.team_id JOIN members m ON m.id=pm.member_id
        WHERE t.state='active' AND pm.state='active' AND m.provider='codex'"""
    ).fetchone()[0]
    all_pending = db.execute(
        """SELECT count(*) FROM peer_deliveries d
        JOIN peer_messages msg ON msg.id=d.message_id
        JOIN peer_members pm ON pm.team_id=msg.team_id AND pm.member_id=d.recipient
        JOIN peer_teams t ON t.id=msg.team_id
        JOIN members m ON m.id=d.recipient
        WHERE m.provider='codex' AND pm.state='active'
        AND (t.state='active' OR msg.kind='lifecycle')
        AND d.received_at IS NULL AND d.state='pending'"""
    ).fetchone()[0]
    if team_id is None:
        team_active, team_pending = all_active, all_pending
    else:
        team_active = db.execute(
            """SELECT count(*) FROM peer_members pm
            JOIN peer_teams t ON t.id=pm.team_id JOIN members m ON m.id=pm.member_id
            WHERE t.id=? AND t.state='active' AND pm.state='active' AND m.provider='codex'""",
            (team_id,),
        ).fetchone()[0]
        team_pending = db.execute(
            """SELECT count(*) FROM peer_deliveries d
            JOIN peer_messages msg ON msg.id=d.message_id
            JOIN peer_members pm ON pm.team_id=msg.team_id AND pm.member_id=d.recipient
            JOIN peer_teams t ON t.id=msg.team_id
            JOIN members m ON m.id=d.recipient
            WHERE t.id=? AND m.provider='codex' AND pm.state='active'
            AND (t.state='active' OR msg.kind='lifecycle')
            AND d.received_at IS NULL AND d.state='pending'""",
            (team_id,),
        ).fetchone()[0]
    return {
        "running": active,
        "active_codex": team_active,
        "pending_codex": team_pending,
        "all_active_codex": all_active,
        "all_pending_codex": all_pending,
    }


def directory(state):
    with readonly_snapshot(state) as db:
        _schema(db)
        stamp = time.time()
        if db is None:
            return {
                "teams": [],
                "read_at": stamp,
                "monitor": _monitor_summary(state, None),
            }
        cooldowns = "0"
        args = ()
        if has_availability(db):
            cooldowns = """(SELECT count(*) FROM peer_members pm
                JOIN member_availability a ON a.member_id=pm.member_id
                WHERE pm.team_id=t.id AND pm.state='active' AND a.status='cooldown'
                AND (a.resume_after IS NULL OR a.resume_after>?))"""
            args = (stamp,)
        rows = []
        # One read transaction and one aggregate query, independent of team count.
        for row in db.execute(
            f"""SELECT t.id,t.name,t.project,t.state,t.updated,
            (SELECT count(*) FROM peer_decisions p WHERE p.team_id=t.id
                AND p.state IN ('proposed','disputed')) AS decisions,
            (SELECT count(*) FROM peer_tasks p WHERE p.team_id=t.id
                AND p.state NOT IN ('accepted','cancelled')
                AND (p.handoff_needed=1 OR p.reviewer_handoff_needed=1)) AS handoff_tasks,
            (SELECT count(*) FROM peer_tasks p WHERE p.team_id=t.id
                AND p.state='blocked') AS blocked_tasks,
            (SELECT count(*) FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id
                WHERE m.team_id=t.id AND d.received_at IS NULL
                AND d.state IN ('failed','uncertain','queued_warning')) AS deliveries,
            {cooldowns} AS cooldowns
            FROM peer_teams t ORDER BY t.state IN ('active','paused') DESC,t.project,t.name,t.id""",
            args,
        ):
            item = dict(row)
            item["attention"] = {
                key: item.pop(key)
                for key in (
                    "decisions",
                    "handoff_tasks",
                    "blocked_tasks",
                    "deliveries",
                    "cooldowns",
                )
            }
            if item["state"] in {"done", "cancelled"}:
                item["attention"].pop("cooldowns")
            rows.append(item)
        return {
            "teams": rows,
            "read_at": stamp,
            "monitor": _monitor_summary(state, db),
        }


def _team(db, identity):
    _schema(db)
    row = (
        None
        if db is None
        else db.execute(
            "SELECT id,name,project,purpose,state,created,updated FROM peer_teams WHERE id=?",
            (identity,),
        ).fetchone()
    )
    if row is None:
        raise LookupError("未找到此团队；请刷新团队列表。")
    return dict(row)


def _cursor(section, value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("Invalid history cursor")
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("Invalid history cursor") from error
    if section == "activity":
        if type(parsed) is not int or parsed < 1:
            raise ValueError("Invalid message cursor")
    elif not (
        isinstance(parsed, list)
        and len(parsed) == 2
        and type(parsed[0]) in (float, int)
        and math.isfinite(parsed[0])
        and isinstance(parsed[1], str)
        and 0 < len(parsed[1]) <= 100
    ):
        raise ValueError("Invalid record cursor")
    return parsed


def _page(db, identity, section, cursor=None):
    if section not in SECTIONS:
        raise ValueError("Unknown history section")
    before = _cursor(section, cursor)
    args = [identity]
    where = "p.team_id=?"
    order = "p.created DESC,p.id DESC"
    if section == "activity":
        fields = """p.seq,p.id,p.kind,p.body,p.created,n.alias AS author,
            (SELECT count(*) FROM peer_deliveries d WHERE d.message_id=p.id) AS recipients,
            (SELECT count(*) FROM peer_deliveries d WHERE d.message_id=p.id AND d.received_at IS NOT NULL) AS received,
            (SELECT count(*) FROM peer_deliveries d WHERE d.message_id=p.id AND d.acked_at IS NOT NULL) AS acknowledged"""
        joins = "JOIN members n ON n.id=p.sender"
        order = "p.seq DESC"
        if before is not None:
            where += " AND p.seq<?"
            args.append(before)
    else:
        if before is not None:
            where += " AND (p.created,p.id)<(?,?)"
            args.extend(before)
        if section == "tasks":
            fields = """p.id,p.brief,p.acceptance,p.state,p.instruction,p.result,p.evidence,
                p.handoff_needed,p.reviewer_handoff_needed,p.paths,
                p.created,p.updated,o.alias AS owner,r.alias AS reviewer,c.alias AS requester"""
            joins = "JOIN members o ON o.id=p.owner JOIN members r ON r.id=p.reviewer JOIN members c ON c.id=p.created_by"
        else:
            fields = "p.id,p.topic,p.proposal,p.state,p.created,p.authority,n.alias AS author"
            joins = "JOIN members n ON n.id=p.proposed_by"
    # Table and column fragments are constants selected above; user values remain parameters.
    table = SECTIONS[section]
    rows = [
        dict(row)
        for row in db.execute(
            f"SELECT {fields} FROM {table} p {joins} WHERE {where} ORDER BY {order} LIMIT ?",
            (*args, PAGE_SIZE + 1),
        )
    ]
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = json.dumps(
            last["seq"] if section == "activity" else [last["created"], last["id"]]
        )
    if section == "decisions":
        for row in rows:
            row["responses"] = [
                dict(vote)
                for vote in db.execute(
                    "SELECT n.alias,v.response,v.note,v.updated FROM peer_votes v "
                    "JOIN members n ON n.id=v.member_id WHERE v.decision_id=? ORDER BY n.alias",
                    (row["id"],),
                )
            ]
    elif section == "activity":
        for row in rows:
            row["deliveries"] = [
                dict(delivery)
                for delivery in db.execute(
                    "SELECT n.alias,d.state,d.received_at,d.acked_at FROM peer_deliveries d "
                    "JOIN members n ON n.id=d.recipient WHERE d.message_id=? ORDER BY n.alias",
                    (row["id"],),
                )
            ]
            row["delivery_attention"] = sum(
                delivery["received_at"] is None
                and delivery["state"] in {"failed", "uncertain", "queued_warning"}
                for delivery in row["deliveries"]
            )
    elif section == "tasks":
        for row in rows:
            try:
                row["changes"] = latest(db, row["id"])
                row["paths"] = json.loads(row["paths"])
            except (json.JSONDecodeError, TypeError) as error:
                raise TeamError("无法解析任务中保存的写入范围。") from error
    return {
        "items": rows,
        "total": db.execute(
            f"SELECT count(*) FROM {table} WHERE team_id=?", (identity,)
        ).fetchone()[0],
        "next_cursor": next_cursor,
    }


def team_detail(state, identity):
    with readonly_snapshot(state) as db:
        stamp = time.time()
        result = _team(db, identity)
        result["members"] = [
            dict(row)
            for row in db.execute(
                """SELECT n.id AS member_id,n.alias,n.provider,p.lane,p.state,p.paths,
                b.current,b.next_step,b.blockers,b.handoffs,b.updated AS reported_at
            FROM peer_members p JOIN members n ON n.id=p.member_id
            LEFT JOIN peer_boards b ON b.team_id=p.team_id AND b.member_id=p.member_id
            WHERE p.team_id=? ORDER BY n.alias""",
                (identity,),
            )
        ]
        for member in result["members"]:
            actor = member.pop("member_id")
            member.update(
                checkouts.member_view(
                    db,
                    result["project"],
                    actor,
                    json.loads(member.pop("paths")),
                    team_id=identity,
                )
            )
            if result["state"] not in {"done", "cancelled"}:
                member.update(member_progress(db, identity, actor, now=stamp))
        result["task_counts"] = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT state,count(*) FROM peer_tasks WHERE team_id=? GROUP BY state",
                (identity,),
            )
        }
        result["decision_counts"] = dict(
            db.execute(
                "SELECT state,count(*) FROM peer_decisions WHERE team_id=? GROUP BY state",
                (identity,),
            ).fetchall()
        )
        result["attention"] = {
            "handoff_tasks": db.execute(
                "SELECT count(*) FROM peer_tasks WHERE team_id=? AND state NOT IN ('accepted','cancelled') "
                "AND (handoff_needed=1 OR reviewer_handoff_needed=1)",
                (identity,),
            ).fetchone()[0],
            "deliveries": db.execute(
                "SELECT count(*) FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id "
                "WHERE m.team_id=? AND d.received_at IS NULL AND d.state IN ('failed','uncertain','queued_warning')",
                (identity,),
            ).fetchone()[0],
        }
        for section in SECTIONS:
            result[section] = _page(db, identity, section)
        result["workflow"] = workflow_history(
            db, identity, result["tasks"], result["decisions"]
        )
        result["read_at"] = stamp
        result["monitor"] = _monitor_summary(state, db, identity)
        if result["state"] not in {"done", "cancelled"}:
            collector = collector_status(state)
            account = shared_claude_account(state, db, now=stamp, team_id=identity)
            if account or "claude" in collector:
                collector.setdefault("claude", {})["shared_account"] = (
                    claude_display_sources(account)
                )
            result["quota_collector"] = collector
        return result


def record_page(state, identity, section, cursor=None):
    with readonly_snapshot(state) as db:
        _team(db, identity)
        return _page(db, identity, section, cursor)
