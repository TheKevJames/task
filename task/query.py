"""Selecting and ordering tasks: filters and sort orders, compiled to SQL."""

import datetime
import enum
import re
from collections.abc import Iterator
from typing import Self

import pydantic

from . import errors
from . import refs
from . import schema


class Target(enum.StrEnum):
    summary = 'summary'
    namespace = 'namespace'
    tag = 'tag'
    claim = 'claim'
    link = 'link'
    priority = 'priority'
    size = 'size'
    next = 'next'
    questions = 'questions'
    blocked = 'blocked'


# Filter values are compared lowercased. `fold` is Python's str.lower,
# registered by db.connect: SQLite's lower() only folds ASCII. Namespaces,
# tags, and claims are stored lowercased.
COLUMNS = {
    Target.summary: 'fold(summary)',
    Target.namespace: 'namespace',
    Target.tag: 'tag',
    Target.claim: "coalesce(claim, '')",
    Target.link: "fold(coalesce(link, ''))",
    Target.priority: "coalesce(priority, '')",
    Target.size: "coalesce(size, '')",
    Target.next: "coalesce(next, '')",
}
OPEN = 'open'
# Whether a task has any question without an answer: `questions=open`.
OPEN_QUESTIONS = (
    'EXISTS (SELECT 1 FROM entry AS asked'
    ' WHERE asked.namespace = task.namespace AND asked.task = task.id'
    " AND asked.kind = 'question' AND NOT EXISTS ("
    '  SELECT 1 FROM entry AS answered WHERE answered.question = asked.id'
    '))'
)


# Whether a task waits on anything: `blocked=yes`, `blocked=no` (the default),
# or `blocked=any`.
BLOCKED = (
    'EXISTS (SELECT 1 FROM blocker'
    ' WHERE blocker.namespace = task.namespace AND blocker.task = task.id)'
)
BLOCKED_SQL = {'yes': BLOCKED, 'no': f'NOT {BLOCKED}', 'any': 'TRUE'}
# A year or month, which `next^=` also takes.
DATE_PREFIX_RE = re.compile(r'[0-9]{4}(?:-(?:0[1-9]|1[0-2]))?')


class Match(enum.StrEnum):
    exact = '='
    contains = '~'
    prefix = '^='


# `instr` finds an empty `data` at 1, so an empty prefix matches everything.
MATCH_SQL = {
    Match.exact: '{column} = {value}',
    Match.contains: 'instr({column}, {value}) > 0',
    Match.prefix: 'instr({column}, {value}) = 1',
}


class Filter(pydantic.BaseModel, extra='forbid'):
    data: str
    negate: bool
    match: Match
    target: Target

    @pydantic.model_validator(mode='after')
    def check_questions(self) -> Self:
        if self.target is Target.questions and (
            self.match is not Match.exact or self.data != OPEN
        ):
            raise errors.TaskError(
                f'questions only supports {OPEN}'
                f' (use questions={OPEN} or questions!={OPEN})'
            )
        return self

    @pydantic.model_validator(mode='after')
    def check_blocked(self) -> Self:
        if self.target is Target.blocked and (
            self.match is not Match.exact or self.data not in BLOCKED_SQL
        ):
            raise errors.TaskError(
                'blocked only supports yes, no, or any (use eg. blocked=yes)'
            )
        return self

    @pydantic.model_validator(mode='after')
    def check_next(self) -> Self:
        """
        Dates match as stored: YYYY-MM-DD, or empty if unset; a prefix may
        also be a year or month.
        """
        if self.target is not Target.next:
            return self
        if self.match is Match.contains:
            raise errors.TaskError(
                'next only supports exact and prefix matches'
                ' (use eg. next=2026-01-31, next^=2026-01, next=)'
            )
        if self.match is Match.prefix and DATE_PREFIX_RE.fullmatch(self.data):
            return self
        if self.data:
            try:
                self.data = datetime.date.fromisoformat(self.data).isoformat()
            except ValueError:
                raise errors.TaskError(
                    f'invalid date: {self.data!r} (use eg. 2026-01-31)'
                ) from None
        return self

    @classmethod
    def parse(cls, raw: str) -> Iterator[Self]:
        for filter_ in raw.split(','):
            if not filter_.strip():
                continue

            parsed = re.match(
                r'(?P<target>[^!=~^]+)(?P<negate>!?)(?P<match>=|~|\^=)'
                r'(?P<data>.*)',
                filter_,
            )
            if not parsed:
                raise errors.TaskError(
                    f'invalid filter: {filter_!r}'
                    ' (use eg. tag=a, tag!=a, tag~a, tag!~a, tag^=a, tag!^=a)'
                )

            yield cls(
                data=parsed.group('data').lower(),
                negate=bool(parsed.group('negate')),
                match=Match(parsed.group('match')),
                target=schema.parse_enum(Target, parsed.group('target')),
            )

    def sql(self, param: str) -> str:
        """A boolean expression comparing against `data`, bound to `param`."""
        value = f':{param}'
        if self.target is Target.questions:
            clause = OPEN_QUESTIONS
        elif self.target is Target.blocked:
            clause = BLOCKED_SQL[self.data]
        else:
            clause = MATCH_SQL[self.match].format(
                column=COLUMNS[self.target], value=value
            )
        return f'NOT ({clause})' if self.negate else f'({clause})'


class SortOrder(enum.StrEnum):
    created = 'created'
    due = 'due'
    ident = 'id'
    priority = 'priority'
    tag = 'tag'
    updated = 'updated'


# Ids repeat across namespaces, so every order ends on both.
ORDER_BY = {
    SortOrder.created: 'created, id, namespace',
    SortOrder.due: 'next IS NOT NULL, next, id, namespace',
    SortOrder.ident: 'id, namespace',
    SortOrder.priority: (
        "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1"
        " WHEN 'low' THEN 2 ELSE 3 END,"
        ' next IS NULL, next, id, namespace'
    ),
    SortOrder.tag: (
        f"namespace != '{refs.DEFAULT}', namespace,"
        f" tag != '{schema.TRIAGE}', tag, id"
    ),
    SortOrder.updated: 'updated DESC, id, namespace',
}


def compile_(
    filter_: str,
    days: int,
    limit: int,
    order: SortOrder,
    *,
    scheduled: bool = False,
    unclaimed: bool = False,
) -> tuple[str, dict[str, object]]:
    """
    The WHERE, ORDER BY, and LIMIT clauses selecting tasks, and params;
    blocked tasks only if `filter_` asks for them.
    """
    clauses: list[str] = []
    params: dict[str, object] = {}
    filters = list(Filter.parse(filter_))
    for i, filter_part in enumerate(filters):
        clauses.append(filter_part.sql(f'filter{i}'))
        params[f'filter{i}'] = filter_part.data
    if all(f.target is not Target.blocked for f in filters):
        clauses.append(BLOCKED_SQL['no'])
    if days >= 0:
        target = datetime.date.today() + datetime.timedelta(days=days)
        clauses.append('(next IS NULL OR next <= :target)')
        params['target'] = target.isoformat()
    if scheduled:
        clauses.append('next IS NOT NULL')
    if unclaimed:
        clauses.append('claim IS NULL')

    sql = f' WHERE {" AND ".join(clauses)}' if clauses else ''
    sql += f' ORDER BY {ORDER_BY[order]}'
    if limit >= 0:
        sql += ' LIMIT :limit'
        params['limit'] = limit
    return sql, params
