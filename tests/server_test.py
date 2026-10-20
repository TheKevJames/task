import asyncio
import json
import os
import pathlib
import re
import socket
import subprocess
import sysconfig
import tempfile
import threading
import tomllib
import types
from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

from task import actors
from task import cli
from task import client
from task import db
from task import ops
from task import rules
from task import schema
from task import server

if TYPE_CHECKING:
    import _typeshed

TASK = pathlib.Path(sysconfig.get_path('scripts')) / 'task'
AGENT = actors.Actor.agent('scout/daily')
RULES = """
[scout.create]
namespaces = ["bakery"]
tags = ["triage"]
require = ["link"]
"""
WRITERS = 8


@pytest.fixture(scope='function', name='address')
def fixture_address(folder: pathlib.Path) -> Generator[pathlib.Path]:
    """A server for AGENT, under RULES; its socket's path is short enough."""
    del folder
    db.add_namespace('bakery')
    guard = rules.parse(tomllib.loads(RULES))['scout'].check
    loop = asyncio.new_event_loop()
    listening = threading.Event()
    with tempfile.TemporaryDirectory(dir='/tmp') as sockets:
        path = pathlib.Path(sockets) / 'task.sock'

        async def start() -> None:
            await cli.listen(path, ops.Writer(AGENT, guard))
            listening.set()

        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        asyncio.run_coroutine_threadsafe(start(), loop).result()
        assert listening.is_set()
        yield path
        loop.call_soon_threadsafe(loop.stop)
        thread.join()


def task(address: pathlib.Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [TASK, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | {client.ADDRESS: str(address)},
    )


def run(address: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = task(address, *args)
    stdout, stderr = proc.communicate()
    print(f'{args}: {proc.returncode} {stdout!r} {stderr!r}')
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def test_agents_write_through_the_server(address: pathlib.Path) -> None:
    added = run(address, 'add', '-n', 'bakery', '--link', 'l', 'x', '--json')
    assert added.returncode == 0
    assert json.loads(added.stdout)['claim'] == AGENT.name

    refused = run(address, 'bakery:1', 'set', '--tag', 'human')
    assert (refused.returncode, refused.stderr) == (
        1,
        'error: you may not move tasks to human\n',
    )
    listed = run(address, 'list', '--json')
    assert [t['tag'] for t in json.loads(listed.stdout)] == ['triage']


@pytest.mark.parametrize(
    ('args', 'error'),
    [
        (('bakery:1', 'describe'), 'only the operator may describe'),
        (('namespace', 'add', 'x'), 'only the operator may namespace add'),
        (
            ('serve', 's', '--rules', 'r', '--agent', 'a', '--as', 'b'),
            'only the operator may serve',
        ),
        (('bakery:1', 'claim', '--force'), 'only the operator may force'),
        (('bakery:1', 'set', '--bogus'), 'No such option: --bogus'),
    ],
)
def test_agents_are_refused_what_only_the_operator_may_do(
    address: pathlib.Path, args: tuple[str, ...], error: str
) -> None:
    schema_task = schema.Task(summary='a', details=None, tag='t')
    schema_task.namespace = 'bakery'
    ops.Writer(actors.OPERATOR_ACTOR).add(schema_task)

    result = run(address, *args)
    assert result.returncode != 0
    assert error in result.stderr


def test_concurrent_requests_keep_their_own_output(
    address: pathlib.Path,
) -> None:
    procs = [
        task(address, 'add', '-n', 'bakery', '--link', f'l{i}', str(i))
        for i in range(WRITERS)
    ]
    outputs = [p.communicate()[0] for p in procs]

    print(outputs)
    assert all(p.returncode == 0 for p in procs)
    refs = []
    for i, output in enumerate(outputs):
        match = re.fullmatch(rf'added task (bakery:\d+): {i}\n', output)
        assert match, output
        refs.append(match.group(1))
    assert len(set(refs)) == WRITERS
    with db.connect() as store:
        assert {t.claim for t in db.select(store)} == {AGENT.name}


def test_help_needs_no_server(folder: pathlib.Path) -> None:
    unreachable = folder / 'missing.sock'
    assert run(unreachable, '--help').returncode == 0
    failed = run(unreachable, 'list')
    assert failed.returncode == 1
    assert failed.stderr.startswith(f'error: task server at {unreachable}: ')


def test_other_protocols_are_refused(address: pathlib.Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(address))
        conn.sendall(b'{"protocol": 0, "argv": ["list"]}\n')
        line = conn.makefile('rb').readline()

    response = server.Response.decode(line)
    assert response.code == 2
    assert response.error is not None
    assert 'protocol 0 is not this task' in response.error


def test_a_reply_is_read_after_the_server_closes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    The server answers and closes as soon as it reads a request, which may
    be before a busy client takes its next step.
    """
    closed = threading.Event()

    class Lagging(socket.socket):
        def sendall(
            self, data: '_typeshed.ReadableBuffer', flags: int = 0, /
        ) -> None:
            super().sendall(data, flags)
            assert closed.wait(10)

    with (
        tempfile.TemporaryDirectory(dir='/tmp') as sockets,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
    ):
        path = pathlib.Path(sockets) / 'task.sock'
        listener.bind(str(path))
        listener.listen()

        def answer() -> None:
            conn, _ = listener.accept()
            with conn, conn.makefile('rb') as stream:
                stream.readline()
                conn.sendall(server.Response(0, 'ok\n').encode())
            closed.set()

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        lagging = types.SimpleNamespace(**vars(socket) | {'socket': Lagging})
        monkeypatch.setattr(client, 'socket', lagging)
        code = client.forward(path, ['list'])
        thread.join()

    captured = capsys.readouterr()
    print(captured)
    assert (code, captured.out, captured.err) == (0, 'ok\n', '')
