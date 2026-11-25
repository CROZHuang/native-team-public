"""Cross-workstream agreement without a privileged peer or implicit approval."""

import time

from .store import TeamError, checked, new_id


class PeerDecisions:
    def __init__(self, peers):
        self.peers, self.store = peers, peers.store

    def _get(self, db, actor, decision_id):
        row = db.execute(
            "SELECT * FROM peer_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise TeamError("Shared decision not found")
        self.peers.member(db, actor, row["team_id"], active=False)
        return row

    def get(self, actor, decision_id):
        with self.store.transaction() as db:
            row = dict(self._get(db, actor, decision_id))
            row["responses"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM peer_votes WHERE decision_id=?", (decision_id,)
                )
            ]
            return row

    def propose(
        self,
        actor,
        team_id,
        topic,
        proposal,
        affected,
        request_key,
        supersedes=None,
        user_instruction="",
    ):
        topic, proposal, request_key = (
            checked(topic, "Decision topic", 200),
            checked(proposal, "Proposal", 6000),
            checked(request_key, "Decision key", 128),
        )
        if user_instruction:
            user_instruction = checked(
                user_instruction, "User direction for decision replacement", 2000
            )
        if (
            not isinstance(affected, list)
            or not 1 <= len(affected) <= 12
            or any(not isinstance(a, str) or not a.strip() for a in affected)
            or len(set(affected)) != len(affected)
        ):
            raise TeamError(
                "Name the distinct affected peers who must explicitly agree"
            )
        with self.store.transaction() as db:
            team, _ = self.peers.member(db, actor, team_id, synced=True)
            ids = {
                self.peers.by_alias(db, team["project"], alias)["id"]
                for alias in affected
            }
            prior = db.execute(
                "SELECT * FROM peer_decisions WHERE team_id=? AND proposed_by=? AND request_key=?",
                (team_id, actor, request_key),
            ).fetchone()
            if prior:
                old_ids = {
                    r[0]
                    for r in db.execute(
                        "SELECT member_id FROM peer_votes WHERE decision_id=?",
                        (prior["id"],),
                    )
                }
                if (
                    prior["topic"],
                    prior["proposal"],
                    prior["supersedes"],
                    prior["authority"],
                    old_ids,
                ) != (topic, proposal, supersedes, user_instruction, ids):
                    raise TeamError("Decision key identifies a different proposal")
                return dict(prior)
            for member in ids:
                self.peers.member(db, member, team_id)
            if (
                db.execute(
                    "SELECT count(*) FROM peer_decisions WHERE team_id=? AND state IN ('proposed','disputed')",
                    (team_id,),
                ).fetchone()[0]
                >= 64
            ):
                raise TeamError(
                    "Too many shared decisions; reconcile current proposals first"
                )
            if supersedes:
                previous = self._get(db, actor, supersedes)
                old_ids = {
                    r[0]
                    for r in db.execute(
                        "SELECT member_id FROM peer_votes WHERE decision_id=?",
                        (supersedes,),
                    )
                }
                if previous["team_id"] != team_id or previous["state"] == "superseded":
                    raise TeamError(
                        "The original decision was already replaced or belongs to another team"
                    )
                omitted = old_ids - ids
                if omitted and (
                    not user_instruction
                    or any(
                        self.peers.member(db, peer, team_id, active=False)[1]["state"]
                        == "active"
                        for peer in omitted
                    )
                ):
                    raise TeamError(
                        "A replacement must include affected peers; omitting inactive peers needs explicit user authorization"
                    )
            identity = new_id()
            db.execute(
                "INSERT INTO peer_decisions(id,team_id,proposed_by,topic,proposal,request_key,created,supersedes,authority) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    team_id,
                    actor,
                    topic,
                    proposal,
                    request_key,
                    time.time(),
                    supersedes,
                    user_instruction,
                ),
            )
            db.executemany(
                "INSERT INTO peer_votes(decision_id,member_id) VALUES(?,?)",
                [(identity, member) for member in sorted(ids)],
            )
            self.peers._publish(
                db,
                actor,
                team_id,
                "proposal",
                f"Decision {identity}: {topic}. Read the proposal and explicitly agree or object; inbox acknowledgement is not approval.",
                f"decision:{identity}:1",
                ids - {actor},
            )
            return dict(
                db.execute(
                    "SELECT * FROM peer_decisions WHERE id=?", (identity,)
                ).fetchone()
            )

    def respond(self, actor, decision_id, revision, response, note):
        if response not in {"agree", "object"}:
            raise TeamError(
                "Choose explicit agreement or an objection; receipt is not a vote"
            )
        note = checked(note, "Decision rationale", 3000)
        with self.store.transaction() as db:
            decision = self._get(db, actor, decision_id)
            self.peers.member(db, actor, decision["team_id"], synced=True)
            if (
                type(revision) is not int
                or decision["revision"] != revision
                or decision["state"] in {"agreed", "superseded"}
            ):
                raise TeamError(
                    "Stale or already resolved proposal; inspect it or propose a replacement"
                )
            if (
                decision["supersedes"]
                and db.execute(
                    "SELECT state FROM peer_decisions WHERE id=?",
                    (decision["supersedes"],),
                ).fetchone()[0]
                == "superseded"
            ):
                raise TeamError(
                    "Another proposal already replaced the original; rebase on the current decision"
                )
            if not db.execute(
                "SELECT 1 FROM peer_votes WHERE decision_id=? AND member_id=?",
                (decision_id, actor),
            ).fetchone():
                raise TeamError(
                    "Only named affected peers can record their own response"
                )
            db.execute(
                "UPDATE peer_votes SET response=?,note=?,updated=? WHERE decision_id=? AND member_id=?",
                (response, note, time.time(), decision_id, actor),
            )
            responses = [
                r[0]
                for r in db.execute(
                    "SELECT response FROM peer_votes WHERE decision_id=?",
                    (decision_id,),
                )
            ]
            state = (
                "disputed"
                if "object" in responses
                else "agreed"
                if all(r == "agree" for r in responses)
                else "proposed"
            )
            db.execute(
                "UPDATE peer_decisions SET state=?,revision=revision+1 WHERE id=?",
                (state, decision_id),
            )
            if state == "agreed":
                db.execute(
                    "UPDATE peer_teams SET epoch=epoch+1,revision=revision+1,updated=? WHERE id=?",
                    (time.time(), decision["team_id"]),
                )
                if decision["supersedes"]:
                    db.execute(
                        "UPDATE peer_decisions SET state='superseded',revision=revision+1 WHERE id=?",
                        (decision["supersedes"],),
                    )
                    db.execute(
                        "UPDATE peer_decisions SET state='superseded',revision=revision+1 WHERE supersedes=? AND id!=? AND state IN ('proposed','disputed')",
                        (decision["supersedes"], decision_id),
                    )
            if state in {"agreed", "disputed"}:
                self.peers._publish(
                    db,
                    actor,
                    decision["team_id"],
                    "decision",
                    f"Decision {decision_id} is {state}: {decision['topic']}. Read recorded responses; no peer can override another's objection by acknowledging a message.",
                    f"decision:{decision_id}:{revision + 1}",
                )
            return dict(
                db.execute(
                    "SELECT * FROM peer_decisions WHERE id=?", (decision_id,)
                ).fetchone()
            )
