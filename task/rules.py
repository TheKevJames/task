"""
Rules: what an agent may create, claim, and change. Whatever no rule grants
is denied.

Rules judge a change by comparing the task before and after it, whichever
command made it, so a grant means the same through every interface:

- create: the namespaces and tags a new task may have, the fields it must
  have, and the others it may have;
- claim: the namespaces and tags of the tasks an agent may claim; a task it
  creates is its own by the create rule instead;
- change: the fields an agent may change on a task it holds, the tags it may
  move one to, and whether it may complete one. Moving a task needs the
  `namespace` field, and lands it in a namespace the agent may claim in.
  `description_append` grants adding paragraphs to a description, but not
  changing what it already says.

In TOML, one table per agent:

    [triage.create]
    namespaces = ["bakery"]
    tags = ["triage", "build"]
    require = ["link"]
    fields = ["description", "priority", "size"]

    [triage.claim]
    namespaces = ["bakery"]
    tags = ["triage"]

    [triage.change]
    fields = ["description_append", "priority", "size"]
    tags = ["build"]
    done = false
"""

import dataclasses
import enum
import pathlib
import tomllib
from collections.abc import Mapping

from . import ops
from . import schema


class Field(enum.StrEnum):
    namespace = 'namespace'
    tag = 'tag'
    summary = 'summary'
    description = 'description'
    description_append = 'description_append'
    link = 'link'
    priority = 'priority'
    size = 'size'
    next = 'next'
    interval = 'interval'
    shift = 'shift'


# The task attributes holding each field; namespaces and tags are scoped
# instead, and a summary is always required.
ATTRIBUTES = {
    Field.description: 'description',
    Field.link: 'link',
    Field.priority: 'priority',
    Field.size: 'size',
    Field.next: 'next_',
    Field.interval: 'interval',
    Field.shift: 'shift',
}


@dataclasses.dataclass(frozen=True)
class Scope:
    namespaces: frozenset[str]
    tags: frozenset[str]

    def holds(self, task: schema.Task) -> bool:
        return task.namespace in self.namespaces and task.tag in self.tags


@dataclasses.dataclass(frozen=True)
class Create:
    scope: Scope
    require: frozenset[Field]
    fields: frozenset[Field]


@dataclasses.dataclass(frozen=True)
class Change:
    fields: frozenset[Field]
    tags: frozenset[str]
    done: bool


@dataclasses.dataclass(frozen=True)
class Rules:
    create: Create | None = None
    claim: Scope | None = None
    change: Change | None = None

    def check(self, change: ops.Change) -> None:
        """A guard: refuses whatever these rules do not grant."""
        before, after = change.before, change.after
        if before is None:
            assert after is not None, 'a change creates or keeps a task'
            self._check_create(after)
            return
        if change.verb is ops.Verb.done:
            if not (self.change and self.change.done):
                raise schema.TaskError(f'you may not complete {before.ref}')
        elif after is None:
            raise schema.TaskError(f'you may not delete {before.ref}')
        # Changing a task claims it first, even one the change deletes.
        if before.claim != change.actor.name and (
            after is None or after.claim == change.actor.name
        ):
            self._check_claim(before)
        if after is not None and change.verb is not ops.Verb.done:
            self._check_change(before, after)

    def _check_create(self, task: schema.Task) -> None:
        if self.create is None or not self.create.scope.holds(task):
            raise schema.TaskError(
                f'you may not create tasks in {_section(task)}'
            )
        for field in self.create.require:
            if _value(task, field) is None:
                raise schema.TaskError(f'a new task needs its {field}')
        allowed = self.create.fields | self.create.require
        for field in ATTRIBUTES.keys() - allowed:
            if _value(task, field) not in {None, False}:
                raise schema.TaskError(f'you may not set {field}')

    def _check_claim(self, task: schema.Task) -> None:
        if self.claim is None or not self.claim.holds(task):
            raise schema.TaskError(
                f'you may not claim tasks in {_section(task)}'
            )

    def _check_change(self, before: schema.Task, after: schema.Task) -> None:
        granted = self.change.fields if self.change else frozenset()
        for field in _changed(before, after):
            if field is Field.tag:
                if not (self.change and after.tag in self.change.tags):
                    raise schema.TaskError(
                        f'you may not move tasks to {after.tag}'
                    )
            elif field is Field.namespace:
                scope = self.claim
                if Field.namespace not in granted or not (
                    scope and after.namespace in scope.namespaces
                ):
                    raise schema.TaskError(
                        f'you may not move tasks to {after.namespace}'
                    )
            elif field is Field.description:
                if Field.description not in granted and not (
                    Field.description_append in granted
                    and _appended(before.description, after.description)
                ):
                    raise schema.TaskError('you may not change description')
            elif field not in granted:
                raise schema.TaskError(f'you may not change {field}')


def _section(task: schema.Task) -> str:
    return f'{task.namespace} > {task.tag}'


def _value(task: schema.Task, field: Field) -> object:
    if field is Field.namespace:
        return task.namespace
    if field is Field.tag:
        return task.tag
    if field is Field.summary:
        return task.summary
    if field in {Field.next, Field.interval, Field.shift}:
        details = task.details
        if details is None:
            return None
        return getattr(details, ATTRIBUTES[field])
    return getattr(task, ATTRIBUTES[field])


def _changed(before: schema.Task, after: schema.Task) -> list[Field]:
    return [
        field
        for field in Field
        if field is not Field.description_append
        and _value(before, field) != _value(after, field)
    ]


def _appended(before: str | None, after: str | None) -> bool:
    if after is None:
        return before is None
    return before is None or after.startswith(f'{before}\n\n')


def parse(data: Mapping[str, object]) -> dict[str, Rules]:
    """Each agent's rules, from one table per agent."""
    return {agent: _rules(agent, table) for agent, table in data.items()}


def load(path: pathlib.Path) -> dict[str, Rules]:
    try:
        return parse(tomllib.loads(path.read_text(encoding='utf-8')))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise schema.TaskError(f'{path}: {e}') from None


class _Table:
    """A TOML table, read strictly: unknown keys and wrong types fail."""

    def __init__(self, data: object, where: str) -> None:
        if not isinstance(data, dict):
            raise schema.TaskError(f'{where}: must be a table')
        self.data: dict[str, object] = data
        self.where = where
        self.used: set[str] = set()

    def has(self, key: str) -> bool:
        return key in self.data

    def table(self, key: str) -> '_Table':
        self.used.add(key)
        return _Table(self.data[key], f'{self.where}.{key}')

    def names(self, key: str, *, normalize: bool = True) -> frozenset[str]:
        self.used.add(key)
        value = self.data.get(key, [])
        strings: list[str] = (
            [v for v in value if isinstance(v, str)]
            if isinstance(value, list)
            else []
        )
        if strings != value:
            raise schema.TaskError(
                f'{self.where}.{key}: must be a list of strings'
            )
        if not normalize:
            return frozenset(strings)
        return frozenset(
            schema.normalize_name(f'{self.where}.{key}', v) for v in strings
        )

    def fields(self, key: str) -> frozenset[Field]:
        return frozenset(
            schema.parse_enum(Field, v)
            for v in self.names(key, normalize=False)
        )

    def boolean(self, key: str) -> bool:
        self.used.add(key)
        value = self.data.get(key, False)
        if not isinstance(value, bool):
            raise schema.TaskError(f'{self.where}.{key}: must be a boolean')
        return value

    def done(self) -> None:
        unknown = set(self.data) - self.used
        if unknown:
            raise schema.TaskError(
                f'{self.where}: unknown keys {", ".join(sorted(unknown))}'
            )


def _scope(table: _Table) -> Scope:
    return Scope(table.names('namespaces'), table.names('tags'))


def _rules(agent: str, data: object) -> Rules:
    table = _Table(data, agent)
    rules = Rules()
    if table.has('create'):
        create = table.table('create')
        rules = dataclasses.replace(
            rules,
            create=Create(
                _scope(create),
                create.fields('require'),
                create.fields('fields'),
            ),
        )
        create.done()
    if table.has('claim'):
        claim = table.table('claim')
        rules = dataclasses.replace(rules, claim=_scope(claim))
        claim.done()
    if table.has('change'):
        change = table.table('change')
        rules = dataclasses.replace(
            rules,
            change=Change(
                change.fields('fields'),
                change.names('tags'),
                change.boolean('done'),
            ),
        )
        change.done()
    table.done()
    return rules
