"""
The task list, as a library: everything outside this package may use, and
nothing else. Reads see the store as it is; writes go through a `Writer`, as
an actor, and a program which knows when its agents start and stop serves
each one's commands with `listen`.
"""

from . import actors
from . import cli
from . import client
from . import conversations
from . import errors
from . import ops
from . import query
from . import refs
from . import relations
from . import rules
from . import schema

__all__ = [
    'RULES_SECTIONS',
    'SERVER_ENV',
    'Actor',
    'Change',
    'Duplicate',
    'ExternalBlocker',
    'Guard',
    'Priority',
    'Question',
    'Ref',
    'Rules',
    'Size',
    'SortOrder',
    'Task',
    'TaskBlocker',
    'TaskError',
    'Until',
    'Verb',
    'Writer',
    'get',
    'list_',
    'listen',
    'parse_rules',
]

Actor = actors.Actor
Change = ops.Change
Duplicate = relations.Duplicate
ExternalBlocker = relations.ExternalBlocker
Guard = ops.Guard
Priority = schema.Priority
Question = conversations.Question
Ref = refs.Ref
Rules = rules.Rules
Size = schema.Size
SortOrder = query.SortOrder
Task = schema.Task
TaskBlocker = relations.TaskBlocker
TaskError = errors.TaskError
Until = relations.Until
Verb = ops.Verb
Writer = ops.Writer

get = query.get
list_ = query.list_
listen = cli.listen
parse_rules = rules.parse
# The table of a rules file holding every section's requirements.
RULES_SECTIONS = rules.SECTIONS
# The environment variable pointing an agent's CLI at its server.
SERVER_ENV = client.ADDRESS
