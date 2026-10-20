import contextlib
import enum
import tempfile

import hypothesis
import pytest
from hypothesis import strategies as st

from task import actors
from task import db
from task import errors
from task import ops
from task import query
from task import refs
from task import schema

AGENTS = (actors.Actor.agent('a/1'), actors.Actor.agent('b/2'))
ACTORS = (actors.OPERATOR_ACTOR, *AGENTS)
REF = refs.Ref(refs.DEFAULT, 1)


class Op(enum.Enum):
    claim = 'claim'
    release = 'release'
    change = 'change'
    next = 'next'


def expected(
    claim: str | None, actor: actors.Actor, op: Op, force: bool
) -> str | None | Exception:
    """The task's claim after `op`, or the error refusing it."""
    mine = claim in {None, actor.name}
    result: str | None | Exception
    match op:
        case _ if force and not actor.is_operator:
            result = errors.TaskError('only the operator may force a change')
        case Op.claim:
            result = actor.name if mine or force else _claimed(claim)
        case Op.release if claim is None:
            result = errors.TaskError('task 1 is not claimed')
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


def _claimed(claim: str | None) -> errors.TaskError:
    return errors.TaskError(f'task 1 is claimed by {claim}')


def apply(actor: actors.Actor, op: Op, force: bool) -> None:
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
    steps: list[tuple[actors.Actor, Op, bool]],
) -> None:
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        ops.Writer(actors.OPERATOR_ACTOR).add(
            schema.Task(summary='a', details=None, tag=schema.TRIAGE)
        )
        claim = None
        for actor, op, force in steps:
            want = expected(claim, actor, op, force)
            print(f'{actor.name} {op.value} force={force}: want {want!r}')
            if isinstance(want, errors.TaskError):
                with pytest.raises(errors.TaskError, match=f'^{want}$'):
                    apply(actor, op, force)
            else:
                apply(actor, op, force)
                claim = want
            with db.connect() as store:
                assert db.get(store, REF).claim == claim


@pytest.mark.usefixtures('folder')
def test_agents_claim_what_they_add() -> None:
    for actor in ACTORS:
        task = schema.Task(summary='a', details=None, tag=schema.TRIAGE)
        ops.Writer(actor).add(task)
        assert task.claim == (None if actor.is_operator else actor.name)


@pytest.mark.parametrize('name', [actors.OPERATOR, 'a b', ''])
def test_agents_cannot_be_misnamed(name: str) -> None:
    with contextlib.suppress(errors.TaskError):
        actors.Actor.agent(name)
        pytest.fail(f'agent {name!r} was allowed')
