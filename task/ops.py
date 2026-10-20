"""
Every change to the task list, made as an actor.

Each runs in a single write transaction, so a claim's compare-and-set and the
change it guards cannot interleave with another writer's. An agent claims
every task it creates or changes; the operator never claims implicitly, and
changes only tasks which are unclaimed or their own, unless forced. Saying
something on a task is not changing it: a change which only adds to a task's
conversation neither needs nor takes a claim.

Whatever is said is said by the writer's actor, and stamped by the store;
nothing already said may change, but for the operator answering a question.

A task's relations belong to it, and the store acts on it when what they
wait on happens, within the write which made it happen: it clears them,
stamping a comment as the writer's actor, then unblocks the task, or removes
it if it was a duplicate. These follow-on changes, `settle`, are made
whatever the writer's rules and whoever holds a claim (see ADR-0004).

A writer's guard sees each change before it is stored, follow-on changes
included, and refuses it by raising TaskError, refusing the whole write; it
may only allow or refuse, never alter, a change.
"""

import dataclasses
import enum
from collections.abc import Callable
from collections.abc import Sequence

from . import actors
from . import conversations
from . import db
from . import errors
from . import query
from . import refs
from . import relations
from . import schema

Edit = Callable[[schema.Task], schema.Task]


class Verb(enum.StrEnum):
    add = 'add'
    set = 'set'
    unset = 'unset'
    describe = 'describe'
    delay = 'delay'
    done = 'done'
    claim = 'claim'
    release = 'release'
    next = 'next'
    # The store acting on a task's relations, after another change.
    settle = 'settle'


@dataclasses.dataclass(frozen=True)
class Change:
    """One change, as a guard sees it before it is stored."""

    actor: actors.Actor
    verb: Verb
    # None when the change creates the task.
    before: schema.Task | None
    # None when the change deletes the task.
    after: schema.Task | None
    # Inside the change's write transaction.
    store: db.Store


Guard = Callable[[Change], None]


def allow(change: Change) -> None:
    del change


@dataclasses.dataclass(frozen=True)
class Writer:
    actor: actors.Actor
    guard: Guard = allow

    def add(self, task: schema.Task) -> None:
        if not self.actor.is_operator:
            task.claim = self.actor.name
        with db.write() as store:
            _check_relations(store, self.actor, None, task)
            self._judge(Change(self.actor, Verb.add, None, task, store))
            db.insert(store, task)

    def change(
        self, verb: Verb, ref: refs.Ref, edit: Edit, *, force: bool = False
    ) -> schema.Task:
        """
        Store `edit` of a task, which returns it as it should be stored;
        moving it to another namespace gives it a new id there.
        """
        with db.write() as store:
            before = db.get(store, ref)
            after = edit(before.model_copy(deep=True))
            if not _only_converses(before, after):
                # Authorized as it was, so that refusals name it as it was.
                task = before.model_copy()
                task.authorize(self.actor, force=force)
                after.claim = task.claim
            _check_relations(store, self.actor, before, after)
            self._judge(Change(self.actor, verb, before, after, store))
            if after.namespace == ref.namespace:
                db.update(store, after)
            else:
                db.move(store, after, ref)
            self._repoint(store, before, after)
            removed = _removed(before, after)
            if removed:
                sort = after.tag != before.tag
                self._clear(store, db.get(store, after.ref), removed, sort)
            after = db.get(store, after.ref)
            self._settle(store, after, done=False)
        return after

    def done(
        self, ref: refs.Ref, ago: int, *, force: bool = False
    ) -> tuple[schema.Task, schema.Task | None]:
        """The task, and its next occurrence; None if it was deleted."""
        with db.write() as store:
            before = db.get(store, ref)
            task = before.model_copy(deep=True)
            task.authorize(self.actor, force=force)
            after = task.done(ago)
            self._judge(Change(self.actor, Verb.done, before, after, store))
            if after is None:
                self._settle(store, before, done=True)
                db.delete(store, ref)
            else:
                db.update(store, after)
                after = db.get(store, ref)
                self._settle(store, after, done=True)
        return task, after

    def claim(self, ref: refs.Ref, *, force: bool = False) -> schema.Task:
        def edit(task: schema.Task) -> schema.Task:
            task.claim_as(self.actor, force=force)
            return task

        return self._set_claim(Verb.claim, ref, edit)

    def release(self, ref: refs.Ref, *, force: bool = False) -> schema.Task:
        def edit(task: schema.Task) -> schema.Task:
            task.release_as(self.actor, force=force)
            return task

        return self._set_claim(Verb.release, ref, edit)

    def next(
        self, filter_: str, days: int, sort: query.SortOrder
    ) -> schema.Task | None:
        """
        The first unclaimed task matching `filter_` which an agent's guard
        lets it claim, and claims; None if there is none. The operator only
        sees it.
        """
        if self.actor.is_operator:
            clauses = query.compile_(filter_, days, 1, sort, unclaimed=True)
            with db.connect() as store:
                return next(iter(db.select(store, *clauses)), None)

        clauses = query.compile_(filter_, days, -1, sort, unclaimed=True)
        with db.write() as store:
            for before in db.select(store, *clauses):
                after = before.model_copy(deep=True)
                after.claim_as(self.actor)
                change = Change(self.actor, Verb.next, before, after, store)
                try:
                    self._judge(change)
                except errors.TaskError:
                    continue
                db.update(store, after)
                return after
        return None

    def _set_claim(self, verb: Verb, ref: refs.Ref, edit: Edit) -> schema.Task:
        with db.write() as store:
            before = db.get(store, ref)
            after = edit(before.model_copy(deep=True))
            self._judge(Change(self.actor, verb, before, after, store))
            db.update(store, after)
        return after

    def _settle(
        self, store: db.Store, target: schema.Task, *, done: bool
    ) -> None:
        """
        Clear what waits on `target`, as stored, once its occurrence is marked
        as done, or whatever else made it decided.
        """
        if not (done or target.decided):
            return
        waiting = [
            i.task
            for i in target.blocks
            if done or i.until is relations.Until.decided
        ]
        if done:
            waiting.extend(i.task for i in target.duplicates)
        for source in dict.fromkeys(waiting):
            before = db.get(store, source)
            duplicate = before.duplicate_of
            if done and duplicate and duplicate.canonical == target.ref:
                self._judge(
                    Change(self.actor, Verb.settle, before, None, store)
                )
                self._settle(store, before, done=True)
                db.delete(store, source)
                continue
            cleared = [
                blocker
                for blocker in before.blocked_by
                if isinstance(blocker, relations.TaskBlocker)
                and blocker.target == target.ref
                and (done or blocker.until is relations.Until.decided)
            ]
            self._clear(store, before, cleared, sort=False)

    def _clear(
        self,
        store: db.Store,
        before: schema.Task,
        cleared: Sequence[relations.Relation],
        sort: bool,
    ) -> None:
        """
        Clear relations `before`, as stored, waits on, if it still does, and
        say so; unblock it once it waits on nothing, unless it was sorted.
        """
        gone = {relations.identity(relation) for relation in cleared}
        after = before.model_copy(deep=True)
        after.blocked_by = tuple(
            b for b in after.blocked_by if relations.identity(b) not in gone
        )
        if (
            after.duplicate_of
            and relations.identity(after.duplicate_of) in gone
        ):
            after.duplicate_of = None
        after.converse(
            self.actor,
            comments=[
                '\n'.join(f'Cleared: {relation}' for relation in cleared)
            ],
        )
        if not (after.waits_on or sort):
            after.tag = schema.TRIAGE
        self._judge(Change(self.actor, Verb.settle, before, after, store))
        db.update(store, after)

    def _repoint(
        self, store: db.Store, before: schema.Task, after: schema.Task
    ) -> None:
        """A canonical made a duplicate hands its duplicates on."""
        canonical = after.duplicate_of
        if canonical is None or canonical == before.duplicate_of:
            return
        for inbound in before.duplicates:
            was = db.get(store, inbound.task)
            now = was.model_copy(deep=True)
            now.duplicate_of = relations.Duplicate(
                actor=self.actor.name, canonical=canonical.canonical
            )
            self._judge(Change(self.actor, Verb.settle, was, now, store))
            db.update(store, now)

    def _judge(self, change: Change) -> None:
        if change.after is not None:
            said = (
                change.before.conversation
                if change.before
                else conversations.Conversation()
            )
            change.after.conversation.continues(said, self.actor)
        self.guard(change)


def _only_converses(before: schema.Task, after: schema.Task) -> bool:
    return (
        after.conversation != before.conversation
        and after.model_copy(update={'conversation': before.conversation})
        == before
    )


def _removed(
    before: schema.Task, after: schema.Task
) -> list[relations.Relation]:
    """What `before` waited on which `after` no longer does."""
    kept = {relations.identity(relation) for relation in after.waits_on}
    return [r for r in before.waits_on if relations.identity(r) not in kept]


def _check_relations(
    store: db.Store,
    actor: actors.Actor,
    before: schema.Task | None,
    after: schema.Task,
) -> None:
    """
    Refuse relations `after` gains which could never clear, or which would
    make a cycle; and any it keeps which are not as stored.
    """
    stored = (
        {relations.identity(r): r for r in before.waits_on} if before else {}
    )
    for relation in after.waits_on:
        if relation.created is not None:
            if stored.get(relations.identity(relation)) != relation:
                raise errors.TaskError(
                    'relations cannot be changed, only made or cleared'
                )
            continue
        if relation.actor != actor.name:
            raise errors.TaskError(
                f'{actor.name} may only relate tasks as themselves, and now'
            )
        if not isinstance(relation, relations.ExternalBlocker):
            _check_target(store, before, relation)
    if after.duplicate_of and after.details and after.details.interval:
        raise errors.TaskError('a recurring task cannot be a duplicate')


def _check_target(
    store: db.Store,
    before: schema.Task | None,
    relation: relations.TaskBlocker | relations.Duplicate,
) -> None:
    ref = relation.target
    target = db.get(store, ref)
    # A new task has nothing waiting on it, so cannot close a cycle.
    sources = [before.ref] if before else []
    if isinstance(relation, relations.Duplicate):
        if target.duplicate_of:
            canonical = target.duplicate_of.canonical
            raise errors.TaskError(
                f'{ref} is itself a duplicate of {canonical}'
            )
        if before:
            # Its duplicates become the new canonical's.
            sources.extend(inbound.task for inbound in before.duplicates)
    elif relation.until is relations.Until.decided and target.decided:
        raise errors.TaskError(f'{ref} is already decided')
    if any(relations.reaches(store.conn, ref, source) for source in sources):
        raise errors.TaskError(f'waiting on {ref} would make a cycle')
