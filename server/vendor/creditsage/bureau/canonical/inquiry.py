from __future__ import annotations

import datetime
from decimal import Decimal

from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.provenance import (
    ObservationProvenance,
    SourceValueStatus,
)


class CanonicalInquiry(SafeBureauModel, frozen=True):
    source_record_ordinal: int
    inquiry_date: datetime.date | None = None
    raw_inquiry_date: str = ""
    inquiry_date_status: SourceValueStatus = SourceValueStatus.MISSING
    amount: Decimal | None = None
    raw_amount: str | None = None
    amount_status: SourceValueStatus = SourceValueStatus.MISSING
    purpose_type: str | None = None
    lender_name: str | None = None
    lender_type: str | None = None
    ownership_type: str | None = None
    inquiry_stage: str | None = None
    provenance: ObservationProvenance | None = None

    def __repr__(self) -> str:
        return f"CanonicalInquiry(ordinal={self.source_record_ordinal})"
