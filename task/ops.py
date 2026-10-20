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

A writer's guard sees each change before it is stored, and refuses it by
raising TaskError; it may only allow or refuse, never alter, a change.
"""

import dataclasses
import enum
from collections.abc import Callable

from . import actors
from . import conversations
from . import db
from . import errors
from . import query
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
            self._judge(Change(self.actor, Verb.add, None, task, store))
            db.insert(store, task)

    def change(
        self, verb: Verb, ref: schema.Ref, edit: Edit, *, force: bool = False
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
            self._judge(Change(self.actor, verb, before, after, store))
            if after.namespace == ref.namespace:
                db.update(store, after)
            else:
                db.move(store, after, ref)
        return after

    def done(
        self, ref: schema.Ref, ago: int, *, force: bool = False
    ) -> tuple[schema.Task, schema.Task | None]:
        """The task, and its next occurrence; None if it was deleted."""
        with db.write() as store:
            before = db.get(store, ref)
            task = before.model_copy(deep=True)
            task.authorize(self.actor, force=force)
            after = task.done(ago)
            self._judge(Change(self.actor, Verb.done, before, after, store))
            if after is None:
                db.delete(store, ref)
            else:
                db.update(store, after)
        return task, after

    def claim(self, ref: schema.Ref, *, force: bool = False) -> schema.Task:
        def edit(task: schema.Task) -> schema.Task:
            task.claim_as(self.actor, force=force)
            return task

        return self._set_claim(Verb.claim, ref, edit)

    def release(self, ref: schema.Ref, *, force: bool = False) -> schema.Task:
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

    def _set_claim(
        self, verb: Verb, ref: schema.Ref, edit: Edit
    ) -> schema.Task:
        with db.write() as store:
            before = db.get(store, ref)
            after = edit(before.model_copy(deep=True))
            self._judge(Change(self.actor, verb, before, after, store))
            db.update(store, after)
        return after

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
