# All namespaces share one database

Every namespace lives in one SQLite database, `$TASK_FOLDER/tasks.sqlite`,
descriptions included (see ADR-0005). Rules, not
filesystem permissions, decide which namespaces an agent may write (see
ADR-0001), so separate databases bought nothing but the machinery to join and
lock them.

## Considered Options

- **One database per namespace**, so that a sandbox could be granted write
  access to some namespaces' directories only. Rejected: agents have no write
  access to `$TASK_FOLDER` at all, and nothing needs to deny reading a
  namespace.

## Consequences

- Every write locks the whole task list, not just the namespaces it touches.
- Changes made to different namespaces on two devices while offline now
  conflict in Syncthing, rather than syncing as separate files.
- Ids stay unique only within a namespace, so the database counts each
  namespace's ids itself.
