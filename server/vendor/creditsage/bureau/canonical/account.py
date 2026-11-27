from __future__ import annotations

import datetime
from decimal import Decimal
from enum import StrEnum

from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.employment import CanonicalBureauIncome
from creditsage.bureau.canonical.installment import CanonicalInstallment
from creditsage.bureau.canonical.payment_history import CanonicalPaymentHistory
from creditsage.bureau.canonical.provenance import (
    ObservationProvenance,
    SourceValueStatus,
)


class AccountType(StrEnum):
    AUTO_LOAN = "AUTO_LOAN"
    BUSINESS_LOAN_UNSECURED = "BUSINESS_LOAN_UNSECURED"
    CONSUMER_LOAN = "CONSUMER_LOAN"
    CREDIT_CARD = "CREDIT_CARD"
    GOLD_LOAN = "GOLD_LOAN"
    OTHER = "OTHER"
    PERSONAL_LOAN = "PERSONAL_LOAN"
    SHORT_TERM_PERSONAL_LOAN = "SHORT_TERM_PERSONAL_LOAN"
    TWO_WHEELER_LOAN = "TWO_WHEELER_LOAN"
    USED_CAR_LOAN = "USED_CAR_LOAN"
    UNKNOWN = "UNKNOWN"


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class OwnershipType(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    UNKNOWN = "UNKNOWN"


class CanonicalSecurityDetail(SafeBureauModel, frozen=True):
    automobile_type: str | None = None
    security_type: str | None = None
    security_valuation: Decimal | None = None

    def __repr__(self) -> str:
        return f"CanonicalSecurityDetail(type={self.security_type!r})"


class HistoricalMetricRecord(SafeBureauModel, frozen=True):
    month_label: str
    raw_value: str
    parsed_amount: Decimal | None = None
    status: SourceValueStatus = SourceValueStatus.PARSED

    def __repr__(self) -> str:
        return (
            f"HistoricalMetricRecord(month={self.month_label!r}, "
            f"status={self.status.value!r})"
        )


class CanonicalAccount(SafeBureauModel, frozen=True):
    source_record_ordinal: int
    raw_account_type: str = ""
    account_type: AccountType = AccountType.UNKNOWN
    raw_account_status: str = ""
    account_status: AccountStatus = AccountStatus.UNKNOWN
    raw_ownership_type: str = ""
    ownership_type: OwnershipType = OwnershipType.UNKNOWN
    raw_security_status: str | None = None
    disbursed_amount: Decimal | None = None
    raw_disbursed_amount: str | None = None
    disbursed_amount_status: SourceValueStatus = SourceValueStatus.MISSING
    credit_limit: Decimal | None = None
    raw_credit_limit: str | None = None
    credit_limit_status: SourceValueStatus = SourceValueStatus.MISSING
    cash_limit: Decimal | None = None
    raw_cash_limit: str | None = None
    cash_limit_status: SourceValueStatus = SourceValueStatus.MISSING
    current_balance: Decimal | None = None
    raw_current_balance: str | None = None
    current_balance_status: SourceValueStatus = SourceValueStatus.MISSING
    overdue_amount: Decimal | None = None
    raw_overdue_amount: str | None = None
    overdue_amount_status: SourceValueStatus = SourceValueStatus.MISSING
    installment: CanonicalInstallment | None = None
    repayment_tenure_months: int | None = None
    term_to_maturity_months: int | None = None
    interest_rate: Decimal | None = None
    open_date: datetime.date | None = None
    open_date_status: SourceValueStatus = SourceValueStatus.MISSING
    closed_date: datetime.date | None = None
    closed_date_status: SourceValueStatus = SourceValueStatus.MISSING
    reported_date: datetime.date | None = None
    reported_date_status: SourceValueStatus = SourceValueStatus.MISSING
    last_payment_date: datetime.date | None = None
    last_payment_date_status: SourceValueStatus = SourceValueStatus.MISSING
    actual_payment_amount: Decimal | None = None
    last_paid_amount: Decimal | None = None
    credit_grantor_name: str | None = None
    credit_grantor_type: str | None = None
    credit_grantor_group: str | None = None
    wilful_default_status: str | None = None
    written_off_settled_status: str | None = None
    write_off_amount: Decimal | None = None
    principal_write_off_amount: Decimal | None = None
    bureau_reported_income: CanonicalBureauIncome | None = None
    security_details: tuple[CanonicalSecurityDetail, ...] = ()
    payment_history: CanonicalPaymentHistory | None = None
    balance_history: tuple[HistoricalMetricRecord, ...] = ()
    high_credit_history: tuple[HistoricalMetricRecord, ...] = ()
    amount_paid_history: tuple[HistoricalMetricRecord, ...] = ()
    provenance: ObservationProvenance | None = None

    def __repr__(self) -> str:
        return f"CanonicalAccount(ordinal={self.source_record_ordinal})"
