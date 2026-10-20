"""Arguments and options shared by the CLI's commands."""

from typing import Annotated

import typer

from . import query
from . import refs
from . import schema

Ago = Annotated[int, typer.Option('-a', '--ago')]
Days = Annotated[int, typer.Option('-d', '--days')]
Filter = Annotated[
    str,
    typer.Option(
        '-f', '--filter', help='namespace=foo,tag=bar,summary!~bq,link^=https:'
    ),
]
Limit = Annotated[int, typer.Option('-l', '--limit')]
Sort = Annotated[query.SortOrder, typer.Option('-s', '--sort')]
AsJson = Annotated[bool, typer.Option('--json', help='Machine-readable.')]

Ref = Annotated[
    refs.Ref,
    typer.Argument(metavar='ID', parser=refs.Ref.parse, help='[namespace:]id'),
]
NamespaceName = Annotated[str, typer.Argument(metavar='NAMESPACE')]

# Shared detail flags: `add` and `set` funnel these into Task.update, keeping a
# single source of truth for task modifications.
Summary = Annotated[str | None, typer.Option('--summary')]
AddNamespace = Annotated[str, typer.Option('-n', '--namespace')]
MoveNamespace = Annotated[
    str | None,
    typer.Option(
        '-n',
        '--namespace',
        help='Move the task here; it gets a new id, and keeps its claim.',
    ),
]
Tag = Annotated[str | None, typer.Option('--tag', help='Section, eg. build')]
Next = Annotated[str | None, typer.Option('--next')]
Interval = Annotated[
    str | None,
    typer.Option(
        '--interval',
        help=(
            'Repeat every N days, weeks, or months, eg. 3d, 2w, 1m. Months'
            ' past the end of a shorter month land on its last day, and stay'
            ' there: Jan 31, Feb 28, Mar 28.'
        ),
    ),
]
Shift = Annotated[bool | None, typer.Option('--shift/--no-shift')]
Description = Annotated[str | None, typer.Option('--description')]
# Said as whoever runs the command, and stamped with when; all repeatable.
Said = list[str] | None
Comments = Annotated[
    Said, typer.Option('--comment', help='Say something on the task.')
]
Questions = Annotated[
    Said, typer.Option('--question', help='Ask the operator.')
]
Answers = Annotated[
    Said, typer.Option('--answer', help='Operator only, eg. "Q1: yes".')
]
# Relations: what the task waits on, made as whoever runs the command.
BlockedBy = Annotated[
    list[str] | None,
    typer.Option(
        '--blocked-by',
        help=(
            'Wait on a task being done (eg. bakery:184) or decided'
            ' (bakery:184:decided), or on a condition outside the task list'
            ' ("https://...: <condition>"); repeatable.'
        ),
    ),
]
DuplicateOf = Annotated[
    str | None,
    typer.Option(
        '--duplicate-of',
        help='The task for the same work; this one goes once it is done.',
    ),
]
Link = Annotated[str | None, typer.Option('--link')]
PriorityOpt = Annotated[schema.Priority | None, typer.Option('--priority')]
SizeOpt = Annotated[schema.Size | None, typer.Option('--size')]
Force = Annotated[
    bool, typer.Option('--force', help='Even if someone else has claimed it.')
]
Cleared = Annotated[
    list[str] | None,
    typer.Option(
        '--blocked-by',
        help=(
            'Stop waiting on a task (eg. bakery:184) or url, as if it had'
            ' cleared; repeatable.'
        ),
    ),
]
