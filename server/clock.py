"""The only place the mobile API reads the current time (pattern from creditsage-lab INJECTED_CLOCK).

Store UTC. Tests freeze time with `clock.freeze(...)` instead of patching datetime.
"""
import datetime as dt
from typing import Optional

_frozen: Optional[dt.datetime] = None


def utcnow() -> dt.datetime:
    """Timezone-naive UTC 'now' (the database stores naive UTC ISO strings)."""
    return _frozen if _frozen is not None else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def today() -> dt.date:
    return utcnow().date()


def epoch() -> int:
    return int(utcnow().replace(tzinfo=dt.timezone.utc).timestamp())


def iso() -> str:
    return utcnow().replace(microsecond=0).isoformat() + "Z"


def freeze(when: Optional[dt.datetime]) -> None:
    """Test hook: pin the clock (pass None to release)."""
    global _frozen
    _frozen = when
