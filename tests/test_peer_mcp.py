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

from native_team.schema import migrate
from native_team.store import Store


class FlatNativeToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_native_connections_work_as_peers_and_alias_lock_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            migrate(Store(root / "state"))

            async def connect(stack, provider):
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
                r, w = await stack.enter_async_context(stdio_client(params))
                peer = await stack.enter_async_context(ClientSession(r, w))
                await asyncio.wait_for(peer.initialize(), 10)
                return peer

            async with AsyncExitStack() as stack:
                peers = {}
                for alias, provider in [
                    ("agy1", "agy"),
                    ("agy2", "agy"),
                    ("code2", "codex"),
                    ("code3", "codex"),
                    ("code4", "codex"),
                ]:
                    peers[alias] = await connect(stack, provider)
                    result = await peers[alias].call_tool(
                        "team_join",
                        {
                            "alias": alias,
                            "project": str(root),
                            **(
                                {"native_session_id": str(uuid.uuid4())}
                                if provider == "codex"
                                else {}
                            ),
                        },
                    )
                    self.assertFalse(result.isError)

                async def call(alias, tool, **args):
                    result = await asyncio.wait_for(
                        peers[alias].call_tool(tool, args), 10
                    )
                    self.assertFalse(result.isError, str(result.content))
                    return result.structuredContent or json.loads(
                        result.content[0].text
                    )

                team = await call(
                    "agy1",
                    "peer_team",
                    name="Five peers",
                    purpose="Bounded native transport fixture",
                    aliases=list(peers),
                    request_key="five",
                )

                empty = await call("agy2", "team_availability")
                self.assertEqual(empty["revision"], 0)
                held = await call(
                    "agy2",
                    "team_availability",
                    status="cooldown",
                    revision=0,
                    reason="Native quota warning",
                )
                self.assertTrue(held["held"])
                self.assertFalse((await call("code2", "team_availability"))["recorded"])
                self.assertTrue(
                    (await call("agy2", "team_state"))["availability"]["held"]
                )
                hint = (await call("code2", "team_state"))["conserve_guidance"]
                self.assertIn("checkpoint", hint.lower())
                self.assertEqual(
                    (await call("code2", "peer_sync", team_id=team["id"]))[
                        "conserve_guidance"
                    ],
                    hint,
                )
                await call(
                    "agy2",
                    "team_availability",
                    status="ready",
                    revision=1,
                    reason="Fixture recovery",
                )
                self.assertNotIn("conserve_guidance", await call("code2", "team_state"))
                for revision, reset in ((2, 4102444800), (4, 4102444800.5)):
                    recorded = await call(
                        "agy2",
                        "team_availability",
                        status="cooldown",
                        revision=revision,
                        reason="Numeric epoch",
                        resume_after=reset,
                    )
                    self.assertEqual(recorded["resume_after"], reset)
                    await call(
                        "agy2",
                        "team_availability",
                        status="ready",
                        revision=revision + 1,
                        reason="Recovered",
                    )
                invalid = await peers["agy2"].call_tool(
                    "team_availability",
                    {
                        "status": "cooldown",
                        "revision": 6,
                        "reason": "Invalid boolean epoch",
                        "resume_after": True,
                    },
                )
                self.assertTrue(invalid.isError)

                posture = await call(
                    "agy2",
                    "team_quota",
                    posture="conserve",
                    revision=6,
                    reason="User requested fewer optional wakes",
                    observations=[
                        {
                            "source": "native notice",
                            "scope": "account",
                            "bucket": "weekly",
                            "observed_at": 100,
                            "expires_at": 200,
                            "remaining": 8,
                            "unit": "percent",
                        }
                    ],
                )
                self.assertEqual(posture["posture"], "conserve")
                self.assertTrue(posture["observations"][0]["stale"])
                self.assertEqual(
                    (await call("code2", "team_quota"))["posture"], "unknown"
                )
                invalid = await peers["agy2"].call_tool(
                    "team_quota",
                    {
                        "posture": "normal",
                        "revision": 7,
                        "reason": "Invalid metadata",
                        "observations": [{"token": "unsupported"}],
                    },
                )
                self.assertTrue(invalid.isError)
                # Existing native clients do not need a new tool to clear cooldown.
                ready = await call(
                    "agy2",
                    "team_availability",
                    status="ready",
                    revision=7,
                    reason="Native recovery",
                )
                self.assertEqual(ready["posture"], "normal")

                async def sync(alias):
                    page = await call(alias, "peer_sync", team_id=team["id"])
                    self.assertFalse(page["more"])
                    await call(
                        alias,
                        "peer_ack",
                        team_id=team["id"],
                        epoch=page["team"]["epoch"],
                        through_seq=page["cursor"],
                    )
                    return page

                for alias in peers:
                    await call(
                        alias,
                        "peer_board",
                        team_id=team["id"],
                        revision=0,
                        current_work=f"{alias} workstream",
                        next_step="Exchange evidence",
                    )
                    await sync(alias)
                first = await call(
                    "agy1",
                    "peer_task_create",
                    team_id=team["id"],
                    owner="code2",
                    reviewer="agy2",
                    brief="Read fixture",
                    acceptance="Checkable evidence",
                    request_key="first",
                )
                await sync("code2")
                claim = (await call("code2", "peer_task_take", team_id=team["id"]))[
                    "task"
                ]
                report = await call(
                    "code2",
                    "peer_task_report",
                    task_id=first["id"],
                    revision=claim["revision"],
                    result="Fixture answer",
                    evidence="Fixture source",
                    changes={"files": []},
                )
                record = await call("agy2", "peer_task_get", task_id=first["id"])
                self.assertEqual(
                    record["task"]["changes"],
                    {"revision": report["revision"], "files": []},
                )
                invalid = await peers["agy1"].call_tool(
                    "peer_task_review",
                    {
                        "task_id": first["id"],
                        "revision": report["revision"],
                        "decision": "accept",
                        "explanation": "Creator is not reviewer",
                    },
                )
                self.assertTrue(invalid.isError)
                await sync("agy2")
                accepted = await call(
                    "agy2",
                    "peer_task_review",
                    task_id=first["id"],
                    revision=report["revision"],
                    decision="accept",
                    explanation="Independent evidence verified",
                )
                self.assertEqual(accepted["state"], "accepted")
                # Reverse provider roles; code3/code4 remain independent workstreams.
                await sync("code2")
                second = await call(
                    "code2",
                    "peer_task_create",
                    team_id=team["id"],
                    owner="agy1",
                    reviewer="code4",
                    brief="Follow-on analysis",
                    acceptance="Evidence",
                    request_key="second",
                )
                await sync("agy1")
                claim = (await call("agy1", "peer_task_take", team_id=team["id"]))[
                    "task"
                ]
                report = await call(
                    "agy1",
                    "peer_task_report",
                    task_id=second["id"],
                    revision=claim["revision"],
                    result="Analysis",
                    evidence="Fixture",
                )
                await sync("code4")
                await call(
                    "code4",
                    "peer_task_review",
                    task_id=second["id"],
                    revision=report["revision"],
                    decision="accept",
                    explanation="Verified",
                )
                msg = await call(
                    "code3",
                    "peer_message",
                    team_id=team["id"],
                    kind="direct",
                    body="Interface ready",
                    request_key="direct",
                    recipients=["agy2"],
                )
                receipts = await call(
                    "code3", "peer_receipts", team_id=team["id"], message_id=msg["id"]
                )
                self.assertIsNone(receipts["recipients"][0]["acked_at"])
                await sync("agy2")
                receipts = await call(
                    "code3", "peer_receipts", team_id=team["id"], message_id=msg["id"]
                )
                self.assertIsNotNone(receipts["recipients"][0]["acked_at"])
                self.assertEqual(len((await call("code3", "team_state"))["teams"]), 1)

                async with AsyncExitStack() as duplicate_stack:
                    duplicate = await connect(duplicate_stack, "agy")
                    collision = await duplicate.call_tool(
                        "team_join", {"alias": "agy1", "project": str(root)}
                    )
                    self.assertTrue(collision.isError)

            # Closing the actual native connection releases its OS lock; history survives.
            async with AsyncExitStack() as reopened:
                peer = await connect(reopened, "agy")
                result = await peer.call_tool(
                    "team_join", {"alias": "agy1", "project": str(root)}
                )
                self.assertFalse(result.isError)
                state = await peer.call_tool("team_state", {})
                payload = state.structuredContent or json.loads(state.content[0].text)
                self.assertEqual(payload["teams"][0]["id"], team["id"])
