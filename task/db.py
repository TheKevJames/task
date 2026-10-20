"""
The task store: one SQLite database, `$TASK_FOLDER/tasks.sqlite`, holding
every namespace, plus one `<namespace>/<id>.txt` file per description.

`default` always exists; other namespaces are added by `add_namespace`. Ids
are unique only within a namespace, so a task's address is its `schema.Ref`,
and moving a task to another namespace gives it a new id there.

Descriptions live beside the database as plain files so they can be edited
by hand; a file's existence is the only record that a task has one. Writers
change them while holding the write lock but before committing, so a failed
commit can strand an orphan, which the next write removes. Ids are never
reused, so an orphan cannot attach itself to a different task.

Marking a one-off task as done deletes it, with its description; recurring
tasks advance instead. `created` and `updated` belong to the store, never to
callers: every write which changes a task's row or description bumps
`updated`. Editing a description file by hand does not.
"""

import contextlib
import dataclasses
import os
import pathlib
import re
import sqlite3
import uuid
from collections.abc import Generator

from . import legacy
from . import migrations
from . import schema
from . import timestamp

BUSY_TIMEOUT = 30.0
DATABASE = 'tasks.sqlite'
DESCRIPTION_RE = re.compile(r'(\d+)\.txt')
DESCRIPTION_TMP_RE = re.compile(r'\.\d+\.txt\.tmp')
WRITABLE = (
    'namespace',
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
        raise schema.TaskError(f'namespace {namespace} already exists')


def delete_namespace(namespace: str) -> None:
    """Delete an empty namespace; its orphans and temporary files go too."""
    if namespace == schema.DEFAULT:
        raise schema.TaskError(f'namespace {schema.DEFAULT} cannot be deleted')
    with write() as store:
        _require_namespace(store, namespace)
        query = 'SELECT 1 FROM task WHERE namespace = ? LIMIT 1'
        if store.conn.execute(query, (namespace,)).fetchone():
            raise schema.TaskError(f'namespace {namespace} is not empty')
        store.conn.execute(
            'DELETE FROM namespace WHERE name = ?', (namespace,)
        )

        path = _directory(namespace)
        if not path.is_dir():
            return
        patterns = (DESCRIPTION_RE, DESCRIPTION_TMP_RE)
        for file in path.iterdir():
            if any(pattern.fullmatch(file.name) for pattern in patterns):
                file.unlink(missing_ok=True)
        try:
            path.rmdir()
        except OSError as e:
            raise schema.TaskError(f'namespace {namespace}: {e}') from e


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
        _remove_orphans(store)


def _directory(namespace: str) -> pathlib.Path:
    return folder() / namespace


def _require_namespace(store: Store, namespace: str) -> None:
    query = 'SELECT 1 FROM namespace WHERE name = ?'
    if not store.conn.execute(query, (namespace,)).fetchone():
        raise schema.TaskError(f'no such namespace: {namespace}')


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
        _migrate(conn)
    except (sqlite3.DatabaseError, schema.TaskError) as e:
        conn.close()
        raise schema.TaskError(f'{DATABASE}: {e}') from e
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
            _migrate(conn)
            with _transaction(conn):
                imported = legacy.import_(conn, path.parent)
        except schema.TaskError:
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
    for database in imported:
        database.unlink(missing_ok=True)


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
        raise schema.TaskError(
            f'database is at version {version}, newer than this task CLI'
        )
    return version


def _migrate(conn: sqlite3.Connection) -> None:
    if _version(conn) == len(migrations.MIGRATIONS):
        return
    with _transaction(conn):
        for migration in migrations.MIGRATIONS[_version(conn) :]:
            conn.execute(migration)
        conn.execute(f'PRAGMA user_version = {len(migrations.MIGRATIONS)}')


def select(
    store: Store, clauses: str = '', params: dict[str, object] | None = None
) -> list[schema.Task]:
    rows = store.conn.execute(
        f'SELECT {", ".join(COLUMNS)} FROM task{clauses}', params or {}
    )
    return [_to_task(row) for row in rows]


def get(store: Store, ref: schema.Ref) -> schema.Task:
    tasks = select(
        store,
        ' WHERE namespace = :namespace AND id = :id',
        {'namespace': ref.namespace, 'id': ref.ident},
    )
    if not tasks:
        raise schema.TaskError(f'task {ref} not found')
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
    _write_description(task)


def update(store: Store, task: schema.Task) -> None:
    """Store a changed task, bumping `updated` unless nothing changed."""
    fields = [c for c in WRITABLE if c not in {'namespace', 'id'}]
    assignments = ', '.join(f'{column} = :{column}' for column in fields)
    # SET expressions all see the row as it was before this statement.
    old = ', '.join(fields)
    new = ', '.join(f':{column}' for column in fields)
    (task.updated,) = store.conn.execute(
        f'UPDATE task SET {assignments},'
        f' updated = CASE WHEN :described OR ({old}) IS NOT ({new})'
        f' THEN {NOW} ELSE updated END'
        ' WHERE namespace = :namespace AND id = :id RETURNING updated',
        _to_row(task)
        | {'described': _read_description(task.ref) != task.description},
    ).fetchone()
    _write_description(task)


def move(store: Store, task: schema.Task, source: schema.Ref) -> None:
    """Store a task, from `source`, under a new id in its namespace."""
    assert task.namespace != source.namespace, 'task has not moved'
    task.ident = None
    insert(store, task)
    (task.created,) = store.conn.execute(
        'UPDATE task SET created = ('
        '  SELECT created FROM task'
        '   WHERE namespace = :source_namespace AND id = :source_id'
        ') WHERE namespace = :namespace AND id = :id RETURNING created',
        {
            'source_namespace': source.namespace,
            'source_id': source.ident,
            'namespace': task.namespace,
            'id': task.ident,
        },
    ).fetchone()
    delete(store, source)


def delete(store: Store, ref: schema.Ref) -> None:
    store.conn.execute(
        'DELETE FROM task WHERE namespace = :namespace AND id = :id',
        {'namespace': ref.namespace, 'id': ref.ident},
    )
    _description_path(ref).unlink(missing_ok=True)


def _to_row(task: schema.Task) -> dict[str, object]:
    details = task.details
    interval = details.interval if details else None
    return {
        'namespace': task.namespace,
        'id': task.ident,
        'summary': task.summary,
        'tag': task.tag,
        'next': details.next_.isoformat() if details else None,
        'interval': interval.raw if interval else None,
        'shift': int(bool(details and details.shift)),
        'claim': task.claim,
        'link': task.link,
        'priority': task.priority,
        'size': task.size,
    }


def _to_task(row: tuple[object, ...]) -> schema.Task:
    fields = dict(zip(COLUMNS, row, strict=True))
    namespace = fields['namespace']
    ident = fields.pop('id')
    assert isinstance(namespace, str)
    assert isinstance(ident, int)
    next_ = fields.pop('next')
    interval = fields.pop('interval')
    shift = fields.pop('shift')
    details = None
    if next_ is not None:
        details = schema.Details.model_validate(
            {
                'next': next_,
                'interval': None if interval is None else {'raw': interval},
                'shift': bool(shift),
            }
        )
    return schema.Task.model_validate(
        fields
        | {
            'ident': ident,
            'details': details,
            'description': _read_description(schema.Ref(namespace, ident)),
        }
    )


def _description_path(ref: schema.Ref) -> pathlib.Path:
    return _directory(ref.namespace) / f'{ref.ident}.txt'


def _read_description(ref: schema.Ref) -> str | None:
    try:
        text = _description_path(ref).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    return text.strip() or None


def _write_description(task: schema.Task) -> None:
    if _read_description(task.ref) == task.description:
        return

    path = _description_path(task.ref)
    if task.description is None:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        f.write(task.description)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _remove_orphans(store: Store) -> None:
    idents: dict[str, set[int]] = {
        name: set()
        for (name,) in store.conn.execute('SELECT name FROM namespace')
    }
    for namespace, ident in store.conn.execute(
        'SELECT namespace, id FROM task'
    ):
        idents[namespace].add(ident)
    for namespace, live in idents.items():
        directory = _directory(namespace)
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            match = DESCRIPTION_RE.fullmatch(path.name)
            if match and int(match.group(1)) not in live:
                path.unlink(missing_ok=True)
