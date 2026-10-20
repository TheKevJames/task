"""
What is said on tasks: comments, questions to the operator, and the
operator's answers, kept in the `entry` table beside their tasks.

Who said something, and when, belong to the store: callers give only what
was said, and the store stamps it. Nothing said changes but for a question
gaining its one answer, which is an entry of its own, so no entry is ever
updated. Entries follow their task by its key: they move with it, and go
when it does.
"""

import re
import sqlite3
import textwrap
from collections.abc import Mapping
from typing import Annotated
from typing import Literal
from typing import Self
from typing import TypeVar

import pydantic

from . import actors
from . import errors
from . import timestamp

# `Q<n>: <answer>`, answering a task's nth question.
ANSWER_RE = re.compile(r'[Qq](?P<number>[1-9][0-9]*):(?P<body>.*)', re.DOTALL)
# A task's namespace and id.
Key = tuple[str, int]


class Entry(pydantic.BaseModel, extra='forbid', frozen=True):
    """
    Something an actor said on a task. Who said it, and when, belong to the
    store rather than to the caller: `created` is None until it is stored.
    """

    actor: str
    body: str
    created: pydantic.AwareDatetime | None = None

    @pydantic.field_validator('actor')
    @classmethod
    def validate_actor(cls, value: str) -> str:
        return actors.check_claim(value)

    @pydantic.field_validator('body')
    @classmethod
    def validate_body(cls, value: str) -> str:
        if not value or value != value.strip():
            raise errors.TaskError('entry is empty or not stripped')
        return value

    def render(self, label: str, indent: str = '') -> str:
        assert self.created, 'entry has not been stored'
        body = textwrap.indent(self.body, f'{indent}  ')
        stamp = timestamp.render(self.created)
        return f'{indent}{label}{stamp} {self.actor}:\n{body}'

    def to_json(self) -> dict[str, object]:
        return {
            'actor': self.actor,
            'created': timestamp.serialize(self.created),
            'body': self.body,
        }


class Comment(Entry):
    kind: Literal['comment'] = 'comment'


class Question(Entry):
    kind: Literal['question'] = 'question'
    answer: Entry | None = None


E = TypeVar('E', bound=Entry)
Said = Comment | Question


class Conversation(pydantic.BaseModel, extra='forbid', frozen=True):
    """What has been said on a task, in the order it was said."""

    entries: tuple[
        Annotated[Said, pydantic.Field(discriminator='kind')], ...
    ] = ()

    @property
    def questions(self) -> tuple[Question, ...]:
        return tuple(e for e in self.entries if isinstance(e, Question))

    @property
    def open_questions(self) -> int:
        return sum(question.answer is None for question in self.questions)

    def comment(self, actor: actors.Actor, text: str) -> Self:
        said = Comment(actor=actor.name, body=_said('comment', text))
        return type(self)(entries=(*self.entries, said))

    def ask(self, actor: actors.Actor, text: str) -> Self:
        said = Question(actor=actor.name, body=_said('question', text))
        return type(self)(entries=(*self.entries, said))

    def answer(self, actor: actors.Actor, text: str) -> Self:
        """Answer a question, as `Q<n>: <answer>`."""
        match = ANSWER_RE.fullmatch(text.strip())
        if not match:
            raise errors.TaskError(
                f'invalid answer: {text!r} (use eg. Q1: yes)'
            )
        number = int(match.group('number'))
        questions = self.questions
        if number > len(questions):
            raise errors.TaskError(f'no such question: Q{number}')
        question = questions[number - 1]
        if question.answer:
            raise errors.TaskError(f'Q{number} is already answered')
        body = _said('answer', match.group('body'))
        answered = question.model_copy(
            update={'answer': Entry(actor=actor.name, body=body)}
        )
        return type(self)(
            entries=tuple(
                answered if entry is question else entry
                for entry in self.entries
            )
        )

    def continues(self, before: 'Conversation', actor: actors.Actor) -> None:
        """
        Refuses anything but `actor` adding to `before`: what was said stays
        said, by whoever said it, when they did.
        """
        if len(self.entries) < len(before.entries):
            raise errors.TaskError('what was said on a task cannot be unsaid')
        for old, new in zip(before.entries, self.entries):
            if new != old:
                _check_answered(old, new, actor)
        for new in self.entries[len(before.entries) :]:
            _check_unstamped(new, actor)
            if isinstance(new, Question) and new.answer:
                raise errors.TaskError(
                    'a question cannot be answered as it is asked'
                )

    def render(self) -> str:
        lines = []
        for entry in self.entries:
            if isinstance(entry, Comment):
                lines.append(entry.render(''))
                continue
            number = self.questions.index(entry) + 1
            lines.append(entry.render(f'Q{number} '))
            if entry.answer:
                lines.append(entry.answer.render('A ', '  '))
            else:
                lines.append('  (open)')
        return '\n'.join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            'comments': [
                entry.to_json()
                for entry in self.entries
                if isinstance(entry, Comment)
            ],
            'questions': [
                {
                    'number': number,
                    **question.to_json(),
                    'answer': (
                        question.answer.to_json() if question.answer else None
                    ),
                }
                for number, question in enumerate(self.questions, 1)
            ],
        }


def _said(kind: str, text: str) -> str:
    body = text.strip()
    if not body:
        raise errors.TaskError(f'invalid {kind}: {text!r}')
    return body


def _check_answered(old: Said, new: Said, actor: actors.Actor) -> None:
    if not (
        isinstance(old, Question)
        and isinstance(new, Question)
        and old.answer is None
        and new.answer is not None
        and new.model_copy(update={'answer': None}) == old
    ):
        raise errors.TaskError('what was said on a task cannot be changed')
    if not actor.is_operator:
        raise errors.TaskError('only the operator may answer questions')
    _check_unstamped(new.answer, actor)


def _check_unstamped(entry: Entry, actor: actors.Actor) -> None:
    if entry.actor != actor.name or entry.created is not None:
        raise errors.TaskError(
            f'{actor.name} may only say something as themselves, and now'
        )


def load(
    conn: sqlite3.Connection, clauses: str, params: Mapping[str, object]
) -> dict[Key, Conversation]:
    """The conversations of the tasks which `clauses` select."""
    rows = conn.execute(
        'SELECT namespace, task, id, kind, question, actor, created, body'
        ' FROM entry WHERE (namespace, task) IN ('
        f'  SELECT namespace, id FROM task{clauses}'
        ') ORDER BY id',
        params,
    )
    entries: dict[Key, list[Said]] = {}
    # A question's id -> its index in its task's entries.
    questions: dict[int, int] = {}
    for namespace, task, ident, kind, question, *fields in rows:
        said = entries.setdefault((namespace, task), [])
        entry = dict(zip(('actor', 'created', 'body'), fields, strict=True))
        if kind == 'answer':
            index = questions[question]
            said[index] = said[index].model_copy(
                update={'answer': Entry.model_validate(entry)}
            )
        elif kind == 'question':
            questions[ident] = len(said)
            said.append(Question.model_validate(entry))
        else:
            said.append(Comment.model_validate(entry))
    return {
        key: Conversation(entries=tuple(said)) for key, said in entries.items()
    }


def unstamped(conversation: Conversation) -> bool:
    """Whether anything has been said which is not yet stored."""
    return any(
        entry.created is None
        or (
            isinstance(entry, Question)
            and entry.answer is not None
            and entry.answer.created is None
        )
        for entry in conversation.entries
    )


def store(
    conn: sqlite3.Connection, key: Key, conversation: Conversation
) -> Conversation:
    """
    Store whatever was said on a stored task since it was loaded; the
    conversation as the store stamped it.
    """
    asked = [
        ident
        for (ident,) in conn.execute(
            'SELECT id FROM entry WHERE namespace = ? AND task = ?'
            " AND kind = 'question' ORDER BY id",
            key,
        )
    ]
    entries: list[Said] = []
    number = 0
    for entry in conversation.entries:
        if entry.created is None:
            ident, entry = _insert(conn, key, entry, entry.kind)
            if isinstance(entry, Question):
                asked.append(ident)
        if isinstance(entry, Question):
            answer = entry.answer
            if answer is not None and answer.created is None:
                _, answer = _insert(conn, key, answer, 'answer', asked[number])
                entry = entry.model_copy(update={'answer': answer})
            number += 1
        entries.append(entry)
    return Conversation(entries=tuple(entries))


def _insert(
    conn: sqlite3.Connection,
    key: Key,
    entry: E,
    kind: str,
    question: int | None = None,
) -> tuple[int, E]:
    """Store `entry`, returning its id and itself as the store stamped it."""
    namespace, task = key
    stamp: tuple[int, str] = conn.execute(
        'INSERT INTO entry (namespace, task, kind, question, actor, body)'
        ' VALUES (:namespace, :task, :kind, :question, :actor, :body)'
        ' RETURNING id, created',
        {
            'namespace': namespace,
            'task': task,
            'kind': kind,
            'question': question,
            'actor': entry.actor,
            'body': entry.body,
        },
    ).fetchone()
    ident, created = stamp
    stamped = entry.model_dump() | {'created': created}
    return ident, type(entry).model_validate(stamped)
