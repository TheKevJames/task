import calendar
import dataclasses
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
from . import timestamp

LINK_RE = re.compile(r'\S+')
# Tags and namespaces; their CHECKs in migrations must agree.
NAME_RE = re.compile(r'[a-z0-9][a-z0-9_.-]*')
REF_RE = re.compile(rf'(?:(?P<namespace>{NAME_RE.pattern}):)?(?P<id>\d+)')
TRIAGE = 'triage'
DEFAULT = 'default'


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


class Unit(enum.StrEnum):
    day = 'd'
    week = 'w'
    month = 'm'


# The interval CHECK in migrations must agree.
INTERVAL_RE = re.compile(rf'[1-9][0-9]*[{"".join(Unit)}]')

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
    if not NAME_RE.fullmatch(name):
        raise errors.TaskError(f'invalid {kind}: {value!r}')
    return name


@dataclasses.dataclass(frozen=True)
class Ref:
    """A task's address: `<namespace>:<id>`, or a bare id in `default`."""

    namespace: str
    ident: int

    @classmethod
    def parse(cls, value: str) -> Self:
        match = REF_RE.fullmatch(value)
        if not match:
            raise errors.TaskError(f'invalid task id: {value!r}')
        return cls(match.group('namespace') or DEFAULT, int(match.group('id')))

    def __str__(self) -> str:
        if self.namespace == DEFAULT:
            return str(self.ident)
        return f'{self.namespace}:{self.ident}'


class Interval(pydantic.BaseModel, extra='forbid'):
    raw: str

    @pydantic.field_validator('raw')
    @classmethod
    def validate_raw(cls, value: str) -> str:
        if not INTERVAL_RE.fullmatch(value):
            raise errors.TaskError(f'invalid interval: {value!r}')
        return value

    @property
    def value(self) -> int:
        return int(self.raw[:-1])

    @property
    def unit(self) -> Unit:
        return Unit(self.raw[-1])

    def __str__(self) -> str:
        if self.value != 1:
            return f'every {self.value} {self.unit.name}s'

        match self.unit:
            case Unit.day:
                return 'daily'
            case Unit.week:
                return 'weekly'
            case Unit.month:
                return 'monthly'

    def apply(self, value: datetime.date) -> datetime.date:
        match self.unit:
            case Unit.day:
                return value + datetime.timedelta(days=self.value)
            case Unit.week:
                return value + datetime.timedelta(weeks=self.value)
            case Unit.month:
                tyear, tmonth = divmod(value.month - 1 + self.value, 12)
                year, month = value.year + tyear, tmonth + 1
                # Clamping drifts: Jan 31 -> Feb 28 -> Mar 28.
                day = min(value.day, calendar.monthrange(year, month)[1])
                return datetime.date(year, month, day)


class Details(pydantic.BaseModel, extra='forbid'):
    interval: Interval | None = None
    next_: datetime.date = pydantic.Field(..., alias='next')
    shift: bool = False

    def __str__(self) -> str:
        result = f'Due: {self.next_}'

        if self.interval:
            result += f', repeats {self.interval}'
            if self.shift:
                result += ' after completion'
            else:
                result += ' since last deadline'

        return result


class Task(pydantic.BaseModel, extra='forbid', validate_assignment=True):
    # pylint: disable=too-many-instance-attributes
    summary: str
    details: Details | None
    ident: int | None = None
    namespace: str = DEFAULT
    tag: str
    description: str | None = None
    # The name of the actor holding the task's claim.
    claim: str | None = None
    link: str | None = None
    priority: Priority | None = None
    size: Size | None = None
    conversation: conversations.Conversation = conversations.Conversation()
    # Assigned by the store: see db.
    created: pydantic.AwareDatetime | None = None
    updated: pydantic.AwareDatetime | None = None

    @pydantic.field_validator('summary')
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if not value.strip() or '\n' in value:
            raise errors.TaskError(f'invalid summary: {value!r}')
        return value

    @pydantic.field_validator('namespace', 'tag')
    @classmethod
    def validate_name(cls, value: str, info: pydantic.ValidationInfo) -> str:
        if not NAME_RE.fullmatch(value):
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
        return None if value is None else actors.check_claim(value)

    @pydantic.field_validator('link')
    @classmethod
    def validate_link(cls, value: str | None) -> str | None:
        return None if value is None else check_link(value)

    @property
    def ref(self) -> Ref:
        assert self.ident is not None, 'task id has not been assigned'
        return Ref(self.namespace, self.ident)

    @property
    def section(self) -> str:
        if self.namespace == DEFAULT:
            return self.tag
        return f'{self.namespace} > {self.tag}'

    def __str__(self) -> str:
        result = f'{self.section}\t{self.ref}: {self.summary}'
        if self.description:
            result += ' +'
        if self.conversation.open_questions:
            result += f' ?{self.conversation.open_questions}'
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
            new_task.details.next_ = self.details.interval.apply(
                datetime.date.today() - datetime.timedelta(days=ago)
            )
        else:
            next_ = self.details.interval.apply(self.details.next_)
            while next_ <= datetime.date.today():
                next_ = self.details.interval.apply(next_)
            new_task.details.next_ = next_
        return new_task

    def delay(self, days: int) -> Self:
        new_task = self.model_copy()
        if not new_task.details:
            details = {'next': datetime.date.today()}
            new_task.details = Details.model_validate(details)
        elif self.details:
            if self.details.interval and not self.details.shift:
                raise errors.TaskError(
                    'recurring tasks with fixed recurrence cannot be delayed'
                )
        else:
            assert False, 'impossible task state'

        new_task.details.next_ = datetime.date.today() + datetime.timedelta(
            days=days
        )
        return new_task

    def update(  # pylint: disable=too-many-arguments
        self,
        *,
        summary: str | None = None,
        namespace: str | None = None,
        tag: str | None = None,
        next_: str | None = None,
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
        if next_ is not None or interval is not None or shift is not None:
            self._schedule(next_, interval, shift)

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
        self, next_: str | None, interval: str | None, shift: bool | None
    ) -> None:
        if self.details is None:
            seed = next_ or datetime.date.today().isoformat()
            self.details = Details.model_validate({'next': seed})
        if next_ is not None:
            self.details.next_ = datetime.date.fromisoformat(next_)
        if interval is not None:
            self.details.interval = Interval(raw=interval)
        if shift is not None:
            self.details.shift = shift

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
            if field is ClearableField.next:
                self.details = None
            elif field in OPTIONAL_FIELDS:
                setattr(self, field.value, None)
            elif self.details is None:
                continue
            elif field is ClearableField.interval:
                self.details.interval = None
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
            'next': self.details.next_.isoformat() if self.details else None,
            'interval': interval.raw if interval else None,
            'shift': bool(self.details and self.details.shift),
            'created': timestamp.serialize(self.created),
            'updated': timestamp.serialize(self.updated),
        }

    def _render_due(self) -> str:
        assert self.details, 'due row requires details'
        diff = (self.details.next_ - datetime.date.today()).days
        if diff > 0:
            status = f'{diff} {"day" if diff == 1 else "days"}'
        elif diff == 0:
            status = 'due today'
        else:
            status = 'overdue'
        return f'{self.details.next_} ({status})'


def _check_force(actor: actors.Actor, force: bool) -> None:
    if force and not actor.is_operator:
        raise errors.TaskError('only the operator may force a change')


class ClearableField(enum.StrEnum):
    description = 'description'
    interval = 'interval'
    link = 'link'
    next = 'next'
    priority = 'priority'
    shift = 'shift'
    size = 'size'


# Task attributes that `unset` clears to None, named as in ClearableField.
OPTIONAL_FIELDS = frozenset(
    {
        ClearableField.description,
        ClearableField.link,
        ClearableField.priority,
        ClearableField.size,
    }
)
