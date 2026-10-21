"""Scheduling: a task's due date, and how it recurs."""

import calendar
import datetime
import enum
import re

import pydantic

from . import errors


class Unit(enum.StrEnum):
    day = 'd'
    week = 'w'
    month = 'm'


# The interval CHECK in migrations must agree.
INTERVAL_RE = re.compile(rf'[1-9][0-9]*[{"".join(Unit)}]')


class Interval(pydantic.BaseModel, extra='forbid'):
    raw: str

    @pydantic.field_validator('raw')
    @classmethod
    def validate_raw(cls, value: str) -> str:
        if not INTERVAL_RE.fullmatch(value):
            raise errors.TaskError(f'invalid interval: {value!r}')
        return value

    @property
    def value(self) -> int:
        return int(self.raw[:-1])

    @property
    def unit(self) -> Unit:
        return Unit(self.raw[-1])

    def __str__(self) -> str:
        if self.value != 1:
            return f'every {self.value} {self.unit.name}s'

        match self.unit:
            case Unit.day:
                return 'daily'
            case Unit.week:
                return 'weekly'
            case Unit.month:
                return 'monthly'

    def apply(self, value: datetime.date) -> datetime.date:
        match self.unit:
            case Unit.day:
                return value + datetime.timedelta(days=self.value)
            case Unit.week:
                return value + datetime.timedelta(weeks=self.value)
            case Unit.month:
                tyear, tmonth = divmod(value.month - 1 + self.value, 12)
                year, month = value.year + tyear, tmonth + 1
                # Clamping drifts: Jan 31 -> Feb 28 -> Mar 28.
                day = min(value.day, calendar.monthrange(year, month)[1])
                return datetime.date(year, month, day)


class Details(pydantic.BaseModel, extra='forbid'):
    interval: Interval | None = None
    due: datetime.date
    shift: bool = False

    def __str__(self) -> str:
        result = f'Due: {self.due}'

        if self.interval:
            result += f', repeats {self.interval}'
            if self.shift:
                result += ' after completion'
            else:
                result += ' since last deadline'

        return result
