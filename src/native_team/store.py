"""Durable work decisions and bounded assignments, independent of provider runtimes."""

from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
import time
import uuid


CLIENT_PROVIDERS = ("codex", "agy", "claude")
CLAUDE_ALIASES = (
    "claude-rules",
    "claude-history",
    "claude-art",
    "claude-ai-test",
    "claude-pathology",
)


class TeamError(ValueError):
    pass


def conversation_uuid(provider, value):
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise TeamError(
            f"{provider.capitalize()} requires THIS conversation’s actual UUID"
        ) from None


def checked(value, label, limit=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TeamError(f"{label} must contain 1–{limit} characters")
    return value.strip()


def new_id():
    return str(uuid.uuid4())


class Store:
    def __init__(self, state):
        self.state = Path(state).expanduser().resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.path = self.state / "team.sqlite3"
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS members (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, alias TEXT NOT NULL,
                    project TEXT NOT NULL, native_id TEXT, last_seen REAL NOT NULL,
                    UNIQUE(project, alias)
                );
                CREATE TABLE IF NOT EXISTS work (
                    id TEXT PRIMARY KEY, lead TEXT NOT NULL REFERENCES members(id),
                    project TEXT NOT NULL, intent TEXT NOT NULL, acceptance TEXT NOT NULL,
                    next_step TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '', request_key TEXT NOT NULL,
                    reminders INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(lead, request_key)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES work(id),
                    owner TEXT NOT NULL REFERENCES members(id), brief TEXT NOT NULL,
                    acceptance TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                    rounds INTEGER NOT NULL DEFAULT 0, request_key TEXT NOT NULL,
                    instruction TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(work_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS notices (
                    id TEXT PRIMARY KEY, recipient TEXT NOT NULL REFERENCES members(id),
                    work_id TEXT NOT NULL REFERENCES work(id),
                    assignment_id TEXT REFERENCES assignments(id),
                    revision INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
                    received_at REAL, request_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT REFERENCES work(id),
                    assignment_id TEXT, kind TEXT NOT NULL, created REAL NOT NULL
                );
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def member(self, db, actor, provider=None):
        row = db.execute("SELECT * FROM members WHERE id=?", (actor,)).fetchone()
        if row is None or (provider and row["provider"] != provider):
            raise TeamError(
                "This operation requires the enrolled native participant of the correct role"
            )
        return row

    def work_for(self, db, actor, work_id, *, lead=False, active=False):
        member = self.member(db, actor)
        row = db.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
        if row is None or row["project"] != member["project"]:
            raise TeamError("Work is outside this project")
        if lead and row["lead"] != actor:
            raise TeamError("Only the coordinator may change this work")
        if (
            not lead
            and row["lead"] != actor
            and not db.execute(
                "SELECT 1 FROM assignments WHERE work_id=? AND owner=?",
                (work_id, actor),
            ).fetchone()
        ):
            raise TeamError("This participant is not assigned to this work")
        if active and row["state"] != "active":
            raise TeamError(
                "Work is not active; reconcile its pause, blocker or cancellation first"
            )
        return row

    def task_for(self, db, actor, assignment_id, revision, *, lead=False):
        task = db.execute(
            "SELECT * FROM assignments WHERE id=?", (assignment_id,)
        ).fetchone()
        if task is None:
            raise TeamError("Assignment not found")
        work = self.work_for(db, actor, task["work_id"], lead=lead, active=True)
        if not lead and task["owner"] != actor:
            raise TeamError("Only the assigned owner can report work")
        if type(revision) is not int or task["revision"] != revision:
            raise TeamError("Stale revision; inspect current state before acting")
        return task, work

    def event(self, db, work_id, kind, assignment_id=None):
        db.execute(
            "INSERT INTO events(work_id,assignment_id,kind,created) VALUES(?,?,?,?)",
            (work_id, assignment_id, kind, time.time()),
        )

    def _check_claude_project(self, alias, project):
        if alias not in CLAUDE_ALIASES:
            raise TeamError(
                f"Choose this Claude conversation's lane: {', '.join(CLAUDE_ALIASES)}"
            )
        pin = self.state / "claude-pathology-project"
        try:
            pathology = pin.is_symlink() and pin.resolve(strict=True) == project
        except FileNotFoundError:
            pathology = False
        except (OSError, RuntimeError) as error:
            raise TeamError(
                "Cannot verify the owner-approved pathology project"
            ) from error
        if alias == "claude-pathology" and not pathology:
            raise TeamError(
                "claude-pathology is reserved for its owner-approved local project"
            )
        if pathology and alias != "claude-pathology":
            raise TeamError("Use claude-pathology for the approved pathology project")

    def join(self, provider, alias, project, native_id=None):
        if provider not in CLIENT_PROVIDERS or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", alias
        ):
            raise TeamError(
                "Choose a supported provider and a short alphanumeric alias"
            )
        project = Path(project).expanduser().resolve()
        if not project.is_dir() or any(c in str(project) for c in "\r\n\0"):
            raise TeamError("Choose an existing project directory")
        if provider == "claude":
            self._check_claude_project(alias, project)
        if provider in {"codex", "claude"}:
            native_id = conversation_uuid(provider, native_id)
        elif native_id is not None:
            raise TeamError(
                "agy is a native tool client; it has no external notification target"
            )
        with self.transaction() as db:
            prior = db.execute(
                "SELECT * FROM members WHERE project=? AND alias=?",
                (str(project), alias),
            ).fetchone()
            if prior:
                if prior["provider"] != provider or prior["native_id"] != native_id:
                    if provider == "claude":
                        raise TeamError(
                            "This lane belongs to another conversation; resume its original session"
                        )
                    raise TeamError(
                        "Alias belongs to another native conversation; use a new alias"
                    )
                member_id = prior["id"]
                db.execute(
                    "UPDATE members SET last_seen=? WHERE id=?",
                    (time.time(), member_id),
                )
            else:
                if provider != "claude" and alias in CLAUDE_ALIASES:
                    raise TeamError(
                        "This alias is reserved for its Claude Code workstream"
                    )
                # The dedicated lane and its UUID stay paired even across projects or a moved pin.
                if (
                    provider == "claude"
                    and db.execute(
                        """SELECT 1 FROM members WHERE provider='claude'
                       AND (alias=:alias OR native_id=:uuid)
                       AND 'claude-pathology' IN (alias, :alias)""",
                        {"alias": alias, "uuid": native_id},
                    ).fetchone()
                ):
                    raise TeamError(
                        "The dedicated pathology lane and its original UUID cannot be reused"
                    )
                if (
                    native_id is not None
                    and db.execute(
                        "SELECT 1 FROM members WHERE project=? AND provider=? AND native_id=?",
                        (str(project), provider, native_id),
                    ).fetchone()
                ):
                    raise TeamError(
                        f"This native {provider.capitalize()} conversation already has a project alias; reuse it rather than enrolling it as another peer"
                    )
                member_id = new_id()
                db.execute(
                    "INSERT INTO members VALUES(?,?,?,?,?,?)",
                    (member_id, provider, alias, str(project), native_id, time.time()),
                )
            return dict(
                db.execute(
                    "SELECT id,provider,alias,project,last_seen FROM members WHERE id=?",
                    (member_id,),
                ).fetchone()
            )

    def touch(self, actor):
        with self.transaction() as db:
            self.member(db, actor)
            db.execute(
                "UPDATE members SET last_seen=? WHERE id=?", (time.time(), actor)
            )

    def start(self, actor, intent, acceptance, next_step, request_key):
        intent, acceptance, next_step, request_key = (
            checked(v, k, n)
            for v, k, n in (
                (intent, "Intent", 12000),
                (acceptance, "Acceptance", 12000),
                (next_step, "Next step", 4000),
                (request_key, "Request key", 128),
            )
        )
        with self.transaction() as db:
            member = self.member(db, actor, "codex")
            prior = db.execute(
                "SELECT * FROM work WHERE lead=? AND request_key=?",
                (actor, request_key),
            ).fetchone()
            if prior:
                if (prior["intent"], prior["acceptance"]) != (intent, acceptance):
                    raise TeamError("Request key already identifies different work")
                return dict(prior)
            count = db.execute(
                "SELECT count(*) FROM work WHERE lead=? AND state NOT IN ('done','cancelled')",
                (actor,),
            ).fetchone()[0]
            if count >= 8:
                raise TeamError("Finish or cancel existing work before adding more")
            work_id, stamp = new_id(), time.time()
            db.execute(
                "INSERT INTO work(id,lead,project,intent,acceptance,next_step,state,revision,request_key,created,updated) VALUES(?,?,?,?,?,?,?,1,?,?,?)",
                (
                    work_id,
                    actor,
                    member["project"],
                    intent,
                    acceptance,
                    next_step,
                    "active",
                    request_key,
                    stamp,
                    stamp,
                ),
            )
            self.event(db, work_id, "started")
            return dict(
                db.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
            )

    def assign(self, actor, work_id, owner, brief, acceptance, request_key):
        brief, acceptance, request_key = (
            checked(v, k, n)
            for v, k, n in (
                (brief, "Brief", 12000),
                (acceptance, "Acceptance", 12000),
                (request_key, "Request key", 128),
            )
        )
        with self.transaction() as db:
            work = self.work_for(db, actor, work_id, lead=True, active=True)
            peer = db.execute(
                "SELECT * FROM members WHERE project=? AND alias=? AND provider='agy'",
                (work["project"], owner),
            ).fetchone()
            if peer is None:
                raise TeamError("The native agy adviser has not joined this project")
            prior = db.execute(
                "SELECT * FROM assignments WHERE work_id=? AND request_key=?",
                (work_id, request_key),
            ).fetchone()
            if prior:
                if (prior["owner"], prior["brief"], prior["acceptance"]) != (
                    peer["id"],
                    brief,
                    acceptance,
                ):
                    raise TeamError(
                        "Request key already identifies a different assignment"
                    )
                return dict(prior)
            count = db.execute(
                "SELECT count(*) FROM assignments WHERE work_id=?", (work_id,)
            ).fetchone()[0]
            pending = db.execute(
                "SELECT count(*) FROM assignments WHERE work_id=? AND state NOT IN ('accepted','cancelled')",
                (work_id,),
            ).fetchone()[0]
            if count >= 64 or pending >= 4:
                raise TeamError(
                    "Assignment limit reached; review current work before adding more"
                )
            assignment_id, stamp = new_id(), time.time()
            db.execute(
                "INSERT INTO assignments(id,work_id,owner,brief,acceptance,state,revision,request_key,created,updated) VALUES(?,?,?,?,?,?,1,?,?,?)",
                (
                    assignment_id,
                    work_id,
                    peer["id"],
                    brief,
                    acceptance,
                    "ready",
                    request_key,
                    stamp,
                    stamp,
                ),
            )
            self.event(db, work_id, "assigned", assignment_id)
            db.execute("UPDATE work SET updated=? WHERE id=?", (stamp, work_id))
            return dict(
                db.execute(
                    "SELECT * FROM assignments WHERE id=?", (assignment_id,)
                ).fetchone()
            )

    def take(self, actor):
        with self.transaction() as db:
            self.member(db, actor, "agy")
            db.execute(
                "UPDATE members SET last_seen=? WHERE id=?", (time.time(), actor)
            )
            task = db.execute(
                "SELECT a.* FROM assignments a JOIN work w ON w.id=a.work_id WHERE a.owner=? AND w.state='active' AND a.state IN ('ready','working') ORDER BY CASE a.state WHEN 'working' THEN 0 ELSE 1 END,a.created LIMIT 1",
                (actor,),
            ).fetchone()
            if task is None:
                return None
            if task["state"] == "ready":
                db.execute(
                    "UPDATE assignments SET state='working',revision=revision+1,updated=? WHERE id=?",
                    (time.time(), task["id"]),
                )
                self.event(db, task["work_id"], "claimed", task["id"])
            return dict(
                db.execute(
                    "SELECT * FROM assignments WHERE id=?", (task["id"],)
                ).fetchone()
            )

    def report(self, actor, assignment_id, revision, summary, evidence, blocked=False):
        summary = checked(summary, "Summary")
        evidence = checked(evidence, "Evidence or exact blocker")
        with self.transaction() as db:
            task, work = self.task_for(db, actor, assignment_id, revision)
            if task["state"] != "working":
                raise TeamError("Claim the assignment before reporting")
            state, stamp = ("blocked" if blocked else "review"), time.time()
            db.execute(
                "UPDATE assignments SET state=?,result=?,evidence=?,revision=revision+1,updated=? WHERE id=?",
                (state, summary, evidence, stamp, assignment_id),
            )
            db.execute(
                "INSERT INTO notices(id,recipient,work_id,assignment_id,revision,created,updated,request_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    new_id(),
                    work["lead"],
                    work["id"],
                    assignment_id,
                    revision + 1,
                    stamp,
                    stamp,
                    f"report:{assignment_id}:{revision + 1}",
                ),
            )
            db.execute("UPDATE work SET updated=? WHERE id=?", (stamp, work["id"]))
            self.event(db, work["id"], state, assignment_id)
            return dict(
                db.execute(
                    "SELECT * FROM assignments WHERE id=?", (assignment_id,)
                ).fetchone()
            )

    def decide(self, actor, assignment_id, revision, decision, explanation):
        explanation = checked(explanation, "Review decision")
        if decision not in {"accept", "revise", "block"}:
            raise TeamError("Choose accept, revise or block")
        with self.transaction() as db:
            task, work = self.task_for(db, actor, assignment_id, revision, lead=True)
            if task["state"] not in {"review", "blocked"} or (
                decision == "accept" and task["state"] != "review"
            ):
                raise TeamError("Only a submitted review can be accepted")
            if decision == "revise" and task["rounds"] >= 2:
                raise TeamError(
                    "Two-round revision limit reached; make a coordinator decision or report a blocker"
                )
            state = {"accept": "accepted", "revise": "ready", "block": "blocked"}[
                decision
            ]
            db.execute(
                "UPDATE assignments SET state=?,instruction=?,revision=revision+1,rounds=rounds+?,updated=? WHERE id=?",
                (
                    state,
                    explanation,
                    int(decision == "revise"),
                    time.time(),
                    assignment_id,
                ),
            )
            db.execute(
                "UPDATE notices SET received_at=coalesce(received_at,?),state=CASE WHEN state='pending' THEN 'suppressed' ELSE state END WHERE assignment_id=?",
                (time.time(), assignment_id),
            )
            self.event(db, work["id"], decision, assignment_id)
            db.execute(
                "UPDATE work SET updated=? WHERE id=?", (time.time(), work["id"])
            )
            return dict(
                db.execute(
                    "SELECT * FROM assignments WHERE id=?", (assignment_id,)
                ).fetchone()
            )

    def update_work(self, actor, work_id, revision, state, next_step, evidence=""):
        if state not in {"active", "paused", "blocked", "done", "cancelled"}:
            raise TeamError("Unsupported work state")
        next_step = checked(next_step, "Next step or reason")
        if state == "done":
            evidence = checked(evidence, "Integrated verification evidence")
        else:
            if not isinstance(evidence, str) or len(evidence) > 12000:
                raise TeamError("Evidence is too long or invalid")
        with self.transaction() as db:
            work = self.work_for(db, actor, work_id, lead=True)
            if (
                type(revision) is not int
                or work["revision"] != revision
                or work["state"] in {"done", "cancelled"}
            ):
                raise TeamError("Stale revision or closed work; inspect current state")
            if (
                state == "done"
                and db.execute(
                    "SELECT 1 FROM assignments WHERE work_id=? AND state NOT IN ('accepted','cancelled')",
                    (work_id,),
                ).fetchone()
            ):
                raise TeamError(
                    "Unaccepted assignments remain; whole-work completion is not allowed"
                )
            db.execute(
                "UPDATE work SET state=?,next_step=?,evidence=?,revision=revision+1,updated=? WHERE id=?",
                (state, next_step, evidence, time.time(), work_id),
            )
            if state == "cancelled":
                db.execute(
                    "UPDATE assignments SET state='cancelled',revision=revision+1,updated=? WHERE work_id=? AND state!='accepted'",
                    (time.time(), work_id),
                )
            if state in {"done", "cancelled"}:
                db.execute(
                    "UPDATE notices SET state='suppressed' WHERE state='pending' AND work_id=?",
                    (work_id,),
                )
            self.event(db, work_id, state)
            return dict(
                db.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
            )

    def _status(self, db, actor, *, include_closed=True):
        member = self.member(db, actor)
        params = {"actor": actor}
        work_scope = (
            "(w.lead=:actor OR EXISTS (SELECT 1 FROM assignments owned "
            "WHERE owned.work_id=w.id AND owned.owner=:actor))"
        )
        assignment_scope = "(w.lead=:actor OR a.owner=:actor)"
        detail = "" if include_closed else " AND w.state='active'"
        works = [
            dict(r)
            for r in db.execute(
                f"SELECT w.* FROM work w WHERE {work_scope}{detail} ORDER BY w.created",
                params,
            )
        ]
        tasks = [
            dict(r)
            for r in db.execute(
                "SELECT a.* FROM assignments a JOIN work w ON w.id=a.work_id "
                f"WHERE {assignment_scope}{detail} ORDER BY a.created",
                params,
            )
        ]
        active = {w["id"] for w in works if w["state"] == "active"}
        live = [t for t in tasks if t["work_id"] in active]
        if member["provider"] == "agy":
            action = (
                "assigned_work"
                if any(t["state"] in {"ready", "working"} for t in live)
                else "idle"
            )
        elif any(t["state"] == "review" for t in live):
            action = "review_required"
        elif any(t["state"] == "blocked" for t in live):
            action = "blocked"
        elif any(
            all(
                t["state"] in {"accepted", "cancelled"}
                for t in live
                if t["work_id"] == wid
            )
            for wid in active
        ):
            action = "next_step_required"
        else:
            action = "waiting_for_peer" if active else "idle"
        notices = [
            dict(r)
            for r in db.execute(
                "SELECT n.id,n.work_id,n.assignment_id,n.revision,n.state,n.detail,n.created,n.updated,n.received_at "
                "FROM notices n JOIN work w ON w.id=n.work_id "
                f"WHERE n.recipient=:actor{detail} ORDER BY n.created",
                params,
            )
        ]
        events = []
        if works:
            events = db.execute(
                "SELECT e.* FROM events e WHERE e.work_id IN "
                f"(SELECT w.id FROM work w WHERE {work_scope}{detail}) "
                "ORDER BY e.sequence DESC LIMIT 80",
                params,
            ).fetchall()
        members = [
            dict(r)
            for r in db.execute(
                "SELECT id,provider,alias,last_seen FROM members WHERE project=?",
                (member["project"],),
            )
        ]
        result = {
            "action": action,
            "work": works,
            "assignments": tasks,
            "notifications": notices,
            "members": members,
            "events": [dict(r) for r in reversed(events)],
        }
        if not include_closed:
            closed = "w.state IN ('done','cancelled')"
            pending_notice = (
                "n.recipient=:actor AND n.received_at IS NULL AND n.state!='suppressed'"
            )
            result["closed_work_count"] = db.execute(
                f"SELECT count(*) FROM work w WHERE {work_scope} AND {closed}", params
            ).fetchone()[0]
            result["closed_assignment_count"] = db.execute(
                "SELECT count(*) FROM assignments a JOIN work w ON w.id=a.work_id "
                f"WHERE {assignment_scope} AND {closed}",
                params,
            ).fetchone()[0]
            parked = [
                dict(r)
                for r in db.execute(
                    "SELECT w.id,w.state,(SELECT count(*) FROM notices n "
                    f"WHERE n.work_id=w.id AND {pending_notice}) AS pending_notices "
                    f"FROM work w WHERE {work_scope} "
                    "AND w.state IN ('paused','blocked') ORDER BY w.created",
                    params,
                )
            ]
            if parked:
                result["parked_work"] = parked
            pending = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT n.work_id FROM notices n JOIN work w ON w.id=n.work_id "
                    f"WHERE {pending_notice} AND {closed} ORDER BY n.work_id",
                    params,
                )
            ]
            if pending:
                result["pending_closed_work_ids"] = pending
        return result

    def status(self, actor, *, include_closed=True):
        with self.transaction() as db:
            return self._status(db, actor, include_closed=include_closed)

    def receive(self, actor, *, include_closed=True):
        with self.transaction() as db:
            self.member(db, actor, "codex")
            db.execute(
                "UPDATE members SET last_seen=? WHERE id=?", (time.time(), actor)
            )
            surfaced = (
                ""
                if include_closed
                else " AND work_id IN (SELECT id FROM work WHERE state='active')"
            )
            db.execute(
                "UPDATE notices SET received_at=? WHERE recipient=? AND received_at IS NULL"
                + surfaced,
                (time.time(), actor),
            )
            return self._status(db, actor, include_closed=include_closed)

    def reserve_notice(self, actor):
        with self.transaction() as db:
            member = self.member(db, actor, "codex")
            notice = db.execute(
                "SELECT n.* FROM notices n JOIN work w ON w.id=n.work_id WHERE n.recipient=? AND n.state='pending' AND n.received_at IS NULL AND w.state='active' ORDER BY n.created LIMIT 1",
                (actor,),
            ).fetchone()
            if notice is None:
                return None
            db.execute(
                "UPDATE notices SET state='sending',updated=? WHERE id=?",
                (time.time(), notice["id"]),
            )
            return {**dict(notice), "native_id": member["native_id"]}

    def notice_result(self, notice_id, state, detail):
        if state not in {"queued", "queued_warning", "uncertain", "failed"}:
            raise TeamError("Unsupported notification result")
        with self.transaction() as db:
            db.execute(
                "UPDATE notices SET state=?,detail=?,updated=? WHERE id=? AND state='sending'",
                (state, detail, time.time(), notice_id),
            )

    def recover_notices(self, actor):
        with self.transaction() as db:
            db.execute(
                "UPDATE notices SET state='uncertain',detail='Sender stopped during a native queue attempt; inspect receipt before retrying',updated=? WHERE recipient=? AND state='sending'",
                (time.time(), actor),
            )

    def watch_work(self, actor, *, after_seconds=300, at=None):
        """Nudge at most twice when receipt happened but a coordinator decision never followed.

        Never retry an unread/uncertain send, infer completion, or touch paused work.
        A native coordinator can checkpoint long implementation with update_work(active).
        """
        from .availability import read_availability

        stamp = time.time() if at is None else at
        if after_seconds < 1:
            raise TeamError("Continuation deadline must be positive")
        with self.transaction() as db:
            self.member(db, actor, "codex")
            if read_availability(db, actor, now=stamp)["held"]:
                return
            for work in db.execute(
                "SELECT * FROM work WHERE lead=? AND state='active' AND reminders<2 AND updated<=?",
                (actor, stamp - after_seconds),
            ).fetchall():
                last_receipt = db.execute(
                    "SELECT max(received_at) FROM notices WHERE work_id=?",
                    (work["id"],),
                ).fetchone()[0]
                # A result delivered late still gives the native coordinator a full grace period.
                if last_receipt is not None and stamp - last_receipt < after_seconds:
                    continue
                tasks = db.execute(
                    "SELECT state FROM assignments WHERE work_id=?", (work["id"],)
                ).fetchall()
                if not tasks or any(
                    t["state"] in {"ready", "working", "blocked"} for t in tasks
                ):
                    continue
                if db.execute(
                    "SELECT 1 FROM notices WHERE work_id=? AND received_at IS NULL AND state!='suppressed'",
                    (work["id"],),
                ).fetchone():
                    continue
                key = f"continue:{work['id']}:{work['reminders'] + 1}"
                db.execute(
                    "INSERT INTO notices(id,recipient,work_id,revision,created,updated,request_key) VALUES(?,?,?,?,?,?,?)",
                    (new_id(), actor, work["id"], work["revision"], stamp, stamp, key),
                )
                db.execute(
                    "UPDATE work SET reminders=reminders+1,updated=? WHERE id=?",
                    (stamp, work["id"]),
                )
                self.event(db, work["id"], "continuation_requested")

    def inspect(self, project):
        project = str(Path(project).expanduser().resolve())
        with self.transaction() as db:
            members = [
                dict(r)
                for r in db.execute(
                    "SELECT id,alias,provider,last_seen FROM members WHERE project=?",
                    (project,),
                )
            ]
            return {
                "project": project,
                "members": [{k: v for k, v in m.items() if k != "id"} for m in members],
                "coordinators": [
                    {"alias": m["alias"], "status": self._status(db, m["id"])}
                    for m in members
                    if m["provider"] == "codex"
                ],
            }
