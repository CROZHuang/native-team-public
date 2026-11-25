"""Read-only, fail-open native SessionStart guidance for already selected agy peers.

The hook supplies a reminder, not a model turn, enrollment or background process.
An exact durable session HMAC is required; no title, cwd or account guessing.
"""

from contextlib import closing
import json
from pathlib import Path
import shlex
import sqlite3
import sys
import sysconfig

from .availability import read_availability
from .quota_capture import pseudonym, read_json, text
from .schema import available, has_native_quota


def console_entry():
    name = "native-team.exe" if sys.platform == "win32" else "native-team"
    entry = Path(sysconfig.get_path("scripts")) / name
    if entry.is_file():
        return entry
    user = sysconfig.get_preferred_scheme("user")
    return Path(sysconfig.get_path("scripts", scheme=user)) / name


def recovery_guidance(project, alias, state):
    linux = sys.platform == "linux"
    mode = "recovery" if linux else "actionable"
    entry = console_entry()
    if sys.platform == "win32":
        # The default native Windows shell is PowerShell; paths can contain apostrophes.
        def literal(value):
            return "'" + str(value).replace("'", "''") + "'"

        command = (
            f"& {literal(entry)} --state {literal(state)} wait --project {literal(project)} "
            f"--alias {literal(alias)} --timeout 28800 --mode {mode}"
        )
    else:
        command = shlex.join(
            [
                str(entry),
                "--state",
                str(state),
                "wait",
                "--project",
                str(project),
                "--alias",
                alias,
                "--timeout",
                "28800",
                "--mode",
                mode,
            ]
        )
    if not linux:
        return (
            f"When idle, run `{command}` through your OWN native background-command tool. "
            "Reuse one wait per conversation and sync on delivery. Respect pauses and native permissions. "
            "Automatic agy quota-recovery observation requires Linux."
        )
    return (
        "Before substantial authorized work in this enrolled agy conversation, verify or arm "
        "ONE recovery watch through your OWN native background-command tool: `"
        + command
        + "`. "
        "Keep it through work and idle; reuse an existing live watch rather than duplicate it. "
        "An ordinary actionable inbox wait is separate and is needed only while idle. "
        "Preserve native permissions, explicit holds, inactive memberships and team pauses. "
        "A reminder or elapsed reset is not evidence of recovery; sync/ACK and verify native "
        "execution and quota before recording readiness or claiming work."
    )


def session_start(payload, state):
    """Return no steps for unknown, ambiguous, disabled or held conversations.

    Old verified association is useful when reopening: its live MCP lock need not yet
    exist. This is identity evidence only, never evidence of current quota or liveness.
    All reads are bounded and fail open; no Store construction or ledger mutation.
    """
    try:
        if (
            not isinstance(payload, dict)
            or payload.get("hookEventName") != "SessionStart"
        ):
            return {}
        session = text(payload.get("conversationId"), 160)
        if not session:
            return {}
        state = Path(state)
        if read_json(state / "quota/config.json", 4096).get("enabled") is not True:
            return {}
        with (state / "quota/identity.key").open("rb") as source:
            key = source.read(33)
        if len(key) != 32:
            return {}
        session_key = pseudonym(key, "agy-session", session)
        with closing(
            sqlite3.connect(
                (state / "team.sqlite3").resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.2,
            )
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if not available(db) or not has_native_quota(db):
                return {}
            matches = db.execute(
                """SELECT m.id,m.alias,m.project FROM native_quota_sources s
                JOIN members m ON m.id=s.member_id WHERE s.provider='agy' AND m.provider='agy'
                AND s.session_key=? AND s.association='verified'""",
                (session_key,),
            ).fetchall()
            if len(matches) != 1:
                return {}
            member = matches[0]
            if not db.execute(
                """SELECT 1 FROM peer_members p JOIN peer_teams t ON t.id=p.team_id
                WHERE p.member_id=? AND p.state='active' AND t.state='active' LIMIT 1""",
                (member["id"],),
            ).fetchone():
                return {}
            if read_availability(db, member["id"])["status"] == "cooldown":
                return {}
        reminder = (
            f"This native conversation has a retained Native Team enrollment as {member['alias']} "
            f"in {member['project']}. Rejoin that same identity with your own native team_join "
            "if this MCP connection is new; do not create a replacement alias. "
            + recovery_guidance(member["project"], member["alias"], state)
        )
        return {"injectSteps": [{"systemMessage": {"systemMessage": reminder}}]}
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return {}


def read_session_start(stream, state):
    try:
        raw = stream.read(262145)
        if len(raw) > 262144:
            return {}
        return session_start(json.loads(raw), state)
    except (OSError, ValueError, TypeError):
        return {}
