from contextlib import AsyncExitStack
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class NativeToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_revision_and_follow_on_without_old_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            async with AsyncExitStack() as stack:
                peers = {}
                for provider in ("codex", "agy"):
                    params = StdioServerParameters(
                        command=sys.executable,
                        args=[
                            "-m",
                            "native_team.cli",
                            "--state",
                            str(root / "state"),
                            "mcp",
                            "--client",
                            provider,
                        ],
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                    peer = await stack.enter_async_context(ClientSession(read, write))
                    await asyncio.wait_for(peer.initialize(), timeout=10)
                    peers[provider] = peer

                async def call(provider, name, args=None):
                    result = await asyncio.wait_for(
                        peers[provider].call_tool(name, args or {}), timeout=10
                    )
                    self.assertFalse(result.isError, str(result.content))
                    return result.structuredContent or json.loads(
                        result.content[0].text
                    )

                await call(
                    "codex",
                    "team_join",
                    dict(
                        alias="lead",
                        project=str(root),
                        native_session_id=str(uuid.uuid4()),
                    ),
                )
                joined = await call(
                    "agy", "team_join", dict(alias="adviser", project=str(root))
                )
                mode = "recovery" if sys.platform == "linux" else "actionable"
                self.assertIn(f"--mode {mode}", joined["recovery_setup"])
                self.assertIn("OWN native background", joined["recovery_setup"])
                work = (
                    await call(
                        "codex",
                        "team_work",
                        dict(
                            intent="Read the fixture",
                            acceptance="Two verified answers",
                            next_step="Revise A, then create B",
                            request_key="test",
                        ),
                    )
                )["changed"]
                for key in ("A", "B"):
                    task = (
                        await call(
                            "codex",
                            "team_assign",
                            dict(
                                work_id=work["id"],
                                owner="adviser",
                                brief="Read fixture",
                                acceptance="Evidence",
                                request_key=key,
                            ),
                        )
                    )["changed"]
                    for round_index in range(2 if key == "A" else 1):
                        taken = (await call("agy", "team_take"))["assignment"]
                        self.assertEqual(taken["id"], task["id"])
                        review = (
                            await call(
                                "agy",
                                "team_report",
                                dict(
                                    assignment_id=taken["id"],
                                    revision=taken["revision"],
                                    summary="Fixture answer",
                                    evidence="Fixture source",
                                ),
                            )
                        )["changed"]
                        received = await call("codex", "team_state")
                        self.assertEqual(received["action"], "review_required")
                        decision = (
                            "revise" if key == "A" and round_index == 0 else "accept"
                        )
                        await call(
                            "codex",
                            "team_review",
                            dict(
                                assignment_id=task["id"],
                                revision=review["revision"],
                                decision=decision,
                                explanation="Add planned detail"
                                if decision == "revise"
                                else "Verified evidence",
                            ),
                        )
                pending = await call("codex", "team_state")
                self.assertEqual(pending["action"], "next_step_required")
                done = await call(
                    "codex",
                    "team_update",
                    dict(
                        work_id=work["id"],
                        revision=1,
                        state="done",
                        next_step="Wait for user discussion",
                        evidence="Both fixture answers verified",
                    ),
                )
                self.assertEqual(done["changed"]["state"], "done")
                self.assertIsNone((await call("agy", "team_take"))["assignment"])
                compact = await call("codex", "team_state")
                self.assertEqual(compact["work"], [])
                self.assertEqual(compact["assignments"], [])
                self.assertEqual(compact["teams"], [])
                self.assertEqual(compact["closed_team_count"], 0)
                self.assertEqual(
                    (compact["closed_work_count"], compact["closed_assignment_count"]),
                    (1, 2),
                )
                self.assertNotIn("conserve_guidance", compact)
                full = await call("codex", "team_state", {"include_closed": True})
                self.assertEqual([w["id"] for w in full["work"]], [work["id"]])
                self.assertEqual(len(full["assignments"]), 2)
                again = await call(
                    "agy", "team_join", dict(alias="adviser", project=str(root))
                )
                self.assertEqual(again["coordination"]["work"], [])
                self.assertEqual(again["coordination"]["closed_assignment_count"], 2)
