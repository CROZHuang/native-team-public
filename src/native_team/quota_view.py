"""Read-only, secret-free quota projections for the terminal and workbench."""

from pathlib import Path
import time

from .availability import read_availability
from .claude_quota import VISIBLE_MEMBERS
from .inspection import readonly_snapshot
from .quota_collector import collector_status
from .quota_store import native_member_view, shared_claude_account
from .recovery import agy_lock_held, agy_watches, read_recovery
from .schema import available


def quota_overview(state):
    stamp = time.time()
    result = {
        "collector": collector_status(state),
        "members": [],
        "read_at": stamp,
        "shared_claude_account": None,
    }
    with readonly_snapshot(state) as db:
        if db is None or not available(db):
            return result
        result["shared_claude_account"] = shared_claude_account(state, db, now=stamp)
        try:
            live_watches = agy_watches(db, Path(state))
        except OSError:
            live_watches = None
        members = db.execute(
            f"""SELECT m.id,m.alias,m.provider,m.project FROM members m
                WHERE {VISIBLE_MEMBERS} ORDER BY m.provider,m.alias"""
        )
        for row in members:
            actor = row["id"]
            is_agy = row["provider"] == "agy"
            if not is_agy:
                watches = None
            elif live_watches is None:
                watches = {"recovery": None, "inbox": None}
            else:
                visible = live_watches.get("visible", set())
                if (
                    actor in visible
                    or agy_lock_held(state, row["project"], row["alias"]) is False
                ):
                    watches = {
                        "recovery": len(live_watches["recovery"].get(actor, [])),
                        "inbox": len(live_watches["inbox"].get(actor, [])),
                    }
                else:
                    watches = {"recovery": None, "inbox": None}
            result["members"].append(
                {
                    "alias": row["alias"],
                    "provider": row["provider"],
                    "project": row["project"],
                    "native_quota": native_member_view(db, actor, now=stamp),
                    "availability": read_availability(db, actor, now=stamp),
                    "recovery": read_recovery(db, actor) if is_agy else None,
                    "watches": watches,
                }
            )
    return result
