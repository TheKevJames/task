import json
import pathlib
import subprocess
import sys
import sysconfig
from collections.abc import Callable

import pytest

WRITERS = 8
# The installed console script, so these also cover the real entry point.
TASK = pathlib.Path(sysconfig.get_path('scripts')) / 'task'


def task(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [TASK, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def as_agent(name: str, code: str) -> subprocess.Popen[str]:
    """Run `code` with `writer` as agent `name`, as its own process."""
    setup = (
        'from task import ops, query, schema;'
        f' writer = ops.Writer(schema.Actor.agent({name!r}))'
    )
    return subprocess.Popen(
        [sys.executable, '-c', f'{setup}; {code}'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def run(*args: str) -> subprocess.CompletedProcess[str]:
    proc = task(*args)
    stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def listed() -> list[dict[str, object]]:
    result = run('list', '--json')
    assert result.returncode == 0, result.stderr
    tasks: list[dict[str, object]] = json.loads(result.stdout)
    return tasks


def refs(tasks: list[dict[str, object]]) -> list[str]:
    return [
        str(t['id'])
        if t['namespace'] == 'default'
        else f'{t["namespace"]}:{t["id"]}'
        for t in tasks
    ]


@pytest.mark.usefixtures('folder')
def test_entry_point_reports_errors_without_traceback() -> None:
    assert run('add', 'a').returncode == 0
    assert run('list').stdout == 'triage\t1: a\n'

    result = run('2', 'done')
    assert result.returncode == 1
    assert result.stderr == 'error: task 2 not found\n'


@pytest.mark.usefixtures('folder')
def test_concurrent_writers_lose_nothing() -> None:
    assert run('namespace', 'add', 'b').returncode == 0
    procs = [
        task('add', '-n', ('default', 'b')[i % 2], f'task {i}')
        for i in range(WRITERS)
    ]
    assert all(p.wait() == 0 for p in procs)

    tasks = listed()
    assert sorted(str(t['summary']) for t in tasks) == sorted(
        f'task {i}' for i in range(WRITERS)
    )
    assert len(set(refs(tasks))) == WRITERS


@pytest.mark.usefixtures('folder')
def test_concurrent_moves_lose_nothing() -> None:
    assert run('namespace', 'add', 'b').returncode == 0
    for i in range(WRITERS):
        namespace = ('default', 'b')[i % 2]
        assert run('add', '-n', namespace, str(i)).returncode == 0
    added = listed()
    procs = [
        task(ref, 'set', '-n', 'b' if ref.isdigit() else 'default')
        for ref in refs(added)
    ]
    assert all(p.wait() == 0 for p in procs)

    tasks = listed()
    assert sorted(int(str(t['summary'])) for t in tasks) == list(
        range(WRITERS)
    )
    assert all(
        t['namespace'] == ('b', 'default')[int(str(t['summary'])) % 2]
        for t in tasks
    )


def test_concurrent_first_writers_import_8x_once(
    folder: pathlib.Path, legacy_namespace: Callable[..., pathlib.Path]
) -> None:
    legacy_namespace('default', [(1, 'old')])
    legacy_namespace('b', [(3, 'old')])
    procs = [
        task('add', '-n', ('default', 'b')[i % 2], f'task {i}')
        for i in range(WRITERS)
    ]
    assert all(p.wait() == 0 for p in procs)

    tasks = listed()
    assert sorted(str(t['summary']) for t in tasks) == sorted(
        ['old', 'old', *(f'task {i}' for i in range(WRITERS))]
    )
    assert len(set(refs(tasks))) == WRITERS + 2
    assert sorted(p.name for p in folder.iterdir()) == [
        'b',
        'default',
        'tasks.sqlite',
    ]
    assert not list(folder.glob('*/tasks.db'))


@pytest.mark.usefixtures('folder')
def test_concurrent_claims_have_one_winner() -> None:
    assert run('add', 'contested').returncode == 0

    procs = [
        as_agent(f'bot/{i}', "writer.claim(schema.Ref.parse('1'))")
        for i in range(WRITERS)
    ]
    winners = [i for i, p in enumerate(procs) if p.wait() == 0]

    assert len(winners) == 1
    assert listed()[0]['claim'] == f'bot/{winners[0]}'


@pytest.mark.usefixtures('folder')
def test_concurrent_agents_take_distinct_next_tasks() -> None:
    assert run('namespace', 'add', 'b').returncode == 0
    for i in range(WRITERS):
        namespace = ('default', 'b')[i % 2]
        assert run('add', '-n', namespace, str(i)).returncode == 0

    code = "print(writer.next('', -1, query.SortOrder.ident).ref)"
    procs = [as_agent(f'bot/{i}', code) for i in range(WRITERS)]
    taken = [p.communicate()[0].strip() for p in procs]

    assert sorted(taken) == sorted(refs(listed()))
    claims = {f'{t["namespace"]}:{t["id"]}': t['claim'] for t in listed()}
    print(f'taken {taken}, claims {claims}')
    assert all(
        claims[ref if ':' in ref else f'default:{ref}'] == f'bot/{i}'
        for i, ref in enumerate(taken)
    )
