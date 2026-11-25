"""Task ownership and independent review within flat teams."""

import json
import time

from . import change_manifest
from .schema import has_task_changes
from . import checkouts
from .store import TeamError, checked, new_id
from .availability import read_availability


class PeerTasks:
    def __init__(self, peers):
        self.peers, self.store = peers, peers.store

    def _task(self, db, actor, task_id, revision=None, *, active=True, synced=False):
        task = db.execute("SELECT * FROM peer_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise TeamError("Peer task not found")
        self.peers.member(db, actor, task["team_id"], active=active, synced=synced)
        if revision is not None and (
            type(revision) is not int or task["revision"] != revision
        ):
            raise TeamError(
                "Stale task revision; inspect current ownership before acting"
            )
        return task

    def _record(self, db, actor, task_id, kind, recipients=()):
        task = dict(
            db.execute("SELECT * FROM peer_tasks WHERE id=?", (task_id,)).fetchone()
        )
        db.execute(
            "INSERT INTO peer_task_history(task_id,revision,actor,kind,snapshot,created) VALUES(?,?,?,?,?,?)",
            (
                task_id,
                task["revision"],
                actor,
                kind,
                json.dumps(task, ensure_ascii=False),
                time.time(),
            ),
        )
        if recipients:
            self.peers._publish(
                db,
                actor,
                task["team_id"],
                "task",
                f"Task {task_id}: {kind}; state={task['state']}; revision={task['revision']}. Read this task's current record and evidence before acting.",
                f"task:{task_id}:{task['revision']}",
                set(recipients) - {actor},
            )
        return task

    def _scope(self, db, team, owner, paths):
        _, membership = self.peers.member(db, owner, team["id"])
        declared = json.loads(membership["paths"])
        for path in paths:
            if not any(
                scope == "." or scope == path or path.startswith(scope + "/")
                for scope in declared
            ):
                raise TeamError(
                    "Task writes exceed the executor's declared scope; agree a scope update first"
                )
        collisions = checkouts.conflicts(
            db, team["project"], owner, paths, team_id=team["id"]
        )
        if any(c["hard"] for c in collisions):
            raise TeamError("Another executor holds an overlapping write scope")
        return collisions

    def create(
        self,
        actor,
        team_id,
        owner_alias,
        reviewer_alias,
        brief,
        acceptance,
        request_key,
        paths=None,
    ):
        brief, acceptance, request_key = (
            checked(brief, "Task brief"),
            checked(acceptance, "Task acceptance"),
            checked(request_key, "Task key", 128),
        )
        with self.store.transaction() as db:
            team, _ = self.peers.member(db, actor, team_id, synced=True)
            owner = self.peers.by_alias(db, team["project"], owner_alias)["id"]
            reviewer = self.peers.by_alias(db, team["project"], reviewer_alias)["id"]
            if owner == reviewer:
                raise TeamError("An executor cannot review their own task")
            scopes = self.peers.paths(
                checkouts.location(db, owner).get("checkout", team["project"]),
                paths or [],
            )
            prior = db.execute(
                "SELECT * FROM peer_tasks WHERE team_id=? AND created_by=? AND request_key=?",
                (team_id, actor, request_key),
            ).fetchone()
            if prior:
                if (
                    prior["owner"],
                    prior["reviewer"],
                    prior["brief"],
                    prior["acceptance"],
                    json.loads(prior["paths"]),
                ) != (owner, reviewer, brief, acceptance, scopes):
                    raise TeamError("Task key already identifies different work")
                return dict(prior)
            self.peers.member(db, reviewer, team_id)
            for recipient in (owner, reviewer):
                if read_availability(db, recipient)["held"]:
                    raise TeamError(
                        "This peer is in quota cooldown; preserve existing work and wait for verified recovery"
                    )
            warnings = self._scope(db, team, owner, scopes)
            if (
                db.execute(
                    "SELECT count(*) FROM peer_tasks WHERE team_id=? AND state NOT IN ('accepted','cancelled')",
                    (team_id,),
                ).fetchone()[0]
                >= 32
            ):
                raise TeamError("Review or finish pending team work before adding more")
            if (
                db.execute(
                    "SELECT count(*) FROM peer_tasks WHERE team_id=? AND owner=? AND state NOT IN ('accepted','cancelled')",
                    (team_id, owner),
                ).fetchone()[0]
                >= 8
            ):
                raise TeamError("This executor has too much pending work")
            task_id, stamp = new_id(), time.time()
            db.execute(
                "INSERT INTO peer_tasks(id,team_id,created_by,owner,reviewer,brief,acceptance,paths,request_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    team_id,
                    actor,
                    owner,
                    reviewer,
                    brief,
                    acceptance,
                    json.dumps(scopes),
                    request_key,
                    stamp,
                    stamp,
                ),
            )
            return {
                **self._record(db, actor, task_id, "assigned", [owner]),
                "merge_warnings": warnings,
            }

    def get(self, actor, task_id):
        with self.store.transaction() as db:
            task = dict(self._task(db, actor, task_id, active=False))
            history = [
                dict(r)
                for r in db.execute(
                    "SELECT revision,actor,kind,created FROM peer_task_history WHERE task_id=? ORDER BY revision",
                    (task_id,),
                )
            ]
            if checkouts.has_checkouts(db):
                loc = db.execute(
                    "SELECT checkout,branch FROM task_checkouts WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if loc:
                    task.update(dict(loc))
            task["changes"] = change_manifest.latest(db, task_id)
            return {"task": task, "history": history}

    def take(self, actor, team_id):
        with self.store.transaction() as db:
            team, _ = self.peers.member(db, actor, team_id, synced=True)
            task = db.execute(
                "SELECT * FROM peer_tasks WHERE team_id=? AND owner=? AND state IN ('ready','working') ORDER BY CASE state WHEN 'working' THEN 0 ELSE 1 END,created LIMIT 1",
                (team_id, actor),
            ).fetchone()
            if task is None:
                return None
            warnings = self._scope(db, team, actor, json.loads(task["paths"]))
            if task["state"] == "working" and not task["handoff_needed"]:
                return {**dict(task), "merge_warnings": warnings}
            if read_availability(db, actor)["held"]:
                raise TeamError(
                    "This peer is in quota cooldown; checkpoint existing work without claiming new work"
                )
            db.execute(
                "UPDATE peer_tasks SET state='working',handoff_needed=0,revision=revision+1,updated=? WHERE id=?",
                (time.time(), task["id"]),
            )
            if checkouts.has_checkouts(db):
                loc = checkouts.location(db, actor)
                if loc:
                    db.execute(
                        "INSERT OR REPLACE INTO task_checkouts VALUES(?,?,?,?)",
                        (task["id"], loc["checkout"], loc["branch"], time.time()),
                    )
            return {
                **self._record(db, actor, task["id"], "claimed"),
                "merge_warnings": warnings,
            }

    def report(
        self, actor, task_id, revision, result, evidence, *, blocked=False, changes=None
    ):
        result, evidence = (
            checked(result, "Result"),
            checked(evidence, "Evidence or blocker"),
        )
        with self.store.transaction() as db:
            task = self._task(db, actor, task_id, revision, active=False)
            team = db.execute(
                "SELECT state FROM peer_teams WHERE id=?", (task["team_id"],)
            ).fetchone()
            if (
                team["state"] != "active"
                or task["owner"] != actor
                or task["state"] != "working"
            ):
                raise TeamError(
                    "Only the current claimed executor can report active work"
                )
            # An inactive executor may preserve a matching-revision late result. A handoff
            # changes the revision and owner, so old evidence cannot replace a new claim.
            state = "blocked" if blocked else "review"
            db.execute(
                "UPDATE peer_tasks SET state=?,result=?,evidence=?,handoff_needed=0,revision=revision+1,updated=? WHERE id=?",
                (state, result, evidence, time.time(), task_id),
            )
            if changes is not None:
                if not has_task_changes(db):
                    raise TeamError(
                        "Run native-team migrate before submitting a change manifest"
                    )
                manifest = change_manifest.validate(changes, json.loads(task["paths"]))
                loc = checkouts.location(db, actor)
                root = loc.get("checkout") or self.store.member(db, actor)["project"]
                for file in changes["files"]:
                    if self.peers.paths(root, [file["path"]]) != [file["path"]]:
                        raise TeamError(
                            "Manifest entry is not a canonical file inside the task scope"
                        )
                db.execute(
                    "INSERT INTO peer_task_changes VALUES(?,?,?,?)",
                    (task_id, revision + 1, manifest, time.time()),
                )
            recipients = {task["reviewer"]}
            if task["reviewer_handoff_needed"] or blocked:
                recipients.add(task["created_by"])
                active = {
                    r[0]
                    for r in db.execute(
                        "SELECT member_id FROM peer_members WHERE team_id=? AND state='active' AND member_id!=?",
                        (task["team_id"], actor),
                    )
                }
                if not recipients & active:
                    recipients.update(active)
            return self._record(db, actor, task_id, state, recipients)

    def review(self, actor, task_id, revision, decision, explanation):
        if decision not in {"accept", "revise", "block"}:
            raise TeamError("Choose accept, revise or block")
        explanation = checked(explanation, "Review evidence")
        with self.store.transaction() as db:
            task = self._task(db, actor, task_id, revision, synced=True)
            if task["reviewer"] != actor or task["owner"] == actor:
                raise TeamError(
                    "Only the designated independent reviewer may decide this task"
                )
            if task["state"] not in {"review", "blocked"} or (
                decision == "accept" and task["state"] != "review"
            ):
                raise TeamError("Only a submitted review can be accepted")
            if decision == "revise" and task["rounds"] >= 2:
                raise TeamError(
                    "Two correction rounds exhausted; record a blocker or resolve the disagreement"
                )
            state = {"accept": "accepted", "revise": "ready", "block": "blocked"}[
                decision
            ]
            db.execute(
                "UPDATE peer_tasks SET state=?,instruction=?,rounds=rounds+?,reviewer_handoff_needed=0,revision=revision+1,updated=? WHERE id=?",
                (state, explanation, int(decision == "revise"), time.time(), task_id),
            )
            return self._record(
                db, actor, task_id, decision, {task["owner"], task["created_by"]}
            )

    def release(self, actor, task_id, revision, reason, evidence):
        reason, evidence = (
            checked(reason, "Release reason"),
            checked(evidence, "Preserved work evidence"),
        )
        with self.store.transaction() as db:
            task = self._task(db, actor, task_id, revision, active=False)
            if task["owner"] != actor or task["state"] not in {"ready", "working"}:
                raise TeamError(
                    "Only the current executor can release an outstanding claim"
                )
            db.execute(
                "UPDATE peer_tasks SET state='blocked',handoff_needed=1,result=?,evidence=?,revision=revision+1,updated=? WHERE id=?",
                (reason, evidence, time.time(), task_id),
            )
            return self._record(
                db, actor, task_id, "released", {task["created_by"], task["reviewer"]}
            )

    def handoff(
        self, actor, task_id, revision, *, owner_alias=None, reviewer_alias=None, reason
    ):
        reason = checked(reason, "Handoff reason")
        with self.store.transaction() as db:
            task = self._task(db, actor, task_id, revision, synced=True)
            if task["state"] in {"accepted", "cancelled"}:
                raise TeamError("Closed task cannot be handed off")
            team, _ = self.peers.member(db, actor, task["team_id"])
            owner = (
                self.peers.by_alias(db, team["project"], owner_alias)["id"]
                if owner_alias
                else task["owner"]
            )
            reviewer = (
                self.peers.by_alias(db, team["project"], reviewer_alias)["id"]
                if reviewer_alias
                else task["reviewer"]
            )
            if owner == reviewer:
                raise TeamError("An executor cannot become their own reviewer")
            owner_changed, reviewer_changed = (
                owner != task["owner"],
                reviewer != task["reviewer"],
            )
            if not owner_changed and not reviewer_changed:
                return dict(task)
            if owner != task["owner"]:
                if task["state"] in {"working", "review"}:
                    raise TeamError(
                        "The existing executor must release its in-flight claim, or the reviewer must revise/block its submitted result, before changing executor"
                    )
                if (
                    actor not in {task["created_by"], task["owner"]}
                    and not task["handoff_needed"]
                ):
                    raise TeamError(
                        "Arrange ownership with the requester or executor before taking over"
                    )
            if (
                reviewer != task["reviewer"]
                and actor not in {task["created_by"], task["reviewer"]}
                and not task["reviewer_handoff_needed"]
            ):
                raise TeamError(
                    "Arrange review ownership before replacing an active peer"
                )
            warnings = []
            if owner_changed:
                warnings = self._scope(db, team, owner, json.loads(task["paths"]))
            if reviewer_changed:
                self.peers.member(db, reviewer, team["id"])
            state = "ready" if owner != task["owner"] else task["state"]
            db.execute(
                "UPDATE peer_teams SET epoch=epoch+1,revision=revision+1,updated=? WHERE id=?",
                (time.time(), team["id"]),
            )
            db.execute(
                "UPDATE peer_tasks SET owner=?,reviewer=?,state=?,instruction=?,handoff_needed=?,reviewer_handoff_needed=?,revision=revision+1,updated=? WHERE id=?",
                (
                    owner,
                    reviewer,
                    state,
                    reason,
                    0 if owner_changed else task["handoff_needed"],
                    0 if reviewer_changed else task["reviewer_handoff_needed"],
                    time.time(),
                    task_id,
                ),
            )
            updated = self._record(
                db,
                actor,
                task_id,
                "handoff",
                {task["owner"], task["reviewer"], owner, reviewer, task["created_by"]},
            )

            return {**updated, "merge_warnings": warnings}

    def recover(self, actor, task_id, revision, user_instruction, stop_evidence):
        instruction = checked(
            user_instruction,
            "User authorization to recover this stopped executor",
            2000,
        )
        evidence = checked(
            stop_evidence, "Evidence that the previous native writer stopped", 3000
        )
        with self.store.transaction() as db:
            task = self._task(db, actor, task_id, revision, synced=True)
            _, owner = self.peers.member(
                db, task["owner"], task["team_id"], active=False
            )
            if owner["state"] != "inactive" or task["state"] != "working":
                raise TeamError(
                    "Recovery is only for an inactive executor with an outstanding claim"
                )
            db.execute(
                "UPDATE peer_tasks SET state='blocked',handoff_needed=1,instruction=?,revision=revision+1,updated=? WHERE id=?",
                (
                    f"User-authorized recovery: {instruction}\nStopped-writer evidence: {evidence}",
                    time.time(),
                    task_id,
                ),
            )
            return self._record(
                db,
                actor,
                task_id,
                "recovered",
                {task["owner"], task["reviewer"], task["created_by"]},
            )

    def cancel(self, actor, task_id, revision, reason):
        reason = checked(reason, "Cancellation reason")
        with self.store.transaction() as db:
            task = self._task(db, actor, task_id, revision, active=False)
            if actor not in {task["created_by"], task["owner"]}:
                raise TeamError(
                    "Arrange cancellation with the task requester or executor"
                )
            if task["state"] == "working" and task["owner"] != actor:
                raise TeamError(
                    "An in-flight executor must release or be explicitly recovered before cancellation"
                )
            if task["state"] in {"accepted", "cancelled"}:
                raise TeamError("Task is already closed")
            db.execute(
                "UPDATE peer_tasks SET state='cancelled',instruction=?,revision=revision+1,updated=? WHERE id=?",
                (reason, time.time(), task_id),
            )
            return self._record(
                db,
                actor,
                task_id,
                "cancelled",
                {task["owner"], task["reviewer"], task["created_by"]},
            )
