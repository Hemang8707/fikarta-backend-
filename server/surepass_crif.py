"""CRIF High Mark credit report via Surepass (POST /api/v1/credit-report-crif/fetch-report).

Two parts, kept separate so the mapper can be tested without the network:
- fetch_report(): the HTTP call. Token and environment come only from the server .env
  (SUREPASS_TOKEN, SUREPASS_ENV=sandbox|production). Nothing here is ever sent to the app.
- map_surepass_crif(): maps Surepass's CRIF JSON onto the mobile report draft used everywhere else.

Mapping rules (same as the rest of the credit engine):
- UNKNOWN != ZERO. A blank or unreadable amount/date stays None and is listed as a parse issue.
- Amounts use Indian grouping ("2,80,000") and are read exactly with Decimal.
- Only PRIMARY-matched accounts are analysed; SECONDARY matches (uncertain or associated accounts) are
  counted and reported, never silently mixed in.
- The raw response (addresses, emails, employer, name/phone variations) is NOT stored: data minimisation.
"""
import datetime as dt
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASES = {"sandbox": "https://sandbox.surepass.app", "production": "https://kyc-api.surepass.app"}
PATH = "/api/v1/credit-report-crif/fetch-report"
BUREAU = "CRIF High Mark"


class BureauError(Exception):
    """kind: not_configured | no_match | unavailable | bad_response"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


def configured() -> bool:
    return bool(os.getenv("SUREPASS_TOKEN", "").strip())


def environment() -> str:
    env = os.getenv("SUREPASS_ENV", "sandbox").strip().lower()
    return env if env in BASES else "sandbox"


def fetch_report(first_name: str, last_name: str, mobile: str, pan: str, timeout: float = 45.0) -> Dict[str, Any]:
    token = os.getenv("SUREPASS_TOKEN", "").strip()
    if not token:
        raise BureauError("not_configured")
    body = {"first_name": first_name, "last_name": last_name or first_name, "mobile": mobile, "pan": pan, "consent": "Y"}
    try:
        base = os.getenv("SUREPASS_BASE_URL", "").strip().rstrip("/") or BASES[environment()]  # override only for local mocks
        r = httpx.post(base + PATH, json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise BureauError("unavailable", type(e).__name__)
    try:
        j = r.json()
    except ValueError:
        j = {}
    code = (j.get("message_code") or "").lower() if isinstance(j, dict) else ""
    if r.status_code == 422 or code in ("verification_failed", "no_record_found", "record_not_found"):
        raise BureauError("no_match", code or str(r.status_code))
    if r.status_code in (401, 403):
        raise BureauError("unavailable", "auth")  # our token is wrong or expired: an operator problem, not the user's
    if r.status_code == 429 or r.status_code >= 500:
        raise BureauError("unavailable", str(r.status_code))
    if r.status_code != 200 or not isinstance(j, dict) or j.get("success") is not True or not isinstance(j.get("data"), dict):
        raise BureauError("bad_response", f"{r.status_code}:{code}")
    return j


# ------------------------------------------------------------------ mapping helpers
def _list(v) -> List[Any]:
    """CRIF JSON uses an object for one item and a list for many."""
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def _money(v) -> Tuple[Optional[Decimal], bool]:
    """('2,80,000') -> (Decimal('280000'), True). Blank -> (None, True). Unreadable -> (None, False)."""
    if v is None:
        return None, True
    s = str(v).strip().replace(",", "").replace("₹", "").replace(" ", "")
    if s == "" or s.upper() in ("NA", "N/A", "-"):
        return None, True
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None, False
    if d < 0:
        return None, False
    return d, True


def _date(v) -> Optional[str]:
    s = (str(v).strip() if v is not None else "")
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d%m%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


TYPE_RULES = [  # (keyword in lower-cased ACCT-TYPE, our type) - first match wins
    ("secured credit card", "Credit Card"), ("credit card", "Credit Card"), ("charge card", "Credit Card"),
    ("home", "Home Loan"), ("housing", "Home Loan"),
    ("property", "Loan Against Property"),
    ("two-wheeler", "Two-Wheeler Loan"), ("two wheeler", "Two-Wheeler Loan"),
    ("auto", "Car Loan"), ("car", "Car Loan"), ("vehicle", "Car Loan"),
    ("gold", "Gold Loan"), ("education", "Education Loan"),
    ("business", "Business Loan"), ("commercial", "Business Loan"),
    ("consumer", "Consumer Durable Loan"),
    ("overdraft", "Overdraft"),
    ("personal", "Personal Loan"),
]


def _type(raw: str) -> str:
    t = (raw or "").lower()
    for k, v in TYPE_RULES:
        if k in t:
            return v
    return "Other"


def _status(raw: str, write_off: Optional[Decimal]) -> str:
    s = (raw or "").lower()
    if "settl" in s:
        return "Settled"
    if "written" in s or "write" in s:
        return "Written-Off"
    if "suit" in s or "wilful" in s:
        return "Suit Filed"
    if "active" in s or "current" in s or "open" in s:
        return "Written-Off" if (write_off or 0) > 0 else "Active"
    if "closed" in s:
        return "Closed"
    return "Unknown"


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_SEG = re.compile(r"([A-Za-z]{3}):(\d{4}),([^/|]*)")


def late_months(history: str) -> Optional[int]:
    """'Jul:2025,000/XXX|Jun:2025,030/STD|' -> months with days-past-due > 0 in the latest 24 readable months.
    None when no month has a readable DPD value (we never assume 'no late payments')."""
    obs = []
    for mon, year, dpd in _SEG.findall(history or ""):
        m = _MONTHS.get(mon.lower())
        d = dpd.strip()
        if m is None or not d.isdigit():
            continue  # 'XXX' / 'DDD' / blank = not reported
        obs.append((int(year), m, int(d)))
    if not obs:
        return None
    obs.sort(reverse=True)
    return sum(1 for o in obs[:24] if o[2] > 0)


def _mask(acct: str) -> Optional[str]:
    a = re.sub(r"\s", "", acct or "")
    return ("XXXX" + a[-4:]) if len(a) >= 4 else None


def map_surepass_crif(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Surepass response -> mobile report draft. Raises BureauError('bad_response') if it is not a CRIF report."""
    data = resp.get("data") if isinstance(resp, dict) else None
    cr = data.get("credit_report") if isinstance(data, dict) else None
    if not isinstance(cr, dict):
        raise BureauError("bad_response", "no credit_report")
    issues: List[Dict[str, str]] = []

    def issue(code, msg, sev="WARNING"):
        issues.append({"severity": sev, "code": code, "message": msg})

    header = cr.get("HEADER") or {}
    if str(header.get("STATUS", "SUCCESS")).upper() not in ("SUCCESS", ""):
        raise BureauError("bad_response", f"header status {header.get('STATUS')}")

    # score: the bureau's own SCORES block is authoritative; the top-level copy is a convenience field
    score = None
    block = [s for s in _list((cr.get("SCORES") or {}).get("SCORE")) if isinstance(s, dict)]
    bureau_score = next((str(s.get("SCORE-VALUE", "")).strip() for s in block if str(s.get("SCORE-VALUE", "")).strip().isdigit()), None)
    top = str(data.get("credit_score") or "").strip()
    chosen = bureau_score or (top if top.isdigit() else None)
    if chosen is not None:
        v = int(chosen)
        if 300 <= v <= 900:
            score = v
        else:
            issue("SCORE_OUT_OF_RANGE", f"The report gave a score of {v}, outside 300-900, so no score is shown.")
    if bureau_score and top.isdigit() and top != bureau_score:
        issue("SCORE_MISMATCH", f"The report lists two scores ({bureau_score} and {top}); the bureau's score block ({bureau_score}) is used.")
    score_type = next((str(s.get("SCORE-TYPE")).strip() for s in block if s.get("SCORE-TYPE")), None)

    accounts, secondary = [], 0
    for item in _list((cr.get("RESPONSES") or {}).get("RESPONSE")):
        a = (item or {}).get("LOAN-DETAILS") if isinstance(item, dict) else None
        if not isinstance(a, dict):
            continue
        if str(a.get("MATCHED-TYPE", "PRIMARY")).strip().upper() == "SECONDARY":
            secondary += 1
            continue
        raw_type = str(a.get("ACCT-TYPE") or "").strip()
        typ = _type(raw_type)
        card = typ == "Credit Card"
        vals, unreadable = {}, []
        for key, fld in (("CURRENT-BAL", "balance"), ("OVERDUE-AMT", "overdue"), ("WRITE-OFF-AMT", "write_off"),
                         ("DISBURSED-AMT", "disbursed"), ("SANCTIONED-AMT", "sanctioned"), ("CREDIT-LIMIT", "limit"), ("HIGH-CREDIT", "high")):
            d, ok = _money(a.get(key))
            vals[fld] = d
            if not ok:
                unreadable.append(key)
        lender = str(a.get("CREDIT-GUARANTOR") or "").strip() or "Lender not named in report"
        if unreadable:
            issue("AMOUNT_UNREADABLE", f"{lender}: could not read {', '.join(unreadable)}; left blank.")
        tenure = str(a.get("REPAYMENT-TENURE") or "").strip()
        accounts.append({
            "lender": lender[:80],
            "type": typ,
            "raw_type": raw_type[:80] or None,
            "status": _status(str(a.get("ACCOUNT-STATUS") or ""), vals["write_off"]),
            "raw_status": (str(a.get("ACCOUNT-STATUS") or "").strip()[:80] or None),
            "account_no": _mask(str(a.get("ACCT-NUMBER") or "")),
            "sanctioned": None if card else (vals["sanctioned"] if vals["sanctioned"] is not None else vals["disbursed"]),
            "limit": vals["limit"] if card else None,  # a card with no CREDIT-LIMIT keeps an unknown limit (never guessed)
            "balance": vals["balance"],
            "overdue": vals["overdue"],
            "opened": _date(a.get("DISBURSED-DT") or a.get("DATE-OPENED") or a.get("OPEN-DATE")),
            "tenure_months": int(tenure) if tenure.isdigit() and 0 < int(tenure) <= 480 else None,
            "dpd_events": late_months(str(a.get("COMBINED-PAYMENT-HISTORY") or "")),
        })
    if secondary:
        issue("SECONDARY_MATCHES", f"{secondary} account(s) were only a secondary match to you and are not included in the analysis.", "INFO")

    enquiries, undated = [], 0
    for q in _list((cr.get("INQUIRY-HISTORY") or {}).get("HISTORY")):
        if not isinstance(q, dict):
            continue
        d = _date(q.get("INQUIRY-DATE"))
        if d is None:
            undated += 1
            continue
        enquiries.append({"lender": (str(q.get("MEMBER-NAME") or "").strip() or "Lender not named")[:80],
                          "purpose": (str(q.get("PURPOSE") or "").strip() or "Not stated")[:60], "date": d})
    if undated:
        issue("INQUIRY_DATE_MISSING", f"{undated} enquiry(ies) had no date and were left out.")

    for a in accounts:  # JSON-safe exact strings, same as uploaded reports
        for f in ("sanctioned", "limit", "balance", "overdue"):
            if a[f] is not None:
                a[f] = str(a[f])
    return {
        "source": "crif_api", "bureau": BUREAU,
        "score": score, "score_model": score_type,
        "report_date": _date(header.get("DATE-OF-ISSUE")) or _date(header.get("DATE-OF-REQUEST")),
        "report_id": str(header.get("REPORT-ID") or "")[:60] or None,
        "accounts": accounts, "enquiries": enquiries[:120], "issues": issues,
    }
