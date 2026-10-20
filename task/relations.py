"""
Relations: what a task says about other tasks, or about the world outside the
task list, kept in the `blocker` table beside their tasks. A relation belongs
to the task it starts from, and the store acts on that task when what it
waits on happens (see ADR-0004 and `ops`).

A blocker waits on another task's current occurrence being marked as done, on
another task being decided, or on an external condition, which only an actor
can clear, by removing it. A duplicate waits on its canonical like the first,
but goes with it rather than being unblocked.

Who made a relation, and when, belong to the store, as for what is said on a
task: `created` is None until it is stored. Relations follow both of their
tasks by key, so they move with them; one waiting on a task must be cleared
before that task is deleted.
"""

import dataclasses
import enum
import re
import sqlite3
from collections.abc import Iterable
from typing import Annotated
from typing import Any
from typing import Literal
from typing import Self
from typing import TypeVar

import pydantic

from . import actors
from . import errors
from . import refs
from . import timestamp

TASK_RE = re.compile(
    rf'(?P<ref>{refs.REF_RE.pattern})(?::(?P<until>decided))?'
)
# A url has a scheme, so that it never reads as a task's ref; the condition
# is one stripped line. Their CHECKs in migrations must agree.
URL_RE = re.compile(r'[a-z][a-z0-9+.-]*://\S+')
EXTERNAL_RE = re.compile(
    rf'(?P<url>{URL_RE.pattern}): (?P<condition>\S(?:.*\S)?)'
)
# A task's namespace and id.
Key = tuple[str, int]


class Until(enum.StrEnum):
    done = 'done'
    decided = 'decided'


class Stamped(pydantic.BaseModel, extra='forbid', frozen=True):
    actor: str
    created: pydantic.AwareDatetime | None = None

    @pydantic.field_validator('actor')
    @classmethod
    def validate_actor(cls, value: str) -> str:
        return actors.check_claim(value)

    def stamp_json(self) -> dict[str, object]:
        return {
            'actor': self.actor,
            'created': timestamp.serialize(self.created),
        }


class TaskBlocker(Stamped):
    kind: Literal['task'] = 'task'
    target: refs.Ref
    until: Until = Until.done

    @property
    def key(self) -> str:
        return str(self.target)

    def __str__(self) -> str:
        if self.until is Until.decided:
            return f'{self.target}:{self.until}'
        return str(self.target)

    def to_json(self) -> dict[str, object]:
        return {
            'kind': self.kind,
            'task': str(self.target),
            'until': self.until,
        } | self.stamp_json()


class ExternalBlocker(Stamped):
    kind: Literal['external'] = 'external'
    url: str
    condition: str

    @pydantic.model_validator(mode='after')
    def validate_external(self) -> Self:
        if not EXTERNAL_RE.fullmatch(str(self)):
            raise errors.TaskError(f'invalid blocker: {str(self)!r}')
        return self

    @property
    def key(self) -> str:
        return self.url

    def __str__(self) -> str:
        return f'{self.url}: {self.condition}'

    def to_json(self) -> dict[str, object]:
        return {
            'kind': self.kind,
            'url': self.url,
            'condition': self.condition,
        } | self.stamp_json()


Blocker = Annotated[
    TaskBlocker | ExternalBlocker, pydantic.Field(discriminator='kind')
]


class Duplicate(Stamped):
    canonical: refs.Ref

    @property
    def target(self) -> refs.Ref:
        return self.canonical

    @property
    def key(self) -> str:
        return str(self.canonical)

    def __str__(self) -> str:
        return f'duplicate of {self.canonical}'

    def to_json(self) -> dict[str, object]:
        return {'task': str(self.canonical)} | self.stamp_json()


Relation = TaskBlocker | ExternalBlocker | Duplicate
R = TypeVar('R', bound=Relation)


class Inbound(pydantic.BaseModel, extra='forbid', frozen=True):
    """A relation another task holds on this one, as the store found it."""

    task: refs.Ref
    until: Until
    actor: str
    created: pydantic.AwareDatetime

    @classmethod
    def of(cls, relation: TaskBlocker | Duplicate, task: refs.Ref) -> Self:
        """`relation`, as stored on `task`, seen from what it waits on."""
        assert relation.created, 'relation has not been stored'
        until = relation.until if isinstance(relation, TaskBlocker) else None
        return cls(
            task=task,
            until=until or Until.done,
            actor=relation.actor,
            created=relation.created,
        )

    def __str__(self) -> str:
        if self.until is Until.decided:
            return f'{self.task} (until decided)'
        return str(self.task)

    def to_json(self) -> dict[str, object]:
        return {
            'task': str(self.task),
            'until': self.until,
            'actor': self.actor,
            'created': timestamp.serialize(self.created),
        }


@dataclasses.dataclass(frozen=True)
class Relations:
    """A task's relations, and those other tasks hold on it."""

    blocked_by: tuple[TaskBlocker | ExternalBlocker, ...] = ()
    duplicate_of: Duplicate | None = None
    blocks: tuple[Inbound, ...] = ()
    duplicates: tuple[Inbound, ...] = ()


def parse_blocker(
    actor: actors.Actor, value: str
) -> TaskBlocker | ExternalBlocker:
    """`<ref>`, `<ref>:decided`, or `<url>: <condition>`, made by `actor`."""
    if match := TASK_RE.fullmatch(value.strip()):
        return TaskBlocker(
            actor=actor.name,
            target=refs.Ref.parse(match.group('ref')),
            until=Until(match.group('until') or Until.done),
        )
    if match := EXTERNAL_RE.fullmatch(value.strip()):
        return ExternalBlocker(
            actor=actor.name,
            url=match.group('url'),
            condition=match.group('condition'),
        )
    raise errors.TaskError(
        f'invalid blocker: {value!r}'
        ' (use eg. bakery:184, bakery:184:decided, or "https://...: merged")'
    )


def block(
    actor: actors.Actor, waits_on: tuple[Relation, ...], values: Iterable[str]
) -> tuple[TaskBlocker | ExternalBlocker, ...]:
    """
    Blockers on `values`, made by `actor`, for a task waiting on `waits_on`:
    a task relates to another at most once.
    """
    keys = {relation.key for relation in waits_on}
    added: list[TaskBlocker | ExternalBlocker] = []
    for value in values:
        blocker = parse_blocker(actor, value)
        if blocker.key in keys:
            raise errors.TaskError(f'already waiting on {blocker.key}')
        keys.add(blocker.key)
        added.append(blocker)
    return tuple(added)


def duplicate(
    actor: actors.Actor, waits_on: tuple[Relation, ...], value: str
) -> Duplicate:
    """Make a task waiting on `waits_on` a duplicate of `value`."""
    canonical = refs.Ref.parse(value)
    for relation in waits_on:
        if relation.key != str(canonical):
            continue
        if isinstance(relation, Duplicate):
            return relation
        raise errors.TaskError(f'already waiting on {canonical}')
    return Duplicate(actor=actor.name, canonical=canonical)


def unblock(
    blocked_by: tuple[TaskBlocker | ExternalBlocker, ...],
    values: Iterable[str],
) -> tuple[TaskBlocker | ExternalBlocker, ...]:
    """`blocked_by` but for blockers on `values`, by task ref or url."""
    keys = {parse_key(value) for value in values}
    for key in keys - {blocker.key for blocker in blocked_by}:
        raise errors.TaskError(f'not blocked by {key}')
    return tuple(b for b in blocked_by if b.key not in keys)


def parse_key(value: str) -> str:
    """Which blocker `value` names: a task's ref, or a url."""
    value = value.strip()
    if refs.REF_RE.fullmatch(value):
        return str(refs.Ref.parse(value))
    if URL_RE.fullmatch(value):
        return value
    raise errors.TaskError(
        f'invalid blocker: {value!r} (use a task, eg. bakery:184, or a url)'
    )


def identity(relation: Relation) -> tuple[str, str]:
    """What a relation is, but for who made it and when."""
    if isinstance(relation, TaskBlocker):
        return relation.until.value, relation.key
    if isinstance(relation, ExternalBlocker):
        return 'external', relation.key
    return 'duplicate', relation.key


def load(
    conn: sqlite3.Connection, clauses: str, params: dict[str, object]
) -> dict[Key, Relations]:
    """The relations of, and on, the tasks which `clauses` select."""
    selected = f'SELECT namespace, id FROM task{clauses}'
    rows = conn.execute(
        'SELECT namespace, task, kind, target_namespace, target, url,'
        ' condition, actor, created FROM blocker'
        f' WHERE (namespace, task) IN ({selected})'
        f' OR (target_namespace, target) IN ({selected}) ORDER BY id',
        params,
    )
    outbound: dict[Key, list[Relation]] = {}
    # Whether each is a duplicate, and the relation, by the task it is on.
    inbound: dict[Key, list[tuple[bool, Inbound]]] = {}
    for namespace, task, *columns in rows:
        relation = _relation(columns)
        outbound.setdefault((namespace, task), []).append(relation)
        if isinstance(relation, ExternalBlocker):
            continue
        target = (relation.target.namespace, relation.target.ident)
        inbound.setdefault(target, []).append(
            (
                isinstance(relation, Duplicate),
                Inbound.of(relation, refs.Ref(namespace, task)),
            )
        )
    return {
        key: _relations(outbound.get(key, []), inbound.get(key, []))
        for key in outbound.keys() | inbound.keys()
    }


def _relations(
    outbound: list[Relation], inbound: list[tuple[bool, Inbound]]
) -> Relations:
    return Relations(
        tuple(r for r in outbound if not isinstance(r, Duplicate)),
        next((r for r in outbound if isinstance(r, Duplicate)), None),
        tuple(held for duplicate, held in inbound if not duplicate),
        tuple(held for duplicate, held in inbound if duplicate),
    )


def _relation(columns: list[Any]) -> Relation:
    """A relation, from its row's columns after its task's key."""
    kind, target_namespace, target, url, condition, *stamp = columns
    fields = dict(zip(('actor', 'created'), stamp, strict=True))
    if kind == 'external':
        return ExternalBlocker(url=url, condition=condition, **fields)
    ref = refs.Ref(target_namespace, target)
    if kind == 'duplicate':
        return Duplicate(canonical=ref, **fields)
    return TaskBlocker(target=ref, until=Until(kind), **fields)


def store(
    conn: sqlite3.Connection,
    key: Key,
    blocked_by: tuple[TaskBlocker | ExternalBlocker, ...],
    duplicate_of: Duplicate | None,
) -> tuple[tuple[TaskBlocker | ExternalBlocker, ...], Duplicate | None, bool]:
    """
    Store a stored task's relations as they now are: those as the store
    stamped them, and whether any changed.
    """
    wanted = [*blocked_by, *([duplicate_of] if duplicate_of else [])]
    kept = {identity(r) for r in wanted if r.created is not None}
    changed = False
    for ident, kind, *target, url in conn.execute(
        'SELECT id, kind, target_namespace, target, url FROM blocker'
        ' WHERE namespace = ? AND task = ?',
        key,
    ).fetchall():
        if (kind, url or str(refs.Ref(*target))) not in kept:
            conn.execute('DELETE FROM blocker WHERE id = ?', (ident,))
            changed = True
    if any(r.created is None for r in wanted):
        changed = True
    stamped_blockers = tuple(_insert(conn, key, b) for b in blocked_by)
    stamped_duplicate = duplicate_of and _insert(conn, key, duplicate_of)
    return stamped_blockers, stamped_duplicate, changed


def _insert(conn: sqlite3.Connection, key: Key, relation: R) -> R:
    if relation.created is not None:
        return relation
    external = relation if isinstance(relation, ExternalBlocker) else None
    target = None if external else relation.target
    (created,) = conn.execute(
        'INSERT INTO blocker (namespace, task, kind, target_namespace,'
        ' target, url, condition, actor)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING created',
        (
            *key,
            identity(relation)[0],
            target and target.namespace,
            target and target.ident,
            external and external.url,
            external and external.condition,
            relation.actor,
        ),
    ).fetchone()
    stamped = relation.model_dump() | {'created': created}
    return type(relation).model_validate(stamped)


def reaches(conn: sqlite3.Connection, start: refs.Ref, goal: refs.Ref) -> bool:
    """Whether `start` is, or waits on what waits on, `goal`."""
    found = conn.execute(
        'WITH RECURSIVE reach (namespace, id) AS ('
        '  SELECT :start_namespace, :start_id'
        '  UNION SELECT target_namespace, target FROM blocker'
        '   JOIN reach ON blocker.namespace = reach.namespace'
        '    AND blocker.task = reach.id'
        '   WHERE target IS NOT NULL'
        ') SELECT 1 FROM reach'
        ' WHERE namespace = :goal_namespace AND id = :goal_id',
        {
            'start_namespace': start.namespace,
            'start_id': start.ident,
            'goal_namespace': goal.namespace,
            'goal_id': goal.ident,
        },
    ).fetchone()
    return found is not None
