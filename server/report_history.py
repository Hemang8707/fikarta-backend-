"""Month-to-month history of a user's credit reports.

Every saved report is a full snapshot (reports table, never overwritten). This module compares consecutive
snapshots deterministically - no AI involved - so 'what changed and why' is always exact:
score, card usage, accounts opened/closed, status changes (e.g. settled, written off), balances, overdue
amounts, new late payments and new enquiries.

Rules (same as the credit engine):
- UNKNOWN != ZERO: a change is only stated when both months have the value. A value that appears or
  disappears is reported as 'now shown' / 'no longer shown', never as a jump from or to zero.
- Accounts are matched across months by masked account number when the bureau gives one, otherwise by
  lender + type + open date. Unmatched accounts are 'new' or 'no longer on the report', never guessed.
"""
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

MAX_SNAPSHOTS = 24  # two years of monthly reports


def _d(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _inr(v: Optional[Decimal]) -> str:
    if v is None:
        return "not shown"
    n = int(round(v))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    return ("-" if n < 0 else "") + "Rs " + s


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def account_key(a: Dict[str, Any]) -> str:
    acct = str(a.get("account_no") or "")
    if re.search(r"\d{3,}", acct):
        return f"no:{_norm(a.get('lender'))}:{_norm(acct)}"
    return f"lto:{_norm(a.get('lender'))}:{_norm(a.get('type'))}:{a.get('opened') or ''}"


def snapshots(c, uid: int, limit: int = MAX_SNAPSHOTS) -> List[Dict[str, Any]]:
    """Newest first. Each item is the stored report with 'pulled_at' and 'snapshot_id'."""
    out = []
    for r in c.execute("SELECT id, data, pulled_at FROM reports WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)):
        d = json.loads(r["data"])
        if d.get("is_sandbox"):
            continue
        d["pulled_at"], d["snapshot_id"] = r["pulled_at"], r["id"]
        out.append(d)
    return out


def _label(rep: Dict[str, Any]) -> str:
    return str(rep.get("report_date") or str(rep.get("pulled_at") or "")[:10])


def diff(prev: Dict[str, Any], cur: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Change events from prev -> cur. Each: kind, title, text, meta (machine-readable old/new values)."""
    ev: List[Dict[str, Any]] = []
    frm, to = _label(prev), _label(cur)

    def add(kind, title, text, **meta):
        ev.append({"kind": kind, "title": title, "text": text, "meta": {"from": frm, "to": to, **meta}})

    ps, cs = prev.get("score"), cur.get("score")
    if ps is not None and cs is not None and ps != cs:
        delta = cs - ps
        add("score", f"Score {'up' if delta > 0 else 'down'} {abs(delta)} points",
            f"Credit score changed from {ps} to {cs} ({'+' if delta > 0 else ''}{delta}) between the reports of {frm} and {to}.", old=ps, new=cs, delta=delta)
    elif ps is None and cs is not None:
        add("score", "Score now shown", f"The report of {to} shows a score of {cs}; the earlier report had none.", old=None, new=cs)

    pu, cu = prev.get("utilisation"), cur.get("utilisation")
    if pu is not None and cu is not None and abs(float(cu) - float(pu)) >= 1:
        add("utilisation", f"Card usage {'up' if cu > pu else 'down'} to {cu}%",
            f"Credit card usage went from {pu}% to {cu}% of limits between {frm} and {to}.", old=pu, new=cu)

    pe, ce = prev.get("enquiries_6m"), cur.get("enquiries_6m")
    old_enq = {(_norm(e.get("lender")), e.get("date")) for e in prev.get("enquiries", [])}
    new_enq = [e for e in cur.get("enquiries", []) if (_norm(e.get("lender")), e.get("date")) not in old_enq]
    if new_enq:
        lst = "; ".join(f"{e.get('lender')} for {e.get('purpose')} on {e.get('date')}" for e in new_enq[:10])
        add("enquiries", f"{len(new_enq)} new {'enquiry' if len(new_enq) == 1 else 'enquiries'}",
            f"New hard enquiries since {frm}: {lst}. Enquiries in the last 6 months: {pe} then, {ce} now.", count=len(new_enq), old_6m=pe, new_6m=ce)

    pa = {account_key(a): a for a in prev.get("accounts", [])}
    ca = {account_key(a): a for a in cur.get("accounts", [])}
    for k, a in ca.items():
        name = f"{a.get('lender')} {a.get('type')}"
        if k not in pa:
            add("account_new", f"New account: {name}", f"{name} appears on the report of {to} (opened {a.get('opened') or 'date not shown'}, status {a.get('status')}).",
                account=k, lender=a.get("lender"), type=a.get("type"))
            continue
        p = pa[k]
        if p.get("status") != a.get("status"):
            add("account_status", f"{name}: {p.get('status')} to {a.get('status')}",
                f"{name} status changed from {p.get('status')} to {a.get('status')} between {frm} and {to}.", account=k, old=p.get("status"), new=a.get("status"))
        for fld, label in (("balance", "Balance"), ("overdue", "Overdue")):
            ov, nv = _d(p.get(fld)), _d(a.get(fld))
            if ov is not None and nv is not None and ov != nv and abs(nv - ov) >= max(Decimal(500), ov * Decimal("0.01")):
                add(f"account_{fld}", f"{name}: {label.lower()} {'up' if nv > ov else 'down'}",
                    f"{name} {label.lower()} went from {_inr(ov)} to {_inr(nv)} between {frm} and {to}.", account=k, old=str(ov), new=str(nv))
            elif ov is None and nv is not None and fld == "overdue" and nv > 0:
                add("account_overdue", f"{name}: overdue now shown", f"{name} now shows {_inr(nv)} overdue (the earlier report did not show this amount).", account=k, old=None, new=str(nv))
        od, nd = p.get("dpd_events"), a.get("dpd_events")
        if od is not None and nd is not None and nd > od:
            add("late_payment", f"{name}: new late payment", f"{name} shows {nd} late months in the last 24, up from {od} in the report of {frm}.", account=k, old=od, new=nd)
    for k, p in pa.items():
        if k not in ca:
            add("account_gone", f"No longer on report: {p.get('lender')} {p.get('type')}",
                f"{p.get('lender')} {p.get('type')} was on the report of {frm} but is not on the report of {to}.", account=k)
    order = {"score": 0, "late_payment": 1, "account_status": 2, "account_overdue": 3, "utilisation": 4, "enquiries": 5, "account_new": 6, "account_gone": 7, "account_balance": 8}
    ev.sort(key=lambda e: order.get(e["kind"], 9))
    return ev


def timeline(snaps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Changes between every consecutive pair, newest pair first."""
    out = []
    for newer, older in zip(snaps, snaps[1:]):
        out.append({"from": _label(older), "to": _label(newer), "changes": diff(older, newer)})
    return out


def account_histories(snaps: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per account (matched across months): its values in each report, oldest first."""
    hist: Dict[str, Dict[str, Any]] = {}
    for rep in reversed(snaps):
        for a in rep.get("accounts", []):
            k = account_key(a)
            h = hist.setdefault(k, {"lender": a.get("lender"), "type": a.get("type"), "points": []})
            h["points"].append({"date": _label(rep), "status": a.get("status"), "balance": a.get("balance"),
                                "overdue": a.get("overdue"), "late_months": a.get("dpd_events")})
    return hist


def change_facts(snaps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Plain-English facts for the assistant about the latest month's changes (None if only one report)."""
    if len(snaps) < 2:
        return None
    t = timeline(snaps[:2])[0]
    return {"Compared reports": f"{t['from']} -> {t['to']}", "Changes": [e["text"] for e in t["changes"]] or ["No changes between these two reports."],
            "Reports on file": len(snaps)}
