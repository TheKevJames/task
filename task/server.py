"""
A unix socket server running commands for one agent, and its protocol.

The socket's path is the agent's identity: whoever embeds the server listens
for each agent on a socket only that agent can connect to, and nothing the
agent sends can change who it is. Each connection carries one request, a JSON
line naming a protocol version and a command's arguments, and gets one JSON
line back: the command's exit code, output, and error.
"""

import asyncio
import dataclasses
import json
import pathlib
from collections.abc import Callable

from . import schema

PROTOCOL = 1
# Far beyond any command's arguments; a longer line fails the request.
LIMIT = 1 << 20


@dataclasses.dataclass(frozen=True)
class Response:
    code: int
    stdout: str = ''
    error: str | None = None

    def encode(self) -> bytes:
        data = {'protocol': PROTOCOL, **dataclasses.asdict(self)}
        return json.dumps(data).encode() + b'\n'

    @classmethod
    def decode(cls, line: bytes) -> 'Response':
        data = json.loads(line)
        _check_protocol(data)
        return cls(int(data['code']), str(data['stdout']), data['error'])


def request(argv: list[str]) -> bytes:
    return json.dumps({'protocol': PROTOCOL, 'argv': argv}).encode() + b'\n'


Handler = Callable[[list[str]], Response]


async def serve(path: pathlib.Path, handle: Handler) -> asyncio.Server:
    """Answer requests on `path` with `handle`, run in a thread each."""

    async def answer(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            response = await _respond(await reader.readline(), handle)
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_unix_server(answer, path=path, limit=LIMIT)


async def serve_forever(path: pathlib.Path, handle: Handler) -> None:
    async with await serve(path, handle) as listening:
        await listening.serve_forever()


async def _respond(line: bytes, handle: Handler) -> Response:
    try:
        data = json.loads(line)
        _check_protocol(data)
        argv = data['argv']
        if not isinstance(argv, list) or not all(
            isinstance(arg, str) for arg in argv
        ):
            raise schema.TaskError('argv must be a list of strings')
    except (ValueError, KeyError, TypeError, schema.TaskError) as e:
        return Response(2, error=f'bad request: {e}')
    return await asyncio.to_thread(handle, argv)


def _check_protocol(data: object) -> None:
    if not isinstance(data, dict) or data.get('protocol') != PROTOCOL:
        version = data.get('protocol') if isinstance(data, dict) else None
        raise schema.TaskError(
            f"protocol {version} is not this task's protocol {PROTOCOL}:"
            ' the server and CLI are different versions'
        )
