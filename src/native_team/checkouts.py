"""Identify linked Git checkouts and compare ledger reservations without running Git."""

from pathlib import Path
import json
import os
import stat
import sys
import unicodedata


def marker(path):
    # Never open a special file or follow a metadata symlink.
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return ""
        with path.open("rb") as handle:
            data = handle.read(4097)
        if len(data) > 4096:
            return ""
        value = os.fsdecode(data).rstrip("\r\n")
        return "" if any(c in value for c in "\0\r\n") else value
    except (OSError, ValueError):
        return ""


def identify(directory):
    root = Path(directory).expanduser().resolve()
    main = root
    admin = root / ".git"
    pointer = marker(admin)
    if pointer.startswith("gitdir: "):
        try:
            linked = (root / pointer[8:]).resolve(strict=True)
            common_marker = marker(linked / "commondir")
            backlink = marker(linked / "gitdir")
            if not common_marker or not backlink:
                return root, ""
            if (linked / common_marker).is_symlink():
                return root, ""
            common = (linked / common_marker).resolve(strict=True)
            if (
                linked.parent != common / "worktrees"
                or common.name != ".git"
                or not (common / "objects").is_dir()
                or not marker(common / "HEAD")
                or not marker(linked / "HEAD")
                or (linked / backlink).resolve(strict=True) != admin
            ):
                return root, ""
            main, admin = common.parent, linked
        except (OSError, RuntimeError, ValueError):
            return root, ""
    head = marker(admin / "HEAD") if admin.is_dir() and not admin.is_symlink() else ""
    return main, head.removeprefix("ref: refs/heads/") if head.startswith(
        "ref: refs/heads/"
    ) else ""


def has_checkouts(db):
    return (
        db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('member_checkouts','task_checkouts')"
        ).fetchone()[0]
        == 2
    )


def project_path(state, directory):
    from .inspection import readonly_snapshot

    root = Path(directory).expanduser().resolve()
    main, _ = identify(root)
    if main != root:
        with readonly_snapshot(state) as db:
            if (
                db is None
                or not has_checkouts(db)
                or db.execute(
                    "SELECT 1 FROM members WHERE project=? LIMIT 1", (str(root),)
                ).fetchone()
            ):
                return root
    return main


def equivalent(first, second):
    if sys.platform in {"darwin", "win32"}:
        return unicodedata.normalize("NFC", first.casefold()) == unicodedata.normalize(
            "NFC", second.casefold()
        )
    return first == second


def location(db, member):
    if has_checkouts(db):
        row = db.execute(
            "SELECT checkout,branch FROM member_checkouts WHERE member_id=?", (member,)
        ).fetchone()
        if row:
            return dict(row)
    return {}


def conflicts(db, project, member, paths, *, team_id, skip_team=None):
    """Enforce all project reservations; reveal named peers only inside this team."""
    from .peers import overlaps

    if not paths:
        return []
    own = location(db, member)
    modern = has_checkouts(db)
    fields = "c.checkout,c.branch" if modern else "NULL,NULL"
    member_join = (
        "LEFT JOIN member_checkouts c ON c.member_id=m.member_id" if modern else ""
    )
    task_join = "LEFT JOIN task_checkouts c ON c.task_id=p.id" if modern else ""
    rows = db.execute(
        f"""SELECT t.id,n.alias,m.paths,NULL,{fields} FROM peer_members m
        JOIN peer_teams t ON t.id=m.team_id JOIN members n ON n.id=m.member_id {member_join}
        WHERE t.project=? AND t.state='active' AND m.state='active' AND m.member_id<>?
        AND (? IS NULL OR t.id<>?)
        UNION ALL
        SELECT t.id,n.alias,p.paths,p.id,{fields} FROM peer_tasks p
        JOIN peer_teams t ON t.id=p.team_id JOIN members n ON n.id=p.owner {task_join}
        WHERE t.project=? AND p.owner<>? AND p.state='working'""",
        (project, member, skip_team, skip_team, project, member),
    )
    result = []
    for other_team, alias, stored, task, checkout, branch in rows:
        other_paths = json.loads(stored)
        shared = sorted({a for a in paths if any(overlaps(a, b) for b in other_paths)})
        if not shared:
            continue
        hard = not (own and checkout) or equivalent(own["checkout"], checkout)
        if other_team != team_id:
            alias, task, checkout, branch = "(another team)", None, None, None
        result.append(
            dict(
                alias=alias,
                task_id=task,
                paths=shared,
                hard=hard,
                checkout=checkout,
                branch=branch,
            )
        )
    return result


def member_view(db, project, member, paths, team_id):
    if (
        not paths
        or not db.execute(
            """SELECT 1 FROM peer_members m JOIN peer_teams t ON t.id=m.team_id
        WHERE m.member_id=? AND t.id=? AND m.state='active' AND t.state='active'""",
            (member, team_id),
        ).fetchone()
    ):
        return {**location(db, member), "merge_warnings": []}
    warnings = [
        c
        for c in conflicts(db, project, member, paths, team_id=team_id)
        if not c["hard"]
    ]
    return {**location(db, member), "merge_warnings": warnings}
