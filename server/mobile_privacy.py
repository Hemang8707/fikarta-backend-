"""
DPDP Act 2023 privacy centre for the Fikarta mobile API (mounted under /api/m/privacy).

Implements: notice before consent (s.5) · purpose-specific consent, withdrawal as easy as giving it (s.6) ·
right to a summary of personal data and sharing (s.11) · correction and erasure (s.12) · grievance redressal (s.13) ·
nomination (s.14) · erasure when the purpose ends or consent is withdrawn (s.8(7)) · published DPO contact (s.8(9)).
"""
import datetime as dt
import hashlib
import json
import os
import secrets
from typing import Dict, Optional

import clock

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from mobile_api import NOTICE_VERSION, audit, current_user, db, dec, mask_pan, notify, now, router

DPO = {"name": "Data Protection Officer, Fikarta", "email": "dpo@fikarta.com", "grievance_email": "grievance@fikarta.com", "response_days": 30}
PURPOSES = {
    "service": {"title": "Run my Fikarta account", "required": True,
                "data": ["Name", "Email", "Password (stored only as an Argon2id hash)", "Sign-in security records"],
                "why": "To create your account, keep you signed in securely and protect it from misuse."},
    "credit_report": {"title": "Process my credit report", "required": False,
                      "data": ["PAN (encrypted)", "Date of birth", "Mobile number", "Credit report: score, accounts, enquiries"],
                      "why": "To show your score, find errors, build your improvement plan and draft disputes. If you ask us to fetch your report, we send your name, PAN and mobile number to our authorised credit-bureau partner, which gets your report from CRIF High Mark. Fetching your own report is a soft enquiry and does not affect your score.",
                      "valid_days": 183},
    "reminders": {"title": "Payment reminders", "required": False,
                  "data": ["Loans and cards you add (lender, last 4 digits, due dates, amounts)", "Payments you record"],
                  "why": "To remind you before due dates and keep your payment history."},
    "ai_chat": {"title": "Fikarta AI chat", "required": False,
                "why": "To answer questions about your own report. Your question, your report summary and the relevant parts of your Fikarta data are sent to our AI service providers, under contract, to generate the answer; they are not used to train models by Fikarta. Fikarta AI remembers goals and preferences you tell it, and saves details you share in chat (such as income, work, city, household, rent and other EMIs) to your profile so later answers fit you; you can see, correct and delete these in Privacy Centre at any time.",
                "data": ["Your chat messages", "Your report summary and Fikarta records, without name, PAN, phone or email", "Goals and preferences you share in chat", "Details you share in chat: income, work, city, household, rent, other EMIs"]},
    "analytics": {"title": "Personalise Fikarta for me", "required": False,
                  "data": ["Screens you open and features you use", "App version, phone model and operating system", "Time of use"],
                  "why": "To put the tools you use most first and to find screens that are slow or broken. Kept for 180 days. Not your IP address, contacts, messages, files or location. Never sold and never sent to our AI provider."},
    "marketing": {"title": "Tips and offers", "required": False,
                  "data": ["Email", "Mobile number"],
                  "why": "To send credit tips and loan offers by email or SMS."},
}


def _expiry(purpose: str, granted_at: str) -> Optional[str]:
    days = PURPOSES.get(purpose, {}).get("valid_days")
    if not days:
        return None
    try:
        start = dt.datetime.fromisoformat(granted_at.rstrip("Z"))
    except Exception:
        start = clock.utcnow()
    return (start + dt.timedelta(days=days)).replace(microsecond=0).isoformat() + "Z"


def _latest(c, uid, purpose):
    return c.execute("SELECT * FROM consents WHERE user_id=? AND purpose=? ORDER BY id DESC LIMIT 1", (uid, purpose)).fetchone()


def consent_status(c, u, purpose) -> str:
    """granted | withdrawn | expired | never. Time-bound consents (credit report) lapse after `valid_days`;
    older grants recorded before expiry existed lapse the same number of days after they were given."""
    if PURPOSES[purpose]["required"]:
        return "granted"
    r = _latest(c, u["id"], purpose)
    if r is None:
        legacy = bool(u["consent_bureau"]) if purpose == "credit_report" else bool(u["consent_marketing"]) if purpose == "marketing" else False
        if not legacy:
            return "never"
        exp = _expiry(purpose, u["consent_at"] or u["created_at"] or clock.iso())
    else:
        if not r["granted"]:
            return "withdrawn"
        exp = r["expires_at"] if "expires_at" in r.keys() and r["expires_at"] else _expiry(purpose, r["at"])
    if exp and dt.datetime.fromisoformat(exp.rstrip("Z")) <= clock.utcnow():
        return "expired"
    return "granted"


def consent_expires_at(c, u, purpose) -> Optional[str]:
    r = _latest(c, u["id"], purpose)
    if r is None or not r["granted"]:
        return None
    return r["expires_at"] if "expires_at" in r.keys() and r["expires_at"] else _expiry(purpose, r["at"])


def consent_state(c, u) -> Dict[str, bool]:
    return {p: consent_status(c, u, p) == "granted" for p in PURPOSES}


def record_consent(c, uid, purpose, granted, lang="en"):
    at = now()
    c.execute("INSERT INTO consents(user_id,purpose,granted,notice_version,language,at,notice_digest,expires_at) VALUES(?,?,?,?,?,?,?,?)",
              (uid, purpose, int(granted), NOTICE_VERSION, lang, at, notice_digest(), _expiry(purpose, at) if granted else None))
    if purpose == "credit_report":
        c.execute("UPDATE users SET consent_bureau=?, consent_at=? WHERE id=?", (int(granted), at, uid))
    if purpose == "marketing":
        c.execute("UPDATE users SET consent_marketing=? WHERE id=?", (int(granted), uid))


def has_consent(c, u, purpose) -> bool:
    return consent_state(c, u).get(purpose, False)


def notice_content():
    return {
        "version": NOTICE_VERSION, "fiduciary": "Fikarta", "dpo": DPO,
        "purposes": [{"key": k, **v} for k, v in PURPOSES.items()],
        "rights": [
            {"key": "access", "title": "Know what we hold", "text": "Get a summary of your personal data, how it is used and who it is shared with."},
            {"key": "correct", "title": "Correct or update", "text": "Fix or complete any personal data that is wrong or out of date."},
            {"key": "erase", "title": "Erase", "text": "Erase your credit data or your whole account. We erase it unless a law requires us to keep it."},
            {"key": "withdraw", "title": "Withdraw consent", "text": "Switch off any optional purpose at any time, as easily as you switched it on."},
            {"key": "nominate", "title": "Nominate", "text": "Name a person who can exercise your rights if you die or become unable to."},
            {"key": "grievance", "title": "Raise a grievance", "text": f"We respond within {DPO['response_days']} days. If you are not satisfied, you can complain to the Data Protection Board of India."},
        ],
        "sharing": "We do not sell or rent your data. It is shared only with service providers that run Fikarta for us (hosting, and our AI service providers when you use Fikarta AI chat, without your name, PAN, phone or email), with our authorised credit-bureau partner and CRIF High Mark only when you ask us to fetch your credit report, and with authorities when the law requires it.",
        "retention": "We keep your data while your account is active. Credit data is erased when you withdraw consent for it or delete your account. Sign-in security records are kept for up to 1 year.",
        "security": "Your data is hosted with Amazon Web Services in Sydney, Australia. Data is encrypted in transit. Your PAN is encrypted at rest. Passwords are stored only as one-way Argon2id hashes. Access to your data is logged.",
        "breach": "If a personal data breach affects you, we will inform you and the Data Protection Board of India as the law requires.",
        "board": "Data Protection Board of India: you may complain to the Board after using our grievance process.",
        "children": "Fikarta is only for people aged 18 and above.",
    }


def notice_digest() -> str:
    """sha256 of the exact notice text shown to users (creditsage-lab policy_content_digest format)."""
    body = json.dumps(notice_content(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


@router.get("/privacy/notice")
def privacy_notice():
    return {**notice_content(), "digest": notice_digest()}


@router.get("/privacy/consents")
def get_consents(u=Depends(current_user)):
    c = db()
    rows = []
    for k, v in PURPOSES.items():
        st = consent_status(c, u, k)
        rows.append({"key": k, "title": v["title"], "why": v["why"], "data": v["data"], "required": v["required"], "granted": st == "granted",
                     "status": st, "expires_at": consent_expires_at(c, u, k) if st in ("granted", "expired") else None, "valid_days": v.get("valid_days")})
    hist = [dict(r) for r in c.execute("SELECT purpose,granted,notice_version,notice_digest,at,expires_at FROM consents WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))]
    c.close()
    return {"notice_version": NOTICE_VERSION, "notice_digest": notice_digest(), "consents": rows, "history": hist}


class ConsentIn(BaseModel):
    purpose: str
    granted: bool


@router.post("/privacy/consents")
def set_consent(body: ConsentIn, u=Depends(current_user)):
    if body.purpose not in PURPOSES or PURPOSES[body.purpose]["required"]:
        raise HTTPException(400, "This is needed to run your account. To stop all processing, delete your account.")
    c = db()
    record_consent(c, u["id"], body.purpose, body.granted, u["language"] or "en")
    audit(c, u["id"], "consent", f"{body.purpose}={body.granted}")
    if not body.granted and body.purpose == "analytics":
        c.execute("DELETE FROM app_events WHERE user_id=?", (u["id"],))
    if not body.granted and body.purpose == "ai_chat":
        c.execute("DELETE FROM chat_messages WHERE user_id=?", (u["id"],))
        for t in ("user_memories", "user_docs", "rag_traces", "user_profile_facts"):  # the AI's memory, index and chat details go with the chat consent
            c.execute(f"DELETE FROM {t} WHERE user_id=?", (u["id"],))
    c.commit()
    c.close()
    return get_consents(u)


@router.get("/privacy/summary")
def privacy_summary(u=Depends(current_user)):
    c = db()

    def q(t):
        return c.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (u["id"],)).fetchone()[0]

    out = {
        "profile": {"name": f"{u['first_name']} {u['last_name'] or ''}".strip(), "email": u["email"], "phone": u["phone"] or None,
                    "pan": mask_pan(dec(u["pan_enc"])), "dob": u["dob"], "city": u["city"] or None},
        "records": [{"label": l, "count": q(t)} for l, t in [
            ("Credit reports", "reports"), ("Score history points", "score_history"), ("Plan tasks", "plan_tasks"), ("Disputes", "disputes"),
            ("Letters", "letters"), ("Chat messages", "chat_messages"), ("Loans", "loans"), ("Cards", "cards"), ("Payments", "payments"),
            ("Reward entries", "rewards"), ("Notifications", "notifications"), ("Activity log entries", "audit_log"), ("Consent records", "consents"),
            ("Things Fikarta AI remembers", "user_memories"), ("Fikarta AI index entries", "user_docs"), ("App usage events", "app_events"), ("Details you shared in chat", "user_profile_facts")]],
        "shared_with": ["Hosting provider (stores data on our behalf)"]
                       + (["AI service providers (receive your question and report summary without identifiers, only when AI chat is on)"]
                          if (os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")) else [])
                       + ["Credit-bureau partner and CRIF High Mark (only when you fetch your report)"],
        "consents": consent_state(c, u),
        "account_created": u["created_at"],
    }
    c.close()
    return out


@router.post("/privacy/erase-credit-data")
def erase_credit(u=Depends(current_user)):
    c = db()
    for t in ("reports", "bureau_raw", "score_history", "plan_tasks", "disputes", "chat_messages", "user_docs", "user_memories", "rag_traces", "user_profile_facts"):
        c.execute(f"DELETE FROM {t} WHERE user_id=?", (u["id"],))
    c.execute("UPDATE users SET pan_enc=NULL WHERE id=?", (u["id"],))
    record_consent(c, u["id"], "credit_report", False)
    audit(c, u["id"], "erase_credit_data")
    c.commit()
    c.close()
    return {"ok": True}


class NomineeIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    relation: str = Field(min_length=2, max_length=40)
    contact: str = Field(min_length=5, max_length=120)


@router.get("/privacy/nominee")
def get_nominee(u=Depends(current_user)):
    c = db()
    r = c.execute("SELECT name,relation,contact,at FROM nominees WHERE user_id=?", (u["id"],)).fetchone()
    c.close()
    return dict(r) if r else None


@router.put("/privacy/nominee")
def put_nominee(body: NomineeIn, u=Depends(current_user)):
    c = db()
    c.execute("INSERT INTO nominees(user_id,name,relation,contact,at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, relation=excluded.relation, contact=excluded.contact, at=excluded.at",
              (u["id"], body.name.strip(), body.relation.strip(), body.contact.strip(), now()))
    audit(c, u["id"], "nominee_set")
    c.commit()
    c.close()
    return get_nominee(u)


@router.delete("/privacy/nominee")
def del_nominee(u=Depends(current_user)):
    c = db()
    c.execute("DELETE FROM nominees WHERE user_id=?", (u["id"],))
    audit(c, u["id"], "nominee_removed")
    c.commit()
    c.close()
    return {"ok": True}


class GrievanceIn(BaseModel):
    category: str = Field(min_length=2, max_length=60)
    message: str = Field(min_length=10, max_length=3000)


@router.get("/privacy/grievances")
def list_grievances(u=Depends(current_user)):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT ref,category,message,status,response,due_by,at FROM grievances WHERE user_id=? ORDER BY id DESC", (u["id"],))]
    c.close()
    return rows


@router.post("/privacy/grievances", status_code=201)
def new_grievance(body: GrievanceIn, u=Depends(current_user)):
    c = db()
    ref = "FKG" + clock.utcnow().strftime("%y%m%d") + secrets.token_hex(3).upper()
    due = (clock.today() + dt.timedelta(days=DPO["response_days"])).isoformat()
    c.execute("INSERT INTO grievances(user_id,ref,category,message,due_by,at,updated_at) VALUES(?,?,?,?,?,?,?)", (u["id"], ref, body.category, body.message, due, now(), now()))
    notify(c, u["id"], "grievance", f"Grievance {ref} received", f"Our Data Protection Officer will respond by {due}.")
    audit(c, u["id"], "grievance", ref)
    c.commit()
    c.close()
    return {"ref": ref, "due_by": due, "status": "open"}
