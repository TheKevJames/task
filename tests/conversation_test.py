"""What was said on a task stays said, by whoever said it, when they did."""

import contextlib
import datetime
import json
import pathlib
import sqlite3
import tempfile
from collections.abc import Callable
from collections.abc import Generator
from typing import Any

import hypothesis
import pytest
from hypothesis import strategies as st
from typer import testing

from task import actors
from task import cli
from task import conversations
from task import db
from task import errors
from task import ops
from task import schema

RUNNER = testing.CliRunner()
AGENTS = (actors.Actor.agent('a/1'), actors.Actor.agent('b/2'))
AGENT = AGENTS[0]
ACTORS = (actors.OPERATOR_ACTOR, *AGENTS)
REF = schema.Ref(schema.DEFAULT, 1)
TEXT = st.text('ab \n', min_size=1, max_size=5).filter(str.strip)
# (actor, kind, text, pick): an answer answers the pick-th open question.
SAYING = st.tuples(
    st.sampled_from(ACTORS),
    st.sampled_from(['comment', 'question', 'answer']),
    TEXT,
    st.integers(min_value=0),
)
# What a conversation holds, but for when: (kind, actor, body, answer).
Said = tuple[str, str, str, tuple[str, str] | None]


@contextlib.contextmanager
def task_folder() -> Generator[None]:
    """A store holding one task per hypothesis example."""
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        ops.Writer(actors.OPERATOR_ACTOR).add(
            schema.Task(summary='a', details=None, tag=schema.TRIAGE)
        )
        yield


def run(*args: str) -> str:
    """The output of a successful invocation, as the operator."""
    result = RUNNER.invoke(cli.app, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.stdout


def refused(*args: str) -> str:
    with pytest.raises(errors.TaskError) as excinfo:
        RUNNER.invoke(cli.app, args, catch_exceptions=False)
    return str(excinfo.value)


def listed(filter_: str = '') -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = json.loads(
        run('list', '--json', '-f', filter_)
    )
    return tasks


def stored() -> conversations.Conversation:
    with db.connect() as store:
        return db.get(store, REF).conversation


def flatten(conversation: conversations.Conversation) -> list[Said]:
    return [
        (
            entry.kind,
            entry.actor,
            entry.body,
            (entry.answer.actor, entry.answer.body)
            if isinstance(entry, conversations.Question) and entry.answer
            else None,
        )
        for entry in conversation.entries
    ]


def converse(actor: actors.Actor, **said: list[str]) -> ops.Edit:
    def edit(task: schema.Task) -> schema.Task:
        task.converse(actor, **said)
        return task

    return edit


def apply(sayings: list[tuple[actors.Actor, str, str, int]]) -> list[Said]:
    """Say each, as its actor; what should then have been said."""
    expected: list[Said] = []
    for actor, kind, text, pick in sayings:
        writer = ops.Writer(actor)
        print(f'{actor.name} {kind}s {text!r}')
        if kind != 'answer':
            writer.change(
                ops.Verb.set, REF, converse(actor, **{f'{kind}s': [text]})
            )
            expected.append((kind, actor.name, text.strip(), None))
            continue
        asked = [i for i, said in enumerate(expected) if said[0] == 'question']
        unanswered = [i for i in asked if expected[i][3] is None]
        if not unanswered:
            continue
        index = unanswered[pick % len(unanswered)]
        edit = converse(actor, answers=[f'Q{asked.index(index) + 1}: {text}'])
        if not actor.is_operator:
            with pytest.raises(errors.TaskError, match='only the operator'):
                writer.change(ops.Verb.set, REF, edit)
            continue
        writer.change(ops.Verb.set, REF, edit)
        question = expected[index]
        expected[index] = (*question[:3], (actor.name, text.strip()))
    return expected


@hypothesis.settings(max_examples=100, deadline=None)
@hypothesis.given(st.lists(SAYING, max_size=8))
def test_whatever_is_said_is_kept_as_the_store_stamped_it(
    sayings: list[tuple[actors.Actor, str, str, int]],
) -> None:
    with task_folder():
        expected = apply(sayings)
        conversation = stored()

    assert flatten(conversation) == expected
    for entry in conversation.entries:
        assert entry.created is not None
        if isinstance(entry, conversations.Question) and entry.answer:
            assert entry.answer.created is not None


def tampers(
    entries: tuple[conversations.Said, ...], writer: actors.Actor
) -> dict[str, Callable[[int], tuple[conversations.Said, ...]]]:
    """Ways to forge `entries` as `writer`, by the index each applies to."""
    other = next(a.name for a in ACTORS if a != writer)
    stamp = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)

    def replaced(
        index: int, **update: object
    ) -> tuple[conversations.Said, ...]:
        changed = entries[index].model_copy(update=update)
        return (*entries[:index], changed, *entries[index + 1 :])

    forged: dict[str, Callable[[int], tuple[conversations.Said, ...]]] = {
        'impersonate': lambda _: (
            *entries,
            conversations.Comment(actor=other, body='x'),
        ),
        'backdate': lambda _: (
            *entries,
            conversations.Question(actor=writer.name, body='x', created=stamp),
        ),
        'answer as asked': lambda _: (
            *entries,
            conversations.Question(
                actor=writer.name,
                body='x',
                answer=conversations.Entry(actor=writer.name, body='y'),
            ),
        ),
    }
    if not entries:
        return forged
    forged |= {
        'unsay': lambda i: (*entries[:i], *entries[i + 1 :]),
        'reword': lambda i: replaced(i, body=f'{entries[i].body}!'),
        'reattribute': lambda i: replaced(
            i, actor=next(a.name for a in ACTORS if a.name != entries[i].actor)
        ),
        'redate': lambda i: replaced(i, created=stamp),
    }
    answers = {
        i: entry.answer
        for i, entry in enumerate(entries)
        if isinstance(entry, conversations.Question)
    }
    answered = [i for i, answer in answers.items() if answer]
    unanswered = [i for i, answer in answers.items() if not answer]
    if answered:
        forged['reanswer'] = lambda i: replaced(
            answered[i % len(answered)],
            answer=conversations.Entry(actor=actors.OPERATOR, body='z'),
        )
    if unanswered and not writer.is_operator:
        forged['answer as an agent'] = lambda i: replaced(
            unanswered[i % len(unanswered)],
            answer=conversations.Entry(actor=writer.name, body='z'),
        )
    return forged


@hypothesis.settings(max_examples=150, deadline=None)
@hypothesis.given(
    st.lists(SAYING, max_size=6), st.sampled_from(ACTORS), st.data()
)
def test_what_was_said_cannot_be_forged(
    sayings: list[tuple[actors.Actor, str, str, int]],
    writer: actors.Actor,
    data: st.DataObject,
) -> None:
    with task_folder():
        apply(sayings)
        before = stored()
        forged = tampers(before.entries, writer)
        how = data.draw(st.sampled_from(sorted(forged)))
        index = data.draw(st.integers(0, max(len(before.entries) - 1, 0)))
        entries = forged[how](index)
        print(f'{writer.name} tries to {how}: {entries}')

        def edit(task: schema.Task) -> schema.Task:
            task.conversation = conversations.Conversation(entries=entries)
            return task

        with pytest.raises(errors.TaskError):
            ops.Writer(writer).change(ops.Verb.set, REF, edit)
        assert stored() == before


@pytest.mark.usefixtures('today')
def test_show_renders_the_conversation(folder: pathlib.Path) -> None:
    run('add', 'a', '--comment', 'first', '--question', 'why?\nreally?')
    said = ['1', 'set', '--question', 'how?', '--comment', 'hm']
    assert cli.run(ops.Writer(AGENT), said).code == 0
    run('1', 'set', '--answer', 'Q1: because')
    stamp = '2026-01-15T16:00:00Z'
    conn = sqlite3.connect(folder / db.DATABASE, isolation_level=None)
    conn.execute('UPDATE entry SET created = ?', (stamp,))
    conn.execute('UPDATE task SET created = ?, updated = ?', (stamp, stamp))
    conn.close()

    hour = '2026-01-15 11:00 (1 hour ago)'
    assert run('1') == (
        'Summary:  a\n'
        'ID:       1\n'
        'Tag:      triage\n'
        f'Created:  {hour}\n'
        f'Updated:  {hour}\n'
        '---\n'
        f'{hour} operator:\n'
        '  first\n'
        f'Q1 {hour} operator:\n'
        '  why?\n'
        '  really?\n'
        f'  A {hour} operator:\n'
        '    because\n'
        f'{hour} {AGENT.name}:\n'
        '  hm\n'
        f'Q2 {hour} {AGENT.name}:\n'
        '  how?\n'
        '  (open)\n'
    )
    assert run('list') == 'triage\t1: a ?1\n'

    [task] = listed()
    operator = {'actor': actors.OPERATOR, 'created': stamp}
    agent = {'actor': AGENT.name, 'created': stamp}
    assert task['comments'] == [
        operator | {'body': 'first'},
        agent | {'body': 'hm'},
    ]
    assert task['questions'] == [
        {'number': 1, **operator, 'body': 'why?\nreally?'}
        | {'answer': operator | {'body': 'because'}},
        {'number': 2, **agent, 'body': 'how?', 'answer': None},
    ]


@pytest.mark.usefixtures('folder')
def test_saying_something_neither_needs_nor_takes_a_claim() -> None:
    run('add', 'a')
    ops.Writer(AGENT).claim(schema.Ref.parse('1'))
    run('1', 'set', '--comment', 'x', '--question', 'y')
    run('1', 'set', '--answer', 'Q1: z')
    assert refused('1', 'set', '--comment', 'x', '--priority', 'high') == (
        f'task 1 is claimed by {AGENT.name}'
    )

    other = ops.Writer(actors.Actor.agent('other/1'))
    response = cli.run(other, ['1', 'set', '--comment', 'w'])
    print(response)
    assert response.code == 0
    [task] = listed()
    assert task['claim'] == AGENT.name
    assert [c['body'] for c in task['comments']] == ['x', 'w']


@pytest.mark.usefixtures('folder')
def test_only_the_operator_answers_each_question_once() -> None:
    run('add', 'a', '--question', 'x?', '--question', 'y?')
    response = cli.run(ops.Writer(AGENT), ['1', 'set', '--answer', 'Q1: n'])
    assert response.error == 'only the operator may answer questions'

    run('1', 'set', '--answer', ' q2:  yes ', '--answer', 'Q1:no')
    for answer, error in (
        ('Q1: again', 'Q1 is already answered'),
        ('Q3: x', 'no such question: Q3'),
        ('Q0: x', "invalid answer: 'Q0: x' (use eg. Q1: yes)"),
        ('Q1 x', "invalid answer: 'Q1 x' (use eg. Q1: yes)"),
    ):
        assert refused('1', 'set', '--answer', answer) == error
    # Answers come first, so they cannot answer what is asked alongside.
    assert refused('1', 'set', '--question', 'z?', '--answer', 'Q3: z') == (
        'no such question: Q3'
    )

    questions = json.loads(run('1', '--json'))['questions']
    assert [q['answer']['body'] for q in questions] == ['no', 'yes']


@pytest.mark.usefixtures('folder')
def test_filter_by_open_questions() -> None:
    run('add', 'none', '--comment', 'c')
    run('add', 'open', '--question', 'q')
    run('add', 'answered', '--question', 'q')
    run('3', 'set', '--answer', 'Q1: a')
    run('add', 'half', '--question', 'q', '--question', 'r')
    run('4', 'set', '--answer', 'Q2: a')

    def summaries(filter_: str) -> list[object]:
        return [t['summary'] for t in listed(filter_)]

    assert summaries('questions=open') == ['open', 'half']
    assert summaries('questions=OPEN,summary~a') == ['half']
    assert summaries('questions!=open') == ['none', 'answered']
    assert run('next', '-f', 'questions!=open', '-s', 'id') == (
        'triage\t1: none\n'
    )


def test_conversations_go_with_their_task(folder: pathlib.Path) -> None:
    run('namespace', 'add', 'b')
    recurring = ('--next', '2000-01-01', '--interval', '1d')
    run('add', 'a', '--comment', 'kept', *recurring)
    run('add', 'b', '--question', 'gone?')
    run('1', 'set', '-n', 'b', '--comment', 'moved')
    run('2', 'done')
    run('b:1', 'done')

    [task] = listed()
    assert [c['body'] for c in task['comments']] == ['kept', 'moved']
    conn = sqlite3.connect(folder / db.DATABASE, isolation_level=None)
    rows = conn.execute('SELECT namespace, task, body FROM entry').fetchall()
    conn.close()
    assert rows == [('b', 1, 'kept'), ('b', 1, 'moved')]


@pytest.mark.parametrize(
    'values',
    [
        "1, 'comment', NULL, 'a b', 'x'",
        "1, 'comment', NULL, 'a', ''",
        "1, 'comment', NULL, 'a', ' x'",
        "1, 'note', NULL, 'a', 'x'",
        "9, 'comment', NULL, 'a', 'x'",
        "1, 'comment', 1, 'a', 'x'",
        "1, 'answer', NULL, 'operator', 'x'",
        "1, 'answer', 3, 'a', 'x'",
        "1, 'answer', 1, 'operator', 'again'",
        "1, 'answer', 4, 'operator', 'a comment'",
        "1, 'answer', 5, 'operator', 'another task'",
    ],
)
@pytest.mark.usefixtures('folder')
def test_entry_constraints_refuse_bad_data(values: str) -> None:
    # Entries: 1 and its answer 2, then 3 (open) and 4 on task 1; 5 on 2.
    answered = conversations.Question(
        actor='a',
        body='q',
        answer=conversations.Entry(actor='operator', body='y'),
    )
    asked = (answered, conversations.Question(actor='a', body='q'))
    with db.write() as conn:
        for summary, said in (
            ('a', (*asked, conversations.Comment(actor='a', body='c'))),
            ('b', asked[1:]),
        ):
            db.insert(
                conn,
                schema.Task(
                    summary=summary,
                    details=None,
                    tag=schema.TRIAGE,
                    conversation=conversations.Conversation(entries=said),
                ),
            )
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.conn.execute(
            'INSERT INTO entry (namespace, task, kind, question, actor, body)'
            f" VALUES ('{schema.DEFAULT}', {values})"
        )
