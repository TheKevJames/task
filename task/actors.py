"""Who makes a change to the task list: the operator, or an agent."""

import dataclasses
import re
from typing import Self

from . import errors

# An actor's name, as its claims and stamps record it; the claim and actor
# CHECKs in migrations must agree.
CLAIM_RE = re.compile(r'[a-z0-9_./-]+')
OPERATOR = 'operator'


def normalize_claim(value: str) -> str:
    """An actor's name: `Triage/1` -> `triage/1`."""
    name = value.lower()
    if not CLAIM_RE.fullmatch(name):
        raise errors.TaskError(f'invalid claim: {value!r}')
    return name


@dataclasses.dataclass(frozen=True)
class Actor:
    """
    Whoever makes a change: the operator, or an agent. An agent's name is
    given by whatever authenticated it, never by the agent itself.
    """

    name: str

    @classmethod
    def agent(cls, name: str) -> Self:
        normalized = normalize_claim(name)
        if normalized == OPERATOR:
            raise errors.TaskError(f'an agent cannot be named {OPERATOR}')
        return cls(normalized)

    @property
    def is_operator(self) -> bool:
        return self.name == OPERATOR


OPERATOR_ACTOR = Actor(OPERATOR)
