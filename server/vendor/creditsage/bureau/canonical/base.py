from __future__ import annotations

from pydantic import BaseModel


class SafeBureauModel(BaseModel, frozen=True):
    """Base class for bureau domain models enforcing non-leaking string representations.

    By default, Pydantic's BaseModel.__str__ outputs all field names and values,
    which risks accidental exposure of sensitive financial balances, limits, DPD,
    and customer data during logging, string formatting, or exception messages.

    SafeBureauModel overrides __str__ to delegate to __repr__, ensuring that
    str(model), f"{model}", and "%s" % model always produce metadata-minimal,
    safe representations without exposing sensitive financial or customer values.
    Full programmatic access and serialization via model_dump() remain intact.
    """

    def __str__(self) -> str:
        return repr(self)
