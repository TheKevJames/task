"""
Rules: what an agent may create, claim, change, and say on tasks. Whatever
no rule grants is denied; only the operator answers questions.

Rules judge a change by comparing the task before and after it, whichever
command made it, so a grant means the same through every interface:

- create: the namespaces and tags a new task may have, the fields it must
  have, and the others it may have;
- claim: the namespaces and tags of the tasks an agent may claim; a task it
  creates is its own by the create rule instead;
- change: the fields an agent may change on a task it holds, the tags it may
  move one to, and whether it may mark one as done. Moving a task needs the
  `namespace` field, and lands it in a namespace the agent may claim in.
  `blocked_by` and `duplicate_of` grant making and clearing a task's
  relations, which refer to any task;
- comment, question: the namespaces and tags of the tasks an agent may
  comment on, and ask the operator questions on, whoever holds them. Saying
  something on a task neither needs nor takes a claim.

What the store does to tasks because of their relations, after an agent's
change, is not the agent's to be granted (see ADR-0004).

Requirements hold for every agent alike: the fields a task must have to be in
a section. A change bringing a task into a section, by creating it there or
moving it there, must give it all of them; a change to a task already there
may not take away any it has. No agent may be granted creating tasks in a
section without setting what the section requires. The store's own follow-on
changes are held to none (see ADR-0004).

In TOML, one table per agent, and requirements under `sections`, by namespace
and tag:

    [triage.create]
    namespaces = ["bakery"]
    tags = ["triage", "build"]
    require = ["link"]
    fields = ["description", "priority", "size"]

    [triage.claim]
    namespaces = ["bakery"]
    tags = ["triage"]

    [triage.change]
    fields = ["priority", "size"]
    tags = ["build"]
    done = false

    [triage.comment]
    namespaces = ["bakery"]
    tags = ["triage", "build"]

    [triage.question]
    namespaces = ["bakery"]
    tags = ["triage"]

    [sections.bakery.build]
    require = ["priority", "size"]
"""

import dataclasses
import enum
import pathlib
import tomllib
from collections.abc import Mapping

from . import conversations
from . import errors
from . import ops
from . import schema


class Field(enum.StrEnum):
    namespace = 'namespace'
    tag = 'tag'
    summary = 'summary'
    description = 'description'
    link = 'link'
    priority = 'priority'
    size = 'size'
    next = 'next'
    interval = 'interval'
    shift = 'shift'
    blocked_by = 'blocked_by'
    duplicate_of = 'duplicate_of'


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
    Field.blocked_by: 'blocked_by',
    Field.duplicate_of: 'duplicate_of',
}
# What a task holds when a field is not set.
UNSET = (None, False, ())
# The fields a section may require: those a task may lack, but for its
# relations, which the store clears itself.
REQUIRABLE = frozenset(
    {
        Field.description,
        Field.interval,
        Field.link,
        Field.next,
        Field.priority,
        Field.size,
    }
)
SECTIONS = 'sections'


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
class Requirement:
    """The fields a task must have to be in a section."""

    namespace: str
    tag: str
    fields: frozenset[Field]

    @property
    def section(self) -> str:
        return f'{self.namespace} > {self.tag}'

    def holds(self, task: schema.Task) -> bool:
        return task.namespace == self.namespace and task.tag == self.tag

    def missing(self, task: schema.Task) -> frozenset[Field]:
        return frozenset(f for f in self.fields if _value(task, f) in UNSET)


@dataclasses.dataclass(frozen=True)
class Rules:
    create: Create | None = None
    claim: Scope | None = None
    change: Change | None = None
    comment: Scope | None = None
    question: Scope | None = None
    requirements: frozenset[Requirement] = frozenset()

    def check(self, change: ops.Change) -> None:
        """
        A guard: refuses whatever these rules do not grant, and whatever
        leaves a task short of its section's requirement.
        """
        if change.verb is ops.Verb.settle:
            return
        self._check_grants(change)
        if change.after is not None:
            self._check_requirement(change.before, change.after)

    def _check_grants(self, change: ops.Change) -> None:
        before, after = change.before, change.after
        if after is not None:
            self._check_conversation(before, after)
        if before is None:
            assert after is not None, 'a change creates or keeps a task'
            self._check_create(after)
            return
        if change.verb is ops.Verb.done:
            if not (self.change and self.change.done):
                raise errors.TaskError(
                    f'you may not mark {before.ref} as done'
                )
        elif after is None:
            raise errors.TaskError(f'you may not delete {before.ref}')
        # Changing a task claims it first, even one the change deletes.
        if before.claim != change.actor.name and (
            after is None or after.claim == change.actor.name
        ):
            self._check_claim(before)
        if after is not None and change.verb is not ops.Verb.done:
            self._check_change(before, after)

    def _check_create(self, task: schema.Task) -> None:
        if self.create is None or not self.create.scope.holds(task):
            raise errors.TaskError(
                f'you may not create tasks in {_section(task)}'
            )
        for field in self.create.require:
            if _value(task, field) in (None, ()):
                raise errors.TaskError(f'a new task needs its {field}')
        allowed = self.create.fields | self.create.require
        for field in ATTRIBUTES.keys() - allowed:
            if _value(task, field) not in UNSET:
                raise errors.TaskError(f'you may not set {field}')

    def _check_conversation(
        self, before: schema.Task | None, after: schema.Task
    ) -> None:
        said = len(before.conversation.entries) if before else 0
        added = after.conversation.entries[said:]
        grants = (
            (conversations.Comment, self.comment, 'comment on'),
            (conversations.Question, self.question, 'ask questions on'),
        )
        for kind, scope, verb in grants:
            if any(isinstance(entry, kind) for entry in added) and not (
                scope and scope.holds(after)
            ):
                raise errors.TaskError(
                    f'you may not {verb} tasks in {_section(after)}'
                )

    def _check_claim(self, task: schema.Task) -> None:
        if self.claim is None or not self.claim.holds(task):
            raise errors.TaskError(
                f'you may not claim tasks in {_section(task)}'
            )

    def _check_change(self, before: schema.Task, after: schema.Task) -> None:
        granted = self.change.fields if self.change else frozenset()
        for field in _changed(before, after):
            if field is Field.tag:
                if not (self.change and after.tag in self.change.tags):
                    raise errors.TaskError(
                        f'you may not move tasks to {after.tag}'
                    )
            elif field is Field.namespace:
                scope = self.claim
                if Field.namespace not in granted or not (
                    scope and after.namespace in scope.namespaces
                ):
                    raise errors.TaskError(
                        f'you may not move tasks to {after.namespace}'
                    )
            elif field not in granted:
                raise errors.TaskError(f'you may not change {field}')

    def _check_requirement(
        self, before: schema.Task | None, after: schema.Task
    ) -> None:
        requirement = next(
            (r for r in self.requirements if r.holds(after)), None
        )
        if requirement is None:
            return
        missing = requirement.missing(after)
        if before is not None and requirement.holds(before):
            # The operator, under no rules, may have put it there without
            # them: agents may still work on it.
            lost = missing - requirement.missing(before)
            if lost:
                raise errors.TaskError(
                    f'{before.ref} keeps its {_listed(lost)}'
                    f' in {requirement.section}'
                )
        elif missing:
            subject = 'a new task' if before is None else str(before.ref)
            raise errors.TaskError(
                f'{subject} needs its {_listed(missing)}'
                f' in {requirement.section}'
            )


def _section(task: schema.Task) -> str:
    return f'{task.namespace} > {task.tag}'


def _listed(fields: frozenset[Field]) -> str:
    """`a`, `a and b`, `a, b and c`, in the order fields are defined."""
    names = [field.value for field in Field if field in fields]
    return ' and '.join(filter(None, [', '.join(names[:-1]), names[-1]]))


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
        if _value(before, field) != _value(after, field)
    ]


def parse(data: Mapping[str, object]) -> dict[str, Rules]:
    """
    Each agent's rules, from one table per agent, each held to every
    section's requirement, from the `sections` table.
    """
    requirements = _requirements(data.get(SECTIONS, {}))
    return {
        agent: _rules(agent, table, requirements)
        for agent, table in data.items()
        if agent != SECTIONS
    }


def load(path: pathlib.Path) -> dict[str, Rules]:
    try:
        return parse(tomllib.loads(path.read_text(encoding='utf-8')))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise errors.TaskError(f'{path}: {e}') from None


class _Table:
    """A TOML table, read strictly: unknown keys and wrong types fail."""

    def __init__(self, data: object, where: str) -> None:
        if not isinstance(data, dict):
            raise errors.TaskError(f'{where}: must be a table')
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
            raise errors.TaskError(
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
            raise errors.TaskError(f'{self.where}.{key}: must be a boolean')
        return value

    def done(self) -> None:
        unknown = set(self.data) - self.used
        if unknown:
            raise errors.TaskError(
                f'{self.where}: unknown keys {", ".join(sorted(unknown))}'
            )


def _scope(table: _Table) -> Scope:
    return Scope(table.names('namespaces'), table.names('tags'))


def _requirements(data: object) -> frozenset[Requirement]:
    sections = _Table(data, SECTIONS)
    found: dict[str, Requirement] = {}
    for namespace_key in list(sections.data):
        tags = sections.table(namespace_key)
        namespace = schema.normalize_name(tags.where, namespace_key)
        for tag_key in list(tags.data):
            table = tags.table(tag_key)
            tag = schema.normalize_name(table.where, tag_key)
            requirement = Requirement(namespace, tag, table.fields('require'))
            table.done()
            if not requirement.fields <= REQUIRABLE:
                unrequirable = requirement.fields - REQUIRABLE
                raise errors.TaskError(
                    f'{table.where}.require: may not require'
                    f' {_listed(unrequirable)}'
                )
            if requirement.section in found:
                raise errors.TaskError(
                    f'{table.where}: {requirement.section} is required twice'
                )
            found[requirement.section] = requirement
    return frozenset(found.values())


def _check_creatable(
    agent: str, create: Create, requirements: frozenset[Requirement]
) -> None:
    """Refuses a grant to create where the agent cannot meet a requirement."""
    settable = create.fields | create.require
    for requirement in requirements:
        unsettable = requirement.fields - settable
        if (
            requirement.namespace in create.scope.namespaces
            and requirement.tag in create.scope.tags
            and unsettable
        ):
            raise errors.TaskError(
                f'{agent}.create: {requirement.section} requires'
                f' {_listed(unsettable)}, which it may not set'
            )


def _rules(
    agent: str, data: object, requirements: frozenset[Requirement]
) -> Rules:
    table = _Table(data, agent)
    rules = Rules(requirements=requirements)
    if table.has('create'):
        create = table.table('create')
        create_rule = Create(
            _scope(create), create.fields('require'), create.fields('fields')
        )
        create.done()
        _check_creatable(agent, create_rule, requirements)
        rules = dataclasses.replace(rules, create=create_rule)
    for key in ('claim', 'comment', 'question'):
        if table.has(key):
            scope = table.table(key)
            rules = dataclasses.replace(rules, **{key: _scope(scope)})
            scope.done()
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
