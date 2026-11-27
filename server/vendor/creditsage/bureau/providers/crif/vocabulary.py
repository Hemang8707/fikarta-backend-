from __future__ import annotations

from creditsage.bureau.canonical.account import (
    AccountStatus,
    AccountType,
    OwnershipType,
)
from creditsage.bureau.canonical.installment import InstallmentFrequency

PROVIDER_NAME = "CRIF"
EXPECTED_PRODUCT_TYPE = "BBC CONSUMER SCORE"
EXPECTED_PRODUCT_VER = "2.0"
CRIF_ADAPTER_VERSION = "0.1.0"

# Observed tradeline account type string mappings from frozen CBL.0 baseline
OBSERVED_ACCOUNT_TYPE_MAP: dict[str, AccountType] = {
    "Auto Loan (Personal)": AccountType.AUTO_LOAN,
    "Business Loan Unsecured": AccountType.BUSINESS_LOAN_UNSECURED,
    "Consumer Loan": AccountType.CONSUMER_LOAN,
    "Credit Card": AccountType.CREDIT_CARD,
    "Gold Loan": AccountType.GOLD_LOAN,
    "Other": AccountType.OTHER,
    "Personal Loan": AccountType.PERSONAL_LOAN,
    "Short Term Personal Loan": AccountType.SHORT_TERM_PERSONAL_LOAN,
    "Two-Wheeler Loan": AccountType.TWO_WHEELER_LOAN,
    "Used Car Loan": AccountType.USED_CAR_LOAN,
}

# Observed tradeline account status string mappings
OBSERVED_ACCOUNT_STATUS_MAP: dict[str, AccountStatus] = {
    "ACTIVE": AccountStatus.ACTIVE,
    "CLOSED": AccountStatus.CLOSED,
}

# Observed tradeline ownership string mappings
OBSERVED_OWNERSHIP_MAP: dict[str, OwnershipType] = {
    "INDIVIDUAL": OwnershipType.INDIVIDUAL,
}

# Observed installment frequency string mappings (only Monthly observed in CBL.0)
OBSERVED_FREQUENCY_MAP: dict[str, InstallmentFrequency] = {
    "MONTHLY": InstallmentFrequency.MONTHLY,
}

MONTH_TOKEN_MAP: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
