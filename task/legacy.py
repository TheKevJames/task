"""
Importing a store written by task 8.x, which kept each namespace in its own
database, `$TASK_FOLDER/<namespace>/tasks.db`, beside its descriptions.
"""

import contextlib
import pathlib
import sqlite3

from . import schema

DATABASE = 'tasks.db'
# Every 8.x command leaves every namespace's database at this version.
VERSION = 25
COLUMNS = (
    'id',
    'summary',
    'tag',
    'next',
    'interval',
    'shift',
    'claim',
    'link',
    'priority',
    'size',
    'created',
    'updated',
)


def import_(
    conn: sqlite3.Connection, folder: pathlib.Path
) -> list[pathlib.Path]:
    """Copy each 8.x namespace in `folder` into `conn`; their databases."""
    databases = sorted(
        path / DATABASE
        for path in folder.iterdir()
        if schema.NAME_RE.fullmatch(path.name) and (path / DATABASE).is_file()
    )
    for database in databases:
        try:
            _import(conn, database)
        except sqlite3.DatabaseError as e:
            name = database.parent.name
            raise schema.TaskError(f'namespace {name}: {e}') from e
    return databases


def _import(conn: sqlite3.Connection, database: pathlib.Path) -> None:
    namespace = database.parent.name
    # mode=rw, since another process may have imported and deleted it.
    uri = f'{database.absolute().as_uri()}?mode=rw'
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as source:
        (version,) = source.execute('PRAGMA user_version').fetchone()
        if version != VERSION:
            raise schema.TaskError(
                f'namespace {namespace}: database is at version {version},'
                ' run any task 8.x command once to bring it up to date'
            )
        sequence = source.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'task'"
        ).fetchone()
        tasks = source.execute(f'SELECT {", ".join(COLUMNS)} FROM task')
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
