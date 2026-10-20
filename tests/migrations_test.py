import contextlib
import datetime
import pathlib
import sqlite3
from collections.abc import Callable

import pytest

from task import conversations
from task import db
from task import errors
from task import legacy
from task import migrations
from task import refs
from task import schema

LegacyNamespace = Callable[..., pathlib.Path]
CREATED = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)


def test_8x_store_is_imported(
    folder: pathlib.Path, legacy_namespace: LegacyNamespace
) -> None:
    databases = [
        legacy_namespace(refs.DEFAULT, [(2, 'x'), (5, 'y')]),
        legacy_namespace('b', [(1, 'z')], deleted=2),
        legacy_namespace('empty', [], deleted=1),
    ]
    (folder / refs.DEFAULT / '5.txt').write_text('notes')

    with db.write() as conn:
        imported = db.select(conn, ' ORDER BY namespace, id')
        for namespace in (refs.DEFAULT, 'b', 'empty'):
            db.insert(
                conn,
                schema.Task(
                    summary='new', details=None, tag='a', namespace=namespace
                ),
            )
        added = db.select(conn, " WHERE summary = 'new' ORDER BY namespace")

    print(imported)
    assert [(t.ref, t.summary, t.description) for t in imported] == [
        (refs.Ref('b', 1), 'z', None),
        (refs.Ref(refs.DEFAULT, 2), 'x', None),
        (refs.Ref(refs.DEFAULT, 5), 'y', 'notes'),
    ]
    assert all(
        t.claim == 'bot'
        and t.details
        and t.details.interval
        and t.details.interval.raw == '1w'
        and t.created == t.updated == CREATED
        for t in imported
    )
    # Ids carry on from each namespace's highest ever, not its highest left.
    assert [t.ref for t in added] == [
        refs.Ref('b', 4),
        refs.Ref(refs.DEFAULT, 6),
        refs.Ref('empty', 2),
    ]
    assert db.namespaces() == ['b', refs.DEFAULT, 'empty']
    assert not any(database.exists() for database in databases)
    assert sorted(p.name for p in folder.iterdir()) == [
        'b',
        refs.DEFAULT,
        'empty',
        db.DATABASE,
    ]


def test_outdated_8x_store_is_refused(
    folder: pathlib.Path, legacy_namespace: LegacyNamespace
) -> None:
    current = legacy_namespace(refs.DEFAULT, [(1, 'x')])
    outdated = legacy_namespace('b', [], version=legacy.VERSION - 1)

    message = (
        f'^namespace b: database is at version {legacy.VERSION - 1},'
        ' run any task 8.x command once to bring it up to date$'
    )
    with pytest.raises(errors.TaskError, match=message), db.connect():
        pass

    assert current.exists()
    assert outdated.exists()
    assert sorted(p.name for p in folder.iterdir()) == ['b', refs.DEFAULT]


def said_on(conversation: conversations.Conversation) -> schema.Task:
    with db.write() as conn:
        task = schema.Task(
            summary='a', details=None, tag='a', conversation=conversation
        )
        db.insert(conn, task)
    return task


CONVERSATION = conversations.Conversation(
    entries=(
        conversations.Comment(actor='a', body='c'),
        conversations.Question(
            actor='a',
            body='q',
            answer=conversations.Entry(actor='operator', body='y'),
        ),
    )
)


@pytest.mark.usefixtures('folder')
def test_rebuilding_a_table_keeps_what_refers_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = said_on(CONVERSATION)
    with db.connect() as conn:
        (sql,) = conn.conn.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'task'"
        ).fetchone()
    rebuild = (
        sql.replace('CREATE TABLE task', 'CREATE TABLE task_new', 1),
        'INSERT INTO task_new SELECT * FROM task',
        'DROP TABLE task',
        'ALTER TABLE task_new RENAME TO task',
    )
    monkeypatch.setattr(
        migrations, 'MIGRATIONS', (*migrations.MIGRATIONS, *rebuild)
    )

    with db.connect() as conn:
        assert db.get(conn, task.ref) == task


@pytest.mark.usefixtures('folder')
def test_migrations_which_break_foreign_keys_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = said_on(CONVERSATION)
    shipped = migrations.MIGRATIONS
    monkeypatch.setattr(
        migrations, 'MIGRATIONS', (*shipped, 'DELETE FROM task')
    )

    message = f'^{db.DATABASE}: migrating left entry row 1 without its task$'
    with pytest.raises(errors.TaskError, match=message), db.connect():
        pass

    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped)
    with db.connect() as conn:
        assert db.get(conn, task.ref) == task


def test_newer_database_is_refused(folder: pathlib.Path) -> None:
    version = len(migrations.MIGRATIONS) + 1
    with contextlib.closing(sqlite3.connect(folder / db.DATABASE)) as conn:
        conn.execute(f'PRAGMA user_version = {version}')

    message = (
        f'^{db.DATABASE}: database is at version {version},'
        ' newer than this task CLI$'
    )
    with pytest.raises(errors.TaskError, match=message), db.connect():
        pass
