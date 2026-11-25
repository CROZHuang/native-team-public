"""Flat project teams: peer context and per-task accountability, never provider seniority."""

import json
from pathlib import Path
import sys
import time
import unicodedata

from . import checkouts
from .schema import available, has_availability
from .store import TeamError, checked, new_id
from .availability import conserve_guidance, member_progress, read_availability


def overlaps(first, second):
    if sys.platform in {"darwin", "win32"}:
        # Default Mac and Windows volumes admit alternate spellings of the same file.
        # Reserve conservatively even for future files, whose identity cannot be probed yet.
        first = unicodedata.normalize("NFC", first.casefold())
        second = unicodedata.normalize("NFC", second.casefold())
    return (
        first == "."
        or second == "."
        or first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
    )


class PeerStore:
    def __init__(self, store):
        self.store = store

    def overview(self, actor, *, include_closed=False):
        with self.store.transaction() as db:
            if not available(db):
                return {"teams": [], "closed_team_count": 0}
            self.store.member(db, actor)
            memberships = "FROM peer_teams t JOIN peer_members p ON p.team_id=t.id WHERE p.member_id=?"
            open_only = "" if include_closed else " AND t.state IN ('active','paused')"
            teams = [
                dict(r)
                for r in db.execute(
                    "SELECT t.id,t.name,t.purpose,t.state,t.epoch,p.lane,p.state AS member_state,p.acked_epoch,"
                    "(SELECT count(*) FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id "
                    "WHERE m.team_id=t.id AND d.recipient=p.member_id AND d.acked_at IS NULL) AS unread "
                    f"{memberships}{open_only} ORDER BY t.state IN ('active','paused') DESC,t.created DESC",
                    (actor,),
                )
            ]
            result = {
                "teams": teams,
                "closed_team_count": db.execute(
                    f"SELECT count(*) {memberships} AND t.state IN ('done','cancelled')",
                    (actor,),
                ).fetchone()[0],
            }
            if not include_closed and result["closed_team_count"]:
                pending = [
                    r[0]
                    for r in db.execute(
                        f"SELECT t.id {memberships} AND t.state IN ('done','cancelled') AND EXISTS ("
                        "SELECT 1 FROM peer_messages m JOIN peer_deliveries d ON d.message_id=m.id "
                        "WHERE m.team_id=t.id AND m.kind='lifecycle' AND d.recipient=p.member_id "
                        "AND d.acked_at IS NULL) ORDER BY t.created",
                        (actor,),
                    )
                ]
                if pending:
                    result["pending_closed_team_ids"] = pending
            if has_availability(db):
                recorded_peers = db.execute(
                    "SELECT DISTINCT peer.member_id FROM peer_members mine "
                    "JOIN peer_teams t ON t.id=mine.team_id JOIN peer_members peer ON peer.team_id=t.id "
                    "JOIN member_availability a ON a.member_id=peer.member_id "
                    "WHERE mine.member_id=? AND mine.state='active' AND t.state='active' AND peer.state='active'",
                    (actor,),
                )
                result.update(
                    conserve_guidance(
                        read_availability(db, r[0]) for r in recorded_peers
                    )
                )
            return result

    def inspect(self, project):
        project = str(checkouts.project_path(self.store.state, project))
        with self.store.transaction() as db:
            if not available(db):
                return []
            teams = []
            for row in db.execute(
                "SELECT id,name,state FROM peer_teams WHERE project=? ORDER BY state IN ('active','paused') DESC,created DESC LIMIT 32",
                (project,),
            ):
                team = dict(row)
                team["members"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT n.id AS member_id,n.alias,n.provider,m.lane,m.state,m.paths FROM peer_members m JOIN members n ON n.id=m.member_id WHERE m.team_id=? ORDER BY n.alias",
                        (row["id"],),
                    )
                ]
                for member in team["members"]:
                    actor = member.pop("member_id")
                    member.update(
                        checkouts.member_view(
                            db,
                            project,
                            actor,
                            json.loads(member.pop("paths")),
                            team_id=team["id"],
                        )
                    )
                    if team["state"] not in {"done", "cancelled"}:
                        member.update(member_progress(db, team["id"], actor))
                team["tasks"] = {
                    r["state"]: r["count"]
                    for r in db.execute(
                        "SELECT state,count(*) AS count FROM peer_tasks WHERE team_id=? GROUP BY state",
                        (row["id"],),
                    )
                }
                team["delivery_attention"] = db.execute(
                    "SELECT count(*) FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.received_at IS NULL AND d.state IN ('failed','uncertain','queued_warning')",
                    (row["id"],),
                ).fetchone()[0]
                teams.append(team)
            return teams

    def receipts(self, actor, team_id, message_id):
        with self.store.transaction() as db:
            self.member(db, actor, team_id, active=False)
            row = db.execute(
                "SELECT id FROM peer_messages WHERE id=? AND team_id=?",
                (message_id, team_id),
            ).fetchone()
            if row is None:
                raise TeamError("Message is not part of this team")
            return {
                "message_id": message_id,
                "recipients": [
                    dict(r)
                    for r in db.execute(
                        "SELECT m.alias,d.state,d.detail,d.received_at,d.acked_at FROM peer_deliveries d JOIN members m ON m.id=d.recipient WHERE d.message_id=?",
                        (message_id,),
                    )
                ],
            }

    def require_schema(self, db):
        if not available(db):
            raise TeamError(
                "Flat teams need the verified ledger upgrade; legacy work remains available"
            )

    def member(self, db, actor, team_id, *, active=True, synced=False):
        self.require_schema(db)
        native = self.store.member(db, actor)
        team = db.execute("SELECT * FROM peer_teams WHERE id=?", (team_id,)).fetchone()
        member = db.execute(
            "SELECT * FROM peer_members WHERE team_id=? AND member_id=?",
            (team_id, actor),
        ).fetchone()
        if team is None or member is None or team["project"] != native["project"]:
            raise TeamError(
                "This native conversation is not a member of that project team"
            )
        if active and (team["state"] != "active" or member["state"] != "active"):
            raise TeamError(
                "This team/member is inactive; reconcile membership before new work"
            )
        if synced and member["acked_epoch"] != team["epoch"]:
            raise TeamError(
                "Fetch and acknowledge the current team snapshot before claiming or assigning work"
            )
        return team, member

    def by_alias(self, db, project, alias):
        row = db.execute(
            "SELECT * FROM members WHERE project=? AND alias=?", (project, alias)
        ).fetchone()
        if row is None:
            raise TeamError(
                "The named native conversation has not enrolled in this project"
            )
        return row

    def paths(self, project, values):
        if not isinstance(values, list) or len(values) > 32:
            raise TeamError(
                "Write scopes must be a list of at most 32 project-relative paths"
            )
        root = Path(project).resolve()
        result = []
        for value in values:
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or Path(value).is_absolute()
                or any(c in value for c in "\r\n\0*?[\\")
            ):
                raise TeamError(
                    "Write scopes must be plain relative paths, without wildcards"
                )
            target = (root / value).resolve()
            if not target.is_relative_to(root):
                raise TeamError("A write scope escapes the project")
            name = target.relative_to(root).as_posix()
            if name not in result:
                result.append(name)
        return sorted(result)

    def _publish(self, db, actor, team_id, kind, body, request_key, recipients=None):
        body, request_key = (
            checked(body, "Message", 6000),
            checked(request_key, "Message key", 128),
        )
        roster = {
            r[0]
            for r in db.execute(
                "SELECT member_id FROM peer_members WHERE team_id=?", (team_id,)
            )
        }
        recipients = roster - {actor} if recipients is None else set(recipients)
        if not recipients <= roster or actor not in roster:
            raise TeamError("Messages may only be sent between selected team members")
        prior = db.execute(
            "SELECT * FROM peer_messages WHERE team_id=? AND sender=? AND request_key=?",
            (team_id, actor, request_key),
        ).fetchone()
        if prior:
            previous = {
                r[0]
                for r in db.execute(
                    "SELECT recipient FROM peer_deliveries WHERE message_id=?",
                    (prior["id"],),
                )
            }
            if (prior["kind"], prior["body"], previous) != (kind, body, recipients):
                raise TeamError(
                    "Message key already identifies different content or recipients"
                )
            return dict(prior)
        count = db.execute(
            "SELECT count(*) FROM peer_messages m WHERE team_id=? AND EXISTS(SELECT 1 FROM peer_deliveries d JOIN peer_members p ON p.team_id=m.team_id AND p.member_id=d.recipient WHERE d.message_id=m.id AND d.acked_at IS NULL AND p.state='active')",
            (team_id,),
        ).fetchone()[0]
        if count >= 2000:
            raise TeamError(
                "Unread team message limit reached; catch up active peers before adding more"
            )
        identity, stamp = new_id(), time.time()
        db.execute(
            "INSERT INTO peer_messages(id,team_id,sender,kind,body,request_key,created) VALUES(?,?,?,?,?,?,?)",
            (identity, team_id, actor, kind, body, request_key, stamp),
        )
        db.executemany(
            "INSERT INTO peer_deliveries(id,message_id,recipient) VALUES(?,?,?)",
            [(new_id(), identity, receiver) for receiver in sorted(recipients)],
        )
        return dict(
            db.execute("SELECT * FROM peer_messages WHERE id=?", (identity,)).fetchone()
        )

    def create(self, actor, name, purpose, aliases, request_key):
        name, purpose, request_key = (
            checked(name, "Team name", 100),
            checked(purpose, "Team purpose"),
            checked(request_key, "Team key", 128),
        )
        if (
            not isinstance(aliases, list)
            or not 2 <= len(aliases) <= 12
            or any(not isinstance(a, str) or not a.strip() for a in aliases)
            or len(set(aliases)) != len(aliases)
        ):
            raise TeamError(
                "Choose 2–12 distinct enrolled conversations, including this one"
            )
        with self.store.transaction() as db:
            self.require_schema(db)
            native = self.store.member(db, actor)
            peers = [self.by_alias(db, native["project"], alias) for alias in aliases]
            native_ids = [
                (p["provider"], p["native_id"])
                for p in peers
                if p["native_id"] is not None
            ]
            if len(set(native_ids)) != len(native_ids):
                raise TeamError(
                    "Two aliases refer to the same native conversation; choose independent peers"
                )
            ids = {p["id"] for p in peers}
            if actor not in ids:
                raise TeamError("A team initiator must join the selected team")
            prior = db.execute(
                "SELECT * FROM peer_teams WHERE created_by=? AND request_key=?",
                (actor, request_key),
            ).fetchone()
            if prior:
                existing = {
                    r[0]
                    for r in db.execute(
                        "SELECT member_id FROM peer_members WHERE team_id=?",
                        (prior["id"],),
                    )
                }
                if (prior["name"], prior["purpose"], existing) != (name, purpose, ids):
                    raise TeamError("Team key already identifies a different team")
                return dict(prior)
            for identity in ids:
                if (
                    db.execute(
                        "SELECT count(*) FROM peer_teams t JOIN peer_members m ON m.team_id=t.id WHERE m.member_id=? AND t.state IN ('active','paused')",
                        (identity,),
                    ).fetchone()[0]
                    >= 8
                ):
                    raise TeamError(
                        "A selected conversation already has eight open teams; reconcile one first"
                    )
            team_id, stamp = new_id(), time.time()
            db.execute(
                "INSERT INTO peer_teams(id,project,name,purpose,created_by,request_key,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (
                    team_id,
                    native["project"],
                    name,
                    purpose,
                    actor,
                    request_key,
                    stamp,
                    stamp,
                ),
            )
            db.executemany(
                "INSERT INTO peer_members(team_id,member_id) VALUES(?,?)",
                [(team_id, peer) for peer in sorted(ids)],
            )
            self._publish(
                db,
                actor,
                team_id,
                "membership",
                f"Team {name} formed. Peers: {', '.join(aliases)}. Purpose: {purpose}",
                "created",
            )
            return dict(
                db.execute("SELECT * FROM peer_teams WHERE id=?", (team_id,)).fetchone()
            )

    def update_member(
        self,
        actor,
        team_id,
        alias,
        revision,
        *,
        lane=None,
        state=None,
        paths=None,
        user_instruction="",
    ):
        if state is not None and state not in {"active", "inactive"}:
            raise TeamError("Member state must be active or inactive")
        if lane is not None:
            lane = checked(lane, "Workstream", 1200)
        with self.store.transaction() as db:
            team, acting = self.member(db, actor, team_id, active=False)
            if team["state"] != "active":
                raise TeamError("Team is not active")
            target = self.by_alias(db, team["project"], alias)
            if actor != target["id"]:
                if acting["state"] != "active":
                    raise TeamError("Inactive members cannot change other peers")
                user_instruction = checked(
                    user_instruction, "Explicit user instruction for another peer", 2000
                )
            old = db.execute(
                "SELECT * FROM peer_members WHERE team_id=? AND member_id=?",
                (team_id, target["id"]),
            ).fetchone()
            adding = old is None
            if adding:
                if revision != 0 or not user_instruction:
                    raise TeamError(
                        "Adding a peer requires revision 0 and an explicit user instruction"
                    )
                if (
                    db.execute(
                        "SELECT count(*) FROM peer_members WHERE team_id=?", (team_id,)
                    ).fetchone()[0]
                    >= 12
                ):
                    raise TeamError("Team membership limit reached")
                if (
                    db.execute(
                        "SELECT count(*) FROM peer_teams t JOIN peer_members m ON m.team_id=t.id WHERE m.member_id=? AND t.state IN ('active','paused')",
                        (target["id"],),
                    ).fetchone()[0]
                    >= 8
                ):
                    raise TeamError("This conversation already has eight open teams")
                db.execute(
                    "INSERT INTO peer_members(team_id,member_id) VALUES(?,?)",
                    (team_id, target["id"]),
                )
                old = db.execute(
                    "SELECT * FROM peer_members WHERE team_id=? AND member_id=?",
                    (team_id, target["id"]),
                ).fetchone()
            elif type(revision) is not int or old["revision"] != revision:
                raise TeamError("Stale membership revision; inspect current state")
            new_state = state or old["state"]
            new_paths = (
                self.paths(
                    checkouts.location(db, target["id"]).get(
                        "checkout", team["project"]
                    ),
                    paths,
                )
                if paths is not None
                else json.loads(old["paths"])
            )
            if (
                not adding
                and (lane is None or lane == old["lane"])
                and new_state == old["state"]
                and new_paths == json.loads(old["paths"])
            ):
                return {
                    **dict(old),
                    **checkouts.member_view(
                        db, team["project"], target["id"], new_paths, team_id=team["id"]
                    ),
                }
            if new_state == "active":
                collisions = checkouts.conflicts(
                    db, team["project"], target["id"], new_paths, team_id=team["id"]
                )
                if any(c["hard"] for c in collisions):
                    raise TeamError(
                        "Write scopes overlap an active peer or in-flight claim; arrange a handoff"
                    )
            db.execute(
                "UPDATE peer_members SET lane=?,state=?,paths=?,revision=revision+1 WHERE team_id=? AND member_id=?",
                (
                    lane if lane is not None else old["lane"],
                    new_state,
                    json.dumps(new_paths),
                    team_id,
                    target["id"],
                ),
            )
            db.execute(
                "UPDATE peer_teams SET epoch=epoch+1,revision=revision+1,updated=? WHERE id=?",
                (time.time(), team_id),
            )
            if new_state == "inactive":
                db.execute(
                    "UPDATE peer_tasks SET handoff_needed=1 WHERE team_id=? AND owner=? AND state IN ('ready','working')",
                    (team_id, target["id"]),
                )
                db.execute(
                    "UPDATE peer_tasks SET reviewer_handoff_needed=1 WHERE team_id=? AND reviewer=? AND state NOT IN ('accepted','cancelled')",
                    (team_id, target["id"]),
                )
            self._publish(
                db,
                actor,
                team_id,
                "membership",
                f"Member {alias}: {new_state}; workstream={lane if lane is not None else old['lane']}; write scopes={new_paths}. Reason: {user_instruction or 'self update'}. Existing native turns are not forcibly stopped.",
                f"member:{target['id']}:{old['revision'] + 1}",
            )
            member = dict(
                db.execute(
                    "SELECT * FROM peer_members WHERE team_id=? AND member_id=?",
                    (team_id, target["id"]),
                ).fetchone()
            )
            member.update(
                checkouts.member_view(
                    db, team["project"], target["id"], new_paths, team_id=team["id"]
                )
            )
            return member

    def set_state(self, actor, team_id, revision, state, user_instruction, evidence=""):
        if state not in {"active", "paused", "done", "cancelled"}:
            raise TeamError("Team state must be active, paused, done or cancelled")
        instruction = checked(
            user_instruction,
            "Explicit user direction for this team-wide lifecycle change",
            2000,
        )
        if state == "done":
            evidence = checked(evidence, "Integrated acceptance evidence", 6000)
        elif not isinstance(evidence, str) or len(evidence) > 6000:
            raise TeamError("Evidence must be text up to 6000 characters")
        with self.store.transaction() as db:
            team, membership = self.member(db, actor, team_id, active=False)
            if type(revision) is not int or team["revision"] != revision:
                raise TeamError("Stale team revision")
            if membership["state"] != "active" or team["state"] in {
                "done",
                "cancelled",
            }:
                raise TeamError(
                    "An active member is required and closed teams cannot be changed"
                )
            if state == team["state"]:
                return dict(team)
            if state == "active":
                for own in db.execute(
                    "SELECT member_id,paths FROM peer_members WHERE team_id=? AND state='active'",
                    (team_id,),
                ):
                    reserved = checkouts.conflicts(
                        db,
                        team["project"],
                        own["member_id"],
                        json.loads(own["paths"]),
                        skip_team=team_id,
                        team_id=team["id"],
                    )
                    if any(c["hard"] for c in reserved):
                        raise TeamError(
                            "Cannot resume overlapping project write scopes; arrange a handoff first"
                        )
            if (
                state in {"done", "cancelled"}
                and db.execute(
                    "SELECT 1 FROM peer_tasks WHERE team_id=? AND state NOT IN ('accepted','cancelled') LIMIT 1",
                    (team_id,),
                ).fetchone()
            ):
                raise TeamError(
                    "Finish, release or cancel outstanding tasks before closing the team"
                )
            self._publish(
                db,
                actor,
                team_id,
                "lifecycle",
                f"Team {state}. User direction: {instruction}\nEvidence: {evidence}",
                f"lifecycle:{revision + 1}",
            )
            db.execute(
                "UPDATE peer_teams SET state=?,epoch=epoch+1,revision=revision+1,updated=? WHERE id=?",
                (state, time.time(), team_id),
            )
            return dict(
                db.execute("SELECT * FROM peer_teams WHERE id=?", (team_id,)).fetchone()
            )

    def message(self, actor, team_id, kind, body, request_key, recipients=None):
        if kind not in {
            "direct",
            "milestone",
            "handoff",
            "blocker",
            "user_instruction",
        }:
            raise TeamError(
                "Choose direct, milestone, handoff, blocker or user_instruction"
            )
        if kind in {"direct", "handoff"} and recipients is None:
            raise TeamError("Direct messages and handoffs need explicit recipients")
        if kind == "user_instruction" and recipients is not None:
            raise TeamError(
                "Shared user instructions must broadcast to every selected peer"
            )
        if recipients is not None and (
            not isinstance(recipients, list)
            or not recipients
            or any(not isinstance(r, str) or not r.strip() for r in recipients)
            or len(set(recipients)) != len(recipients)
        ):
            raise TeamError(
                "Recipients must be a nonempty list of distinct peer aliases"
            )
        with self.store.transaction() as db:
            team, _ = self.member(db, actor, team_id)
            ids = (
                None
                if recipients is None
                else [
                    self.by_alias(db, team["project"], name)["id"]
                    for name in recipients
                ]
            )
            if ids and actor in ids:
                raise TeamError(
                    "Send to other peers; use your board for your own progress"
                )
            prior = db.execute(
                "SELECT 1 FROM peer_messages WHERE team_id=? AND sender=? AND request_key=?",
                (team_id, actor, request_key),
            ).fetchone()
            result = self._publish(db, actor, team_id, kind, body, request_key, ids)
            if kind == "user_instruction" and not prior:
                db.execute(
                    "UPDATE peer_teams SET epoch=epoch+1,revision=revision+1,updated=? WHERE id=?",
                    (time.time(), team_id),
                )
            return result

    def board(
        self, actor, team_id, revision, current, next_step, blockers="", handoffs=""
    ):
        values = [current, next_step, blockers, handoffs]
        if any(not isinstance(v, str) or len(v) > 2000 for v in values):
            raise TeamError("Each board field must be text of at most 2000 characters")
        with self.store.transaction() as db:
            self.member(db, actor, team_id)
            old = db.execute(
                "SELECT * FROM peer_boards WHERE team_id=? AND member_id=?",
                (team_id, actor),
            ).fetchone()
            if type(revision) is not int or revision != (old["revision"] if old else 0):
                raise TeamError(
                    "Stale board revision; each peer writes only its own section"
                )
            db.execute(
                "INSERT INTO peer_boards(team_id,member_id,revision,current,next_step,blockers,handoffs,updated) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(team_id,member_id) DO UPDATE SET revision=excluded.revision,current=excluded.current,next_step=excluded.next_step,blockers=excluded.blockers,handoffs=excluded.handoffs,updated=excluded.updated",
                (team_id, actor, revision + 1, *values, time.time()),
            )
            return dict(
                db.execute(
                    "SELECT * FROM peer_boards WHERE team_id=? AND member_id=?",
                    (team_id, actor),
                ).fetchone()
            )

    def sync(self, actor, team_id, *, after=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise TeamError("Inbox batch size must be 1–50")
        with self.store.transaction() as db:
            team, membership = self.member(db, actor, team_id, active=False)
            after = membership["acked_seq"] if after is None else after
            if type(after) is not int or not 0 <= after <= membership["read_seq"]:
                raise TeamError("Cursor cannot skip unread messages")
            messages = [
                dict(r)
                for r in db.execute(
                    "SELECT m.*,d.received_at,d.acked_at FROM peer_messages m JOIN peer_deliveries d ON d.message_id=m.id WHERE m.team_id=? AND d.recipient=? AND m.seq>? ORDER BY m.seq LIMIT ?",
                    (team_id, actor, after, limit),
                )
            ]
            stamp = time.time()
            for row in messages:
                db.execute(
                    "UPDATE peer_deliveries SET received_at=coalesce(received_at,?),state=CASE WHEN state='pending' THEN 'suppressed' ELSE state END WHERE message_id=? AND recipient=?",
                    (stamp, row["id"], actor),
                )
                row["received_at"] = row["received_at"] or stamp
            cursor = max([after, *(m["seq"] for m in messages)])
            db.execute(
                "UPDATE peer_members SET seen_epoch=?,read_seq=max(read_seq,?) WHERE team_id=? AND member_id=?",
                (team["epoch"], cursor, team_id, actor),
            )
            roster = [
                dict(r)
                for r in db.execute(
                    "SELECT m.*,n.alias,n.provider,n.last_seen FROM peer_members m JOIN members n ON n.id=m.member_id WHERE m.team_id=?",
                    (team_id,),
                )
            ]
            for member in roster:
                member.update(
                    checkouts.member_view(
                        db,
                        team["project"],
                        member["member_id"],
                        json.loads(member["paths"]),
                        team_id=team["id"],
                    )
                )
                member.update(member_progress(db, team_id, member["member_id"]))
            tasks = [
                dict(r)
                for r in db.execute(
                    "SELECT id,team_id,created_by,owner,reviewer,substr(brief,1,600) AS brief_preview,state,revision,paths,handoff_needed,reviewer_handoff_needed FROM peer_tasks WHERE team_id=? AND state NOT IN ('accepted','cancelled') ORDER BY created LIMIT 64",
                    (team_id,),
                )
            ]
            action = (
                "review_required"
                if any(t["reviewer"] == actor and t["state"] == "review" for t in tasks)
                else "assigned_work"
                if any(
                    t["owner"] == actor and t["state"] in {"ready", "working"}
                    for t in tasks
                )
                else "inbox"
                if any(m["acked_at"] is None for m in messages)
                or db.execute(
                    "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.recipient=? AND d.acked_at IS NULL LIMIT 1",
                    (team_id, actor),
                ).fetchone()
                else "idle"
            )
            if membership["state"] != "active":
                action = "inactive"
            elif team["state"] != "active":
                action = team["state"]
            guidance = {}
            if membership["state"] == "active" and team["state"] == "active":
                guidance = conserve_guidance(
                    m["availability"] for m in roster if m["state"] == "active"
                )
            return {
                "team": dict(team),
                "decisions": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,topic,proposed_by,state,revision FROM peer_decisions WHERE team_id=? AND state!='superseded' ORDER BY state IN ('proposed','disputed') DESC,created DESC LIMIT 64",
                        (team_id,),
                    )
                ],
                "members": roster,
                "board": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM peer_boards WHERE team_id=?", (team_id,)
                    )
                ],
                "tasks": tasks,
                "messages": messages,
                "cursor": cursor,
                "more": bool(
                    db.execute(
                        "SELECT 1 FROM peer_messages m JOIN peer_deliveries d ON d.message_id=m.id WHERE m.team_id=? AND d.recipient=? AND m.seq>? LIMIT 1",
                        (team_id, actor, cursor),
                    ).fetchone()
                ),
                "sync_required": membership["acked_epoch"] != team["epoch"],
                "action": action,
                **guidance,
            }

    def acknowledge(self, actor, team_id, epoch, through_seq):
        with self.store.transaction() as db:
            team, member = self.member(db, actor, team_id, active=False)
            if (
                type(epoch) is not int
                or epoch != team["epoch"]
                or epoch > member["seen_epoch"]
            ):
                raise TeamError(
                    "Fetch the current snapshot before acknowledging its epoch"
                )
            if (
                type(through_seq) is not int
                or not member["acked_seq"] <= through_seq <= member["read_seq"]
            ):
                raise TeamError(
                    "Acknowledgement cannot advance past retrieved messages"
                )
            if db.execute(
                "SELECT 1 FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id WHERE m.team_id=? AND d.recipient=? AND m.seq<=? AND d.received_at IS NULL",
                (team_id, actor, through_seq),
            ).fetchone():
                raise TeamError(
                    "Read missing messages before advancing the acknowledgement cursor"
                )
            complete = not db.execute(
                "SELECT 1 FROM peer_messages m JOIN peer_deliveries d ON d.message_id=m.id WHERE m.team_id=? AND d.recipient=? AND m.seq>? LIMIT 1",
                (team_id, actor, through_seq),
            ).fetchone()
            acknowledged_epoch = epoch if complete else member["acked_epoch"]
            db.execute(
                "UPDATE peer_members SET acked_epoch=?,acked_seq=? WHERE team_id=? AND member_id=?",
                (acknowledged_epoch, through_seq, team_id, actor),
            )
            db.execute(
                "UPDATE peer_deliveries SET acked_at=coalesce(acked_at,?) WHERE recipient=? AND message_id IN (SELECT id FROM peer_messages WHERE team_id=? AND seq<=?)",
                (time.time(), actor, team_id, through_seq),
            )
            return {
                "epoch": acknowledged_epoch,
                "through_seq": through_seq,
                "sync_required": acknowledged_epoch != team["epoch"],
                "meaning": "Context received; this does not approve decisions or accept tasks",
            }
