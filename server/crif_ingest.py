"""CRIF report ingestion for the mobile API.

Parsing is done by creditsage-lab's CRIF adapter (vendored unmodified under vendor/creditsage/bureau).
This module only maps the typed canonical report onto the mobile report draft. Rules:
- UNKNOWN != ZERO: a value the bureau did not report stays None, never 0.
- Every mapped field keeps the adapter's parse status so the app can show "missing in report".
- Raw bureau data is never altered; unmappable types/statuses become "Other"/"Unknown", never a guess.
"""
import os
import sys
from decimal import Decimal
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from creditsage.bureau.canonical.account import AccountStatus, AccountType  # noqa: E402
from creditsage.bureau.canonical.provenance import SourceValueStatus  # noqa: E402
from creditsage.bureau.providers.crif.adapter import CrifJsonAdapter  # noqa: E402
from creditsage.bureau.providers.crif.issues import ParseStatus  # noqa: E402

TYPE_MAP = {
    AccountType.AUTO_LOAN: "Car Loan",
    AccountType.USED_CAR_LOAN: "Car Loan",
    AccountType.BUSINESS_LOAN_UNSECURED: "Business Loan",
    AccountType.CONSUMER_LOAN: "Consumer Durable Loan",
    AccountType.CREDIT_CARD: "Credit Card",
    AccountType.GOLD_LOAN: "Gold Loan",
    AccountType.PERSONAL_LOAN: "Personal Loan",
    AccountType.SHORT_TERM_PERSONAL_LOAN: "Personal Loan",
    AccountType.TWO_WHEELER_LOAN: "Two-Wheeler Loan",
    AccountType.OTHER: "Other",
    AccountType.UNKNOWN: "Other",
}


def _money(v: Optional[Decimal], status: SourceValueStatus) -> Optional[str]:
    return str(v) if (v is not None and status == SourceValueStatus.PARSED) else None


def _status(a) -> str:
    wo = (a.written_off_settled_status or "").strip().lower()
    if wo:
        if "settl" in wo:
            return "Settled"
        if "written" in wo or "write" in wo or wo in {"02", "03"}:
            return "Written-Off"
    if a.account_status == AccountStatus.ACTIVE:
        return "Active"
    if a.account_status == AccountStatus.CLOSED:
        return "Closed"
    return "Unknown"


def _late_months(a) -> Optional[int]:
    """Months with days-past-due > 0 in the latest 24 observations; None if the bureau gave no usable history."""
    ph = a.payment_history
    if ph is None:
        return None
    obs = [o for o in ph.observations if o.dpd_status == SourceValueStatus.PARSED and o.parsed_dpd_days is not None]
    if not obs:
        return None
    obs.sort(key=lambda o: ((o.year or 0), (o.month or 0)), reverse=True)
    return sum(1 for o in obs[:24] if o.parsed_dpd_days > 0)


def map_crif(raw: Any) -> Dict[str, Any]:
    """Returns a mobile report draft (never saved here). Raises ValueError if the report cannot be mapped."""
    res = CrifJsonAdapter().parse(raw)
    if res.status == ParseStatus.FAILED_UNMAPPABLE or res.report is None:
        raise ValueError("This file is not a CRIF report we can read.")
    r = res.report
    score = None
    if r.score is not None and r.score.score_value_status == SourceValueStatus.PARSED and r.score.parsed_numeric_score is not None:
        s = int(r.score.parsed_numeric_score)
        score = s if 300 <= s <= 900 else None
    accounts = []
    for a in r.accounts:
        typ = TYPE_MAP.get(a.account_type, "Other")
        card = typ == "Credit Card"
        accounts.append({
            "lender": (a.credit_grantor_name or "").strip() or "Lender not named in report",
            "type": typ,
            "raw_type": a.raw_account_type,
            "status": _status(a),
            "raw_status": a.raw_account_status,
            "sanctioned": None if card else _money(a.disbursed_amount, a.disbursed_amount_status),
            "limit": _money(a.credit_limit, a.credit_limit_status) if card else None,
            "balance": _money(a.current_balance, a.current_balance_status),
            "overdue": _money(a.overdue_amount, a.overdue_amount_status),
            "opened": a.open_date.isoformat() if a.open_date else None,
            "tenure_months": a.repayment_tenure_months if (a.repayment_tenure_months or 0) > 0 else None,  # 0 = not applicable (e.g. cards)
            "dpd_events": _late_months(a),
            "field_status": {
                "sanctioned": a.disbursed_amount_status.value, "limit": a.credit_limit_status.value,
                "balance": a.current_balance_status.value, "overdue": a.overdue_amount_status.value,
                "opened": a.open_date_status.value,
            },
        })
    enquiries = []
    for q in r.inquiries:
        if q.inquiry_date is None:
            continue  # an enquiry without a date cannot be placed in the 6-month window; reported as an issue below
        enquiries.append({"lender": (q.lender_name or "Lender not named").strip(), "purpose": (q.purpose_type or "Not stated").strip()[:60], "date": q.inquiry_date.isoformat()})
    issues = [{"severity": i.severity.value, "code": i.issue_code, "message": i.message} for i in res.issues]
    undated = sum(1 for q in r.inquiries if q.inquiry_date is None)
    if undated:
        issues.append({"severity": "WARNING", "code": "INQUIRY_DATE_MISSING", "message": f"{undated} enquiry(ies) had no date and were left out."})
    return {
        "source": "crif", "bureau": "CRIF High Mark",
        "score": score,
        "report_date": r.metadata.issue_date.isoformat() if r.metadata.issue_date else None,
        "accounts": accounts, "enquiries": enquiries,
        "parse_status": res.status.value, "issues": issues,
        "found": {"score": score is not None, "accounts": len(accounts), "enquiries": len(enquiries)},
    }
