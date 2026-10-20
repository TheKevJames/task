"""
Every change to the task list, made as an actor.

Each runs in a single write transaction, so a claim's compare-and-set and the
change it guards cannot interleave with another writer's. An agent claims
every task it creates or changes; the operator never claims implicitly, and
changes only tasks which are unclaimed or their own, unless forced.

A writer's guard sees each change before it is stored, and refuses it by
raising TaskError; it may only allow or refuse, never alter, a change.
"""

import dataclasses
import enum
from collections.abc import Callable

from . import db
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

    actor: schema.Actor
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
    actor: schema.Actor
    guard: Guard = allow

    def add(self, task: schema.Task) -> None:
        if not self.actor.is_operator:
            task.claim = self.actor.name
        with db.write() as store:
            self.guard(Change(self.actor, Verb.add, None, task, store))
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
            task = before.model_copy(deep=True)
            task.authorize(self.actor, force=force)
            after = edit(task)
            self.guard(Change(self.actor, verb, before, after, store))
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
            self.guard(Change(self.actor, Verb.done, before, after, store))
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
                    self.guard(change)
                except schema.TaskError:
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
            self.guard(Change(self.actor, verb, before, after, store))
            db.update(store, after)
        return after
