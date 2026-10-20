"""Who makes a change to the task list: the operator, or an agent."""

import dataclasses
import re
from typing import Self

from . import errors

# An actor's name, as its claims record it; the claim CHECK in migrations must
# agree.
CLAIM_RE = re.compile(r'[A-Za-z0-9_./-]+')
OPERATOR = 'operator'


def check_claim(value: str) -> str:
    if not CLAIM_RE.fullmatch(value):
        raise errors.TaskError(f'invalid claim: {value!r}')
    return value


@dataclasses.dataclass(frozen=True)
class Actor:
    """
    Whoever makes a change: the operator, or an agent. An agent's name is
    given by whatever authenticated it, never by the agent itself.
    """

    name: str

    @classmethod
    def agent(cls, name: str) -> Self:
        if name == OPERATOR:
            raise errors.TaskError(f'an agent cannot be named {OPERATOR}')
        return cls(check_claim(name))

    @property
    def is_operator(self) -> bool:
        return self.name == OPERATOR


OPERATOR_ACTOR = Actor(OPERATOR)
