from __future__ import annotations

import datetime
from decimal import Decimal

from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.provenance import SourceValueStatus


class CanonicalEmploymentRecord(SafeBureauModel, frozen=True):
    occupation: str | None = None
    acct_type: str | None = None
    source_indicator: str | None = None
    first_reported_date: datetime.date | None = None
    last_reported_date: datetime.date | None = None

    def __repr__(self) -> str:
        return f"CanonicalEmploymentRecord(occupation={self.occupation!r})"


class CanonicalBureauIncome(SafeBureauModel, frozen=True):
    raw_amount: str | None = None
    parsed_amount: Decimal | None = None
    amount_status: SourceValueStatus = SourceValueStatus.MISSING
    raw_frequency: str | None = None
    stated_occupation: str | None = None

    def __repr__(self) -> str:
        return f"CanonicalBureauIncome(amount_status={self.amount_status.value!r})"
