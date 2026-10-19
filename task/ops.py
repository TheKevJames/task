"""
Every change to the task list, made as an actor.

Each runs in a single write transaction, so a claim's compare-and-set and the
change it guards cannot interleave with another writer's. An agent claims
every task it creates or changes; the operator never claims implicitly, and
changes only tasks which are unclaimed or their own, unless forced.
"""

from collections.abc import Callable

from . import db
from . import query
from . import schema

Edit = Callable[[schema.Task], schema.Task]


def add(actor: schema.Actor, task: schema.Task) -> None:
    if not actor.is_operator:
        task.claim = actor.name
    with db.write(task.namespace) as store:
        db.insert(store, task)


def change(
    actor: schema.Actor,
    ref: schema.Ref,
    edit: Edit,
    *,
    force: bool = False,
    target: str | None = None,
) -> schema.Task:
    """
    Store `edit` of a task, which returns it as it should be stored; moving it
    to another namespace, `target`, gives it a new id there.
    """
    with db.write(ref.namespace, *filter(None, [target])) as store:
        task = db.get(store, ref)
        task.authorize(actor, force=force)
        changed = edit(task)
        assert changed.namespace in store.schemas, 'moved to unlocked target'
        if changed.namespace == ref.namespace:
            db.update(store, changed)
        else:
            db.move(store, changed, ref)
    return changed


def done(
    actor: schema.Actor, ref: schema.Ref, ago: int, *, force: bool = False
) -> tuple[schema.Task, schema.Task | None]:
    """The task, and its next occurrence; None if it was deleted."""
    with db.write(ref.namespace) as store:
        task = db.get(store, ref)
        task.authorize(actor, force=force)
        completed = task.complete(ago)
        if completed is None:
            db.delete(store, ref)
        else:
            db.update(store, completed)
    return task, completed


def claim(
    actor: schema.Actor, ref: schema.Ref, *, force: bool = False
) -> schema.Task:
    def edit(task: schema.Task) -> None:
        task.claim_as(actor, force=force)

    return _set_claim(ref, edit)


def release(
    actor: schema.Actor, ref: schema.Ref, *, force: bool = False
) -> schema.Task:
    def edit(task: schema.Task) -> None:
        task.release_as(actor, force=force)

    return _set_claim(ref, edit)


def next_(
    actor: schema.Actor, filter_: str, days: int, sort: query.SortOrder
) -> schema.Task | None:
    """
    The first unclaimed task matching `filter_`, which an agent claims; None
    if there is none.
    """
    clauses = query.compile_(filter_, days, 1, sort, unclaimed=True)
    if actor.is_operator:
        with db.connect() as store:
            return next(iter(db.select(store, *clauses)), None)

    namespaces = db.namespaces()
    if not namespaces:
        return None
    # Any namespace may hold the first match, so every one is locked while it
    # is found and claimed.
    with db.write(*namespaces) as store:
        tasks = db.select(store, *clauses)
        if not tasks:
            return None
        [task] = tasks
        task.claim_as(actor)
        db.update(store, task)
    return task


def _set_claim(
    ref: schema.Ref, edit: Callable[[schema.Task], None]
) -> schema.Task:
    with db.write(ref.namespace) as store:
        task = db.get(store, ref)
        edit(task)
        db.update(store, task)
    return task
