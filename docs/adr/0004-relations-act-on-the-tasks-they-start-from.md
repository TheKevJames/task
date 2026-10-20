# Relations act on the tasks they start from

A relation belongs to the task it starts from, and the store acts on that
task when the relation's target changes: once a blocked task's last blocker
clears, the store moves it to triage and stamps a comment saying what
cleared; once a canonical's current occurrence is marked as done, the store
removes its duplicates. These follow-on changes are part of the write which
triggered them, made whatever the triggering actor's rules and whoever holds
a claim on the task: the task opted in when its relation was made, by an
actor allowed to change it then.

## Considered Options

- **Refuse the triggering write** when its actor may not change the related
  tasks. Rejected: an agent marking its own task as done would fail because
  of a relation someone else made, which it cannot see or remove.
- **Leave cleared blockers in place**, for the embedder to find and route
  its unblocked tasks. Rejected: every embedder would rebuild the same
  routing, and triage is already the task list's own tag for unsorted tasks.
- **Return an unblocked task to its tag from before it was blocked.**
  Rejected: what it waited on changed, so it is sorted again.

## Consequences

- Embedders' guards see the follow-on changes as part of the triggering
  write; rejecting one rejects the whole write.
- Clearing is permanent: a target becoming undecided again re-blocks nothing.
- Removing a blocker clears it, so removing the last one unblocks the task
  unless the same write sets its tag. Removing a duplicate's relation
  unblocks it the same way: only its canonical being done removes it.
- A canonical made a duplicate hands its duplicates on to its own
  canonical, in the same write, so canonicals are never duplicates.
- Relations refuse cycles when made, since a cycle's tasks would never
  unblock.
