"""Bounded, read-only workflow evidence; never reconstruct missing transitions."""

import json

TASK_LIMIT = 30
DECISION_LIMIT = 20
EVENT_LIMIT = 200
MESSAGE_LIMIT = 100
# Peer prose kinds shown as collaboration; system notices (membership, quota,
# lifecycle, decision) stay out. Only addressed kinds become graph edges.
MESSAGE_KINDS = ("handoff", "direct", "blocker", "user_instruction")
EDGE_KINDS = ("handoff", "direct")
TASK_STATES = {"ready", "working", "review", "accepted", "blocked", "cancelled"}


def workflow_history(db, team_id, tasks, decisions):
    task_ids = [item["id"] for item in tasks["items"][:TASK_LIMIT]]
    selected_decisions = decisions["items"][:DECISION_LIMIT]
    decision_ids = [item["id"] for item in selected_decisions]
    events = []
    invalid = 0
    truncated = False
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        rows = db.execute(
            f"""SELECT h.task_id,h.revision,h.kind,h.snapshot,h.created,n.alias AS actor
            FROM peer_task_history h JOIN members n ON n.id=h.actor
            WHERE h.task_id IN ({placeholders})
            ORDER BY h.created DESC,h.task_id,h.revision DESC LIMIT ?""",
            (*task_ids, EVENT_LIMIT + 1),
        ).fetchall()
        truncated = len(rows) > EVENT_LIMIT
        for row in rows:
            state = review_round = None
            try:
                snapshot = json.loads(row["snapshot"])
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("state") not in TASK_STATES
                ):
                    raise ValueError("Invalid task snapshot")
                state = snapshot["state"]
                value = snapshot.get("rounds")
                review_round = value if type(value) is int and value >= 0 else None
            except (ValueError, TypeError):
                invalid += 1
            events.append(
                {
                    "id": f"task:{row['task_id']}:{row['revision']}",
                    "target_type": "task",
                    "target_id": row["task_id"],
                    "kind": row["kind"],
                    "state": state,
                    "actor": row["actor"],
                    "at": row["created"],
                    "revision": row["revision"],
                    "round": review_round,
                }
            )
    for decision in selected_decisions:
        events.append(
            {
                "id": f"decision:{decision['id']}:1",
                "target_type": "decision",
                "target_id": decision["id"],
                "kind": "proposed",
                "state": "proposed",
                "actor": decision["author"],
                "at": decision["created"],
                "revision": 1,
            }
        )
    if decision_ids:
        placeholders = ",".join("?" for _ in decision_ids)
        rows = db.execute(
            f"""SELECT d.id,m.request_key,m.body,m.created,n.alias AS actor
            FROM peer_messages m JOIN members n ON n.id=m.sender
            JOIN peer_decisions d ON m.request_key LIKE ('decision:' || d.id || ':%')
            WHERE m.team_id=? AND d.team_id=m.team_id AND m.kind='decision'
            AND d.id IN ({placeholders}) ORDER BY m.created DESC,m.seq DESC LIMIT ?""",
            (team_id, *decision_ids, EVENT_LIMIT + 1),
        ).fetchall()
        truncated |= len(rows) > EVENT_LIMIT
        # These are server-generated decision messages, not arbitrary peer prose.
        # Current votes overwrite earlier votes; they cannot supply a full history.
        for row in rows:
            prefix = f"decision:{row['id']}:"
            revision = row["request_key"][len(prefix) :]
            state = next(
                (
                    s
                    for s in ("agreed", "disputed")
                    if row["body"].startswith(f"Decision {row['id']} is {s}: ")
                ),
                None,
            )
            if not revision.isdecimal() or int(revision) < 2 or state is None:
                invalid += 1
                continue
            events.append(
                {
                    "id": row["request_key"],
                    "target_type": "decision",
                    "target_id": row["id"],
                    "kind": "decision",
                    "state": state,
                    "actor": row["actor"],
                    "at": row["created"],
                    "revision": int(revision),
                }
            )
    messages, more_messages = message_history(db, team_id)
    truncated |= more_messages
    for message in messages:
        recipients = message["recipients"]
        received = sum(r["received"] for r in recipients)
        events.append(
            {
                "id": f"message:{message['seq']}",
                "target_type": "message",
                "target_id": message["id"],
                "kind": message["kind"],
                "state": "received"
                if recipients and received == len(recipients)
                else "sent",
                "actor": message["sender"],
                "at": message["at"],
                "revision": message["seq"],
                "seq": message["seq"],
                "recipients": recipients,
            }
        )
    events.sort(key=lambda e: (e["at"], e["target_id"], e["revision"]))
    truncated |= len(events) > EVENT_LIMIT
    return {
        "task_ids": task_ids,
        "decision_ids": decision_ids,
        "messages": messages,
        "events": events[-EVENT_LIMIT:],
        "truncated": truncated,
        "invalid_records": invalid,
        "limits": {
            "tasks": TASK_LIMIT,
            "decisions": DECISION_LIMIT,
            "messages": MESSAGE_LIMIT,
            "events": EVENT_LIMIT,
        },
        "scope": "Selected tasks' recorded revisions, retained decision proposals/outcomes, and the latest handoff, direct, blocker and user-instruction messages (metadata and delivery receipt only); not a complete audit log.",
    }


def message_history(db, team_id):
    """Latest peer messages with their actual team-scoped delivery recipients.

    Metadata only: the body stays in the activity record, which the chart links to.
    """
    placeholders = ",".join("?" for _ in MESSAGE_KINDS)
    rows = db.execute(
        f"""SELECT m.id,m.seq,m.kind,m.created,n.alias AS sender
        FROM peer_messages m JOIN members n ON n.id=m.sender
        WHERE m.team_id=? AND m.kind IN ({placeholders})
        ORDER BY m.seq DESC LIMIT ?""",
        (team_id, *MESSAGE_KINDS, MESSAGE_LIMIT + 1),
    ).fetchall()
    more = len(rows) > MESSAGE_LIMIT
    rows = rows[:MESSAGE_LIMIT]
    recipients = {row["id"]: [] for row in rows}
    if rows:
        marks = ",".join("?" for _ in rows)
        for delivery in db.execute(
            f"""SELECT d.message_id,r.alias,d.received_at IS NOT NULL AS received
            FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id
            JOIN members r ON r.id=d.recipient
            WHERE m.team_id=? AND d.message_id IN ({marks}) ORDER BY r.alias""",
            (team_id, *recipients),
        ):
            recipients[delivery["message_id"]].append(
                {"alias": delivery["alias"], "received": bool(delivery["received"])}
            )
    messages = [
        {
            "id": row["id"],
            "seq": row["seq"],
            "kind": row["kind"],
            "sender": row["sender"],
            "at": row["created"],
            "recipients": recipients[row["id"]],
            "edge": row["kind"] in EDGE_KINDS,
        }
        for row in reversed(rows)
    ]
    return messages, more
