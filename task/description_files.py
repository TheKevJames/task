"""
Moving the description files written by task 9.3 and earlier, and by 8.x,
into the database: one `<namespace>/<id>.txt` per described task, beside
`.<id>.txt.tmp` files a writer may have left half-written.
"""

import contextlib
import pathlib
import re
import sqlite3
from collections.abc import Iterable

DESCRIPTION_RE = re.compile(r'(\d+)\.txt')
DESCRIPTION_TMP_RE = re.compile(r'\.\d+\.txt\.tmp')


def migrate(
    conn: sqlite3.Connection, folder: pathlib.Path
) -> list[pathlib.Path]:
    """Move every namespace's description files in; the files left stale."""
    # A fresh database holds no tasks yet, and any files beside it belong to
    # an 8.x store, which legacy imports once this has run: sweeping them
    # here as orphans would lose them.
    if not conn.execute('SELECT 1 FROM task LIMIT 1').fetchone():
        return []
    stale: list[pathlib.Path] = []
    for (namespace,) in conn.execute('SELECT name FROM namespace').fetchall():
        stale.extend(import_(conn, namespace, folder / namespace))
    return stale


def import_(
    conn: sqlite3.Connection, namespace: str, directory: pathlib.Path
) -> list[pathlib.Path]:
    """
    Move `directory`'s descriptions onto `namespace`'s tasks; the files left
    stale. Ids are never reused, so a file whose task is gone is an orphan.
    """
    if not directory.is_dir():
        return []
    stale: list[pathlib.Path] = []
    for path in sorted(directory.iterdir()):
        match = DESCRIPTION_RE.fullmatch(path.name)
        if match:
            text = path.read_text(encoding='utf-8').strip()
            conn.execute(
                'UPDATE task SET description = :description'
                ' WHERE namespace = :namespace AND id = :id',
                {
                    'description': text or None,
                    'namespace': namespace,
                    'id': int(match.group(1)),
                },
            )
        if match or DESCRIPTION_TMP_RE.fullmatch(path.name):
            stale.append(path)
    return stale


def remove(paths: Iterable[pathlib.Path]) -> None:
    """Delete `paths`, then whichever of their directories are left empty."""
    directories: set[pathlib.Path] = set()
    for path in paths:
        path.unlink(missing_ok=True)
        directories.add(path.parent)
    for directory in directories:
        with contextlib.suppress(OSError):
            directory.rmdir()
