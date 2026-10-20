import contextlib
import datetime
import tempfile
from collections.abc import Generator

import hypothesis
import pytest
from hypothesis import strategies as st

from task import actors
from task import api
from task import conversations
from task import db
from task import refs
from task import schedule
from task import schema

PRIORITY_RANK = {
    api.Priority.high: 0,
    api.Priority.medium: 1,
    api.Priority.low: 2,
    None: 3,
}
CONVERSATIONS = (
    conversations.Conversation(),
    conversations.Conversation(
        entries=(conversations.Comment(actor='a', body='c'),)
    ),
    conversations.Conversation(
        entries=(conversations.Question(actor='a', body='q'),)
    ),
    conversations.Conversation(
        entries=(
            conversations.Question(
                actor='a',
                body='q',
                answer=conversations.Entry(actor=actors.OPERATOR, body='y'),
            ),
        )
    ),
)
# Small alphabets, so that filters often match, and summaries non-ASCII case.
NAME = st.sampled_from(['a', 'a-b', 'b', schema.TRIAGE, refs.DEFAULT])
# Due dates are drawn within a few days of the stopped clock's today, which
# NEXT_DATE straddles.
DATES = (-3, 5)
FILTER_VALUE = (
    st.sampled_from(['', 'high', 'small'])
    | NAME
    | st.text('aAbÉé-! ', max_size=4)
)
NEXT_DATE = st.just('') | st.dates(
    datetime.date(2026, 1, 11), datetime.date(2026, 1, 21)
).map(datetime.date.isoformat)
FIELDS = [
    'summary',
    'description',
    'namespace',
    'tag',
    'claim',
    'link',
    'priority',
    'size',
]
# (field, operator, value): `list -f` syntax, joined only when listed.
FILTER = (
    st.tuples(
        st.sampled_from(FIELDS),
        st.sampled_from(['=', '!=', '~', '!~', '^=', '!^=']),
        FILTER_VALUE | FILTER_VALUE.map(str.upper),
    )
    | st.tuples(st.just('next'), st.sampled_from(['=', '!=']), NEXT_DATE)
    | st.tuples(
        st.just('next'),
        st.sampled_from(['^=', '!^=']),
        NEXT_DATE | st.sampled_from(['2026', '2026-01', '2026-02']),
    )
    | st.tuples(
        st.just('questions'), st.sampled_from(['=', '!=']), st.just('open')
    )
)


def task(today: datetime.date) -> st.SearchStrategy[api.Task]:
    first, last = (today + datetime.timedelta(days) for days in DATES)
    details = st.none() | st.builds(
        lambda next_, interval: schedule.Details.model_validate(
            {'next': next_, 'interval': interval and {'raw': interval}}
        ),
        st.dates(first, last),
        st.sampled_from([None, '1d', '2w']),
    )
    return st.builds(
        api.Task,
        summary=st.text('aAbÉé ', min_size=1, max_size=3).filter(str.strip),
        description=st.sampled_from([None, 'a', 'A b', 'É\nb']),
        details=details,
        namespace=st.sampled_from([refs.DEFAULT, 'a', 'b']),
        tag=NAME,
        claim=st.sampled_from([None, 'a', 'A', 'b/c']),
        link=st.sampled_from([None, 'a', 'A/b']),
        priority=st.none() | st.sampled_from(api.Priority),
        size=st.none() | st.sampled_from(api.Size),
        conversation=st.sampled_from(CONVERSATIONS),
    )


@contextlib.contextmanager
def task_list(tasks: list[api.Task]) -> Generator[None]:
    """A store holding `tasks`, per hypothesis example."""
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        for namespace in {t.namespace for t in tasks} - {refs.DEFAULT}:
            db.add_namespace(namespace)
        with db.write() as store:
            for t in tasks:
                db.insert(store, t)
        yield


def location(t: api.Task) -> tuple[str, int | None]:
    return t.namespace, t.ident


def reference_match(filter_: tuple[str, str, str], t: api.Task) -> bool:
    field, op, value = filter_
    if field == 'next':
        candidate = t.details.next_.isoformat() if t.details else ''
    else:
        candidate = str(getattr(t, field, '') or '').lower()
    if field == 'questions':
        matched = t.conversation.open_questions > 0
    elif op.endswith('~'):
        matched = value.lower() in candidate
    elif op.endswith('^='):
        matched = candidate.startswith(value.lower())
    else:
        matched = value.lower() == candidate
    return matched != op.startswith('!')


def reference_key(order: api.SortOrder, t: api.Task) -> tuple[object, ...]:
    if order == api.SortOrder.tag:
        return (
            t.namespace != refs.DEFAULT,
            t.namespace,
            t.tag != schema.TRIAGE,
            t.tag,
            t.ident,
        )
    if order == api.SortOrder.due:
        next_ = t.details.next_ if t.details else datetime.date.min
        key: tuple[object, ...] = (t.details is not None, next_)
    elif order == api.SortOrder.created:
        assert t.created
        key = (t.created,)
    elif order == api.SortOrder.updated:
        assert t.updated
        key = (-t.updated.timestamp(),)
    elif order == api.SortOrder.priority:
        next_ = t.details.next_ if t.details else datetime.date.max
        key = (PRIORITY_RANK[t.priority], t.details is None, next_)
    else:
        key = ()
    return (*key, t.ident, t.namespace)


# The clock stays stopped across examples, which is what they need.
@hypothesis.settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture],
)
@hypothesis.given(
    data=st.data(),
    filters=st.lists(FILTER, max_size=2),
    due_within=st.none() | st.integers(0, 4),
    limit=st.none() | st.integers(0, 4),
    order=st.sampled_from(api.SortOrder),
)
def test_list_matches_reference(  # pylint: disable=too-many-arguments
    today: datetime.date,
    data: st.DataObject,
    filters: list[tuple[str, str, str]],
    due_within: int | None,
    limit: int | None,
    order: api.SortOrder,
) -> None:
    tasks = data.draw(st.lists(task(today), max_size=10))
    filter_ = ','.join(''.join(f) for f in filters)
    with task_list(tasks):
        listed = api.list_(
            filter_, sort=order, limit=limit, due_within=due_within
        )

    expected = [
        t
        for t in tasks
        if all(reference_match(f, t) for f in filters)
        and (
            due_within is None
            or not t.details
            or t.details.next_ <= today + datetime.timedelta(due_within)
        )
    ]
    expected.sort(key=lambda t: reference_key(order, t))
    print(filter_)
    assert [location(t) for t in listed] == [
        location(t) for t in expected[:limit]
    ]


@hypothesis.settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture],
)
@hypothesis.given(data=st.data())
def test_get_finds_each_listed_task(
    today: datetime.date, data: st.DataObject
) -> None:
    tasks = data.draw(st.lists(task(today), max_size=6))
    with task_list(tasks):
        listed = api.list_()
        found = [api.get(t.ref) for t in listed]

    assert sorted(map(location, listed)) == sorted(map(location, tasks))
    assert found == listed
