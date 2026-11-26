"""One durable wake per native Codex recipient, across old and flat-team work."""

import time

from .schema import available
from .store import TeamError, new_id
from .availability import read_availability


class Outbox:
    def __init__(self, store):
        self.store, self.state = store, store.state

    def recipients(self):
        with self.store.readonly() as db:
            result = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT w.lead FROM work w JOIN members m ON m.id=w.lead WHERE w.state='active' AND m.provider='codex'"
                )
            }
            if available(db):
                result.update(
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT p.member_id FROM peer_members p JOIN peer_teams t ON t.id=p.team_id JOIN members m ON m.id=p.member_id WHERE p.state='active' AND m.provider='codex' AND (t.state='active' OR EXISTS(SELECT 1 FROM peer_messages msg JOIN peer_deliveries d ON d.message_id=msg.id WHERE msg.team_id=t.id AND msg.kind='lifecycle' AND d.recipient=p.member_id AND d.received_at IS NULL))"
                    )
                )
            return result

    def watch_work(self, actor):
        self.store.watch_work(actor)

    def reserve_notice(self, actor):
        with self.store.readonly() as db:
            modern = available(db)
            availability = read_availability(db, actor)
            if availability["held"]:
                return None
            if modern:
                legacy_states = [
                    row[0]
                    for row in db.execute(
                        "SELECT n.state FROM notices n JOIN work w ON w.id=n.work_id WHERE n.recipient=? AND n.received_at IS NULL AND w.state='active' AND n.state IN ('pending','sending','queued','queued_warning','uncertain')",
                        (actor,),
                    )
                ]
                peer_rows = db.execute(
                    "SELECT d.state,msg.kind FROM peer_deliveries d JOIN peer_messages msg ON msg.id=d.message_id JOIN peer_members p ON p.team_id=msg.team_id AND p.member_id=d.recipient JOIN peer_teams t ON t.id=msg.team_id WHERE d.recipient=? AND d.received_at IS NULL AND p.state='active' AND (t.state='active' OR msg.kind='lifecycle') AND d.state IN ('pending','sending','queued','queued_warning','uncertain')",
                    (actor,),
                ).fetchall()
                if not legacy_states and not peer_rows:
                    return None
                if any(s != "pending" for s in legacy_states) or any(
                    r["state"] != "pending" for r in peer_rows
                ):
                    return None
                if (
                    availability["posture"] == "conserve"
                    and not legacy_states
                    and not any(r["kind"] != "milestone" for r in peer_rows)
                ):
                    return None
            else:
                if not db.execute(
                    "SELECT 1 FROM notices n JOIN work w ON w.id=n.work_id WHERE n.recipient=? AND n.state='pending' AND n.received_at IS NULL AND w.state='active' LIMIT 1",
                    (actor,),
                ).fetchone():
                    return None
        if not modern:
            return self.store.reserve_notice(actor)
        with self.store.transaction() as db:
            member = self.store.member(db, actor, "codex")
            # Recheck under the same writer transaction that reserves the send. A cooldown
            # arriving between inspections must not allow a new native notification.
            availability = read_availability(db, actor)
            if availability["held"]:
                return None
            # An accepted or uncertain native enqueue may still wake the same conversation.
            # New messages remain durable but do not produce a storm while receipt is unknown.
            legacy = db.execute(
                "SELECT n.* FROM notices n JOIN work w ON w.id=n.work_id WHERE n.recipient=? AND n.received_at IS NULL AND w.state='active' AND n.state IN ('pending','sending','queued','queued_warning','uncertain') ORDER BY n.created",
                (actor,),
            ).fetchall()
            peers = db.execute(
                "SELECT d.*,msg.kind FROM peer_deliveries d JOIN peer_messages msg ON msg.id=d.message_id JOIN peer_members p ON p.team_id=msg.team_id AND p.member_id=d.recipient JOIN peer_teams t ON t.id=msg.team_id WHERE d.recipient=? AND d.received_at IS NULL AND p.state='active' AND (t.state='active' OR msg.kind='lifecycle') AND d.state IN ('pending','sending','queued','queued_warning','uncertain') ORDER BY msg.seq",
                (actor,),
            ).fetchall()
            if any(row["state"] != "pending" for row in [*legacy, *peers]):
                return None
            # Defer FYI milestones only. Keep all task/review transitions, membership,
            # decisions, user steering and blockers; include retained FYIs on the next wake.
            if (
                availability["posture"] == "conserve"
                and not legacy
                and not any(row["kind"] != "milestone" for row in peers)
            ):
                return None
            items = [("legacy", row["id"]) for row in legacy[:200]] + [
                ("peer", row["id"]) for row in peers[:200]
            ]
            if not items:
                return None
            identity, stamp = new_id(), time.time()
            db.execute(
                "INSERT INTO wake_batches(id,recipient,state,created,updated) VALUES(?,?,'sending',?,?)",
                (identity, actor, stamp, stamp),
            )
            for origin, item in items:
                table = "notices" if origin == "legacy" else "peer_deliveries"
                db.execute(
                    f"UPDATE {table} SET state='sending' WHERE id=? AND state='pending'",
                    (item,),
                )
                db.execute(
                    "INSERT INTO wake_items VALUES(?,?,?)", (identity, origin, item)
                )
            return {"id": identity, "native_id": member["native_id"]}

    def notice_result(self, notice_id, state, detail):
        if state not in {"queued", "queued_warning", "uncertain", "failed"}:
            raise TeamError("Unsupported notification result")
        with self.store.transaction() as db:
            batch = (
                db.execute(
                    "SELECT 1 FROM wake_batches WHERE id=?", (notice_id,)
                ).fetchone()
                if available(db)
                else None
            )
            if batch:
                db.execute(
                    "UPDATE wake_batches SET state=?,detail=?,updated=? WHERE id=? AND state='sending'",
                    (state, detail, time.time(), notice_id),
                )
                for item in db.execute(
                    "SELECT * FROM wake_items WHERE batch_id=?", (notice_id,)
                ):
                    table = (
                        "notices" if item["origin"] == "legacy" else "peer_deliveries"
                    )
                    db.execute(
                        f"UPDATE {table} SET state=?,detail=? WHERE id=? AND state='sending'",
                        (state, detail, item["item_id"]),
                    )
        if not batch:
            self.store.notice_result(notice_id, state, detail)

    def recover_notices(self, actor):
        self.store.recover_notices(actor)
        with self.store.transaction() as db:
            if available(db):
                detail = "Sender stopped during native enqueue; inspect receipt before retrying"
                db.execute(
                    "UPDATE peer_deliveries SET state='uncertain',detail=? WHERE recipient=? AND state='sending'",
                    (detail, actor),
                )
                db.execute(
                    "UPDATE wake_batches SET state='uncertain',detail=?,updated=? WHERE recipient=? AND state='sending'",
                    (detail, time.time(), actor),
                )
