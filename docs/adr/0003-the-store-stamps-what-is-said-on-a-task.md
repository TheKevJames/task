# The store stamps what is said on a task

Actors say things on a task as comments, questions to the operator, and the
operator's answers, kept as rows of their own beside the task. Whoever says
something gives only what they said: the store records which actor said it,
as the writer knows them (see ADR-0001), and when, by its own clock. Nothing
said is ever changed or withdrawn; an answer is a row of its own, so no row
is ever updated. These replace appending to a task's description.

## Considered Options

- **Appending to the description**, as `--description-append` did. Rejected:
  the description is a plain file anyone writing it may rewrite, so an agent
  could put words in someone else's mouth, or date them as it liked; and
  appends from two writers depend on the order they land in.
- **Plain files beside the descriptions.** Rejected for the same reason: they
  are edited by hand.
- **Triggers refusing any update or delete of an entry.** Rejected: they only
  guard against bugs in other writers, which have no reason to touch entries,
  and would get in the way of entries following their task when it moves or
  is deleted.

## Consequences

- Entries refer to their task by its key, so they move with it (its key
  changes in place) and go when it does, by `ON UPDATE CASCADE` and
  `ON DELETE CASCADE`. Migrations run with foreign keys off, so rebuilding a
  table does not cascade, and are refused if they leave a foreign key broken.
- Saying something on a task is not changing it: it neither needs nor takes a
  claim, and rules grant it by the tasks' namespaces and tags.
- Only the operator answers questions, and each question at most once.
- The operator can still edit the database by hand; the stamps hold against
  agents, which have no write access to it.
- A migration may normalize how a stamp spells its actor's name (eg. to
  lowercase), but never changes which actor it names.
