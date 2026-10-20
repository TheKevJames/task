import datetime
import enum
import re
from collections.abc import Iterable
from typing import Self
from typing import TypeVar

import pydantic

from . import actors
from . import conversations
from . import errors
from . import refs
from . import relations
from . import schedule
from . import timestamp

LINK_RE = re.compile(r'\S+')
TRIAGE = 'triage'


def check_link(value: str) -> str:
    if not LINK_RE.fullmatch(value):
        raise errors.TaskError(f'invalid link: {value!r}')
    return value


class Priority(enum.StrEnum):
    low = 'low'
    medium = 'medium'
    high = 'high'


class Size(enum.StrEnum):
    small = 'small'
    medium = 'medium'
    large = 'large'


E = TypeVar('E', bound=enum.StrEnum)


def parse_enum(kind: type[E], value: str) -> E:
    try:
        return kind(value)
    except ValueError:
        choices = ', '.join(kind)
        raise errors.TaskError(
            f'invalid {kind.__name__.lower()}: {value!r} (use {choices})'
        ) from None


def normalize_name(kind: str, value: str) -> str:
    """A tag or namespace: ` Foo ` -> `foo`."""
    name = value.strip().lower()
    if not refs.NAME_RE.fullmatch(name):
        raise errors.TaskError(f'invalid {kind}: {value!r}')
    return name


class Task(pydantic.BaseModel, extra='forbid', validate_assignment=True):
    # pylint: disable=too-many-instance-attributes
    summary: str
    details: schedule.Details | None
    ident: int | None = None
    namespace: str = refs.DEFAULT
    tag: str
    description: str | None = None
    # The name of the actor holding the task's claim.
    claim: str | None = None
    link: str | None = None
    priority: Priority | None = None
    size: Size | None = None
    conversation: conversations.Conversation = conversations.Conversation()
    blocked_by: tuple[relations.Blocker, ...] = ()
    duplicate_of: relations.Duplicate | None = None
    # Assigned by the store: see db.
    created: pydantic.AwareDatetime | None = None
    updated: pydantic.AwareDatetime | None = None
    # Relations other tasks hold on this one; changing them changes nothing.
    blocks: tuple[relations.Inbound, ...] = ()
    duplicates: tuple[relations.Inbound, ...] = ()

    @pydantic.field_validator('summary')
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if not value.strip() or '\n' in value:
            raise errors.TaskError(f'invalid summary: {value!r}')
        return value

    @pydantic.field_validator('namespace', 'tag')
    @classmethod
    def validate_name(cls, value: str, info: pydantic.ValidationInfo) -> str:
        if not refs.NAME_RE.fullmatch(value):
            raise errors.TaskError(f'invalid {info.field_name}: {value!r}')
        return value

    @pydantic.field_validator('description')
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise errors.TaskError('description is not stripped')
        return value or None

    @pydantic.field_validator('claim')
    @classmethod
    def validate_claim(cls, value: str | None) -> str | None:
        return None if value is None else actors.normalize_claim(value)

    @pydantic.field_validator('link')
    @classmethod
    def validate_link(cls, value: str | None) -> str | None:
        return None if value is None else check_link(value)

    @property
    def ref(self) -> refs.Ref:
        assert self.ident is not None, 'task id has not been assigned'
        return refs.Ref(self.namespace, self.ident)

    @property
    def waits_on(self) -> tuple[relations.Relation, ...]:
        duplicate = (self.duplicate_of,) if self.duplicate_of else ()
        return (*self.blocked_by, *duplicate)

    @property
    def decided(self) -> bool:
        return not (
            self.tag == TRIAGE
            or self.waits_on
            or self.conversation.open_questions
        )

    @property
    def section(self) -> str:
        if self.namespace == refs.DEFAULT:
            return self.tag
        return f'{self.namespace} > {self.tag}'

    def __str__(self) -> str:
        result = f'{self.section}\t{self.ref}: {self.summary}'
        if self.description:
            result += ' +'
        if self.conversation.open_questions:
            result += f' ?{self.conversation.open_questions}'
        if self.waits_on:
            result += ' \N{MULTIPLICATION SIGN}'
        if self.priority:
            result += f' !{self.priority}'
        if self.claim:
            result += f' @{self.claim}'
        if self.details:
            result += f'\n\t{self.details}'
        return result

    def done(self, ago: int) -> Self | None:
        """The next occurrence if recurring; None, to delete it, if not."""
        if not (self.details and self.details.interval):
            if ago:
                raise errors.TaskError('--ago only applies to recurring tasks')
            return None

        new_task = self.model_copy()
        assert new_task.details
        if self.details.shift:
            new_task.details.due = self.details.interval.apply(
                datetime.date.today() - datetime.timedelta(days=ago)
            )
        else:
            due = self.details.delayed_from or self.details.due
            next_ = self.details.interval.apply(due)
            while next_ <= datetime.date.today():
                next_ = self.details.interval.apply(next_)
            new_task.details.due = next_
            new_task.details.delayed_from = None
        return new_task

    def delay(self, days: int) -> Self:
        """
        Make its current occurrence due `days` from today; a fixed recurrence
        keeps the date it was due, to count its next occurrence from.
        """
        new_task = self.model_copy()
        due = datetime.date.today() + datetime.timedelta(days=days)
        if not new_task.details:
            details = {'due': due}
            new_task.details = schedule.Details.model_validate(details)
        elif new_task.details.fixed:
            was = new_task.details.delayed_from or new_task.details.due
            new_task.details.delayed_from = None if was == due else was
        new_task.details.due = due
        return new_task

    def update(  # pylint: disable=too-many-arguments
        self,
        *,
        summary: str | None = None,
        namespace: str | None = None,
        tag: str | None = None,
        due: str | None = None,
        interval: str | None = None,
        shift: bool | None = None,
        description: str | None = None,
        link: str | None = None,
        priority: str | None = None,
        size: str | None = None,
    ) -> None:
        if namespace is not None:
            self.namespace = normalize_name('namespace', namespace)
        if summary is not None:
            self.summary = summary
        if tag is not None:
            self.tag = normalize_name('tag', tag)
        if description is not None:
            self.description = description.strip() or None
        self._set_metadata(link, priority, size)
        if due is not None or interval is not None or shift is not None:
            self._schedule(due, interval, shift)

    def _set_metadata(
        self, link: str | None, priority: str | None, size: str | None
    ) -> None:
        if link is not None:
            self.link = check_link(link)
        if priority is not None:
            self.priority = parse_enum(Priority, priority)
        if size is not None:
            self.size = parse_enum(Size, size)

    def _schedule(
        self, due: str | None, interval: str | None, shift: bool | None
    ) -> None:
        if self.details is None:
            seed = due or datetime.date.today().isoformat()
            self.details = schedule.Details.model_validate({'due': seed})
        if due is not None:
            # The occurrence is now due then, as if never delayed.
            self.details.due = datetime.date.fromisoformat(due)
            self.details.delayed_from = None
        if interval is not None:
            self.details.interval = schedule.Interval(raw=interval)
        if shift is not None:
            self.details.shift = shift
        if not self.details.fixed:
            self.details.delayed_from = None

    def relate(
        self,
        actor: actors.Actor,
        *,
        blocked_by: Iterable[str] = (),
        duplicate_of: str | None = None,
        cleared: Iterable[str] = (),
    ) -> None:
        """
        Change what this task waits on, as `actor`: clear blockers, by ref or
        url, then add others.
        """
        self.blocked_by = relations.unblock(self.blocked_by, cleared)
        self.blocked_by += relations.block(actor, self.waits_on, blocked_by)
        if duplicate_of is not None:
            self.duplicate_of = relations.duplicate(
                actor, self.waits_on, duplicate_of
            )

    def converse(
        self,
        actor: actors.Actor,
        *,
        comments: Iterable[str] = (),
        questions: Iterable[str] = (),
        answers: Iterable[str] = (),
    ) -> None:
        """
        Say something on this task, as `actor`. Answers come first, so that
        they answer only questions asked before.
        """
        conversation = self.conversation
        for answer in answers:
            conversation = conversation.answer(actor, answer)
        for comment in comments:
            conversation = conversation.comment(actor, comment)
        for question in questions:
            conversation = conversation.ask(actor, question)
        self.conversation = conversation

    def claim_as(self, actor: actors.Actor, *, force: bool = False) -> None:
        """Compare-and-set: only an unclaimed task may change hands."""
        _check_force(actor, force)
        if self.claim not in {None, actor.name} and not force:
            raise errors.TaskError(
                f'task {self.ref} is claimed by {self.claim}'
            )
        self.claim = actor.name

    def release_as(self, actor: actors.Actor, *, force: bool = False) -> None:
        _check_force(actor, force)
        if self.claim is None:
            raise errors.TaskError(f'task {self.ref} is not claimed')
        if self.claim != actor.name and not force:
            raise errors.TaskError(
                f'task {self.ref} is claimed by {self.claim}'
            )
        self.claim = None

    def authorize(self, actor: actors.Actor, *, force: bool = False) -> None:
        """
        Before `actor` changes this task: an agent claims it, and the operator
        may change only an unclaimed task or their own, unless forced.
        """
        if not actor.is_operator:
            self.claim_as(actor, force=force)
        elif self.claim not in {None, actors.OPERATOR} and not force:
            raise errors.TaskError(
                f'task {self.ref} is claimed by {self.claim}'
            )

    def clear(self, fields: Iterable['ClearableField']) -> None:
        for field in fields:
            if field is ClearableField.due:
                self.details = None
            elif field in OPTIONAL_FIELDS:
                setattr(self, field.value, None)
            elif self.details is None:
                continue
            elif field is ClearableField.interval:
                self.details.interval = None
                self.details.delayed_from = None
            elif field is ClearableField.shift:
                self.details.shift = False

    def render_detail(self) -> str:
        rows = [
            ('Summary', self.summary),
            ('ID', str(self.ref)),
            ('Tag', self.section),
        ]
        if self.claim:
            rows.append(('Claim', self.claim))
        if self.link:
            rows.append(('Link', self.link))
        if self.priority:
            rows.append(('Priority', self.priority))
        if self.size:
            rows.append(('Size', self.size))
        canonical = self.duplicate_of.canonical if self.duplicate_of else None
        related: tuple[tuple[str, tuple[object, ...]], ...] = (
            ('Blocked by', self.blocked_by),
            ('Duplicate of', (canonical,) if canonical else ()),
            ('Blocks', self.blocks),
            ('Duplicates', self.duplicates),
        )
        rows.extend(
            (label, ', '.join(map(str, items)))
            for label, items in related
            if items
        )
        if self.details:
            rows.append(('Due', self._render_due()))
            if self.details.interval:
                anchor = (
                    'after completion'
                    if self.details.shift
                    else 'since last deadline'
                )
                rows.append(
                    ('Recurrence', f'{self.details.interval} {anchor}')
                )
        rows.extend(
            (label, timestamp.render(value))
            for label, value in (
                ('Created', self.created),
                ('Updated', self.updated),
            )
            if value
        )

        width = max(len(label) for label, _ in rows)
        header = '\n'.join(
            f'{label + ":":<{width + 2}} {value}' for label, value in rows
        )
        sections = [header, self.description, self.conversation.render()]
        return '\n---\n'.join(section for section in sections if section)

    def to_json(self) -> dict[str, object]:
        interval = self.details.interval if self.details else None
        delayed_from = self.details.delayed_from if self.details else None
        return {
            'id': self.ident,
            'namespace': self.namespace,
            'summary': self.summary,
            'tag': self.tag,
            'claim': self.claim,
            'link': self.link,
            'priority': self.priority,
            'size': self.size,
            'description': self.description,
            **self.conversation.to_json(),
            'blocked_by': [blocker.to_json() for blocker in self.blocked_by],
            'duplicate_of': (
                self.duplicate_of.to_json() if self.duplicate_of else None
            ),
            'blocks': [inbound.to_json() for inbound in self.blocks],
            'duplicates': [inbound.to_json() for inbound in self.duplicates],
            'due': self.details.due.isoformat() if self.details else None,
            'delayed_from': (
                delayed_from.isoformat() if delayed_from else None
            ),
            'interval': interval.raw if interval else None,
            'shift': bool(self.details and self.details.shift),
            'created': timestamp.serialize(self.created),
            'updated': timestamp.serialize(self.updated),
        }

    def _render_due(self) -> str:
        assert self.details, 'due row requires details'
        diff = (self.details.due - datetime.date.today()).days
        if diff > 0:
            status = f'{diff} {"day" if diff == 1 else "days"}'
        elif diff == 0:
            status = 'due today'
        else:
            status = 'overdue'
        if self.details.delayed_from:
            status += f', delayed from {self.details.delayed_from}'
        return f'{self.details.due} ({status})'


def _check_force(actor: actors.Actor, force: bool) -> None:
    if force and not actor.is_operator:
        raise errors.TaskError('only the operator may force a change')


class ClearableField(enum.StrEnum):
    description = 'description'
    due = 'due'
    duplicate_of = 'duplicate_of'
    interval = 'interval'
    link = 'link'
    priority = 'priority'
    shift = 'shift'
    size = 'size'


# Task attributes that `unset` clears to None, named as in ClearableField.
OPTIONAL_FIELDS = frozenset(
    {
        ClearableField.description,
        ClearableField.duplicate_of,
        ClearableField.link,
        ClearableField.priority,
        ClearableField.size,
    }
)
