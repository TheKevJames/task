import contextlib
import operator
import tempfile

import hypothesis
import pytest
from hypothesis import strategies as st

from task import db
from task import errors
from task import query
from task import schema

FILTER_TEXT = st.text() | st.lists(
    st.sampled_from(
        [*query.Target, 'bogus', '=', '!', '~', '^', ',', ' ', 'x', '2026-01']
    )
).map(''.join)
# Small alphabets, so that values cut from one field often recur in it.
TASK = st.builds(
    schema.Task,
    summary=st.text('aAbÉé ', min_size=1, max_size=4).filter(str.strip),
    details=st.none(),
    tag=st.from_regex(r'[ab][ab-]{0,3}', fullmatch=True),
    claim=st.none() | st.from_regex(r'[ab/]{1,4}', fullmatch=True),
    link=st.none() | st.from_regex(r'[aAb/]{1,4}', fullmatch=True),
    priority=st.none() | st.sampled_from(schema.Priority),
    size=st.none() | st.sampled_from(schema.Size),
)
MATCHES = {
    query.Match.exact: operator.eq,
    query.Match.contains: operator.contains,
    query.Match.prefix: str.startswith,
}


@hypothesis.given(FILTER_TEXT)
def test_filters_parse_or_are_refused(raw: str) -> None:
    with contextlib.suppress(errors.TaskError):
        list(query.Filter.parse(raw))


# Values are cut from the task's own field, which tells matches apart far more
# often than api_test.test_list_matches_reference's independent ones.
@hypothesis.settings(deadline=None)
@hypothesis.given(
    data=st.data(),
    field=st.sampled_from(
        ['summary', 'namespace', 'tag', 'claim', 'link', 'priority', 'size']
    ),
    match=st.sampled_from(query.Match),
    negate=st.booleans(),
)
def test_filters_match_as_python_compares_lowercased(
    data: st.DataObject, field: str, match: query.Match, negate: bool
) -> None:
    task = data.draw(TASK)
    text = str(getattr(task, field) or '')
    start = data.draw(st.integers(0, len(text)))
    value = text[start : data.draw(st.integers(start, len(text)))]
    value = data.draw(st.sampled_from([value, value.upper()]))
    raw = f'{field}{"!" if negate else ""}{match}{value}'
    clauses = query.compile_(raw, -1, -1, query.SortOrder.tag)
    with (
        tempfile.TemporaryDirectory() as folder,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setenv('TASK_FOLDER', folder)
        with db.write() as store:
            db.insert(store, task)
        with db.connect() as store:
            selected = bool(db.select(store, *clauses))

    print(raw, repr(text))
    assert selected == (MATCHES[match](text.lower(), value.lower()) != negate)


@pytest.mark.parametrize(
    ('raw', 'message'),
    [
        ('questions^=open', '^questions only supports open '),
        ('blocked^=yes', '^blocked only supports yes, no, or any '),
        ('next=2026-01', "^invalid date: '2026-01' "),
        ('next^=2026-1', "^invalid date: '2026-1' "),
        ('next^=2026-13', "^invalid date: '2026-13' "),
    ],
)
def test_prefixes_are_refused_where_unsupported(
    raw: str, message: str
) -> None:
    with pytest.raises(errors.TaskError, match=message):
        list(query.Filter.parse(raw))


@pytest.mark.parametrize(
    ('raw', 'data'), [('next^=2026', '2026'), ('next^=20260131', '2026-01-31')]
)
def test_next_takes_a_year_month_or_date_prefix(raw: str, data: str) -> None:
    filters = list(query.Filter.parse(raw))
    assert [f.data for f in filters] == [data]
