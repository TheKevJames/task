"""A section's requirement holds for every agent's writes, but not the store's."""

import tempfile
import tomllib

import hypothesis
import pytest
from hypothesis import strategies as st

from task import actors
from task import db
from task import errors
from task import ops
from task import refs
from task import rules
from task import schema

AGENT = actors.Actor.agent('triage/1')
OPERATOR = ops.Writer(actors.OPERATOR_ACTOR)
RULES = """
[sections.bakery.build]
require = ["priority", "size"]

[triage.create]
namespaces = ["bakery"]
tags = ["build"]
fields = ["priority", "size"]

[triage.claim]
namespaces = ["bakery", "default"]
tags = ["build", "triage"]

[triage.change]
fields = ["namespace", "priority", "size"]
tags = ["build", "triage"]
"""
REQUIRED = (rules.Field.priority, rules.Field.size)
REF = refs.Ref('bakery', 1)


def agent(text: str = RULES) -> ops.Writer:
    return ops.Writer(AGENT, rules.parse(tomllib.loads(text))['triage'].check)


def task(
    tag: str, has: set[rules.Field], namespace: str = 'bakery'
) -> schema.Task:
    new = schema.Task(summary='a', details=None, namespace=namespace, tag=tag)
    give(new, has)
    return new


def give(task_: schema.Task, fields: set[rules.Field]) -> None:
    task_.update(
        priority='high' if rules.Field.priority in fields else None,
        size='small' if rules.Field.size in fields else None,
    )


def stored(ref: refs.Ref) -> schema.Task:
    with db.connect() as store:
        return db.get(store, ref)


# How an agent's write treats each required field.
TREATMENTS = st.sampled_from(['keep', 'set', 'unset'])


def edited(tag: str, treatments: dict[rules.Field, str]) -> ops.Edit:
    def edit(before: schema.Task) -> schema.Task:
        before.update(tag=tag)
        for field, treatment in treatments.items():
            if treatment == 'set':
                give(before, {field})
            elif treatment == 'unset':
                before.clear([schema.ClearableField(field.value)])
        return before

    return edit


def expected_refusal(
    before_tag: str,
    before_has: set[rules.Field],
    after_tag: str,
    after_has: set[rules.Field],
) -> str | None:
    """What the glossary's Requirement says of a write to bakery:1."""
    if after_tag != 'build':
        return None
    if before_tag == 'build':
        lost = [f for f in REQUIRED if f in before_has - after_has]
        return f'bakery:1 keeps its {" and ".join(lost)}' if lost else None
    missing = [f for f in REQUIRED if f not in after_has]
    return f'bakery:1 needs its {" and ".join(missing)}' if missing else None


@hypothesis.settings(max_examples=150, deadline=None)
@hypothesis.given(
    before_tag=st.sampled_from(['build', 'triage']),
    before_has=st.sets(st.sampled_from(REQUIRED)),
    after_tag=st.sampled_from(['build', 'triage']),
    treatments=st.fixed_dictionaries({f: TREATMENTS for f in REQUIRED}),
)
def test_agents_enter_only_meeting_it_and_never_take_from_it(
    before_tag: str,
    before_has: set[rules.Field],
    after_tag: str,
    treatments: dict[rules.Field, str],
) -> None:
    after_has = {
        f
        for f in REQUIRED
        if treatments[f] == 'set' or (treatments[f] == 'keep' and f in before_has)
    }
    want = expected_refusal(before_tag, before_has, after_tag, after_has)
    print(f'{before_tag} {before_has} -> {after_tag} {after_has}: {want}')
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        db.add_namespace('bakery')
        OPERATOR.add(task(before_tag, before_has))
        write = edited(after_tag, treatments)
        if want is None:
            agent().change(ops.Verb.set, REF, write)
            assert stored(REF).tag == after_tag
        else:
            with pytest.raises(errors.TaskError, match=f'^{want} in bakery > build$'):
                agent().change(ops.Verb.set, REF, write)
            assert stored(REF).claim is None


@pytest.fixture(scope='function', name='bakery')
def fixture_bakery(folder: object) -> None:
    del folder
    db.add_namespace('bakery')


@pytest.mark.parametrize(
    ('has', 'error'),
    [
        (set(), 'a new task needs its priority and size in bakery > build'),
        ({rules.Field.size}, 'a new task needs its priority in bakery > build'),
        (set(REQUIRED), None),
    ],
)
@pytest.mark.usefixtures('bakery')
def test_agents_create_only_what_meets_it(
    has: set[rules.Field], error: str | None
) -> None:
    if error is None:
        agent().add(task('build', has))
        return
    with pytest.raises(errors.TaskError, match=f'^{error}$'):
        agent().add(task('build', has))


@pytest.mark.usefixtures('bakery')
def test_moving_namespaces_enters_a_section() -> None:
    OPERATOR.add(task('build', set(), namespace=refs.DEFAULT))

    def moved(before: schema.Task) -> schema.Task:
        before.update(namespace='bakery')
        return before

    with pytest.raises(
        errors.TaskError, match='needs its priority and size in bakery > build'
    ):
        agent().change(ops.Verb.set, refs.Ref(refs.DEFAULT, 1), moved)


@pytest.mark.usefixtures('bakery')
def test_the_stores_follow_on_changes_meet_no_requirement() -> None:
    text = """
    [sections.bakery.triage]
    require = ["priority"]

    [triage.claim]
    namespaces = ["bakery"]
    tags = ["triage"]

    [triage.change]
    done = true
    """
    OPERATOR.add(task('triage', set()))
    OPERATOR.add(task('build', set()))

    def blocked(before: schema.Task) -> schema.Task:
        before.relate(actors.OPERATOR_ACTOR, blocked_by=['bakery:1'])
        return before

    OPERATOR.change(ops.Verb.set, refs.Ref('bakery', 2), blocked)

    # Unblocked, bakery:2 returns to triage without the priority it requires.
    agent(text).done(REF, 0)
    assert stored(refs.Ref('bakery', 2)).tag == schema.TRIAGE


def test_sections_are_not_an_agent() -> None:
    assert set(rules.parse(tomllib.loads(RULES))) == {'triage'}


@pytest.mark.parametrize(
    ('text', 'error'),
    [
        ('[sections.a.b]\nrequire = ["summary"]', 'may not require summary'),
        (
            '[sections.a.b]\nrequire = ["blocked_by", "shift"]',
            'may not require shift and blocked_by',
        ),
        ('[sections.a.b]\nfields = ["size"]', 'sections.a.b: unknown keys'),
        ('[sections.a]\nb = 1', 'sections.a.b: must be a table'),
        ('[sections.a.B]\n[sections.a.b]', 'a > b is required twice'),
        ('[sections."A B".c]', 'invalid sections.A B'),
        (
            '[sections.a.b]\nrequire = ["link", "size"]\n'
            '[t.create]\nnamespaces = ["a"]\ntags = ["b"]\nfields = ["size"]',
            't.create: a > b requires link, which it may not set',
        ),
    ],
)
def test_invalid_requirements_are_refused(text: str, error: str) -> None:
    with pytest.raises(errors.TaskError, match=error):
        rules.parse(tomllib.loads(text))
