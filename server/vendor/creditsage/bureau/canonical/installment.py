from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.provenance import SourceValueStatus


class InstallmentFrequency(StrEnum):
    MONTHLY = "MONTHLY"
    UNKNOWN = "UNKNOWN"


class CanonicalInstallment(SafeBureauModel, frozen=True):
    raw_installment: str | None = None
    parsed_amount: Decimal | None = None
    amount_status: SourceValueStatus = SourceValueStatus.MISSING
    raw_frequency: str | None = None
    normalized_frequency: InstallmentFrequency | None = None
    frequency_status: SourceValueStatus = SourceValueStatus.MISSING
    raw_obligation: str | None = None
    parsed_obligation: Decimal | None = None
    obligation_status: SourceValueStatus = SourceValueStatus.MISSING

    def __repr__(self) -> str:
        freq_repr = (
            self.normalized_frequency.value
            if self.normalized_frequency is not None
            else None
        )
        return (
            f"CanonicalInstallment(amount_status={self.amount_status.value!r}, "
            f"frequency={freq_repr!r})"
        )
