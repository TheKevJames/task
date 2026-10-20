import contextlib
import datetime
import pathlib
import re
import sqlite3
from collections.abc import Callable

import pytest

from task import conversations
from task import db
from task import errors
from task import legacy
from task import migrations
from task import refs
from task import schedule
from task import schema

LegacyNamespace = Callable[..., pathlib.Path]
CREATED = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
# How many migrations task 9.0.0, 9.3.0, 9.4.0, and 10.0.0 shipped.
SHIPPED_9_0_0 = 11
SHIPPED_9_3_0 = 29
SHIPPED_9_4_0 = 31
SHIPPED_10_0_0 = 32


def test_8x_store_is_imported(
    folder: pathlib.Path, legacy_namespace: LegacyNamespace
) -> None:
    databases = [
        legacy_namespace(refs.DEFAULT, [(2, 'x'), (5, 'y')]),
        legacy_namespace('b', [(1, 'z')], deleted=2),
        legacy_namespace('empty', [], deleted=1),
    ]
    (folder / refs.DEFAULT / '5.txt').write_text('\nnotes\n')
    (folder / 'b' / '2.txt').write_text('orphan')
    (folder / 'b' / '.1.txt.tmp').write_text('half written')

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
    assert [p.name for p in folder.iterdir()] == [db.DATABASE]


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
    # SQLite quotes the name of a table it has renamed.
    rebuild = (
        re.sub(r'^CREATE TABLE "?task"?', 'CREATE TABLE task_new', sql),
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


def schema_objects(store: db.Store) -> list[tuple[str, str]]:
    objects: list[tuple[str, str]] = store.conn.execute(
        'SELECT type, name FROM sqlite_schema ORDER BY type, name'
    ).fetchall()
    return objects


@pytest.mark.usefixtures('folder')
def test_9_0_0_actor_names_are_lowercased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipped = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped[:SHIPPED_9_0_0])
    with db.write() as store:
        for sql in (
            'INSERT INTO task (namespace, id, summary, tag, claim)'
            " VALUES ('default', 1, 'a', 'a', 'Triage/1')",
            'INSERT INTO task (namespace, id, summary, tag)'
            " VALUES ('default', 2, 'b', 'a')",
            'INSERT INTO entry (namespace, task, kind, actor, body)'
            " VALUES ('default', 1, 'comment', 'Triage/1', 'c')",
            'INSERT INTO blocker'
            ' (namespace, task, kind, target_namespace, target, actor)'
            " VALUES ('default', 1, 'done', 'default', 2, 'Triage/1')",
        ):
            store.conn.execute(sql)
        before = schema_objects(store)

    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped)
    with db.connect() as store:
        names = store.conn.execute(
            'SELECT claim FROM task WHERE id = 1'
            ' UNION ALL SELECT actor FROM entry'
            ' UNION ALL SELECT actor FROM blocker'
        ).fetchall()
        after = schema_objects(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE task SET claim = 'A' WHERE id = 2")

    print(before, after, sep='\n')
    assert names == [('triage/1',)] * 3
    assert after == before


def test_9_3_0_description_files_move_into_the_database(
    folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped[:SHIPPED_9_3_0])
    db.add_namespace('b')
    db.add_namespace('c')
    with db.write() as store:
        store.conn.execute(
            'INSERT INTO task (namespace, id, summary, tag, created, updated)'
            " VALUES ('default', 1, 'a', 'a', :t, :t),"
            " ('b', 1, 'b', 'a', :t, :t)",
            {'t': '2000-01-01T00:00:00Z'},
        )
    files = {
        'default/1.txt': '\n described \n',
        'default/.1.txt.tmp': 'half written',
        'default/9.txt': 'orphan',
        'b/1.txt': ' \n',
        'c/2.txt': 'orphan',
        'c/unexpected': 'kept',
    }
    for name, text in files.items():
        (folder / name).parent.mkdir(exist_ok=True)
        (folder / name).write_text(text)

    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped)
    with db.connect() as store:
        tasks = db.select(store, ' ORDER BY namespace, id')

    print(tasks)
    assert [(t.ref, t.description) for t in tasks] == [
        (refs.Ref('b', 1), None),
        (refs.Ref(refs.DEFAULT, 1), 'described'),
    ]
    assert all(t.updated == CREATED for t in tasks)
    remaining = sorted(str(p.relative_to(folder)) for p in folder.rglob('*'))
    assert remaining == ['c', 'c/unexpected', db.DATABASE]


@pytest.mark.usefixtures('folder')
def test_9_4_0_next_is_renamed_due(monkeypatch: pytest.MonkeyPatch) -> None:
    shipped = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped[:SHIPPED_9_4_0])
    with db.write() as store:
        store.conn.execute(
            'INSERT INTO task (namespace, id, summary, tag, next, interval,'
            " shift) VALUES ('default', 1, 'a', 'a', '2026-01-31', '1m', 1),"
            " ('default', 2, 'b', 'a', NULL, NULL, 0)"
        )
        before = schema_objects(store)

    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped)
    with db.connect() as store:
        tasks = db.select(store, ' ORDER BY id')
        after = schema_objects(store)
        # The CHECKs on the column now hold it by its new name.
        for sql in (
            "UPDATE task SET due = '2026-02-30' WHERE id = 1",
            "UPDATE task SET interval = '1d' WHERE id = 2",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                store.conn.execute(sql)

    print(tasks)
    assert [t.details for t in tasks] == [
        schedule.Details.model_validate(
            {'due': '2026-01-31', 'interval': {'raw': '1m'}, 'shift': True}
        ),
        None,
    ]
    assert after == before


@pytest.mark.usefixtures('folder')
def test_10_0_0_tasks_are_not_delayed(monkeypatch: pytest.MonkeyPatch) -> None:
    shipped = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped[:SHIPPED_10_0_0])
    with db.write() as store:
        store.conn.execute(
            'INSERT INTO task (namespace, id, summary, tag, due, interval,'
            " shift) VALUES ('default', 1, 'a', 'a', '2026-01-12', '1w', 0),"
            " ('default', 2, 'b', 'a', '2026-01-12', '1w', 1)"
        )

    monkeypatch.setattr(migrations, 'MIGRATIONS', shipped)
    with db.connect() as store:
        tasks = db.select(store, ' ORDER BY id')
        store.conn.execute(
            "UPDATE task SET delayed_from = '2026-01-05' WHERE id = 1"
        )
        # Only a fixed recurrence is ever delayed from a date.
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "UPDATE task SET delayed_from = '2026-01-05' WHERE id = 2"
            )

    print(tasks)
    assert [t.summary for t in tasks] == ['a', 'b']
    assert all(t.details and t.details.delayed_from is None for t in tasks)


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
