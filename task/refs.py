"""Addressing tasks: namespaces, and a task's id within its namespace."""

import dataclasses
import re
from typing import Self

from . import errors

# Tags and namespaces; their CHECKs in migrations must agree.
NAME_RE = re.compile(r'[a-z0-9][a-z0-9_.-]*')
REF_RE = re.compile(rf'(?:(?P<namespace>{NAME_RE.pattern}):)?(?P<id>\d+)')
DEFAULT = 'default'


@dataclasses.dataclass(frozen=True)
class Ref:
    """A task's address: `<namespace>:<id>`, or a bare id in `default`."""

    namespace: str
    ident: int

    @classmethod
    def parse(cls, value: str) -> Self:
        match = REF_RE.fullmatch(value)
        if not match:
            raise errors.TaskError(f'invalid task id: {value!r}')
        return cls(match.group('namespace') or DEFAULT, int(match.group('id')))

    def __str__(self) -> str:
        if self.namespace == DEFAULT:
            return str(self.ident)
        return f'{self.namespace}:{self.ident}'
