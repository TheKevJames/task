import contextlib
import datetime
import pathlib
import sqlite3
import zoneinfo
from collections.abc import Callable
from collections.abc import Generator

import pytest
import time_machine

from task import legacy

# Not UTC, so that rendering local times is exercised.
NOW = datetime.datetime(
    2026, 1, 15, 12, tzinfo=zoneinfo.ZoneInfo('America/Toronto')
)
# The task table as every 8.x command leaves it.
LEGACY_SCHEMA = """
CREATE TABLE task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    tag TEXT NOT NULL,
    next TEXT,
    interval TEXT,
    shift INTEGER NOT NULL DEFAULT 0,
    claim TEXT,
    link TEXT,
    priority TEXT,
    size TEXT,
    created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT
"""
LEGACY_CREATED = '2000-01-01T00:00:00Z'


@pytest.fixture(scope='function', name='folder')
def fixture_folder(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """An empty task store, used by in-process and subprocess invocations."""
    monkeypatch.setenv('TASK_FOLDER', str(tmp_path))
    return tmp_path


@pytest.fixture(scope='function')
def legacy_namespace(folder: pathlib.Path) -> Callable[..., pathlib.Path]:
    """
    Writes an 8.x namespace holding `tasks`, as (id, summary), which then
    held `deleted` more, since deleted; returns its database.
    """

    def create(
        namespace: str,
        tasks: list[tuple[int, str]],
        *,
        deleted: int = 0,
        version: int = legacy.VERSION,
    ) -> pathlib.Path:
        (folder / namespace).mkdir()
        database = folder / namespace / legacy.DATABASE
        conn = sqlite3.connect(database, isolation_level=None)
        with contextlib.closing(conn):
            conn.execute(LEGACY_SCHEMA)
            conn.execute(f'PRAGMA user_version = {version}')
            conn.executemany(
                'INSERT INTO task (id, summary, tag, next, interval, claim,'
                ' created, updated)'
                " VALUES (?, ?, 'a', '2026-01-01', '1w', 'bot', ?, ?)",
                [
                    (i, summary, LEGACY_CREATED, LEGACY_CREATED)
                    for i, summary in tasks
                ],
            )
            top = max((ident for ident, _ in tasks), default=0)
            conn.executemany(
                "INSERT INTO task (id, summary, tag) VALUES (?, 'gone', 'a')",
                [(top + i + 1,) for i in range(deleted)],
            )
            conn.execute("DELETE FROM task WHERE summary = 'gone'")
        return database

    return create


@pytest.fixture(scope='function', name='now')
def fixture_now() -> Generator[datetime.datetime]:
    """
    Stop the clock. SQLite's own clock, which stamps `created` and `updated`,
    keeps running.
    """
    with time_machine.travel(NOW, tick=False):
        yield NOW


@pytest.fixture(scope='function')
def today(now: datetime.datetime) -> datetime.date:
    return now.date()
