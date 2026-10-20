import asyncio
import contextvars
import functools
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from typing import Annotated
from typing import TextIO

import typer
from typer import _click
from typer import core

from . import actors
from . import client
from . import db
from . import errors
from . import ops
from . import options
from . import query
from . import refs
from . import rules
from . import schema
from . import server

# Whoever runs the CLI directly writes the database, so is the operator; the
# server runs commands for an agent as that agent, under its rules.
WRITER = contextvars.ContextVar(
    'WRITER', default=ops.Writer(actors.OPERATOR_ACTOR)
)
# Where commands print; None for stdout. The server collects each command's.
OUTPUT: contextvars.ContextVar[TextIO | None] = contextvars.ContextVar(
    'OUTPUT', default=None
)


def writer() -> ops.Writer:
    return WRITER.get()


def echo(value: object, file: TextIO | None = None) -> None:
    print(value, file=file or OUTPUT.get() or sys.stdout)


def operator_only(command: str) -> None:
    if not writer().actor.is_operator:
        raise errors.TaskError(f'only the operator may {command}')


class SubjectGroup(core.TyperGroup):
    """
    Enable `task <id> <cmd>` by rewriting a task-led invocation.

    A leading task id (the subject) is moved to sit behind the command so
    ordinary Typer parsing applies. A bare task id means `show`.
    """

    def parse_args(self, ctx: _click.Context, args: list[str]) -> list[str]:
        if args and refs.REF_RE.fullmatch(args[0]):
            if len(args) == 1:
                args = ['show', args[0]]
            else:
                ident, cmd, *rest = args
                if cmd in self.commands:
                    args = [cmd, ident, *rest]
                else:
                    args = ['show', ident, cmd, *rest]
        return super().parse_args(ctx, args)


app = typer.Typer(cls=SubjectGroup, add_completion=False, no_args_is_help=True)
namespace_app = typer.Typer(no_args_is_help=True)
app.add_typer(namespace_app, name='namespace', help='Manage namespaces.')


@app.callback()
def main() -> None:
    """Simple personal task manager, shared by an operator and agents."""


# Per-task commands are invoked as `task <id> <cmd>`; group them under their
# own help panel so they don't read as bare top-level commands.
SUBJECT_PANEL = 'Task commands (use as: task <id> <cmd>)'


def print_tasks(tasks: Iterable[schema.Task], as_json: bool) -> None:
    if as_json:
        echo(json.dumps([t.to_json() for t in tasks], indent=2))
        return
    for task in tasks:
        echo(task)


def report(task: schema.Task | None, as_json: bool, message: str = '') -> None:
    """With --json, the task as it stands after the command; else message."""
    if as_json:
        echo(json.dumps(task.to_json() if task else None, indent=2))
    elif message:
        echo(message)


@app.command('list')
def list_(
    days: options.Days = -1,
    filter_: options.Filter = '',
    limit: options.Limit = -1,
    sort: options.Sort = query.SortOrder.tag,
    as_json: options.AsJson = False,
) -> None:
    """List all tasks."""
    print_tasks(_listed(filter_, days, limit, sort), as_json)


@app.command('due')
def due(
    days: options.Days = 0,
    filter_: options.Filter = '',
    limit: options.Limit = -1,
    sort: options.Sort = query.SortOrder.due,
    as_json: options.AsJson = False,
) -> None:
    """List scheduled tasks due within the given number of days."""
    print_tasks(_listed(f'next!=,{filter_}', days, limit, sort), as_json)


def _listed(
    filter_: str, days: int, limit: int, sort: query.SortOrder
) -> list[schema.Task]:
    return query.list_(
        filter_,
        sort=sort,
        limit=None if limit < 0 else limit,
        due_within=None if days < 0 else days,
    )


@app.command('add')
def add(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    summary: str,
    namespace: options.AddNamespace = refs.DEFAULT,
    tag: options.Tag = None,
    next_: options.Next = None,
    interval: options.Interval = None,
    shift: options.Shift = None,
    description: options.Description = None,
    link: options.Link = None,
    priority: options.PriorityOpt = None,
    size: options.SizeOpt = None,
    comment: options.Comments = None,
    question: options.Questions = None,
    blocked_by: options.BlockedBy = None,
    duplicate_of: options.DuplicateOf = None,
    as_json: options.AsJson = False,
) -> None:
    """Add a new task, optionally with schedule details."""
    task = schema.Task(
        summary=summary,
        details=None,
        namespace=schema.normalize_name('namespace', namespace),
        tag=schema.TRIAGE,
    )
    task.update(
        tag=tag,
        next_=next_,
        interval=interval,
        shift=shift,
        description=description,
        link=link,
        priority=priority,
        size=size,
    )
    task.converse(
        writer().actor, comments=comment or (), questions=question or ()
    )
    task.relate(
        writer().actor, blocked_by=blocked_by or (), duplicate_of=duplicate_of
    )
    writer().add(task)
    report(task, as_json, f'added task {task.ref}: {task.summary}')


@app.command('show', rich_help_panel=SUBJECT_PANEL)
def show(ref: options.Ref, as_json: options.AsJson = False) -> None:
    """Show a task's full details and status."""
    task = query.get(ref)
    report(task, as_json, task.render_detail())


@app.command('done', rich_help_panel=SUBJECT_PANEL)
def done(
    ref: options.Ref,
    ago: options.Ago = 0,
    force: options.Force = False,
    as_json: options.AsJson = False,
) -> None:
    """Mark a task as done: delete it, or advance it if it recurs."""
    task, next_occurrence = writer().done(ref, ago, force=force)
    if next_occurrence is None:
        report(None, as_json, f'deleted task {ref}: {task.summary}')
        return
    assert next_occurrence.details, 'recurring task has no details'
    report(
        next_occurrence,
        as_json,
        f'completed recurring task {ref},'
        f' next occurrence: {next_occurrence.details.next_}',
    )


@app.command('delay', rich_help_panel=SUBJECT_PANEL)
def delay(
    ref: options.Ref,
    days: int,
    force: options.Force = False,
    as_json: options.AsJson = False,
) -> None:
    """Delay a task by the given number of days."""
    delayed = writer().change(
        ops.Verb.delay, ref, lambda task: task.delay(days), force=force
    )
    assert delayed.details, 'delayed task has no details'
    report(
        delayed,
        as_json,
        f'delayed task {ref}, next occurrence: {delayed.details.next_}',
    )


@app.command('set', rich_help_panel=SUBJECT_PANEL)
def set_(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    ref: options.Ref,
    summary: options.Summary = None,
    namespace: options.MoveNamespace = None,
    tag: options.Tag = None,
    next_: options.Next = None,
    interval: options.Interval = None,
    shift: options.Shift = None,
    description: options.Description = None,
    link: options.Link = None,
    priority: options.PriorityOpt = None,
    size: options.SizeOpt = None,
    comment: options.Comments = None,
    question: options.Questions = None,
    answer: options.Answers = None,
    blocked_by: options.BlockedBy = None,
    duplicate_of: options.DuplicateOf = None,
    force: options.Force = False,
    as_json: options.AsJson = False,
) -> None:
    """
    Edit a task's summary, namespace, section, metadata, schedule, or what it
    waits on, or say something on it.
    """

    def edit(task: schema.Task) -> schema.Task:
        task.update(
            summary=summary,
            namespace=namespace,
            tag=tag,
            next_=next_,
            interval=interval,
            shift=shift,
            description=description,
            link=link,
            priority=priority,
            size=size,
        )
        task.converse(
            writer().actor,
            comments=comment or (),
            questions=question or (),
            answers=answer or (),
        )
        task.relate(
            writer().actor,
            blocked_by=blocked_by or (),
            duplicate_of=duplicate_of,
        )
        return task

    task = writer().change(ops.Verb.set, ref, edit, force=force)
    moved = task.namespace != ref.namespace
    report(task, as_json, f'moved to {task.ref}' if moved else '')


@app.command('describe', rich_help_panel=SUBJECT_PANEL)
def describe(
    ref: options.Ref,
    force: options.Force = False,
    as_json: options.AsJson = False,
) -> None:
    """Edit a task's description in $EDITOR."""
    operator_only('describe')
    current = query.get(ref)
    current.authorize(writer().actor, force=force)
    original = current.description

    # The editor runs without the write lock, so other writers carry on; the
    # result is only saved if nobody else changed the description meanwhile.
    with tempfile.NamedTemporaryFile(
        'w', suffix='.txt', delete=False, encoding='utf-8'
    ) as f:
        f.write(original or '')
        path = pathlib.Path(f.name)
    try:
        subprocess.run([os.environ.get('EDITOR', 'vim'), path], check=True)
        edited = path.read_text(encoding='utf-8')
    finally:
        path.unlink()

    def edit(task: schema.Task) -> schema.Task:
        if task.description != original:
            raise errors.TaskError(
                f"task {ref}'s description changed while editing"
            )
        task.update(description=edited)
        return task

    report(writer().change(ops.Verb.describe, ref, edit, force=force), as_json)


@app.command('unset', rich_help_panel=SUBJECT_PANEL)
def unset(
    ref: options.Ref,
    fields: Annotated[
        list[schema.ClearableField] | None, typer.Argument()
    ] = None,
    blocked_by: options.Cleared = None,
    force: options.Force = False,
    as_json: options.AsJson = False,
) -> None:
    """
    Clear fields from a task, or what it waits on: once it waits on nothing,
    it goes back to triage.
    """
    if not (fields or blocked_by):
        raise errors.TaskError('nothing to unset')

    def edit(task: schema.Task) -> schema.Task:
        task.clear(fields or ())
        task.relate(writer().actor, cleared=blocked_by or ())
        return task

    report(writer().change(ops.Verb.unset, ref, edit, force=force), as_json)


@app.command('claim', rich_help_panel=SUBJECT_PANEL)
def claim(
    ref: options.Ref,
    force: options.Force = False,
    as_json: options.AsJson = False,
) -> None:
    """Claim a task, so that no one else changes it."""
    report(writer().claim(ref, force=force), as_json)


@app.command('release', rich_help_panel=SUBJECT_PANEL)
def release(
    ref: options.Ref,
    force: options.Force = False,
    as_json: options.AsJson = False,
) -> None:
    """Release your claim on a task, or with --force anyone's."""
    report(writer().release(ref, force=force), as_json)


@app.command('next')
def next_task(
    days: options.Days = 0,
    filter_: options.Filter = '',
    sort: options.Sort = query.SortOrder.priority,
    as_json: options.AsJson = False,
) -> None:
    """
    Show the highest-priority unclaimed task due within the given number of
    days; an agent claims it too.
    """
    task = writer().next(filter_, days, sort)
    if as_json:
        report(task, as_json)
    else:
        echo(task or 'no task available')


@namespace_app.command('list')
def namespace_list() -> None:
    """List namespaces."""
    for namespace in db.namespaces():
        echo(namespace)


@namespace_app.command('add')
def namespace_add(namespace: options.NamespaceName) -> None:
    """Add a namespace."""
    operator_only('namespace add')
    db.add_namespace(schema.normalize_name('namespace', namespace))


@namespace_app.command('delete')
def namespace_delete(namespace: options.NamespaceName) -> None:
    """Delete an empty namespace."""
    operator_only('namespace delete')
    db.delete_namespace(schema.normalize_name('namespace', namespace))


@app.command('serve')
def serve(
    socket: Annotated[pathlib.Path, typer.Argument()],
    rules_file: Annotated[
        pathlib.Path,
        typer.Option('--rules', help="Every agent's rules, in TOML."),
    ],
    agent: Annotated[
        str, typer.Option('--agent', help='The rules to apply, by agent.')
    ],
    name: Annotated[
        str, typer.Option('--as', help='Who the agent is, as it claims.')
    ],
) -> None:
    """Serve one agent's writes on a unix socket, under its rules."""
    operator_only('serve')
    rules_ = rules.load(rules_file)
    if agent not in rules_:
        raise errors.TaskError(f'{rules_file}: no rules for {agent}')
    guarded = ops.Writer(actors.Actor.agent(name), rules_[agent].check)
    asyncio.run(server.serve_forever(socket, functools.partial(run, guarded)))


async def listen(path: pathlib.Path, writer_: ops.Writer) -> asyncio.Server:
    """Serve commands on `path`, run as `writer_`."""
    return await server.serve(path, functools.partial(run, writer_))


def run(writer_: ops.Writer, argv: list[str]) -> server.Response:
    """Run a command as `writer_`, collecting what it prints."""
    output = io.StringIO()
    writer_token = WRITER.set(writer_)
    output_token = OUTPUT.set(output)
    try:
        code = app(argv, prog_name='task', standalone_mode=False)
    except errors.TaskError as e:
        return server.Response(1, output.getvalue(), str(e))
    except _click.exceptions.ClickException as e:
        return server.Response(
            e.exit_code, output.getvalue(), e.format_message()
        )
    except typer.Abort:
        return server.Response(1, output.getvalue(), 'aborted')
    finally:
        OUTPUT.reset(output_token)
        WRITER.reset(writer_token)
    return server.Response(
        code if isinstance(code, int) else 0, output.getvalue()
    )


def cli() -> None:
    address = os.environ.get(client.ADDRESS)
    argv = sys.argv[1:]
    # Help is the same everywhere, and the server only answers commands.
    if address and argv and '--help' not in argv:
        sys.exit(client.forward(pathlib.Path(address), argv))
    try:
        app()
    except errors.TaskError as e:
        echo(f'error: {e}', file=sys.stderr)
        sys.exit(1)
