"""A task waits on what it relates to, and the store acts when that happens."""

import contextlib
import json
import tempfile
import tomllib
from collections.abc import Generator
from typing import Any

import hypothesis
import pytest
from hypothesis import strategies as st
from typer import testing

from task import actors
from task import cli
from task import errors
from task import ops
from task import refs
from task import relations
from task import rules
from task import schema

RUNNER = testing.CliRunner()
AGENT = actors.Actor.agent('triage/1')
RULES = """
[triage.claim]
namespaces = ["bakery"]
tags = ["triage"]

[triage.change]
fields = ["blocked_by", "duplicate_of"]
tags = ["triage"]
done = true
"""


def run(*args: str) -> str:
    """The output of a successful invocation, as the operator."""
    result = RUNNER.invoke(cli.app, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.stdout


def refused(*args: str) -> str:
    with pytest.raises(errors.TaskError) as excinfo:
        RUNNER.invoke(cli.app, args, catch_exceptions=False)
    return str(excinfo.value)


def show(ref: str) -> dict[str, Any]:
    task: dict[str, Any] = json.loads(run(ref, '--json'))
    return task


def listed(filter_: str = '') -> list[str]:
    tasks = json.loads(run('list', '--json', '-f', filter_))
    return [str(refs.Ref(t['namespace'], t['id'])) for t in tasks]


def comments(ref: str) -> list[tuple[str, str]]:
    return [(c['actor'], c['body']) for c in show(ref)['comments']]


def agent() -> ops.Writer:
    return ops.Writer(AGENT, rules.parse(tomllib.loads(RULES))['triage'].check)


def blocking(*values: str) -> ops.Edit:
    def edit(task: schema.Task) -> schema.Task:
        task.relate(AGENT, blocked_by=values)
        return task

    return edit


NAMESPACES = st.sampled_from(['', 'bakery:', 'a.b-c_d:'])
IDS = st.integers(min_value=1, max_value=999)
URLS = st.sampled_from(['https://github.com/a/b/issues/4', 'ftp://x'])
CONDITIONS = st.text('ab :,', min_size=1, max_size=8).map(str.strip)


@hypothesis.given(
    st.one_of(
        st.builds(lambda ns, i: f'{ns}{i}', NAMESPACES, IDS),
        st.builds(lambda ns, i: f'{ns}{i}:decided', NAMESPACES, IDS),
        st.builds(lambda u, c: f'{u}: {c}', URLS, CONDITIONS.filter(bool)),
    )
)
def test_blockers_read_back_as_written(value: str) -> None:
    blocker = relations.parse_blocker(AGENT, value)
    print(blocker)
    assert str(blocker) == value


@pytest.mark.parametrize(
    'value', ['', 'bakery', '1:done', 'https://x', 'x: closed', 'bakery:1: y']
)
def test_malformed_blockers_are_refused(value: str) -> None:
    with pytest.raises(errors.TaskError, match='^invalid blocker'):
        relations.parse_blocker(AGENT, value)


@contextlib.contextmanager
def task_folder(tasks: int) -> Generator[None]:
    """A store holding `tasks` tasks, per hypothesis example."""
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        for i in range(tasks):
            run('add', str(i))
        yield


def reaches(edges: set[tuple[int, int]], start: int, goal: int) -> bool:
    seen, frontier = {start}, [start]
    while frontier:
        node = frontier.pop()
        for source, target in edges:
            if source == node and target not in seen:
                seen.add(target)
                frontier.append(target)
    return goal in seen


@hypothesis.settings(max_examples=50, deadline=None)
@hypothesis.given(
    st.lists(
        st.tuples(
            st.integers(1, 4),
            st.integers(1, 4),
            st.sampled_from(['', ':decided']),
        ),
        max_size=10,
    )
)
def test_blockers_never_make_a_cycle(
    attempts: list[tuple[int, int, str]],
) -> None:
    edges: set[tuple[int, int]] = set()
    with task_folder(4):
        for source, target, until in attempts:
            print(f'{source} --blocked-by {target}{until}')
            if (source, target) in edges:
                error = f'already waiting on {target}'
            elif reaches(edges, target, source):
                error = f'waiting on {target} would make a cycle'
            else:
                run(str(source), 'set', '--blocked-by', f'{target}{until}')
                edges.add((source, target))
                continue
            args = (str(source), 'set', '--blocked-by', f'{target}{until}')
            assert refused(*args) == error
        stored = {
            (int(ref), int(blocker['task']))
            for ref in listed('blocked=any')
            for blocker in show(ref)['blocked_by']
        }
    assert stored == edges


@pytest.mark.usefixtures('folder')
def test_blocked_tasks_are_hidden_until_they_clear() -> None:
    run('add', 'target', '--tag', 'build')
    run('add', 'waiting', '--tag', 'build', '--blocked-by', '1')
    run('add', 'ready', '--tag', 'build')

    assert listed() == ['1', '3']
    assert listed('blocked=yes') == ['2']
    assert listed('blocked=any') == ['1', '2', '3']
    assert 'waiting' not in run('next', '-f', 'summary=waiting')
    assert '2: waiting \N{MULTIPLICATION SIGN}' in run(
        'list', '-f', 'blocked=yes'
    )
    assert show('1')['blocks'][0]['task'] == '2'

    run('1', 'done')

    waiting = show('2')
    assert (waiting['tag'], waiting['blocked_by']) == ('triage', [])
    assert comments('2') == [('operator', 'Cleared: 1')]
    assert listed() == ['2', '3']


@pytest.mark.usefixtures('folder')
def test_a_task_clears_once_all_it_waits_on_has() -> None:
    run('add', 'a')
    run('add', 'b')
    run('add', 'waiting', '--tag', 'build', '--blocked-by', '1')
    run('3', 'set', '--blocked-by', 'https://x.io/1: merged')

    run('1', 'done')
    assert show('3')['tag'] == 'build'
    run('3', 'unset', '--blocked-by', 'https://x.io/1')

    assert show('3')['tag'] == 'triage'
    assert comments('3') == [
        ('operator', 'Cleared: 1'),
        ('operator', 'Cleared: https://x.io/1: merged'),
    ]


@pytest.mark.usefixtures('folder')
def test_removing_a_blocker_while_sorting_keeps_the_tag() -> None:
    run('add', 'a')
    run('add', 'waiting', '--blocked-by', '1')

    def sorted_(task: schema.Task) -> schema.Task:
        task.relate(actors.OPERATOR_ACTOR, cleared=['1'])
        task.update(tag='build')
        return task

    ops.Writer(actors.OPERATOR_ACTOR).change(
        ops.Verb.set, refs.Ref.parse('2'), sorted_
    )
    assert (show('2')['tag'], comments('2')) == (
        'build',
        [('operator', 'Cleared: 1')],
    )


@pytest.mark.usefixtures('folder')
def test_decided_means_sorted_unblocked_and_unasked() -> None:
    run('add', 'canonical')
    run('add', 'other')
    run('add', 'follower', '--tag', 'build', '--blocked-by', '1:decided')
    run('1', 'set', '--blocked-by', '2', '--question', 'which?')

    for step in (
        ('set', '--tag', 'human'),
        # Unblocked, so back in triage.
        ('unset', '--blocked-by', '2'),
        ('set', '--answer', 'Q1: that one'),
    ):
        run('1', *step)
        assert show('3')['blocked_by'][0]['task'] == '1'
    run('1', 'set', '--tag', 'human')

    assert (show('3')['tag'], comments('3')) == (
        'triage',
        [('operator', 'Cleared: 1:decided')],
    )
    assert refused('2', 'set', '--blocked-by', '1:decided') == (
        '1 is already decided'
    )


@pytest.mark.usefixtures('folder')
def test_recurring_targets_clear_with_their_occurrence() -> None:
    run('add', 'weekly', '--next', '2026-01-01', '--interval', '1w')
    run('add', 'waiting', '--blocked-by', '1')
    run('add', 'dup', '--duplicate-of', '1')

    run('1', 'done')

    assert show('2')['blocked_by'] == []
    assert listed('blocked=any') == ['1', '2']


@pytest.mark.usefixtures('folder')
def test_duplicates_go_with_their_canonical() -> None:
    run('add', 'canonical')
    run('add', 'dup', '--duplicate-of', '1')
    run('add', 'waits on dup', '--blocked-by', '2')
    assert show('1')['duplicates'][0]['task'] == '2'

    run('1', 'done')

    assert listed('blocked=any') == ['3']
    assert comments('3') == [('operator', 'Cleared: 2')]


@pytest.mark.usefixtures('folder')
def test_undoing_a_duplicate_unblocks_it() -> None:
    run('add', 'canonical')
    run('add', 'dup', '--tag', 'build', '--duplicate-of', '1')

    run('2', 'unset', 'duplicate_of')

    assert (show('2')['tag'], show('2')['duplicate_of']) == ('triage', None)
    assert comments('2') == [('operator', 'Cleared: duplicate of 1')]


@pytest.mark.usefixtures('folder')
def test_a_canonical_made_a_duplicate_hands_its_duplicates_on() -> None:
    run('add', 'new canonical')
    run('add', 'old canonical')
    run('add', 'dup', '--duplicate-of', '2')

    run('2', 'set', '--duplicate-of', '1')

    assert [d['task'] for d in show('1')['duplicates']] == ['2', '3']
    assert refused('1', 'set', '--duplicate-of', '2') == (
        '2 is itself a duplicate of 1'
    )


@pytest.mark.usefixtures('folder')
def test_duplicates_are_one_off() -> None:
    run('add', 'canonical')
    run('add', 'weekly', '--next', '2026-01-01', '--interval', '1w')
    run('add', 'dup', '--duplicate-of', '1')

    assert refused('2', 'set', '--duplicate-of', '1') == (
        'a recurring task cannot be a duplicate'
    )
    assert refused('3', 'set', '--interval', '1w') == (
        'a recurring task cannot be a duplicate'
    )


@pytest.mark.usefixtures('folder')
def test_relations_follow_moved_tasks() -> None:
    run('namespace', 'add', 'bakery')
    run('add', 'target')
    run('add', 'waiting', '--blocked-by', '1:decided')

    run('1', 'set', '-n', 'bakery')
    run('2', 'set', '-n', 'bakery')

    assert show('bakery:2')['blocked_by'][0]['task'] == 'bakery:1'
    assert show('bakery:1')['blocks'][0]['task'] == 'bakery:2'


@pytest.mark.usefixtures('folder')
def test_relations_need_a_target() -> None:
    assert refused('add', 'a', '--blocked-by', '7') == 'task 7 not found'
    assert refused('add', 'a', '--duplicate-of', 'x:1') == 'task x:1 not found'
    run('add', 'a')
    assert refused('1', 'set', '--blocked-by', '1') == (
        'waiting on 1 would make a cycle'
    )
    assert refused('1', 'unset', '--blocked-by', '2') == 'not blocked by 2'
    assert refused('1', 'unset') == 'nothing to unset'


@pytest.mark.usefixtures('folder')
def test_relations_are_stamped() -> None:
    run('namespace', 'add', 'bakery')
    run('add', 'target')
    run('add', '-n', 'bakery', 'waiting')
    agent().change(ops.Verb.set, refs.Ref.parse('bakery:1'), blocking('1'))

    (blocker,) = show('bakery:1')['blocked_by']
    (inbound,) = show('1')['blocks']
    assert blocker['actor'] == inbound['actor'] == AGENT.name
    assert blocker['created'] is not None
    assert blocker['created'] == inbound['created']


@pytest.mark.usefixtures('folder')
def test_what_relations_do_ignores_rules_and_claims() -> None:
    run('namespace', 'add', 'bakery')
    run('add', 'operator task', '--tag', 'personal')
    run('add', '-n', 'bakery', 'claw task')
    run('1', 'set', '--blocked-by', 'bakery:1')
    run('1', 'claim')
    writer = agent()

    with pytest.raises(errors.TaskError, match='claimed by operator'):
        writer.change(
            ops.Verb.set, refs.Ref.parse('1'), blocking('https://x.io: y')
        )
    writer.done(refs.Ref.parse('bakery:1'), 0)

    unblocked = show('1')
    assert (unblocked['tag'], unblocked['claim']) == ('triage', 'operator')
    assert comments('1') == [(AGENT.name, 'Cleared: bakery:1')]


@pytest.mark.usefixtures('folder')
def test_guards_judge_what_relations_do() -> None:
    run('add', 'target')
    run('add', 'waiting', '--tag', 'build', '--blocked-by', '1')
    seen: list[tuple[ops.Verb, str | None]] = []

    def guard(change: ops.Change) -> None:
        after = change.after
        seen.append((change.verb, after.tag if after else None))
        if change.verb is ops.Verb.settle:
            raise errors.TaskError('not now')

    writer = ops.Writer(actors.OPERATOR_ACTOR, guard)
    with pytest.raises(errors.TaskError, match='not now'):
        writer.done(refs.Ref.parse('1'), 0)

    assert seen == [(ops.Verb.done, None), (ops.Verb.settle, 'triage')]
    assert listed('blocked=any') == ['1', '2']
