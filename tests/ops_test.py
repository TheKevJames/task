import contextlib
import datetime
import enum
import tempfile

import hypothesis
import pytest
import time_machine
from hypothesis import strategies as st

from task import db
from task import ops
from task import query
from task import schema

AGENTS = (schema.Actor.agent('a/1'), schema.Actor.agent('b/2'))
ACTORS = (schema.OPERATOR_ACTOR, *AGENTS)
REF = schema.Ref(schema.DEFAULT, 1)
TODAY = datetime.date(2026, 1, 15)
RANK = {
    schema.Priority.high: 0,
    schema.Priority.medium: 1,
    schema.Priority.low: 2,
    None: 3,
}


class Op(enum.Enum):
    claim = 'claim'
    release = 'release'
    change = 'change'
    next = 'next'


def expected(
    claim: str | None, actor: schema.Actor, op: Op, force: bool
) -> str | None | Exception:
    """The task's claim after `op`, or the error refusing it."""
    mine = claim in {None, actor.name}
    result: str | None | Exception
    match op:
        case _ if force and not actor.is_operator:
            result = schema.TaskError('only the operator may force a change')
        case Op.claim:
            result = actor.name if mine or force else _claimed(claim)
        case Op.release if claim is None:
            result = schema.TaskError('task 1 is not claimed')
        case Op.release:
            result = None if claim == actor.name or force else _claimed(claim)
        case Op.change if actor.is_operator:
            result = claim if mine or force else _claimed(claim)
        case Op.change:
            result = actor.name if mine else _claimed(claim)
        case Op.next if claim is None and not actor.is_operator:
            result = actor.name
        case Op.next:
            result = claim
    return result


def _claimed(claim: str | None) -> schema.TaskError:
    return schema.TaskError(f'task 1 is claimed by {claim}')


def apply(actor: schema.Actor, op: Op, force: bool) -> None:
    writer = ops.Writer(actor)
    match op:
        case Op.claim:
            writer.claim(REF, force=force)
        case Op.release:
            writer.release(REF, force=force)
        case Op.change:
            writer.change(ops.Verb.set, REF, lambda task: task, force=force)
        case Op.next if not force:
            writer.next('', -1, query.SortOrder.ident)


STEP = st.tuples(
    st.sampled_from(ACTORS), st.sampled_from(Op), st.booleans()
).filter(lambda step: step[1] is not Op.next or not step[2])


@hypothesis.settings(max_examples=200, deadline=None)
@hypothesis.given(st.lists(STEP, max_size=12))
def test_claims_follow_the_matrix(
    steps: list[tuple[schema.Actor, Op, bool]],
) -> None:
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        ops.Writer(schema.OPERATOR_ACTOR).add(
            schema.Task(summary='a', details=None, tag=schema.TRIAGE)
        )
        claim = None
        for actor, op, force in steps:
            want = expected(claim, actor, op, force)
            print(f'{actor.name} {op.value} force={force}: want {want!r}')
            if isinstance(want, schema.TaskError):
                with pytest.raises(schema.TaskError, match=f'^{want}$'):
                    apply(actor, op, force)
            else:
                apply(actor, op, force)
                claim = want
            with db.connect() as store:
                assert db.get(store, REF).claim == claim


def urgency(task: schema.Task) -> tuple[object, ...]:
    """Lower is taken first: priority, then scheduled earliest."""
    due = task.details.next_ if task.details else datetime.date.max
    return RANK[task.priority], task.details is None, due


# A task's priority, days until it is due if scheduled, and whether claimed.
SEED = st.tuples(
    st.sampled_from([None, *schema.Priority]),
    st.none() | st.integers(-2, 2),
    st.booleans(),
)


@hypothesis.settings(max_examples=100, deadline=None)
@hypothesis.given(st.lists(SEED, max_size=8), st.sampled_from(ACTORS))
def test_next_takes_the_most_urgent_task_due(
    seeds: list[tuple[schema.Priority | None, int | None, bool]],
    actor: schema.Actor,
) -> None:
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
        time_machine.travel(TODAY, tick=False),
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        candidates: list[schema.Task] = []
        for priority, days, claimed in seeds:
            task = schema.Task(
                summary='a',
                details=None
                if days is None
                else schema.Details.model_validate(
                    {'next': TODAY + datetime.timedelta(days=days)}
                ),
                tag=schema.TRIAGE,
                priority=priority,
                claim=AGENTS[1].name if claimed else None,
            )
            ops.Writer(schema.OPERATOR_ACTOR).add(task)
            if not claimed and (days is None or days <= 0):
                candidates.append(task)

        taken = ops.Writer(actor).next('', 0, query.SortOrder.priority)
        print(f'{actor.name} took {taken!r} from {candidates!r}')
        if not candidates:
            assert taken is None
            return
        assert taken is not None
        assert taken.ref in {task.ref for task in candidates}
        assert urgency(taken) == min(map(urgency, candidates))


@pytest.mark.usefixtures('folder')
def test_agents_claim_what_they_add() -> None:
    for actor in ACTORS:
        task = schema.Task(summary='a', details=None, tag=schema.TRIAGE)
        ops.Writer(actor).add(task)
        assert task.claim == (None if actor.is_operator else actor.name)


@pytest.mark.parametrize('name', [schema.OPERATOR, 'a b', ''])
def test_agents_cannot_be_misnamed(name: str) -> None:
    with contextlib.suppress(schema.TaskError):
        schema.Actor.agent(name)
        pytest.fail(f'agent {name!r} was allowed')
