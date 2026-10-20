# Agents write through a server; the operator writes the database

Agents change the task list only through a server that applies their rules,
and have no write access to `$TASK_FOLDER` at all. Any check made inside a
process the agent itself runs (the CLI, a tool) is advice rather than a
boundary: the agent's shell could write the SQLite database directly. The
operator keeps writing the database directly, under no rules.

## Considered Options

- **Everyone, operator included, writes through the server.** Rejected:
  `$TASK_FOLDER` is synced both ways by Syncthing, so other devices write the
  database behind any server's back anyway, and the operator's own use of `task`
  would depend on a daemon being up.
- **Rules enforced in the CLI and the agents' tools.** Rejected: not a
  boundary (see above).

## Consequences

- The server is one writer among several, holding no state of its own: the
  database stays the only source of truth, and invariants every writer must keep
  (eg. claiming only an unclaimed task) live in the store, not the server.
- The server is a library, embedded by whatever knows when an agent starts and
  stops working, which listens for each agent on a socket only that agent can
  reach: the socket is the agent's identity. `task serve` runs one
  standalone.
- Inside an agent's sandbox, the CLI runs every command on the server, which
  runs it as the operator's CLI would, as that agent: the rules hold for
  every interface the agent has, not just its tools.
- Rules deny whatever they do not grant. Embedders attach their own checks as
  guards, which may only allow or reject a write, never change it.
