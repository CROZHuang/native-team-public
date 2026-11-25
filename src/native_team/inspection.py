"""Read-only team directory, without enrollment, receipts or runtime control."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

from .schema import available
from .store import TeamError


@contextmanager
def readonly_snapshot(state):
    """Read an existing ledger without creating state or initializing Store."""
    path = Path(state).expanduser().resolve() / "team.sqlite3"
    if not path.is_file():
        yield None
        return
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
    except sqlite3.Error as error:
        raise TeamError(f"Could not read team directory at {path}: {error}") from error


def list_teams(state, project=None):
    project = str(Path(project).expanduser().resolve()) if project is not None else None
    with readonly_snapshot(state) as db:
        if db is None:
            return []
        if not available(db):
            return []
        query = "SELECT id,name,project,state FROM peer_teams"
        parameters = ()
        if project is not None:
            query += " WHERE project=?"
            parameters = (project,)
        query += " ORDER BY state IN ('active','paused') DESC,project,name,id"
        teams = []
        for row in db.execute(query, parameters):
            team = {key: row[key] for key in ("name", "project", "state")}
            team["members"] = [
                dict(member)
                for member in db.execute(
                    "SELECT n.alias,n.provider,p.state FROM peer_members p "
                    "JOIN members n ON n.id=p.member_id WHERE p.team_id=? ORDER BY n.alias",
                    (row["id"],),
                )
            ]
            teams.append(team)
        return teams
