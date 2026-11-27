from __future__ import annotations

import datetime

from creditsage.bureau.canonical.account import CanonicalAccount
from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.employment import CanonicalEmploymentRecord
from creditsage.bureau.canonical.inquiry import CanonicalInquiry
from creditsage.bureau.canonical.provenance import (
    ObservationProvenance,
    SourceType,
)
from creditsage.bureau.canonical.score import CanonicalScore
from creditsage.bureau.canonical.summary import BureauReportSummary


class BureauMetadata(SafeBureauModel, frozen=True):
    provider: str
    product_type: str
    product_version: str
    adapter_version: str
    source_type: SourceType = SourceType.CRIF_NATIVE_JSON
    issue_date: datetime.date | None = None
    request_date: datetime.date | None = None
    raw_status: str | None = None

    def __repr__(self) -> str:
        return (
            f"BureauMetadata(provider={self.provider!r}, "
            f"product={self.product_type!r}, version={self.product_version!r})"
        )


class CanonicalBureauReport(SafeBureauModel, frozen=True):
    metadata: BureauMetadata
    score: CanonicalScore | None = None
    accounts: tuple[CanonicalAccount, ...] = ()
    inquiries: tuple[CanonicalInquiry, ...] = ()
    summary: BureauReportSummary | None = None
    employment_records: tuple[CanonicalEmploymentRecord, ...] = ()
    provenance: ObservationProvenance

    def __repr__(self) -> str:
        return (
            f"CanonicalBureauReport(provider={self.metadata.provider!r}, "
            f"accounts_count={len(self.accounts)}, "
            f"inquiries_count={len(self.inquiries)}, "
            f"has_score={self.score is not None})"
        )
