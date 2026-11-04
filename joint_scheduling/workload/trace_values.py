"""Exact time and positive token-count parsing shared by trace adapters."""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from ..validation import number


def nanoseconds(value, units_per_second=1):
    try:
        result = Decimal(str(value)) * Decimal(10**9) / units_per_second
        if not result.is_finite() or result < 0 or result >= 2**63:
            raise ValueError("trace time is outside the nonnegative nanosecond clock")
        return int(result.to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, TypeError) as error:
        raise ValueError("trace time must be a finite number") from error


def positive_integer(value, label):
    if type(value) is int:
        return number(value, label, positive=True, integral=True)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return number(int(value), label, positive=True, integral=True)
    raise ValueError("{} must be a positive integer; failed/empty responses require explicit preprocessing".format(label))
