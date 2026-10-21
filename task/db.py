"""
The task store: one SQLite database, `$TASK_FOLDER/tasks.sqlite`, holding
every namespace.

`default` always exists; other namespaces are added by `add_namespace`. Ids
are unique only within a namespace, so a task's address is its `refs.Ref`,
and moving a task to another namespace gives it a new id there.

Marking a one-off task as done deletes it; recurring tasks advance instead.
`created` and `updated` belong to the store, never to callers: every write
which changes a task's row or conversation bumps `updated`.
"""

import contextlib
import dataclasses
import os
import pathlib
import sqlite3
import uuid
from collections.abc import Generator

from . import conversations
from . import description_files
from . import errors
from . import legacy
from . import migrations
from . import refs
from . import relations
from . import schedule
from . import schema
from . import timestamp

BUSY_TIMEOUT = 30.0
DATABASE = 'tasks.sqlite'
WRITABLE = (
    'namespace',
    'id',
    'summary',
    'description',
    'tag',
    'due',
    'interval',
    'shift',
    'claim',
    'link',
    'priority',
    'size',
)
COLUMNS = (*WRITABLE, 'created', 'updated')
NEXT_ID = '(SELECT seq + 1 FROM namespace WHERE name = :namespace)'
NOW = f"strftime('{timestamp.FORMAT}', 'now')"


@dataclasses.dataclass(frozen=True)
class Store:
    """A connection to the task store."""

    conn: sqlite3.Connection


def folder() -> pathlib.Path:
    return pathlib.Path(os.environ['TASK_FOLDER'])


def namespaces() -> list[str]:
    with connect() as store:
        rows = store.conn.execute('SELECT name FROM namespace ORDER BY name')
        return [name for (name,) in rows]


def add_namespace(namespace: str) -> None:
    with write() as store:
        added = store.conn.execute(
            'INSERT INTO namespace (name) VALUES (?)'
            ' ON CONFLICT DO NOTHING RETURNING name',
            (namespace,),
        ).fetchone()
    if added is None:
        raise errors.TaskError(f'namespace {namespace} already exists')


def delete_namespace(namespace: str) -> None:
    """Delete an empty namespace."""
    if namespace == refs.DEFAULT:
        raise errors.TaskError(f'namespace {refs.DEFAULT} cannot be deleted')
    with write() as store:
        _require_namespace(store, namespace)
        query = 'SELECT 1 FROM task WHERE namespace = ? LIMIT 1'
        if store.conn.execute(query, (namespace,)).fetchone():
            raise errors.TaskError(f'namespace {namespace} is not empty')
        store.conn.execute(
            'DELETE FROM namespace WHERE name = ?', (namespace,)
        )


@contextlib.contextmanager
def connect() -> Generator[Store]:
    """The store, outside any transaction: statements stand alone."""
    conn = _open()
    try:
        yield Store(conn)
    finally:
        conn.close()


@contextlib.contextmanager
def write() -> Generator[Store]:
    """The store under its write lock, committed if the block succeeds."""
    with connect() as store, _transaction(store.conn):
        yield store


def _require_namespace(store: Store, namespace: str) -> None:
    query = 'SELECT 1 FROM namespace WHERE name = ?'
    if not store.conn.execute(query, (namespace,)).fetchone():
        raise errors.TaskError(f'no such namespace: {namespace}')


def _connect(database: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        database, timeout=BUSY_TIMEOUT, isolation_level=None
    )
    conn.create_function('fold', 1, str.lower, deterministic=True)
    conn.execute('PRAGMA foreign_keys = ON')
    # A rollback journal keeps the database a single self-contained file
    # between commits, which is what Syncthing backs up. WAL would not.
    conn.execute('PRAGMA journal_mode = DELETE')
    return conn


def _open() -> sqlite3.Connection:
    """A connection to the store's database, created and migrated."""
    path = folder() / DATABASE
    if not path.is_file():
        _create(path)
    conn = _connect(path)
    try:
        _migrate(conn, path.parent)
    except (sqlite3.DatabaseError, errors.TaskError) as e:
        conn.close()
        raise errors.TaskError(f'{DATABASE}: {e}') from e
    return conn


def _create(path: pathlib.Path) -> None:
    """
    Build the database aside, importing any 8.x store, then link it into
    place: of several processes creating it at once, only one wins the link,
    and only the winner removes the 8.x databases it imported. The winner
    links before it removes them, so a loser which fails to import one has
    already lost.
    """
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        conn = _connect(tmp)
        try:
            _migrate(conn, path.parent)
            with _transaction(conn):
                imported = legacy.import_(conn, path.parent)
        except errors.TaskError:
            if path.is_file():
                return
            raise
        finally:
            conn.close()
        try:
            path.hardlink_to(tmp)
        except FileExistsError:
            return
    finally:
        tmp.unlink(missing_ok=True)
    description_files.remove(imported)


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection) -> Generator[None]:
    # IMMEDIATE takes the write lock up front, so read-check-write sequences
    # (eg. claiming a task) cannot interleave with another writer.
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


def _version(conn: sqlite3.Connection) -> int:
    version: int = conn.execute('PRAGMA user_version').fetchone()[0]
    if version > len(migrations.MIGRATIONS):
        raise errors.TaskError(
            f'database is at version {version}, newer than this task CLI'
        )
    return version


def _migrate(conn: sqlite3.Connection, directory: pathlib.Path) -> None:
    if _version(conn) == len(migrations.MIGRATIONS):
        return
    stale: list[pathlib.Path] = []
    # SQLite's procedure for changing a table's schema; foreign keys can only
    # be turned off outside a transaction.
    conn.execute('PRAGMA foreign_keys = OFF')
    try:
        with _transaction(conn):
            for migration in migrations.MIGRATIONS[_version(conn) :]:
                if isinstance(migration, str):
                    conn.execute(migration)
                else:
                    stale.extend(migration(conn, directory))
            broken = conn.execute('PRAGMA foreign_key_check').fetchone()
            if broken:
                table, rowid, parent, _ = broken
                raise errors.TaskError(
                    f'migrating left {table} row {rowid} without its {parent}'
                )
            version = len(migrations.MIGRATIONS)
            conn.execute(f'PRAGMA user_version = {version}')
    finally:
        conn.execute('PRAGMA foreign_keys = ON')
    description_files.remove(stale)


def select(
    store: Store, clauses: str = '', params: dict[str, object] | None = None
) -> list[schema.Task]:
    params = params or {}
    rows = store.conn.execute(
        f'SELECT {", ".join(COLUMNS)} FROM task{clauses}', params
    ).fetchall()
    said = conversations.load(store.conn, clauses, params)
    related = relations.load(store.conn, clauses, params)
    return [_to_task(row, said, related) for row in rows]


def get(store: Store, ref: refs.Ref) -> schema.Task:
    tasks = select(
        store,
        ' WHERE namespace = :namespace AND id = :id',
        {'namespace': ref.namespace, 'id': ref.ident},
    )
    if not tasks:
        raise errors.TaskError(f'task {ref} not found')
    return tasks[0]


def insert(store: Store, task: schema.Task) -> None:
    """Store a new task, assigning its id."""
    _require_namespace(store, task.namespace)
    names = ', '.join(WRITABLE)
    values = ', '.join(
        NEXT_ID if column == 'id' else f':{column}' for column in WRITABLE
    )
    task.ident, task.created, task.updated = store.conn.execute(
        f'INSERT INTO task ({names}) VALUES ({values})'
        ' RETURNING id, created, updated',
        _to_row(task),
    ).fetchone()
    _store_conversation(store, task)
    _store_relations(store, task)


def update(store: Store, task: schema.Task) -> None:
    """Store a changed task, bumping `updated` unless nothing changed."""
    related = _store_relations(store, task)
    fields = [c for c in WRITABLE if c not in {'namespace', 'id'}]
    assignments = ', '.join(f'{column} = :{column}' for column in fields)
    # SET expressions all see the row as it was before this statement.
    old = ', '.join(fields)
    new = ', '.join(f':{column}' for column in fields)
    (task.updated,) = store.conn.execute(
        f'UPDATE task SET {assignments},'
        ' updated = CASE WHEN :conversed OR :related'
        f' OR ({old}) IS NOT ({new}) THEN {NOW} ELSE updated END'
        ' WHERE namespace = :namespace AND id = :id RETURNING updated',
        _to_row(task)
        | {
            'conversed': conversations.unstamped(task.conversation),
            'related': related,
        },
    ).fetchone()
    _store_conversation(store, task)


def move(store: Store, task: schema.Task, source: refs.Ref) -> None:
    """
    Store a task, from `source`, under a new id in its namespace. Its key
    changes in place, so whatever refers to it by key follows it there.
    """
    assert task.namespace != source.namespace, 'task has not moved'
    _require_namespace(store, task.namespace)
    (task.ident,) = store.conn.execute(
        f'UPDATE task SET namespace = :namespace, id = {NEXT_ID},'
        f' updated = {NOW}'
        ' WHERE namespace = :source_namespace AND id = :source_id'
        ' RETURNING id',
        {
            'namespace': task.namespace,
            'source_namespace': source.namespace,
            'source_id': source.ident,
        },
    ).fetchone()
    update(store, task)


def _store_conversation(store: Store, task: schema.Task) -> None:
    key = (task.ref.namespace, task.ref.ident)
    task.conversation = conversations.store(store.conn, key, task.conversation)


def _store_relations(store: Store, task: schema.Task) -> bool:
    """Store a task's relations; whether any changed."""
    key = (task.ref.namespace, task.ref.ident)
    task.blocked_by, task.duplicate_of, changed = relations.store(
        store.conn, key, task.blocked_by, task.duplicate_of
    )
    return changed


def delete(store: Store, ref: refs.Ref) -> None:
    store.conn.execute(
        'DELETE FROM task WHERE namespace = :namespace AND id = :id',
        {'namespace': ref.namespace, 'id': ref.ident},
    )


def _to_row(task: schema.Task) -> dict[str, object]:
    details = task.details
    interval = details.interval if details else None
    return {
        'namespace': task.namespace,
        'id': task.ident,
        'summary': task.summary,
        'description': task.description,
        'tag': task.tag,
        'due': details.due.isoformat() if details else None,
        'interval': interval.raw if interval else None,
        'shift': int(bool(details and details.shift)),
        'claim': task.claim,
        'link': task.link,
        'priority': task.priority,
        'size': task.size,
    }


def _to_task(
    row: tuple[object, ...],
    said: dict[conversations.Key, conversations.Conversation],
    related: dict[relations.Key, relations.Relations],
) -> schema.Task:
    fields = dict(zip(COLUMNS, row, strict=True))
    namespace = fields['namespace']
    ident = fields.pop('id')
    assert isinstance(namespace, str)
    assert isinstance(ident, int)
    due = fields.pop('due')
    interval = fields.pop('interval')
    shift = fields.pop('shift')
    details = None
    if due is not None:
        details = schedule.Details.model_validate(
            {
                'due': due,
                'interval': None if interval is None else {'raw': interval},
                'shift': bool(shift),
            }
        )
    relation = related.get((namespace, ident), relations.Relations())
    return schema.Task.model_validate(
        fields
        | {
            'ident': ident,
            'details': details,
            'conversation': said.get(
                (namespace, ident), conversations.Conversation()
            ),
            'blocked_by': relation.blocked_by,
            'duplicate_of': relation.duplicate_of,
            'blocks': relation.blocks,
            'duplicates': relation.duplicates,
        }
    )
