import contextlib

import hypothesis
from hypothesis import strategies as st

from task import errors
from task import query

FILTER_TEXT = st.text() | st.lists(
    st.sampled_from([*query.Target, 'bogus', '=', '!', '~', ',', ' ', 'x'])
).map(''.join)


@hypothesis.given(FILTER_TEXT)
def test_filters_parse_or_are_refused(raw: str) -> None:
    with contextlib.suppress(errors.TaskError):
        list(query.Filter.parse(raw))
