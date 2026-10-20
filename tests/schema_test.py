import datetime
from typing import Any

import hypothesis
import pytest
import time_machine
from hypothesis import strategies as st

from task import errors
from task import refs
from task import schedule
from task import schema

INTERVAL = st.builds(
    lambda count, unit: schedule.Interval(raw=f'{count}{unit}'),
    st.integers(1, 99),
    st.sampled_from('dwm'),
)


def month_end(date: datetime.date) -> datetime.date:
    following = date.replace(day=28) + datetime.timedelta(days=4)
    return following.replace(day=1) - datetime.timedelta(days=1)


# Far enough from date.max that any INTERVAL stays representable; month ends
# are where adding months goes wrong.
ANY_DATE = st.dates(max_value=datetime.date(9000, 1, 1))
DATE = ANY_DATE | ANY_DATE.map(month_end)


@hypothesis.given(INTERVAL, DATE)
def test_interval_moves_forward(
    interval: schedule.Interval, date: datetime.date
) -> None:
    assert interval.apply(date) > date


@hypothesis.given(
    INTERVAL,
    st.dates(datetime.date(2000, 1, 1), datetime.date(2100, 1, 1)),
    st.integers(-1000, 1000),
)
def test_marking_a_fixed_recurrence_as_done_moves_past_today(
    interval: schedule.Interval, today: datetime.date, offset: int
) -> None:
    due = today + datetime.timedelta(days=offset)
    task = schema.Task(
        summary='a',
        tag='a',
        details=schedule.Details.model_validate(
            {'due': due, 'interval': interval}
        ),
    )
    noon = datetime.datetime.combine(
        today, datetime.time(12), tzinfo=datetime.UTC
    )
    with time_machine.travel(noon, tick=False):
        next_occurrence = task.done(0)

    assert next_occurrence
    assert next_occurrence.details
    assert next_occurrence.details.due > max(today, due)


@hypothesis.given(
    INTERVAL,
    st.dates(datetime.date(2000, 1, 1), datetime.date(2100, 1, 1)),
    st.integers(-1000, 1000),
    st.lists(st.integers(0, 1000), min_size=1, max_size=3),
)
def test_a_delayed_fixed_recurrence_recurs_from_its_due_date(
    interval: schedule.Interval,
    today: datetime.date,
    offset: int,
    delays: list[int],
) -> None:
    due = today + datetime.timedelta(days=offset)
    task = fixed(due, interval.raw)
    noon = datetime.datetime.combine(
        today, datetime.time(12), tzinfo=datetime.UTC
    )
    with time_machine.travel(noon, tick=False):
        undelayed = task.model_copy(deep=True).done(0)
        for days in delays:
            task = task.delay(days)
        assert task.details
        assert task.details.due == today + datetime.timedelta(days=delays[-1])
        next_occurrence = task.done(0)

    assert next_occurrence
    assert next_occurrence.details
    assert next_occurrence.details.delayed_from is None
    assert next_occurrence == undelayed


def fixed(due: datetime.date, interval: str) -> schema.Task:
    return schema.Task(
        summary='a',
        tag='a',
        details=schedule.Details.model_validate(
            {'due': due, 'interval': {'raw': interval}}
        ),
    )


def delayed_from(task: schema.Task) -> datetime.date | None:
    assert task.details
    return task.details.delayed_from


@pytest.mark.usefixtures('today')
def test_delay_only_moves_a_fixed_recurrence() -> None:
    due = datetime.date(2026, 1, 12)
    assert delayed_from(fixed(due, '1w').delay(2)) == due
    # Delayed back to the date it was due, it is no longer delayed.
    assert delayed_from(fixed(due, '1w').delay(2).delay(-3)) is None
    shifting = fixed(due, '1w')
    shifting.update(shift=True)
    assert delayed_from(shifting.delay(2)) is None
    one_off = schema.Task(summary='a', tag='a', details=None)
    assert delayed_from(one_off.delay(2)) is None


@pytest.mark.parametrize(
    ('change', 'kept'),
    [
        ({'interval': '2w'}, True),
        ({'summary': 'b'}, True),
        ({'due': '2026-01-20'}, False),
        ({'shift': True}, False),
    ],
)
@pytest.mark.usefixtures('today')
def test_rescheduling_forgets_the_delay(
    change: dict[str, Any], kept: bool
) -> None:
    due = datetime.date(2026, 1, 12)
    task = fixed(due, '1w').delay(2)
    task.update(**change)
    assert delayed_from(task) == (due if kept else None)


@pytest.mark.usefixtures('today')
def test_unsetting_the_interval_forgets_the_delay() -> None:
    task = fixed(datetime.date(2026, 1, 12), '1w').delay(2)
    task.clear([schema.ClearableField.interval])
    assert delayed_from(task) is None


@hypothesis.given(
    st.from_regex(r'[a-z0-9][a-z0-9_.-]{0,10}', fullmatch=True),
    st.integers(min_value=0),
)
def test_ref_roundtrips(namespace: str, ident: int) -> None:
    ref = refs.Ref(namespace, ident)
    assert refs.Ref.parse(str(ref)) == ref


@hypothesis.given(st.text() | st.text(' \tAZaz09_.-'))
def test_normalize_name_is_idempotent(value: str) -> None:
    try:
        name = schema.normalize_name('tag', value)
    except errors.TaskError:
        return
    assert schema.normalize_name('tag', name) == name


@pytest.mark.parametrize(
    ('raw', 'rendered'),
    [
        ('1d', 'daily'),
        ('3d', 'every 3 days'),
        ('1w', 'weekly'),
        ('2w', 'every 2 weeks'),
        ('1m', 'monthly'),
        ('12m', 'every 12 months'),
    ],
)
def test_interval_renders(raw: str, rendered: str) -> None:
    assert str(schedule.Interval(raw=raw)) == rendered
