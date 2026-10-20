# Descriptions live in the database

A task's description is a column of its row, not a `<namespace>/<id>.txt`
file beside the database. As files, descriptions were edited by hand behind
claims, bumped no `updated`, and were written before their write committed:
a write which failed afterwards (a guard rejecting a follow-on change, a
trigger, the commit itself) rolled back the row but kept the new text. In
the database, a description is changed only by a write, which holds it to
every rule the rest of the task is held to, all or nothing.

## Considered Options

- **Keep the files, written after committing.** Rejected: a crash between
  the two still splits them, and hand edits still skip claims and `updated`.
- **A table of their own**, keyed by task. Rejected: a description is one
  value changed like any other part of the task, and keeping its history is
  not wanted.

## Consequences

- Descriptions are no longer edited by hand: `task describe` edits one in
  `$EDITOR`, saving it only if nobody changed it meanwhile.
- Editing descriptions on two devices while offline conflicts in Syncthing
  as any other change to the database does (see ADR-0002), rather than
  syncing as separate files.
- Descriptions can be filtered on like any other field.
- Stores from before 9.4 have their description files moved in, and orphans
  swept, by a migration run in Python; it goes when the 8.x import does,
  since both only serve stores that have not upgraded since.
