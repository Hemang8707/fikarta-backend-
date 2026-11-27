from __future__ import annotations

from decimal import Decimal

from creditsage.bureau.canonical.base import SafeBureauModel


class AccountSummaryMetrics(SafeBureauModel, frozen=True):
    active_accounts_count: int | None = None
    number_of_accounts: int | None = None
    overdue_accounts_count: int | None = None
    secured_accounts_count: int | None = None
    unsecured_accounts_count: int | None = None
    total_current_balance: Decimal | None = None
    total_disbursed_amount: Decimal | None = None
    total_overdue_amount: Decimal | None = None
    total_sanctioned_amount: Decimal | None = None

    def __repr__(self) -> str:
        return (
            f"AccountSummaryMetrics(accounts_count={self.number_of_accounts}, "
            f"active_count={self.active_accounts_count})"
        )


class BureauReportSummary(SafeBureauModel, frozen=True):
    primary_accounts: AccountSummaryMetrics | None = None
    secondary_accounts: AccountSummaryMetrics | None = None

    def __repr__(self) -> str:
        return (
            f"BureauReportSummary(has_primary={self.primary_accounts is not None}, "
            f"has_secondary={self.secondary_accounts is not None})"
        )
