# Task

A personal task list shared by an operator and the agents working alongside
them, where the rules on what each agent may change hold whatever tool it
uses.

## Language

### Tasks

**Task**:
One entry in the task list, addressed by its namespace and id.
_Avoid_: ticket, issue, item

**Namespace**:
A separately-granted part of the task list; a task's id is unique only
within its namespace. The `default` namespace always exists, and its tasks
are addressed by id alone.

**Tag**:
A flat name placing a task in a section of its namespace.
_Avoid_: label, status

**Triage**:
The tag of tasks not yet sorted under any other; a new task's tag unless it
is given one.
_Avoid_: inbox, backlog

### Scheduling

**Due date**:
The day a task should be done by.
_Avoid_: next, deadline

**Scheduled task**:
A task with a due date.

**One-off task**:
A task which is gone once marked as done.
_Avoid_: single, non-recurring

**Recurring task**:
A scheduled task which, once marked as done, comes back with a later due
date.
_Avoid_: periodic, repeating

**Occurrence**:
One round of a recurring task, ending when it is marked as done.

**Fixed recurrence**:
Recurrence counted from each due date, whenever the task is marked as done.
_Avoid_: unshifted, since last deadline

**Shifting recurrence**:
Recurrence counted from the day the task is marked as done.
_Avoid_: after completion

**Mark as Done**:
End a task's current occurrence: a one-off task is then gone, and a recurring
task's next occurrence begins.
_Avoid_: complete, finish, close

**Delay**:
Give a task a later due date, for its current occurrence only.
_Avoid_: postpone, snooze, defer

### Actors

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
changes, and may claim only what its rules allow; it may release its claims
whenever it likes, and loses them all when it stops working. The operator
changes only tasks no one else has claimed, unless they force it. Saying
something on a task is not changing it, so needs no claim.
_Avoid_: owner, claimant, assignee, lease, lock

### Conversation

**Comment**:
Something an actor says on a task, stamped by the task list with who said it
and when; it can never be changed or withdrawn.
_Avoid_: note, remark

**Question**:
A request to the operator made on a task, numbered within it (`Q1`, `Q2`,
...) and stamped like a comment.
_Avoid_: ask, query

**Answer**:
The operator's one reply to a question, stamped like a comment; only the
operator answers.
_Avoid_: reply, response

**Open question**:
A question with no answer yet.
_Avoid_: pending question, unanswered
