"""Add flat-team state without changing tables used by already-open pair clients."""

from contextlib import closing
import os
import sqlite3
import time
import uuid

VERSION = 1
AVAILABILITY_TABLE = """CREATE TABLE member_availability (
    member_id TEXT PRIMARY KEY REFERENCES members(id),
    status TEXT NOT NULL CHECK(status IN ('ready','cooldown')),
    resume_after REAL, reason TEXT NOT NULL,
    revision INTEGER NOT NULL, updated REAL NOT NULL, recorded_by TEXT NOT NULL
)"""
QUOTA_TABLE = """CREATE TABLE member_quota (
    member_id TEXT PRIMARY KEY REFERENCES members(id),
    posture TEXT NOT NULL CHECK(posture IN ('unknown','normal','conserve','cooldown')),
    availability_revision INTEGER NOT NULL,
    observations TEXT NOT NULL DEFAULT '[]'
)"""
NATIVE_QUOTA_TABLES = [
    """CREATE TABLE IF NOT EXISTS native_quota_sources (
        member_id TEXT PRIMARY KEY REFERENCES members(id), source_id TEXT NOT NULL,
        provider TEXT NOT NULL, connection TEXT NOT NULL, session_key TEXT NOT NULL,
        account_key TEXT, model TEXT NOT NULL, observed_at REAL NOT NULL,
        received_at REAL NOT NULL, snapshot TEXT NOT NULL, association TEXT NOT NULL DEFAULT 'verified'
    )""",
    """CREATE TABLE IF NOT EXISTS native_quota_policy (
        member_id TEXT PRIMARY KEY REFERENCES members(id), posture TEXT NOT NULL,
        reason TEXT NOT NULL, resets_at REAL, updated REAL NOT NULL,
        evidence_at REAL NOT NULL, blocking_source TEXT, revision INTEGER NOT NULL,
        blocking_windows TEXT NOT NULL DEFAULT '[]'
    )""",
]
RECOVERY_TABLE = """CREATE TABLE native_quota_recovery (
    member_id TEXT PRIMARY KEY REFERENCES members(id), incident TEXT NOT NULL,
    due_at REAL, target TEXT, offered_at REAL, received_at REAL, confirmed_at REAL,
    state TEXT NOT NULL, detail TEXT NOT NULL, watch_count INTEGER NOT NULL,
    updated REAL NOT NULL
)"""
TABLES = [
    """CREATE TABLE peer_teams (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL,
        purpose TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES members(id),
        state TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
        epoch INTEGER NOT NULL DEFAULT 1, request_key TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(created_by, request_key)
    )""",
    """CREATE TABLE peer_members (
        team_id TEXT NOT NULL REFERENCES peer_teams(id), member_id TEXT NOT NULL REFERENCES members(id),
        lane TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'active',
        paths TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1,
        seen_epoch INTEGER NOT NULL DEFAULT 0, acked_epoch INTEGER NOT NULL DEFAULT 0,
        read_seq INTEGER NOT NULL DEFAULT 0, acked_seq INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(team_id, member_id)
    )""",
    """CREATE TABLE peer_boards (
        team_id TEXT NOT NULL, member_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        current TEXT NOT NULL, next_step TEXT NOT NULL, blockers TEXT NOT NULL,
        handoffs TEXT NOT NULL, updated REAL NOT NULL,
        PRIMARY KEY(team_id, member_id),
        FOREIGN KEY(team_id, member_id) REFERENCES peer_members(team_id, member_id)
    )""",
    """CREATE TABLE peer_messages (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        team_id TEXT NOT NULL REFERENCES peer_teams(id), sender TEXT NOT NULL REFERENCES members(id),
        kind TEXT NOT NULL, body TEXT NOT NULL, request_key TEXT NOT NULL, created REAL NOT NULL,
        UNIQUE(team_id, sender, request_key)
    )""",
    """CREATE TABLE peer_deliveries (
        id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES peer_messages(id),
        recipient TEXT NOT NULL REFERENCES members(id), state TEXT NOT NULL DEFAULT 'pending',
        detail TEXT NOT NULL DEFAULT '', received_at REAL, acked_at REAL,
        UNIQUE(message_id, recipient)
    )""",
    """CREATE TABLE peer_tasks (
        id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES peer_teams(id),
        created_by TEXT NOT NULL REFERENCES members(id), owner TEXT NOT NULL REFERENCES members(id),
        reviewer TEXT NOT NULL REFERENCES members(id), brief TEXT NOT NULL, acceptance TEXT NOT NULL,
        paths TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'ready',
        revision INTEGER NOT NULL DEFAULT 1, rounds INTEGER NOT NULL DEFAULT 0,
        instruction TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '', handoff_needed INTEGER NOT NULL DEFAULT 0,
        reviewer_handoff_needed INTEGER NOT NULL DEFAULT 0,
        request_key TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(team_id, created_by, request_key), CHECK(owner != reviewer)
    )""",
    """CREATE TABLE peer_decisions (
        id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES peer_teams(id),
        proposed_by TEXT NOT NULL REFERENCES members(id), topic TEXT NOT NULL,
        proposal TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'proposed',
        revision INTEGER NOT NULL DEFAULT 1, request_key TEXT NOT NULL, created REAL NOT NULL,
        supersedes TEXT REFERENCES peer_decisions(id), authority TEXT NOT NULL DEFAULT '',
        UNIQUE(team_id, proposed_by, request_key)
    )""",
    """CREATE TABLE peer_task_history (
        task_id TEXT NOT NULL REFERENCES peer_tasks(id), revision INTEGER NOT NULL,
        actor TEXT NOT NULL REFERENCES members(id), kind TEXT NOT NULL,
        snapshot TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(task_id, revision)
    )""",
    """CREATE TABLE peer_votes (
        decision_id TEXT NOT NULL REFERENCES peer_decisions(id), member_id TEXT NOT NULL REFERENCES members(id),
        response TEXT, note TEXT NOT NULL DEFAULT '', updated REAL,
        PRIMARY KEY(decision_id, member_id)
    )""",
    """CREATE TABLE wake_batches (
        id TEXT PRIMARY KEY, recipient TEXT NOT NULL REFERENCES members(id),
        state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
    )""",
    """CREATE TABLE wake_items (
        batch_id TEXT NOT NULL REFERENCES wake_batches(id), origin TEXT NOT NULL,
        item_id TEXT NOT NULL, PRIMARY KEY(batch_id, origin, item_id)
    )""",
    "CREATE INDEX peer_delivery_inbox ON peer_deliveries(recipient, received_at, state)",
    "CREATE INDEX peer_task_owner ON peer_tasks(owner, state)",
]


def available(db):
    return db.execute("PRAGMA user_version").fetchone()[0] == VERSION


def has_availability(db):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='member_availability'"
        ).fetchone()
        is not None
    )


def has_quota(db):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='member_quota'"
        ).fetchone()
        is not None
    )


def has_native_quota(db):
    return (
        db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('native_quota_sources','native_quota_policy')"
        ).fetchone()[0]
        == 2
    )


def has_recovery(db):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_quota_recovery'"
        ).fetchone()
        is not None
    )


def migrate(store):
    """Explicit, idempotent migration; a stable live backup precedes all DDL."""
    backup = None
    with store.transaction() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if (
            version == VERSION
            and has_availability(db)
            and has_quota(db)
            and has_native_quota(db)
            and has_recovery(db)
        ):
            return None
        if version not in (0, VERSION):
            raise ValueError(f"Unsupported ledger schema version {version}")
        directory = store.state / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        backup = (
            directory
            / f"before-native-team-{int(time.time())}-{uuid.uuid4().hex[:8]}.sqlite3"
        )
        # The writer lock above freezes changes. A separate read connection can back up WAL
        # safely without invoking backup() on the connection holding a write transaction.
        os.close(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        try:
            with (
                closing(sqlite3.connect(store.path)) as source,
                closing(sqlite3.connect(backup)) as target,
            ):
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Migration backup integrity check failed")
                for (table,) in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ):
                    quoted = '"' + table.replace('"', '""') + '"'
                    if (
                        source.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                        != target.execute(f"SELECT count(*) FROM {quoted}").fetchone()[
                            0
                        ]
                    ):
                        raise ValueError("Migration backup record count differs")
        except Exception:
            backup.unlink(missing_ok=True)
            raise
        if version == 0:
            for statement in TABLES:
                db.execute(statement)
        if not has_availability(db):
            db.execute(AVAILABILITY_TABLE)
        if not has_quota(db):
            db.execute(QUOTA_TABLE)
        if not has_native_quota(db):
            for statement in NATIVE_QUOTA_TABLES:
                db.execute(statement)
        if not has_recovery(db):
            db.execute(RECOVERY_TABLE)
        db.execute(f"PRAGMA user_version={VERSION}")
    return backup
