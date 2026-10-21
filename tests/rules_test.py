import tomllib
from collections.abc import Callable
from typing import TypedDict
from typing import Unpack

import pytest

from task import actors
from task import db
from task import errors
from task import ops
from task import query
from task import refs
from task import rules
from task import schema

RULES = """
[triage.create]
namespaces = ["bakery"]
tags = ["triage", "build"]
require = ["link"]
fields = ["description", "priority"]

[triage.claim]
namespaces = ["bakery"]
tags = ["triage"]

[triage.change]
fields = ["priority"]
tags = ["build", "triage"]

[triage.comment]
namespaces = ["bakery"]
tags = ["triage", "build"]

[triage.question]
namespaces = ["bakery"]
tags = ["build"]
"""
AGENT = actors.Actor.agent('triage/task-1')


class Fields(TypedDict, total=False):
    summary: str
    namespace: str
    tag: str
    due: str
    description: str
    link: str
    priority: str
    size: str


OPERATOR = ops.Writer(actors.OPERATOR_ACTOR)


def agent(text: str = RULES) -> ops.Writer:
    return ops.Writer(AGENT, rules.parse(tomllib.loads(text))['triage'].check)


def ref(value: str) -> refs.Ref:
    return refs.Ref.parse(value)


def new(**fields: Unpack[Fields]) -> schema.Task:
    task = schema.Task(
        summary='a', details=None, namespace='bakery', tag=schema.TRIAGE
    )
    task.update(**fields)
    return task


def stored(value: str) -> schema.Task:
    with db.connect() as store:
        return db.get(store, ref(value))


@pytest.fixture(scope='function', autouse=True)
def bakery(folder: object) -> None:
    del folder
    db.add_namespace('bakery')
    OPERATOR.add(new(description='x'))
    OPERATOR.add(new(tag='build'))


def set_(**fields: Unpack[Fields]) -> Callable[[ops.Writer], object]:
    def edit(task: schema.Task) -> schema.Task:
        task.update(**fields)
        return task

    return lambda w: w.change(ops.Verb.set, ref('bakery:1'), edit)


def say(
    value: str,
    *,
    comments: tuple[str, ...] = (),
    questions: tuple[str, ...] = (),
) -> Callable[[ops.Writer], object]:
    def edit(task: schema.Task) -> schema.Task:
        task.converse(AGENT, comments=comments, questions=questions)
        return task

    return lambda w: w.change(ops.Verb.set, ref(value), edit)


def asking(task: schema.Task) -> schema.Task:
    task.converse(AGENT, questions=['q'])
    return task


@pytest.mark.parametrize(
    ('action', 'error'),
    [
        (lambda w: w.add(new(link='l', description='d')), None),
        (
            lambda w: w.add(schema.Task(summary='a', details=None, tag='t')),
            'you may not create tasks in default > t',
        ),
        (lambda w: w.add(new(tag='human', link='l')), 'in bakery > human'),
        (lambda w: w.add(new()), 'a new task needs its link'),
        (lambda w: w.add(new(link='l', size='small')), 'not set size'),
        (lambda w: w.add(new(link='l', due='2026-02-01')), 'not set due'),
        (lambda w: w.claim(ref('bakery:1')), None),
        (lambda w: w.claim(ref('bakery:2')), 'claim tasks in bakery > build'),
        (set_(tag='build', priority='high'), None),
        (set_(tag='human'), 'you may not move tasks to human'),
        (say('bakery:1', comments=('c',)), None),
        (
            say('bakery:1', questions=('q',)),
            'ask questions on tasks in bakery > triage',
        ),
        (say('bakery:2', comments=('c',), questions=('q',)), None),
        (lambda w: w.add(asking(new(link='l'))), 'ask questions on tasks in'),
        (lambda w: w.add(asking(new(link='l', tag='build'))), None),
        (set_(description='other'), 'you may not change description'),
        (set_(summary='b'), 'you may not change summary'),
        (set_(namespace='default'), 'you may not move tasks to default'),
        (lambda w: w.change(ops.Verb.unset, ref('bakery:1'), _unlinked), None),
        (lambda w: w.done(ref('bakery:1'), 0), 'not mark bakery:1 as done'),
    ],
)
def test_rules_grant_only_what_they_name(
    action: Callable[[ops.Writer], object], error: str | None
) -> None:
    writer = agent()
    if error is None:
        action(writer)
        return
    with pytest.raises(errors.TaskError, match=error):
        action(writer)
    assert stored('bakery:1').claim is None


def test_saying_something_takes_no_claim() -> None:
    # bakery:2 is in build, where the agent may ask but not claim.
    agent().change(ops.Verb.set, ref('bakery:2'), asking)
    assert stored('bakery:2').claim is None

    def prioritized_asking(task: schema.Task) -> schema.Task:
        return asking(_prioritized(task))

    with pytest.raises(
        errors.TaskError, match='claim tasks in bakery > build'
    ):
        agent().change(ops.Verb.set, ref('bakery:2'), prioritized_asking)
    assert len(stored('bakery:2').conversation.questions) == 1


def _unlinked(task: schema.Task) -> schema.Task:
    task.clear([schema.ClearableField.link])
    return task


def test_no_rules_deny_everything() -> None:
    actions: list[Callable[[ops.Writer], object]] = [
        lambda w: w.add(new(link='l')),
        lambda w: w.claim(ref('bakery:1')),
        set_(priority='high'),
        say('bakery:1', comments=('c',)),
        say('bakery:1', questions=('q',)),
    ]
    for action in actions:
        with pytest.raises(errors.TaskError, match='^you may not'):
            action(agent('[triage]'))


def test_agents_change_what_they_create_under_its_rule() -> None:
    writer = agent()
    created = new(tag='build', link='l')
    writer.add(created)

    # Created in build, which it may not claim, yet it holds the claim.
    writer.change(ops.Verb.set, created.ref, _prioritized)
    writer.release(created.ref)
    with pytest.raises(
        errors.TaskError, match='claim tasks in bakery > build'
    ):
        writer.change(ops.Verb.set, created.ref, _prioritized)


def _prioritized(task: schema.Task) -> schema.Task:
    task.update(priority='low')
    return task


def test_next_skips_what_the_agent_may_not_claim() -> None:
    OPERATOR.claim(ref('bakery:1'))
    OPERATOR.add(new())
    taken = agent().next('', -1, query.SortOrder.ident)

    assert taken is not None
    assert (str(taken.ref), taken.claim) == ('bakery:3', AGENT.name)
    assert agent().next('', -1, query.SortOrder.ident) is None


@pytest.mark.parametrize(
    ('text', 'error'),
    [
        ('[a.create]\nnamespaces = ["b"]\ntag = ["c"]', 'unknown keys tag'),
        ('[a.claim]\nnamespaces = "b"', 'must be a list of strings'),
        ('[a.change]\nfields = ["owner"]', "invalid field: 'owner'"),
        ('[a.change]\ndone = "yes"', 'must be a boolean'),
        ('[a.claim]\nnamespaces = ["B c"]', 'invalid a.claim.namespaces'),
        ('a = 1', 'a: must be a table'),
    ],
)
def test_invalid_rules_are_refused(text: str, error: str) -> None:
    with pytest.raises(errors.TaskError, match=error):
        rules.parse(tomllib.loads(text))
