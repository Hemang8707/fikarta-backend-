from __future__ import annotations

from enum import StrEnum

from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.provenance import SourceValueStatus


class AlignmentStatus(StrEnum):
    PERFECT_MATCH = "PERFECT_MATCH"
    LENGTH_MISMATCH = "LENGTH_MISMATCH"
    MALFORMED_TOKENS = "MALFORMED_TOKENS"


class MonthlyPaymentObservation(SafeBureauModel, frozen=True):
    year: int | None = None
    month: int | None = None
    month_label: str = ""
    raw_compound_token: str = ""
    raw_dpd_token: str | None = None
    parsed_dpd_days: int | None = None
    dpd_status: SourceValueStatus = SourceValueStatus.MISSING
    raw_asset_classification: str | None = None
    asset_classification_status: SourceValueStatus = SourceValueStatus.MISSING

    def __repr__(self) -> str:
        return (
            f"MonthlyPaymentObservation(month_label={self.month_label!r}, "
            f"dpd_status={self.dpd_status.value!r})"
        )


class CanonicalPaymentHistory(SafeBureauModel, frozen=True):
    observations: tuple[MonthlyPaymentObservation, ...] = ()
    raw_dates_string: str = ""
    raw_values_string: str = ""
    alignment_status: AlignmentStatus = AlignmentStatus.PERFECT_MATCH

    def __repr__(self) -> str:
        return (
            f"CanonicalPaymentHistory(observations_count={len(self.observations)}, "
            f"alignment={self.alignment_status.value!r})"
        )
