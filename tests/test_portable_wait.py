import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from native_team.peers import PeerStore
from native_team.schema import migrate
from native_team.store import Store, TeamError
from native_team.waiting import wait


class PortableWaitTests(unittest.TestCase):
    def test_readonly_poll_observes_work_until_ack_in_a_unicode_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve() / "研究 #100%"
            project.mkdir()
            store = Store(project / "local state")
            migrate(store)
            peers = PeerStore(store)
            sender = store.join("agy", "sender", project)["id"]
            target = store.join("agy", "reader", project)["id"]
            team = peers.create(
                sender, "Test", "Share a result", ["sender", "reader"], "test"
            )
            # Exercise the portable branch on Linux as well as on the hosted Mac and Windows jobs.
            with (
                patch("native_team.waiting.sys.platform", "darwin"),
                patch(
                    "native_team.waiting.ctypes.CDLL",
                    side_effect=AssertionError("unexpected inotify"),
                ),
            ):

                def inbox():
                    return wait(
                        store.state, project, "reader", timeout=1, mode="actionable"
                    )

                first = inbox()
                self.assertEqual(first["wait_mode"], "poll")
                self.assertIn("team_activation", first["reasons"])
                self.assertTrue(inbox()["ready"])
                page = peers.sync(target, team["id"])
                peers.acknowledge(
                    target, team["id"], page["team"]["epoch"], page["cursor"]
                )
                self.assertEqual(inbox()["reason"], "deadline")
                with self.assertRaisesRegex(TeamError, "requires Linux"):
                    wait(store.state, project, "reader", timeout=1, mode="recovery")
