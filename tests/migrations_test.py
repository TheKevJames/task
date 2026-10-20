import contextlib
import datetime
import pathlib
import sqlite3
from collections.abc import Callable

import pytest

from task import db
from task import legacy
from task import migrations
from task import schema

LegacyNamespace = Callable[..., pathlib.Path]
CREATED = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)


def test_8x_store_is_imported(
    folder: pathlib.Path, legacy_namespace: LegacyNamespace
) -> None:
    databases = [
        legacy_namespace(schema.DEFAULT, [(2, 'x'), (5, 'y')]),
        legacy_namespace('b', [(1, 'z')], deleted=2),
        legacy_namespace('empty', [], deleted=1),
    ]
    (folder / schema.DEFAULT / '5.txt').write_text('notes')

    with db.write() as conn:
        imported = db.select(conn, ' ORDER BY namespace, id')
        for namespace in (schema.DEFAULT, 'b', 'empty'):
            db.insert(
                conn,
                schema.Task(
                    summary='new', details=None, tag='a', namespace=namespace
                ),
            )
        added = db.select(conn, " WHERE summary = 'new' ORDER BY namespace")

    print(imported)
    assert [(t.ref, t.summary, t.description) for t in imported] == [
        (schema.Ref('b', 1), 'z', None),
        (schema.Ref(schema.DEFAULT, 2), 'x', None),
        (schema.Ref(schema.DEFAULT, 5), 'y', 'notes'),
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
        schema.Ref('b', 4),
        schema.Ref(schema.DEFAULT, 6),
        schema.Ref('empty', 2),
    ]
    assert db.namespaces() == ['b', schema.DEFAULT, 'empty']
    assert not any(database.exists() for database in databases)
    assert sorted(p.name for p in folder.iterdir()) == [
        'b',
        schema.DEFAULT,
        'empty',
        db.DATABASE,
    ]


def test_outdated_8x_store_is_refused(
    folder: pathlib.Path, legacy_namespace: LegacyNamespace
) -> None:
    current = legacy_namespace(schema.DEFAULT, [(1, 'x')])
    outdated = legacy_namespace('b', [], version=legacy.VERSION - 1)

    message = (
        f'^namespace b: database is at version {legacy.VERSION - 1},'
        ' run any task 8.x command once to bring it up to date$'
    )
    with pytest.raises(schema.TaskError, match=message), db.connect():
        pass

    assert current.exists()
    assert outdated.exists()
    assert sorted(p.name for p in folder.iterdir()) == ['b', schema.DEFAULT]


def test_newer_database_is_refused(folder: pathlib.Path) -> None:
    version = len(migrations.MIGRATIONS) + 1
    with contextlib.closing(sqlite3.connect(folder / db.DATABASE)) as conn:
        conn.execute(f'PRAGMA user_version = {version}')

    message = (
        f'^{db.DATABASE}: database is at version {version},'
        ' newer than this task CLI$'
    )
    with pytest.raises(schema.TaskError, match=message), db.connect():
        pass
