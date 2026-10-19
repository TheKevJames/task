"""
The CLI as a client of a task server, for an agent: with $TASK_SERVER set,
every command but help runs on the server, which has the only write access.
"""

import pathlib
import socket
import sys

from . import schema
from . import server

ADDRESS = 'TASK_SERVER'


def forward(path: pathlib.Path, argv: list[str]) -> int:
    """Run a command on the server at `path`; its exit code."""
    try:
        response = _exchange(path, server.request(argv))
    except (OSError, ValueError, KeyError, schema.TaskError) as e:
        print(f'error: task server at {path}: {e}', file=sys.stderr)
        return 1
    sys.stdout.write(response.stdout)
    if response.error:
        print(f'error: {response.error}', file=sys.stderr)
    return response.code


def _exchange(path: pathlib.Path, line: bytes) -> server.Response:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(path))
        conn.sendall(line)
        conn.shutdown(socket.SHUT_WR)
        with conn.makefile('rb') as stream:
            return server.Response.decode(stream.readline())
