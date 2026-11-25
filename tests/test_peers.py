import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from native_team.peers import PeerStore
from native_team.schema import migrate
from native_team.store import Store, TeamError


class PeerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root / "state")
        self.members = {}
        for provider, alias in [
            ("agy", "agy1"),
            ("agy", "agy2"),
            ("codex", "code2"),
            ("codex", "code3"),
            ("codex", "code4"),
        ]:
            self.members[alias] = self.store.join(
                provider,
                alias,
                self.root,
                str(uuid.uuid4()) if provider == "codex" else None,
            )["id"]
        self.backup = migrate(self.store)
        self.peers = PeerStore(self.store)
        self.team = self.peers.create(
            self.members["agy1"],
            "Study",
            "Solve the shared problem",
            list(self.members),
            "test-team",
        )

    def sync(self, alias):
        actor = self.members[alias]
        result = self.peers.sync(actor, self.team["id"])
        self.peers.acknowledge(
            actor, self.team["id"], result["team"]["epoch"], result["cursor"]
        )
        return result


class FlatTeamTests(PeerFixture):
    def test_case_and_unicode_aliases_cannot_reserve_the_same_portable_write_scope(
        self,
    ):
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, paths=["Art/Café"]
        )
        for system in ("darwin", "win32"):
            with (
                self.subTest(system=system),
                patch("native_team.peers.sys.platform", system),
            ):
                with self.assertRaisesRegex(TeamError, "overlap"):
                    self.peers.update_member(
                        self.members["agy2"],
                        self.team["id"],
                        "agy2",
                        1,
                        paths=["art/CAFE\u0301/sprites"],
                    )

    def test_opt_in_history_includes_older_teams_and_respects_membership(self):
        owner, peer = self.members["agy1"], self.members["code2"]
        closed = []
        for i in range(33):
            team = self.peers.create(
                owner, f"Past {i}", "Finished", ["agy1", "code2"], f"past-{i}"
            )
            self.peers.set_state(
                owner, team["id"], team["revision"], "done", "Completed", "Checked"
            )
            closed.append(team["id"])
        view = self.peers.overview(peer)
        self.assertEqual([t["id"] for t in view["teams"]], [self.team["id"]])
        self.assertEqual(view["closed_team_count"], len(closed))
        self.assertEqual(set(view["pending_closed_team_ids"]), set(closed))
        full = self.peers.overview(peer, include_closed=True)
        self.assertEqual({t["id"] for t in full["teams"]}, {self.team["id"], *closed})
        other = self.peers.overview(self.members["code3"], include_closed=True)
        self.assertEqual([t["id"] for t in other["teams"]], [self.team["id"]])
        self.assertEqual(other["closed_team_count"], 0)

    def test_compact_overview_keeps_closed_lifecycle_discoverable_until_ack(self):
        own, peer = self.members["agy1"], self.members["code2"]
        self.sync("agy1")
        self.sync("code2")
        closed = self.peers.set_state(
            own, self.team["id"], 1, "done", "Test complete", "Verified"
        )
        before = self.peers.overview(peer)
        self.assertEqual(before["teams"], [])
        self.assertEqual(before["closed_team_count"], 1)
        self.assertEqual(before["pending_closed_team_ids"], [closed["id"]])
        pg = self.peers.sync(peer, closed["id"])
        self.assertEqual(pg["action"], "done")
        self.assertEqual(pg["messages"][-1]["kind"], "lifecycle")
        self.assertEqual(
            self.peers.overview(peer)["pending_closed_team_ids"], [closed["id"]]
        )
        self.peers.acknowledge(peer, closed["id"], pg["team"]["epoch"], pg["cursor"])
        self.assertFalse(self.peers.overview(peer).get("pending_closed_team_ids"))
        self.assertEqual(
            [t["id"] for t in self.peers.overview(peer, include_closed=True)["teams"]],
            [closed["id"]],
        )

    def test_completed_history_does_not_exhaust_a_continuing_team(self):
        with self.store.transaction() as db:
            db.executemany(
                "INSERT INTO peer_messages(id,team_id,sender,kind,body,request_key,created) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        f"old-{i}",
                        self.team["id"],
                        self.members["agy1"],
                        "milestone",
                        "Historical finished milestone",
                        f"old-{i}",
                        i,
                    )
                    for i in range(2000)
                ],
            )
        message = self.peers.message(
            self.members["agy1"],
            self.team["id"],
            "direct",
            "Continue the workstream",
            "new-after-history",
            ["code2"],
        )
        self.assertEqual(message["body"], "Continue the workstream")

    def test_resume_rechecks_write_scopes_claimed_by_another_team(self):
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, paths=["src"]
        )
        snapshot = self.peers.sync(self.members["agy1"], self.team["id"])
        paused = self.peers.set_state(
            self.members["agy1"],
            self.team["id"],
            snapshot["team"]["revision"],
            "paused",
            "User pauses this team",
        )
        other = self.peers.create(
            self.members["agy2"],
            "Other",
            "Work while paused",
            ["agy2", "code3"],
            "paused-other",
        )
        self.peers.update_member(
            self.members["code3"], other["id"], "code3", 1, paths=["src"]
        )
        with self.assertRaises(TeamError):
            self.peers.set_state(
                self.members["agy1"],
                self.team["id"],
                paused["revision"],
                "active",
                "User resumes",
            )

    def test_user_steering_reaches_all_peers_affected_by_new_epoch(self):
        with self.assertRaises(TeamError):
            self.peers.message(
                self.members["agy1"],
                self.team["id"],
                "user_instruction",
                "New shared scope",
                "steering",
                ["code2"],
            )
        msg = self.peers.message(
            self.members["agy1"],
            self.team["id"],
            "user_instruction",
            "New shared scope",
            "steering",
        )
        self.assertEqual(
            len(
                self.peers.receipts(self.members["agy1"], self.team["id"], msg["id"])[
                    "recipients"
                ]
            ),
            4,
        )

    def test_one_codex_conversation_cannot_enroll_as_two_peer_reviewers(self):
        with self.store.transaction() as db:
            native_id = db.execute(
                "SELECT native_id FROM members WHERE id=?", (self.members["code2"],)
            ).fetchone()[0]
        with self.assertRaises(TeamError):
            self.store.join("codex", "another-alias", self.root, native_id)

    def test_partial_ack_does_not_complete_a_new_epoch(self):
        for alias in self.members:
            self.sync(alias)
        self.peers.message(
            self.members["agy1"],
            self.team["id"],
            "direct",
            "Earlier detail",
            "early",
            ["code2"],
        )
        self.peers.update_member(
            self.members["agy1"], self.team["id"], "agy1", 1, lane="Research"
        )
        actor = self.members["code2"]
        page = self.peers.sync(actor, self.team["id"], limit=1)
        self.assertTrue(page["more"])
        self.peers.acknowledge(
            actor, self.team["id"], page["team"]["epoch"], page["cursor"]
        )
        with self.store.transaction() as db:
            with self.assertRaises(TeamError):
                self.peers.member(db, actor, self.team["id"], synced=True)
        self.sync("code2")
        with self.store.transaction() as db:
            self.peers.member(db, actor, self.team["id"], synced=True)

    def test_direct_messages_require_explicit_distinct_other_peers(self):
        for recipients in (None, [], "code2", ["agy1"], ["code2", "code2"]):
            with self.subTest(recipients=recipients), self.assertRaises(TeamError):
                self.peers.message(
                    self.members["agy1"],
                    self.team["id"],
                    "direct",
                    "Scoped",
                    "invalid",
                    recipients,
                )

    def test_noop_membership_and_acknowledged_history_do_not_create_work(self):
        for alias in self.members:
            self.sync(alias)
        before = self.peers.sync(self.members["agy1"], self.team["id"])
        member = self.peers.update_member(
            self.members["agy1"], self.team["id"], "agy1", 1
        )
        self.assertEqual(member["revision"], 1)
        after = self.peers.sync(self.members["agy1"], self.team["id"])
        self.assertEqual(before["team"]["epoch"], after["team"]["epoch"])
        self.assertEqual(
            self.peers.sync(self.members["code2"], self.team["id"], after=0)["action"],
            "idle",
        )

    def test_migration_is_additive_backed_up_and_rerunnable(self):
        with (
            closing(sqlite3.connect(self.backup)) as before,
            closing(self.store.connect()) as after,
        ):
            for table in ("members", "work", "assignments", "notices", "events"):
                self.assertEqual(
                    before.execute(f"PRAGMA table_info({table})").fetchall(),
                    [tuple(r) for r in after.execute(f"PRAGMA table_info({table})")],
                )
                self.assertEqual(
                    before.execute(f"SELECT * FROM {table}").fetchall(),
                    [tuple(r) for r in after.execute(f"SELECT * FROM {table}")],
                )
        self.assertIsNone(migrate(self.store))
        if os.name == "nt":
            import win32security

            # Compare the entire access policy to a new file in the same private
            # directory. An effective ACE need not carry the inherited marker.
            def file_dacl(path):
                info = win32security.DACL_SECURITY_INFORMATION
                descriptor = win32security.GetNamedSecurityInfo(
                    str(path), win32security.SE_FILE_OBJECT, info
                )
                acl = descriptor.GetSecurityDescriptorDacl()
                self.assertIsNotNone(acl)
                self.assertGreater(acl.GetAceCount(), 0)
                return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                    descriptor, win32security.SDDL_REVISION_1, info
                )

            reference = self.backup.with_suffix(".acl-reference")
            reference.touch(exist_ok=False)
            self.assertEqual(file_dacl(self.backup), file_dacl(reference))
        else:
            self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)

    def test_any_provider_can_create_a_team_and_write_only_its_own_board(self):
        second = self.peers.create(
            self.members["code2"],
            "Second",
            "Another scoped effort",
            ["agy1", "code2"],
            "second",
        )
        self.assertNotEqual(self.team["id"], second["id"])
        self.peers.board(
            self.members["agy1"], self.team["id"], 0, "Research", "Share findings"
        )
        self.peers.board(
            self.members["code2"],
            self.team["id"],
            0,
            "Implementation",
            "Verify behavior",
        )
        result = self.sync("code2")
        self.assertEqual(
            {b["member_id"]: b["current"] for b in result["board"]},
            {self.members["agy1"]: "Research", self.members["code2"]: "Implementation"},
        )
        with self.assertRaises(TeamError):
            self.peers.board(
                self.members["agy1"], self.team["id"], 0, "Stale", "Overwrite"
            )

    def test_direct_message_and_broadcast_receipts_are_distinct_from_ack(self):
        for alias in self.members:
            self.sync(alias)
        direct = self.peers.message(
            self.members["agy2"],
            self.team["id"],
            "direct",
            "Please check this interface",
            "direct",
            ["code3"],
        )
        self.assertEqual(
            direct["id"],
            self.peers.message(
                self.members["agy2"],
                self.team["id"],
                "direct",
                "Please check this interface",
                "direct",
                ["code3"],
            )["id"],
        )
        self.assertEqual(
            self.peers.sync(self.members["code2"], self.team["id"])["messages"], []
        )
        result = self.peers.sync(self.members["code3"], self.team["id"])
        self.assertEqual([m["id"] for m in result["messages"]], [direct["id"]])
        self.assertIsNotNone(result["messages"][0]["received_at"])
        self.assertIsNone(result["messages"][0]["acked_at"])
        self.peers.acknowledge(
            self.members["code3"],
            self.team["id"],
            result["team"]["epoch"],
            result["cursor"],
        )
        self.assertEqual(
            self.peers.sync(self.members["code3"], self.team["id"])["messages"], []
        )
        self.peers.message(
            self.members["code3"],
            self.team["id"],
            "milestone",
            "Interface verified",
            "milestone",
        )
        self.assertEqual(
            len(self.peers.sync(self.members["agy2"], self.team["id"])["messages"]), 1
        )

    def test_inactive_keeps_context_and_cross_peer_changes_require_user_instruction(
        self,
    ):
        self.sync("code2")
        with self.assertRaises(TeamError):
            self.peers.update_member(
                self.members["agy1"], self.team["id"], "code2", 1, state="inactive"
            )
        changed = self.peers.update_member(
            self.members["agy1"],
            self.team["id"],
            "code2",
            1,
            state="inactive",
            user_instruction="User asked to pause code2",
        )
        self.peers.message(
            self.members["agy2"],
            self.team["id"],
            "milestone",
            "Preserve this change",
            "while-inactive",
        )
        inactive = self.peers.sync(self.members["code2"], self.team["id"])
        self.assertEqual(inactive["action"], "inactive")
        self.assertTrue(inactive["sync_required"])
        self.assertTrue(
            any(m["body"] == "Preserve this change" for m in inactive["messages"])
        )
        self.peers.update_member(
            self.members["code2"],
            self.team["id"],
            "code2",
            changed["revision"],
            state="active",
        )
        self.assertTrue(
            self.peers.sync(self.members["agy2"], self.team["id"])["sync_required"]
        )

    def test_write_scopes_reject_escape_overlap_and_stale_changes(self):
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, paths=["src/core"]
        )
        for paths in (["../outside"], ["src"], ["src/core/sub"]):
            with self.assertRaises(TeamError):
                self.peers.update_member(
                    self.members["agy2"], self.team["id"], "agy2", 1, paths=paths
                )
        changed = self.peers.update_member(
            self.members["agy2"], self.team["id"], "agy2", 1, paths=["docs"]
        )
        self.assertEqual(json.loads(changed["paths"]), ["docs"])
        with self.assertRaises(TeamError):
            self.peers.update_member(
                self.members["agy2"], self.team["id"], "agy2", 1, paths=[]
            )

    def test_overlapping_writers_cannot_hide_in_another_team_of_same_project(self):
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, paths=["src"]
        )
        other = self.peers.create(
            self.members["agy2"],
            "Other team",
            "Separate workstream",
            ["agy2", "code3"],
            "other-team",
        )
        with self.assertRaises(TeamError):
            self.peers.update_member(
                self.members["code3"], other["id"], "code3", 1, paths=["src/module"]
            )

    def test_bounded_sync_cannot_skip_unread_messages_or_confuse_epoch_with_cursor(
        self,
    ):
        for alias in self.members:
            self.sync(alias)
        for i in range(5):
            self.peers.message(
                self.members["agy1"],
                self.team["id"],
                "direct",
                f"Detail {i}",
                f"message-{i}",
                ["code2"],
            )
        actor = self.members["code2"]
        with self.assertRaises(TeamError):
            self.peers.sync(actor, self.team["id"], after=999)
        page = self.peers.sync(actor, self.team["id"], limit=2)
        self.assertEqual(len(page["messages"]), 2)
        self.assertTrue(page["more"])
        with self.assertRaises(TeamError):
            self.peers.acknowledge(
                actor, self.team["id"], page["cursor"], page["cursor"]
            )
        self.peers.acknowledge(
            actor, self.team["id"], page["team"]["epoch"], page["cursor"]
        )
        next_page = self.peers.sync(actor, self.team["id"], limit=2)
        self.assertTrue(all(m["seq"] > page["cursor"] for m in next_page["messages"]))

    def test_unselected_and_cross_project_conversations_cannot_read_or_receive(self):
        outsider = self.store.join("agy", "outsider", self.root)["id"]
        other_project = self.root / "other"
        other_project.mkdir()
        remote = self.store.join("agy", "remote", other_project)["id"]
        for actor in (outsider, remote):
            with self.assertRaises(TeamError):
                self.peers.sync(actor, self.team["id"])
        with self.assertRaises(TeamError):
            self.peers.message(
                self.members["agy1"],
                self.team["id"],
                "direct",
                "Scope violation",
                "outside",
                ["outsider"],
            )
