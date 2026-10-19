# Task

A personal task list shared by an operator and the agents working alongside
them, where the rules on what each agent may change hold whatever tool it
uses.

## Language

**Task**:
One entry in the task list, addressed by its namespace and id.
_Avoid_: ticket, issue, item

**Namespace**:
A separately-granted part of the task list; a task's id is unique only
within its namespace.

**Tag**:
A flat name placing a task in a section of its namespace.
_Avoid_: label, status

**Actor**:
Whoever makes a change to the task list, as known to the task list rather
than as claimed by the caller.
_Avoid_: user, caller, client

**Operator**:
The actor who changes the task list directly, under no rules.
_Avoid_: owner, admin, user

**Agent**:
An actor whose every change goes through the task list's rules for it.
_Avoid_: bot, restricted actor, claw

**Rule**:
A grant of what an agent may create, claim, or change; whatever no rule
grants is denied.
_Avoid_: permission, policy

**Claim**:
An actor's exclusive hold on a task. An agent claims every task it creates or
changes, may claim only what its rules allow, and holds its claims only while
it is working; the operator changes only tasks no one else has claimed,
unless they force it.
_Avoid_: owner, claimant, assignee, lease, lock
