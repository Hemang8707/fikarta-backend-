"""One report format for every credit bureau.

Each bureau sends a different JSON. This module turns Experian and CRIF (B2C) reports into the same mobile
report draft that `mobile_api.save_report` stores, and keeps *everything useful* the bureau sent:

  draft = {source, bureau, score, score_model, report_date, report_id,
           accounts:   [{lender, type, raw_type, status, raw_status, account_no (masked), sanctioned, limit, balance,
                         overdue, opened, tenure_months, dpd_events,
                         emi, interest_rate, last_payment_date, last_payment_amount, reported_date, closed_date,
                         collateral_type, collateral_value, settlement_amount, written_off_amount, suit_filed,
                         ownership, payment_history: [{month: 'YYYY-MM', dpd: int|None, asset: str|None}]}],
           enquiries:  [{lender, purpose, date, amount, kind: 'credit'|'non_credit'}],
           profile:    {age, gender, city, state, pincode_area, employment, occupation, stated_income},
           bureau_summary: {...the bureau's own totals...},
           score_factors: [str], issues: [...]}

Rules (same as the CRIF mappers):
- UNKNOWN != ZERO: a value the bureau left blank stays None, never 0.
- Identifiers never enter the draft: no PAN, phone, email, address lines or full account numbers. Account
  numbers are masked to the last 4. The complete raw report is stored separately, encrypted
  (`bureau_raw`), and is never given to the AI.
- Unknown codes are kept raw ("raw_type", "raw_status") and shown as Other/Unknown, never guessed.
"""
import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional


class FormatError(ValueError):
    pass


# ------------------------------------------------------------------ helpers
def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _money(v) -> Optional[str]:
    s = _s(v).replace(",", "").split("/")[0].strip()  # CRIF writes EMIs as '12,345/Monthly'
    if s == "" or s.lower() in ("na", "null", "none", "-"):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return str(d.quantize(Decimal(1)))


def _rate(v) -> Optional[str]:
    s = _s(v).replace("%", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return str(d.normalize()) if Decimal(0) < d < Decimal(100) else None


def _date(v) -> Optional[str]:
    s = _s(v)
    if not s:
        return None
    for fmt in ("%Y%m%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            d = dt.datetime.strptime(s[:11].strip(), fmt).date()
        except ValueError:
            continue
        # placeholder dates (1800, 1900) and future dates count as missing
        return d.isoformat() if dt.date(1950, 1, 1) <= d <= dt.date.today() + dt.timedelta(days=1) else None
    return None


def _d(v) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _int(v) -> Optional[int]:
    s = _s(v)
    return int(s) if s.isdigit() else None


def _mask(acct: str) -> Optional[str]:
    a = re.sub(r"\s", "", _s(acct))
    return ("XXXX" + a[-4:]) if len(a) >= 4 else None


def _late(history: List[Dict[str, Any]]) -> Optional[int]:
    known = [h for h in history[:24] if h.get("dpd") is not None]
    return sum(1 for h in known if h["dpd"] > 0) if known else None


# ------------------------------------------------------------------ Experian
EXPERIAN_TYPES = {
    "01": "Car Loan", "02": "Home Loan", "03": "Loan Against Property", "04": "Other", "05": "Personal Loan",
    "06": "Consumer Durable Loan", "07": "Gold Loan", "08": "Education Loan", "09": "Business Loan", "10": "Credit Card",
    "11": "Other", "12": "Overdraft", "13": "Two-Wheeler Loan", "14": "Other", "15": "Other", "16": "Credit Card",
    "17": "Car Loan", "31": "Credit Card", "32": "Car Loan", "33": "Business Loan", "34": "Business Loan", "35": "Credit Card",
    "36": "Credit Card", "37": "Personal Loan", "38": "Home Loan", "39": "Business Loan", "40": "Business Loan",
    "41": "Personal Loan", "42": "Home Loan", "43": "Other", "44": "Personal Loan", "51": "Business Loan", "52": "Business Loan",
    "53": "Business Loan", "54": "Business Loan", "55": "Business Loan", "56": "Business Loan", "57": "Business Loan",
    "58": "Personal Loan", "59": "Business Loan", "61": "Business Loan", "69": "Personal Loan", "70": "Gold Loan", "71": "Overdraft",
}
EXPERIAN_TYPE_NAMES = {
    "01": "Auto Loan", "02": "Housing Loan", "03": "Property Loan", "04": "Loan Against Shares", "05": "Personal Loan",
    "06": "Consumer Loan", "07": "Gold Loan", "08": "Education Loan", "09": "Loan to Professional", "10": "Credit Card",
    "11": "Leasing", "12": "Overdraft", "13": "Two-Wheeler Loan", "14": "Non-Funded Credit Facility", "15": "Loan Against Bank Deposits",
    "16": "Fleet Card", "17": "Commercial Vehicle Loan", "31": "Secured Credit Card", "32": "Used Car Loan",
    "33": "Construction Equipment Loan", "34": "Tractor Loan", "35": "Corporate Credit Card", "36": "Kisan Credit Card",
    "37": "Loan on Credit Card", "38": "PMAY Housing Loan", "39": "Mudra Loan", "40": "Microfinance Business Loan",
    "41": "Microfinance Personal Loan", "42": "Microfinance Housing Loan", "43": "Microfinance Other", "44": "P2P Loan",
    "51": "Business Loan", "61": "Business Loan Secured", "69": "Short Term Personal Loan", "70": "Priority Sector Gold Loan",
    "71": "Temporary Overdraft",
}
_EXP_ACTIVE = {"11", "21", "22", "23", "24", "25", "71", "78", "80", "82", "83", "84"}
_EXP_CLOSED = {"12", "13", "14", "15", "16", "17"}
_EXP_WRITTEN = {"89", "97"}
_EXP_SUIT = {"93"}


def _exp_status(code: str, wo: str, suit: str, closed: Optional[str]) -> str:
    if wo in ("03",):
        return "Settled"
    if wo in ("02", "04", "05", "06", "07", "08", "09", "10", "11") or code in _EXP_WRITTEN:
        return "Written-Off"
    if suit in ("01", "02", "03") or code in _EXP_SUIT:
        return "Suit Filed"
    if code in _EXP_CLOSED or closed:
        return "Closed"
    if code in _EXP_ACTIVE:
        return "Active"
    return "Unknown"


def is_experian(raw: Any) -> bool:
    cr = raw.get("credit_report") if isinstance(raw, dict) else None
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        cr = raw["data"].get("credit_report", cr)
    return isinstance(cr, dict) and "CAIS_Account" in cr


def map_experian(raw: Dict[str, Any]) -> Dict[str, Any]:
    root = raw.get("data") if isinstance(raw.get("data"), dict) and "credit_report" in raw["data"] else raw
    cr = root.get("credit_report")
    if not isinstance(cr, dict) or "CAIS_Account" not in cr:
        raise FormatError("This is not an Experian report we can read.")
    issues: List[Dict[str, str]] = []

    def issue(code, msg, sev="WARNING"):
        issues.append({"severity": sev, "code": code, "message": msg})

    sc = cr.get("SCORE") or {}
    score = None
    bs = _int(sc.get("BureauScore"))
    top = _int(root.get("credit_score"))
    chosen = bs if bs is not None else top
    if chosen is not None:
        if 300 <= chosen <= 900:
            score = chosen
        else:
            issue("SCORE_OUT_OF_RANGE", f"The report gave a score of {chosen}, outside 300-900, so no score is shown.")
    if bs is not None and top is not None and bs != top:
        issue("SCORE_MISMATCH", f"The report lists two scores ({bs} and {top}); the bureau's score block ({bs}) is used.")

    accounts = []
    for a in (cr.get("CAIS_Account") or {}).get("CAIS_Account_DETAILS") or []:
        code = _s(a.get("Account_Type")).zfill(2)
        typ = EXPERIAN_TYPES.get(code, "Other")
        card = typ == "Credit Card"
        hist = []
        for h in a.get("CAIS_Account_History") or []:
            y, m = _s(h.get("Year")), _s(h.get("Month"))
            if not (y.isdigit() and m.isdigit()):
                continue
            dpd = _s(h.get("Days_Past_Due"))
            asset = _s(h.get("Asset_Classification"))
            hist.append({"month": f"{y}-{m.zfill(2)}", "dpd": int(dpd) if dpd.isdigit() else None,
                         "asset": None if asset in ("", "?") else asset})
        hist.sort(key=lambda h: h["month"], reverse=True)
        review = sorted([r for r in a.get("Account_Review_Data") or [] if _s(r.get("Year")).isdigit()],
                        key=lambda r: (_s(r.get("Year")), _s(r.get("Month")).zfill(2)), reverse=True)
        latest = review[0] if review else {}
        closed = _date(a.get("Date_Closed"))
        wo, suit = _s(a.get("Written_off_Settled_Status")), _s(a.get("SuitFiled_WillfulDefault"))
        limit = _money(a.get("Credit_Limit_Amount"))
        high = _money(a.get("Highest_Credit_or_Original_Loan_Amount"))
        lender = _s(a.get("Subscriber_Name")) or "Lender not named in report"
        tenure = _int(a.get("Repayment_Tenure")) or _int(a.get("Terms_Duration"))
        accounts.append({
            "lender": lender[:80], "type": typ, "raw_type": f"{code} {EXPERIAN_TYPE_NAMES.get(code, 'Unknown type')}",
            "status": _exp_status(_s(a.get("Account_Status")), wo, suit, closed), "raw_status": _s(a.get("Account_Status")) or None,
            "account_no": _mask(a.get("Account_Number")),
            "sanctioned": None if card else high,
            "limit": (limit or high) if card else None,
            "balance": _money(a.get("Current_Balance")),
            "overdue": _money(a.get("Amount_Past_Due")),
            "opened": _date(a.get("Open_Date")),
            "tenure_months": tenure if tenure and 0 < tenure <= 480 else None,
            "dpd_events": _late(hist),
            "emi": _money(a.get("Scheduled_Monthly_Payment_Amount")) or _money(latest.get("EMI_Amount")),
            "interest_rate": _rate(a.get("Rate_of_Interest")),
            "last_payment_date": _date(a.get("Date_of_Last_Payment")),
            "last_payment_amount": _money(latest.get("Actual_Payment_Amount")),
            "reported_date": _date(a.get("Date_Reported")),
            "closed_date": closed,
            "first_delinquency_date": _date(a.get("Date_of_First_Delinquency")),
            "collateral_type": _s(a.get("Type_of_Collateral")) or None,
            "collateral_value": _money(a.get("Value_of_Collateral")),
            "settlement_amount": _money(a.get("Settlement_Amount")),
            "written_off_amount": _money(a.get("Written_Off_Amt_Total")),
            "suit_filed": None if suit in ("",) else suit not in ("00", "99"),
            "ownership": {"1": "Individual", "2": "Joint", "3": "Authorised user", "4": "Guarantor"}.get(_s(a.get("AccountHoldertypeCode"))),
            "payment_history": hist[:36],
        })

    enquiries = []
    for kind, block in (("credit", cr.get("CAPS")), ("non_credit", cr.get("NonCreditCAPS"))):
        for q in (block or {}).get("CAPS_Application_Details") or []:
            d = _date(q.get("Date_of_Request"))
            if d is None:
                continue
            purpose = _s(q.get("Finance_Purpose")).zfill(2)
            enquiries.append({"lender": (_s(q.get("Subscriber_Name")) or "Lender not named")[:80],
                              "purpose": EXPERIAN_TYPE_NAMES.get(purpose, f"Purpose code {purpose}")[:60],
                              "date": d, "amount": _money(q.get("Amount_Financed")), "kind": kind})
    enquiries.sort(key=lambda e: e["date"], reverse=True)

    app = (cr.get("Current_Application") or {}).get("Current_Application_Details") or {}
    ap = app.get("Current_Applicant_Details") or {}
    other = app.get("Current_Other_Details") or {}
    addr = app.get("Current_Applicant_Address_Details") or {}
    dob = _date(ap.get("Date_Of_Birth_Applicant"))
    profile = {
        "age": (dt.date.today() - dt.date.fromisoformat(dob)).days // 365 if dob else None,
        "gender": {"1": "Female", "2": "Male", "3": "Transgender"}.get(_s(ap.get("Gender_Code"))),
        "city": _s(addr.get("City")).title() or None, "state": _s(addr.get("State")) or None,
        "employment": _s(other.get("Employment_Status")) or None,
        "stated_income": _money(other.get("Income")),
    }
    cs = (cr.get("CAIS_Account") or {}).get("CAIS_Summary") or {}
    ca, ob = cs.get("Credit_Account") or {}, cs.get("Total_Outstanding_Balance") or {}
    tc = cr.get("TotalCAPS_Summary") or {}
    summary = {
        "accounts_total": _int(ca.get("CreditAccountTotal")), "accounts_active": _int(ca.get("CreditAccountActive")),
        "accounts_default": _int(ca.get("CreditAccountDefault")), "accounts_closed": _int(ca.get("CreditAccountClosed")),
        "outstanding_secured": _money(ob.get("Outstanding_Balance_Secured")), "outstanding_unsecured": _money(ob.get("Outstanding_Balance_UnSecured")),
        "outstanding_total": _money(ob.get("Outstanding_Balance_All")),
        "enquiries_7d": _int(tc.get("TotalCAPSLast7Days")), "enquiries_30d": _int(tc.get("TotalCAPSLast30Days")),
        "enquiries_90d": _int(tc.get("TotalCAPSLast90Days")), "enquiries_180d": _int(tc.get("TotalCAPSLast180Days")),
    }
    hdr = cr.get("Header") or {}
    rdate = _date(hdr.get("ReportDate")) or _date((cr.get("CreditProfileHeader") or {}).get("ReportDate"))
    if not rdate:
        issue("REPORT_DATE_MISSING", "The bureau did not give a valid report date; the date it was added is shown instead.")
    return {
        "source": "experian", "bureau": "Experian", "score": score, "score_model": "Experian Bureau Score",
        "report_date": rdate or dt.date.today().isoformat(),
        "report_id": _s((cr.get("CreditProfileHeader") or {}).get("ReportNumber"))[:60] or None,
        "accounts": accounts, "enquiries": enquiries[:200], "profile": profile, "bureau_summary": summary,
        "score_factors": [], "issues": issues,
    }


# ------------------------------------------------------------------ CRIF B2C: the fields the canonical adapter does not keep
def is_crif_b2c(raw: Any) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("B2C-REPORT"), dict)


def _crif_hist(h) -> List[Dict[str, Any]]:
    out = []
    for blk in h or []:
        if not isinstance(blk, dict):
            continue
        names, dates, vals = _s(blk.get("NAME")).upper(), _s(blk.get("DATES")), _s(blk.get("VALUES"))
        if "DPD" not in names and "PAYMENT" not in names and "COMBINED" not in names:
            continue
        for d, v in zip(dates.split("|"), vals.split("|")):
            m = re.match(r"(\d{1,2})[-/:](\d{4})|([A-Za-z]{3})[:-](\d{4})", d.strip())
            if not m:
                continue
            if m.group(1):
                month = f"{m.group(2)}-{m.group(1).zfill(2)}"
            else:
                try:
                    month = dt.datetime.strptime(m.group(3) + m.group(4), "%b%Y").strftime("%Y-%m")
                except ValueError:
                    continue
            num = re.match(r"\s*(\d{1,3})", v)
            out.append({"month": month, "dpd": int(num.group(1)) if num else None, "asset": (re.sub(r"[\d/,\s]", "", v) or None)})
        if out:
            break
    out.sort(key=lambda h: h["month"], reverse=True)
    return out[:36]


def enrich_crif(draft: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """Adds EMI, interest, payment history, collateral, enquiry amounts, bureau totals, score factors and a
    non-identifying profile from a CRIF B2C report to a draft made by crif_ingest.map_crif."""
    b2c = raw.get("B2C-REPORT") or {}
    std = ((b2c.get("REPORT-DATA") or {}).get("STANDARD-DATA") or {})
    tl = [t for t in std.get("TRADELINES") or [] if isinstance(t, dict)]
    accts = draft.get("accounts") or []
    # the adapter keeps tradelines in order; join by position only when the counts agree
    if len(tl) == len(accts):
        for a, t in zip(accts, tl):
            hist = _crif_hist(t.get("HISTORY"))
            a.update({
                "account_no": a.get("account_no") or _mask(t.get("ACCT-NUMBER")),
                "emi": _money(t.get("INSTALLMENT-AMT")), "interest_rate": _rate(t.get("INTEREST-RATE")),
                "last_payment_date": _date(t.get("LAST-PAYMENT-DT")), "last_payment_amount": _money(t.get("LAST-PAID-AMOUNT") or t.get("ACTUAL-PAYMENT")),
                "reported_date": _date(t.get("REPORTED-DT")), "closed_date": _date(t.get("CLOSED-DT")),
                "collateral_type": next((_s(s.get("SECURITY-TYPE") or s.get("TYPE")) for s in t.get("SECURITY-DETAILS") or [] if isinstance(s, dict)), None) or None,
                "collateral_value": next((_money(s.get("SECURITY-VALUE") or s.get("VALUE")) for s in t.get("SECURITY-DETAILS") or [] if isinstance(s, dict)), None),
                "settlement_amount": _money(t.get("SETTLEMENT-AMT")), "written_off_amount": _money(t.get("WRITE-OFF-AMT")),
                "suit_filed": bool(_s(t.get("SUIT-FILED-WILFUL-DEFAULT-STATUS"))) or None,
                "ownership": _s(t.get("OWNERSHIP-TYPE")).title() or None, "lender_type": _s(t.get("CREDIT-GRANTOR-TYPE")) or None,
                "payment_history": hist,
            })
            if a.get("dpd_events") is None and hist:
                a["dpd_events"] = _late(hist)
    else:
        draft.setdefault("issues", []).append({"severity": "INFO", "code": "DETAILS_PARTIAL",
                                               "message": "Some account details (EMI, interest, month-by-month history) could not be matched and are not shown."})
    inq = [q for q in std.get("INQUIRY-HISTORY") or [] if isinstance(q, dict)]
    by_key = {(_s(q.get("LENDER-NAME")).lower(), _date(q.get("INQUIRY-DT"))): q for q in inq}
    for e in draft.get("enquiries") or []:
        q = by_key.get(((e.get("lender") or "").lower(), e.get("date")))
        e["kind"] = "credit"
        e["amount"] = _money(q.get("AMOUNT")) if q else None
    score = next((s for s in std.get("SCORE") or [] if isinstance(s, dict)), {})
    draft["score_factors"] = [_s(f.get("DESC") or f.get("DESCRIPTION") or f) for f in score.get("FACTORS") or [] if _s(f.get("DESC") if isinstance(f, dict) else f)][:8]
    draft["score_model"] = draft.get("score_model") or _s(score.get("NAME")) or None
    ps = ((b2c.get("REPORT-DATA") or {}).get("ACCOUNTS-SUMMARY") or {}).get("PRIMARY-ACCOUNTS-SUMMARY") or {}
    draft["bureau_summary"] = {
        "accounts_total": _int(ps.get("NUMBER-OF-ACCOUNTS")), "accounts_active": _int(ps.get("ACTIVE-ACCOUNTS")),
        "accounts_overdue": _int(ps.get("OVERDUE-ACCOUNTS")), "accounts_secured": _int(ps.get("SECURED-ACCOUNTS")),
        "accounts_unsecured": _int(ps.get("UNSECURED-ACCOUNTS")), "outstanding_total": _money(ps.get("TOTAL-CURRENT-BALANCE")),
        "outstanding_secured": _money(ps.get("CURRENT-BALANCE-SECURED")), "outstanding_unsecured": _money(ps.get("CURRENT-BALANCE-UNSECURED")),
        "sanctioned_total": _money(ps.get("TOTAL-SANCTIONED-AMT")), "overdue_total": _money(ps.get("TOTAL-AMT-OVERDUE")),
    }
    ap = ((b2c.get("REQUEST-DATA") or {}).get("APPLICANT-SEGMENT") or {})
    addr = next((x for x in ap.get("ADDRESSES") or [] if isinstance(x, dict)), {})
    emp = next((_d(x.get("EMPLOYMENT-DETAIL")) or x for x in std.get("EMPLOYMENT-DETAILS") or [] if isinstance(x, dict)), {})
    draft["profile"] = {
        "age": _int(_d(ap.get("DOB")).get("AGE")), "gender": _s(ap.get("GENDER")).title() or None,
        "city": _s(addr.get("CITY")).title() or None, "state": _s(addr.get("STATE")) or None,
        "employment": _s(emp.get("OCCUPATION")).title() or None,  # ACCT-TYPE here is the account the job was reported on, not a job
        "stated_income": _money(emp.get("INCOME")),
    }
    return draft


def map_any(raw: Any) -> Dict[str, Any]:
    """Detects the bureau format and returns the full draft."""
    if is_experian(raw):
        return map_experian(raw)
    if is_crif_b2c(raw):
        import crif_ingest
        return enrich_crif(crif_ingest.map_crif(raw), raw)
    raise FormatError("This report format is not supported yet. Supported: Experian, CRIF.")


def identity(raw: Any) -> Dict[str, Optional[str]]:
    """Name, PAN and mobile as the bureau has them (for matching to the account holder; never stored in the draft)."""
    if is_experian(raw):
        root = raw.get("data") if isinstance(raw.get("data"), dict) and "credit_report" in raw["data"] else raw
        ap = (((root.get("credit_report") or {}).get("Current_Application") or {}).get("Current_Application_Details") or {}).get("Current_Applicant_Details") or {}
        return {"name": _s(root.get("name")) or " ".join(x for x in (_s(ap.get("First_Name")), _s(ap.get("Last_Name"))) if x),
                "pan": (_s(root.get("pan")) or _s(ap.get("IncomeTaxPan"))).upper() or None,
                "mobile": re.sub(r"\D", "", _s(root.get("mobile")) or _s(ap.get("MobilePhoneNumber")))[-10:] or None}
    if is_crif_b2c(raw):
        ap = ((raw["B2C-REPORT"].get("REQUEST-DATA") or {}).get("APPLICANT-SEGMENT") or {})
        ids = [i for i in ap.get("IDS") or [] if isinstance(i, dict)]
        pan = next((_s(i.get("VALUE")) for i in ids if "PAN" in _s(i.get("TYPE")).upper()), None)
        ph = next((_s(p.get("VALUE")) for p in ap.get("PHONES") or [] if isinstance(p, dict)), "")
        return {"name": " ".join(x for x in (_s(ap.get("FIRST-NAME")), _s(ap.get("LAST-NAME"))) if x),
                "pan": pan.upper() if pan else None, "mobile": re.sub(r"\D", "", ph)[-10:] or None}
    return {"name": None, "pan": None, "mobile": None}


# ------------------------------------------------------------------ every field: sanitised copy + searchable sections
# Whole blocks that only describe who the person is (identity), never their credit.
_ID_BLOCKS = {"Current_Applicant_Details", "Current_Applicant_Address_Details", "Current_Applicant_Additional_AddressDetails",
              "CAIS_Holder_Details", "CAIS_Holder_Address_Details", "CAIS_Holder_Phone_Details", "CAIS_Holder_ID_Details",
              "APPLICANT-SEGMENT", "DEMOGS", "IDS", "ADDRESSES", "PHONES", "EMAILS", "CreditProfileHeader"}
_ID_TOKENS = {"PAN", "PHONE", "TELEPHONE", "MOBILE", "EMAIL", "MAIL", "ADDRESS", "ADDRESSTEXT", "PIN", "PINCODE", "DOB", "BIRTH",
              "PASSPORT", "VOTER", "AADHAAR", "AADHAR", "UID", "RATION", "LICENSE", "LICENCE", "FLAT", "BLDG", "ROAD", "LANDMARK",
              "LOCALITY", "HOUSE", "SOCIETY", "SURNAME", "USERNAME", "ID", "REF", "BATCH"}
_ID_KEYS = {"name", "PREPARED-FOR", "client_id", "Subscriber", "Enquiry_Username", "ReportNumber", "Subscriber_code"}
_KEEP_KEYS = {"Gender_Code", "GENDER", "AGE"}
# identity blocks that hold the person's name; with keep_name they are cleaned field by field instead of dropped
_NAME_BLOCKS = {"Current_Applicant_Details", "CAIS_Holder_Details", "APPLICANT-SEGMENT"}


def _tokens(key: str) -> List[str]:
    k = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(key))
    return [t for t in re.split(r"[^A-Za-z]+", k.upper()) if t]


def _name_key(key: str) -> bool:
    t = set(_tokens(key))
    return key in ("name", "Alias") or "SURNAME" in t or ("NAME" in t and bool(t & {"FIRST", "LAST", "MIDDLE", "FULL"}))


def _identity_key(key: str, keep_name: bool = False) -> bool:
    if key in _KEEP_KEYS:
        return False
    if keep_name and _name_key(key):
        return False
    if key in _ID_KEYS or key in _ID_BLOCKS:
        return True
    t = set(_tokens(key))
    if t & _ID_TOKENS:
        return True
    if "NAME" in t and t & {"FIRST", "LAST", "MIDDLE", "FULL", "FATHER", "MOTHER", "SPOUSE"}:
        return True
    if "NUMBER" in t and not t & {"OF", "NO"}:  # account / report / phone numbers; keeps NUMBER-OF-ACCOUNTS
        return True
    return False


_SCRUB_VAL = [re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.I), re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
              re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), re.compile(r"\b(?:\+?91[\s-]?)?[6-9]\d{9}\b"), re.compile(r"\b\d{11,18}\b")]


def sanitize(raw: Any, keep_name: bool = False) -> Any:
    """A deep copy of a bureau report with every identity field removed and identifier-like values masked.
    Every other field is kept exactly, so the credit record stays complete. keep_name=True keeps the person's
    name fields (used only for the account holder's own report); PAN, phone, email, address, date of birth,
    ID and account numbers are removed either way."""
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            if keep_name and str(k) in _NAME_BLOCKS and isinstance(v, dict):
                kept = sanitize({kk: vv for kk, vv in v.items() if kk not in _ID_BLOCKS}, keep_name=True)
                if kept:
                    out[k] = kept
                continue
            if _identity_key(str(k), keep_name):
                if str(k) == "Current_Applicant_Details" and isinstance(v, dict) and v.get("Gender_Code"):
                    out[k] = {"Gender_Code": v.get("Gender_Code")}
                elif str(k) == "APPLICANT-SEGMENT" and isinstance(v, dict):
                    out[k] = {kk: vv for kk, vv in (("GENDER", v.get("GENDER")), ("AGE", _d(v.get("DOB")).get("AGE"))) if vv}
                continue
            out[k] = sanitize(v, keep_name)
        return out
    if isinstance(raw, list):
        return [sanitize(x, keep_name) for x in raw]
    if isinstance(raw, str):
        s = raw
        for rx in _SCRUB_VAL:
            s = rx.sub("[masked]", s)
        return s
    return raw


def _label(key: str) -> str:
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in re.split(r"[-_]+", str(key)) if w)


def _flat(obj: Any, path: str, out: List[str]):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flat(v, f"{path} > {_label(k)}" if path else _label(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flat(v, f"{path} #{i + 1}", out)
    else:
        s = _s(obj)
        if s and s.lower() not in ("none", "null", "na", "n/a", "-", "?", "xxx"):
            out.append(f"{path}: {s}")


def field_sections(clean: Any) -> List[Dict[str, Any]]:
    """Every remaining field of a SANITISED report as readable 'path: value' lines, grouped by section and by
    account, so each group can be one or more search chunks. Pass sanitize(raw), never the raw report."""
    sections: List[Dict[str, Any]] = []

    def add(title, obj, meta):
        lines: List[str] = []
        _flat(obj, "", lines)
        if lines:
            sections.append({"title": title, "lines": lines, "meta": meta})

    if is_experian(clean) or (isinstance(clean, dict) and "credit_report" in clean):
        root = clean.get("data") if isinstance(clean.get("data"), dict) and "credit_report" in clean["data"] else clean
        cr = root.get("credit_report") or {}
        for k, v in cr.items():
            if k == "CAIS_Account" and isinstance(v, dict):
                add("Account summary", v.get("CAIS_Summary"), {"section": "CAIS_Summary"})
                for i, a in enumerate(v.get("CAIS_Account_DETAILS") or []):
                    if isinstance(a, dict):
                        code = _s(a.get("Account_Type")).zfill(2)
                        add(f"Account {i + 1}: {_s(a.get('Subscriber_Name')) or 'Lender'} {EXPERIAN_TYPE_NAMES.get(code, 'account')}", a,
                            {"section": "account", "index": i + 1, "lender": _s(a.get("Subscriber_Name"))})
            else:
                add(_label(k), v, {"section": k})
        add("Report header", {k: v for k, v in root.items() if k != "credit_report"}, {"section": "root"})
    elif is_crif_b2c(clean):
        b2c = clean["B2C-REPORT"]
        for k, v in b2c.items():
            if k == "REPORT-DATA" and isinstance(v, dict):
                for kk, vv in v.items():
                    if kk == "STANDARD-DATA" and isinstance(vv, dict):
                        for k3, v3 in vv.items():
                            if k3 == "TRADELINES" and isinstance(v3, list):
                                for i, t in enumerate(v3):
                                    if isinstance(t, dict):
                                        add(f"Account {i + 1}: {_s(t.get('CREDIT-GRANTOR')) or 'Lender'} {_s(t.get('ACCT-TYPE')) or 'account'}", t,
                                            {"section": "account", "index": i + 1, "lender": _s(t.get("CREDIT-GRANTOR"))})
                            else:
                                add(_label(k3), v3, {"section": k3})
                    else:
                        add(_label(kk), vv, {"section": kk})
            else:
                add(_label(k), v, {"section": k})
    else:
        add("Report", clean, {"section": "all"})
    return sections
