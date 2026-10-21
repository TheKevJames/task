"""
Importing a store written by task 8.x, which kept each namespace in its own
database, `$TASK_FOLDER/<namespace>/tasks.db`, beside its description files.
Dropping it also drops migrations.MIGRATIONS' description_files.migrate.
"""

import contextlib
import pathlib
import sqlite3

from . import description_files
from . import errors
from . import refs

DATABASE = 'tasks.db'
# Every 8.x command leaves every namespace's database at this version.
VERSION = 25
COLUMNS = (
    'id',
    'summary',
    'tag',
    'due',
    'interval',
    'shift',
    'claim',
    'link',
    'priority',
    'size',
    'created',
    'updated',
)
# 8.x claims may hold capitals, and due dates were `next`.
SOURCES = {'claim': 'lower(claim)', 'due': 'next'}
SELECTED = ', '.join(SOURCES.get(column, column) for column in COLUMNS)


def import_(
    conn: sqlite3.Connection, folder: pathlib.Path
) -> list[pathlib.Path]:
    """
    Copy each 8.x namespace in `folder` into `conn`; the files left stale:
    their databases and description files.
    """
    databases = sorted(
        path / DATABASE
        for path in folder.iterdir()
        if refs.NAME_RE.fullmatch(path.name) and (path / DATABASE).is_file()
    )
    stale = databases.copy()
    for database in databases:
        name = database.parent.name
        try:
            _import(conn, database)
            stale.extend(
                description_files.import_(conn, name, database.parent)
            )
        except sqlite3.DatabaseError as e:
            raise errors.TaskError(f'namespace {name}: {e}') from e
    return stale


def _import(conn: sqlite3.Connection, database: pathlib.Path) -> None:
    namespace = database.parent.name
    # mode=rw, since another process may have imported and deleted it.
    uri = f'{database.absolute().as_uri()}?mode=rw'
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as source:
        (version,) = source.execute('PRAGMA user_version').fetchone()
        if version != VERSION:
            raise errors.TaskError(
                f'namespace {namespace}: database is at version {version},'
                ' run any task 8.x command once to bring it up to date'
            )
        sequence = source.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'task'"
        ).fetchone()
        tasks = source.execute(f'SELECT {SELECTED} FROM task')
        rows = [(namespace, *task) for task in tasks]

    conn.execute(
        'INSERT INTO namespace (name, seq) VALUES (:name, :seq)'
        ' ON CONFLICT (name) DO UPDATE SET seq = excluded.seq',
        {'name': namespace, 'seq': sequence[0] if sequence else 0},
    )
    columns = ', '.join(COLUMNS)
    placeholders = ', '.join('?' for _ in range(len(COLUMNS) + 1))
    conn.executemany(
        f'INSERT INTO task (namespace, {columns}) VALUES ({placeholders})',
        rows,
    )
