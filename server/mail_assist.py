"""Emails to banks and NBFCs from Fikarta AI, and the user's bank emails as AI memory.

Flow
- The user tells Fikarta AI about a problem with a lender. The AI explains the options and offers to draft an email.
- POST /mail/draft: Fikarta AI writes the email. The model only sees the problem (scrubbed) and non-identifying
  account details; the user's name, phone and masked account number are filled in by Fikarta after generation.
- The draft is shown in chat. The user can edit it. Nothing is sent until the user confirms that one email
  (POST /mail/drafts/{id}/send with confirm=true). With Gmail connected it goes from the user's own Gmail;
  otherwise the app opens the user's mail app with the draft filled in.

Lender addresses (lender_contacts)
- Built once from Fikarta's bank/NBFC directory (RBI-registered entities with grievance email, website). Several
  addresses per lender are kept in escalation order: customer care / grievance first, then nodal officers.
- If an email bounces (found in the user's Gmail during sync), that address is marked bounced, the next official
  address is used, and if none is left Fikarta reads the lender's own website for addresses on the lender's
  domain only. The corrected email is prepared again and the user confirms before it is sent.

Gmail (optional; needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET on the server)
- Separate consent ('email_access'). Read-only access to messages from known bank/NBFC domains, plus sending
  the drafts the user confirms. Tokens are stored encrypted. Bank emails become the user's own AI memory
  (doc type 'bank_email', scrubbed, never another user's). Disconnecting revokes access and deletes it all.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from mail_store import (_EMAIL, _domain, contacts_for, discover_from_website, generic_contacts, init, is_personal, lender_key, next_address,  # noqa: F401
                        seed_contacts)
import ai_assistant  # noqa: F401,E402  (loads GOOGLE_* and other keys from the git-ignored .env)
from mobile_api import SECRET, audit, current_user, db, dec, enc, latest_report, notify, now, router

log = logging.getLogger("fikarta.mail")

GMAIL_SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"


# ------------------------------------------------------------------ drafting
def _account_for(rep: Optional[dict], lender: str, ref: Optional[str]) -> Optional[dict]:
    if not rep:
        return None
    accts = rep.get("accounts") or []
    if ref:
        a = next((x for x in accts if x.get("ref") == ref), None)
        if a:
            return a
    key = set(lender_key(lender).split())
    best = max(accts, key=lambda a: len(key & set(lender_key(a.get("lender", "")).split())), default=None)
    return best if best and key & set(lender_key(best.get("lender", "")).split()) else None


def draft_text(problem: str, lender: str, account: Optional[dict], lang: str = "en") -> Tuple[str, str]:
    """Subject and body written by the model, with {NAME}, {PHONE}, {ACCOUNT} placeholders (filled in by Fikarta)."""
    import ai_assistant
    import user_rag
    acct = {k: account.get(k) for k in ("type", "status", "opened", "sanctioned", "limit", "balance", "overdue", "emi", "closed_date",
                                        "last_payment_date", "dpd_events")} if account else {}
    system = ("You write short, polite, firm emails from an Indian borrower to their bank or NBFC. Plain English, under 180 words. "
              "Use these exact placeholders and never invent their values: {NAME} for the borrower's name, {PHONE} for their mobile, "
              "{ACCOUNT} for the loan or card account number. State the problem, what the borrower wants (for example correction, "
              "closure certificate/NOC, refund, statement, stopping calls), and ask for a written reply with a reference number. "
              "Mention that unresolved complaints can be escalated to the RBI Integrated Ombudsman after 30 days only when it fits. "
              "Do not invent amounts, dates or facts that are not in the input. "
              'Reply as JSON: {"subject": "...", "body": "..."}')
    user = json.dumps({"lender": lender, "problem": user_rag.scrub(problem)[:1500], "account_from_credit_report": acct}, ensure_ascii=False, default=str)
    out = ai_assistant._groq_json(system, user, 700) if hasattr(ai_assistant, "_groq_json") else None
    if not isinstance(out, dict) or not out.get("body"):
        out = ai_assistant._gemini_json(system, user, 900)
    if not isinstance(out, dict) or not str(out.get("body", "")).strip():
        subj = f"Request regarding my account {{ACCOUNT}} with {lender}"
        body = (f"Dear {lender} Customer Care,\n\nI am writing about my account {{ACCOUNT}}.\n\n{user_rag.scrub(problem).strip()}\n\n"
                "Please look into this and reply in writing with a complaint reference number.\n\nRegards,\n{NAME}\nMobile: {PHONE}")
        return subj, body
    return str(out.get("subject") or f"Regarding my account with {lender}")[:160], str(out["body"])[:4000]


def fill(text: str, u, account: Optional[dict]) -> str:
    name = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or "Account holder"
    acct = (account or {}).get("account_no") or "[your account number]"
    phone = u["phone"] or "[your mobile number]"
    return text.replace("{NAME}", name).replace("{PHONE}", phone).replace("{ACCOUNT}", acct)


# ------------------------------------------------------------------ Gmail
def gmail_configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def _redirect_uri() -> str:
    return os.getenv("GOOGLE_REDIRECT_URI", "https://api.fikarta.com/api/m/mail/gmail/callback")


def _sign_state(uid: int) -> str:
    exp = int(time.time()) + 600
    msg = f"{uid}.{exp}"
    sig = hmac.new(SECRET if isinstance(SECRET, bytes) else str(SECRET).encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{msg}.{sig}"


def _check_state(state: str) -> Optional[int]:
    try:
        uid, exp, sig = state.split(".")
    except ValueError:
        return None
    good = hmac.new(SECRET if isinstance(SECRET, bytes) else str(SECRET).encode(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good) or int(exp) < time.time():
        return None
    return int(uid)


def _access_token(c, uid: int) -> Optional[str]:
    g = c.execute("SELECT * FROM gmail_links WHERE user_id=?", (uid,)).fetchone()
    if not g:
        return None
    if g["access_enc"] and (g["expires_at"] or 0) > time.time() + 60:
        return dec(g["access_enc"])
    try:
        r = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""), "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "refresh_token": dec(g["refresh_enc"]), "grant_type": "refresh_token"}, timeout=15)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        log.warning("gmail token refresh status=%s", r.status_code)
        return None
    j = r.json()
    c.execute("UPDATE gmail_links SET access_enc=?, expires_at=? WHERE user_id=?", (enc(j["access_token"]), time.time() + int(j.get("expires_in", 3600)), uid))
    return j["access_token"]


def gmail_send(c, uid: int, to: str, subject: str, body: str) -> Optional[Dict[str, str]]:
    tok = _access_token(c, uid)
    if not tok:
        return None
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"], msg["Subject"] = to, subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        r = httpx.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", headers={"Authorization": f"Bearer {tok}"},
                       json={"raw": raw}, timeout=20)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        log.warning("gmail send status=%s", r.status_code)
        return None
    j = r.json()
    return {"id": j.get("id", ""), "thread": j.get("threadId", "")}


def _bank_domains(c) -> List[str]:
    doms = {_domain(r[0]) for r in c.execute("SELECT DISTINCT email FROM lender_contacts WHERE source='directory'")}
    doms |= {"hdfcbank.net", "hdfcbank.com", "icicibank.com", "sbi.co.in", "sbicard.com", "axisbank.com", "kotak.com", "yesbank.in",
             "idfcfirstbank.com", "indusind.com", "bajajfinserv.in", "rblbank.com", "aubank.in", "hsbc.co.in", "americanexpress.com", "aexp.com"}
    return sorted(d for d in doms if d and "." in d and not d.endswith(("gmail.com", "yahoo.com", "outlook.com", "rediffmail.com", "hotmail.com")))


def _body_text(payload: dict) -> str:
    def walk(p):
        if p.get("mimeType", "").startswith("text/plain") and p.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", "ignore")
        for q in p.get("parts") or []:
            t = walk(q)
            if t:
                return t
        return ""
    t = walk(payload)
    if not t and payload.get("body", {}).get("data"):
        t = re.sub(r"<[^>]+>", " ", base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "ignore"))
    return re.sub(r"\s+", " ", t).strip()


def gmail_sync(c, uid: int, max_msgs: int = 80) -> Dict[str, int]:
    """Bank/NBFC emails (last 12 months) into bank_emails + the user's AI index; detects bounces of Fikarta drafts."""
    import user_rag
    tok = _access_token(c, uid)
    if not tok:
        return {"fetched": 0, "bounces": 0}
    doms = _bank_domains(c)
    got, bounces = 0, 0
    H = {"Authorization": f"Bearer {tok}"}
    # 1. bank emails, in batches of domains (Gmail query length limit)
    for i in range(0, len(doms), 40):
        q = "newer_than:365d from:(" + " OR ".join(doms[i:i + 40]) + ")"
        try:
            r = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages", headers=H, params={"q": q, "maxResults": max_msgs}, timeout=20)
        except httpx.HTTPError:
            break
        if r.status_code != 200:
            break
        for m in (r.json().get("messages") or [])[:max_msgs]:
            if c.execute("SELECT 1 FROM bank_emails WHERE user_id=? AND msg_id=?", (uid, m["id"])).fetchone():
                continue
            try:
                d = httpx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}", headers=H, params={"format": "full"}, timeout=20).json()
            except (httpx.HTTPError, ValueError):
                continue
            hdr = {h["name"].lower(): h["value"] for h in d.get("payload", {}).get("headers", [])}
            frm = (_EMAIL.findall(hdr.get("from", "")) or [""])[0]
            when = dt.datetime.fromtimestamp(int(d.get("internalDate", "0")) / 1000, dt.timezone.utc).date().isoformat()
            text = user_rag.scrub(_body_text(d.get("payload", {})) or d.get("snippet", ""))[:2500]
            lender = next((r2[0] for r2 in c.execute("SELECT name FROM lender_contacts WHERE email LIKE ? LIMIT 1", ("%@" + _domain(frm),))), _domain(frm))
            c.execute("INSERT OR IGNORE INTO bank_emails(user_id,msg_id,from_domain,lender,subject,received,text) VALUES(?,?,?,?,?,?,?)",
                      (uid, m["id"], _domain(frm), lender, user_rag.scrub(hdr.get("subject", ""))[:200], when, text))
            got += 1
    # 2. bounces of emails Fikarta sent for this user
    sent = [dict(r) for r in c.execute("SELECT * FROM email_drafts WHERE user_id=? AND status='sent' AND via='gmail'", (uid,))]
    if sent:
        try:
            r = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages", headers=H,
                          params={"q": "newer_than:30d from:(mailer-daemon OR postmaster)", "maxResults": 20}, timeout=20)
            msgs = r.json().get("messages") or [] if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            msgs = []
        for m in msgs:
            try:
                d = httpx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}", headers=H, params={"format": "full"}, timeout=20).json()
            except (httpx.HTTPError, ValueError):
                continue
            blob = _body_text(d.get("payload", {})) + " " + d.get("snippet", "")
            for s in sent:
                if s["to_addr"] and s["to_addr"].lower() in blob.lower() and (s["thread_id"] and s["thread_id"] == d.get("threadId") or s["subject"][:40] in blob):
                    bounces += _handle_bounce(c, uid, s)
    c.execute("UPDATE gmail_links SET last_sync=? WHERE user_id=?", (now(), uid))
    user_rag.sync_user_docs(c, uid, latest_report(c, uid, raw=True), now())
    return {"fetched": got, "bounces": bounces}


def _handle_bounce(c, uid: int, s: dict) -> int:
    c.execute("UPDATE email_drafts SET status='bounced' WHERE id=?", (s["id"],))
    new = next_address(c, s["lender"], bounced=s["to_addr"])
    if not new:
        notify(c, uid, "mail", f"Email to {s['lender']} bounced", "No other official address was found. Please check the lender's website for the right address.")
        return 1
    c.execute("INSERT INTO email_drafts(user_id,lender,to_addr,subject,body,status,created_at,note) VALUES(?,?,?,?,?,?,?,?)",
              (uid, s["lender"], new, s["subject"], s["body"], "draft", now(), f"Resend: {s['to_addr']} bounced"))
    notify(c, uid, "mail", f"Email to {s['lender']} bounced", f"It was not delivered to {s['to_addr']}. Fikarta found the official address {new}. Open Fikarta AI to review and send it again.")
    audit(c, uid, "mail_bounced", f"{s['lender']}:{s['to_addr']}->{new}")
    return 1


# ------------------------------------------------------------------ API
def _consented(c, uid: int) -> bool:
    r = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='email_access' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    return bool(r and r["granted"])


def _draft_out(r) -> Dict[str, Any]:
    d = dict(r)
    return {k: d.get(k) for k in ("id", "lender", "to_addr", "subject", "body", "status", "via", "created_at", "sent_at", "note")}


class DraftIn(BaseModel):
    lender: str = Field(min_length=2, max_length=120)
    problem: str = Field(min_length=3, max_length=2000)
    account_ref: Optional[str] = Field(None, max_length=60)


class DraftEdit(BaseModel):
    to_addr: Optional[str] = Field(None, max_length=200)
    subject: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = Field(None, max_length=6000)


class SendIn(BaseModel):
    confirm: bool


@router.get("/mail/status")
def mail_status(u=Depends(current_user)):
    c = db()
    try:
        g = c.execute("SELECT email, connected_at, last_sync FROM gmail_links WHERE user_id=?", (u["id"],)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM bank_emails WHERE user_id=?", (u["id"],)).fetchone()[0]
        return {"gmail_available": gmail_configured(), "connected": bool(g), "email": g["email"] if g else None,
                "connected_at": g["connected_at"] if g else None, "last_sync": g["last_sync"] if g else None, "bank_emails": n,
                "pending_drafts": [_draft_out(r) for r in c.execute("SELECT * FROM email_drafts WHERE user_id=? AND status='draft' ORDER BY id DESC LIMIT 5", (u["id"],))]}
    finally:
        c.close()


@router.get("/mail/contacts")
def mail_contacts(lender: str = Query(..., min_length=2, max_length=120), u=Depends(current_user)):
    c = db()
    try:
        out = {"lender": lender, "contacts": generic_contacts(c, lender)[:6]}
        c.commit()
        return out
    finally:
        c.close()


@router.post("/mail/draft", status_code=201)
def mail_draft(body: DraftIn, u=Depends(current_user)):
    c = db()
    try:
        rep = latest_report(c, u["id"], raw=True)
        acct = _account_for(rep, body.lender, body.account_ref)
        subj, text = draft_text(body.problem, body.lender, acct, u["language"] or "en")
        subj, text = fill(subj, u, acct), fill(text, u, acct)
        cs = generic_contacts(c, body.lender)  # generic customer care first; never a named person
        to = cs[0]["email"] if cs else None
        cur = c.execute("INSERT INTO email_drafts(user_id,lender,to_addr,subject,body,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        (u["id"], body.lender.strip(), to, subj, text, "draft", now()))
        audit(c, u["id"], "mail_drafted", body.lender[:60])
        c.commit()
        out = _draft_out(c.execute("SELECT * FROM email_drafts WHERE id=?", (cur.lastrowid,)).fetchone())
        out["other_addresses"] = [x["email"] for x in cs[1:5]]
        out["gmail_connected"] = bool(c.execute("SELECT 1 FROM gmail_links WHERE user_id=?", (u["id"],)).fetchone())
        return out
    finally:
        c.close()


@router.get("/mail/drafts")
def mail_drafts(u=Depends(current_user)):
    c = db()
    try:
        return [_draft_out(r) for r in c.execute("SELECT * FROM email_drafts WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))]
    finally:
        c.close()


@router.put("/mail/drafts/{did}")
def mail_edit(did: int, body: DraftEdit, u=Depends(current_user)):
    c = db()
    try:
        r = c.execute("SELECT * FROM email_drafts WHERE id=? AND user_id=?", (did, u["id"])).fetchone()
        if not r or r["status"] != "draft":
            raise HTTPException(404, "Draft not found")
        if body.to_addr is not None and body.to_addr.strip() and not _EMAIL.fullmatch(body.to_addr.strip()):
            raise HTTPException(400, "Enter a valid email address")
        c.execute("UPDATE email_drafts SET to_addr=COALESCE(?,to_addr), subject=COALESCE(?,subject), body=COALESCE(?,body) WHERE id=?",
                  ((body.to_addr or "").strip() or None if body.to_addr is not None else None, body.subject, body.body, did))
        c.commit()
        return _draft_out(c.execute("SELECT * FROM email_drafts WHERE id=?", (did,)).fetchone())
    finally:
        c.close()


@router.post("/mail/drafts/{did}/send")
def mail_send(did: int, body: SendIn, u=Depends(current_user)):
    """Sends ONE confirmed draft. Without Gmail, returns a mailto link so the user's own mail app sends it."""
    if body.confirm is not True:
        raise HTTPException(400, "Please confirm before sending")
    c = db()
    try:
        r = c.execute("SELECT * FROM email_drafts WHERE id=? AND user_id=?", (did, u["id"])).fetchone()
        if not r or r["status"] != "draft":
            raise HTTPException(404, "Draft not found")
        if not r["to_addr"]:
            raise HTTPException(400, "Add the bank's email address first")
        if c.execute("SELECT COUNT(*) FROM email_drafts WHERE user_id=? AND status='sent' AND sent_at>=?",
                     (u["id"], (dt.datetime.utcnow() - dt.timedelta(days=1)).isoformat())).fetchone()[0] >= 10:
            raise HTTPException(429, "You have sent 10 emails today. Please try again tomorrow.")
        linked = c.execute("SELECT 1 FROM gmail_links WHERE user_id=?", (u["id"],)).fetchone()
        if linked and _consented(c, u["id"]):
            res = gmail_send(c, u["id"], r["to_addr"], r["subject"], r["body"])
            if not res:
                raise HTTPException(503, "gmail_send_failed")
            c.execute("UPDATE email_drafts SET status='sent', via='gmail', provider_id=?, thread_id=?, sent_at=? WHERE id=?",
                      (res["id"], res["thread"], now(), did))
            audit(c, u["id"], "mail_sent", f"gmail:{r['lender'][:40]}:{r['to_addr']}")
            c.commit()
            return {"status": "sent", "via": "gmail", "to": r["to_addr"]}
        link = f"mailto:{r['to_addr']}?subject={quote(r['subject'])}&body={quote(r['body'])}"
        c.execute("UPDATE email_drafts SET status='sent', via='mail_app', sent_at=? WHERE id=?", (now(), did))
        audit(c, u["id"], "mail_handed_to_app", f"{r['lender'][:40]}:{r['to_addr']}")
        c.commit()
        return {"status": "open_mail_app", "via": "mail_app", "mailto": link, "to": r["to_addr"]}
    finally:
        c.close()


@router.delete("/mail/drafts/{did}")
def mail_cancel(did: int, u=Depends(current_user)):
    c = db()
    try:
        c.execute("UPDATE email_drafts SET status='cancelled' WHERE id=? AND user_id=? AND status='draft'", (did, u["id"]))
        c.commit()
        return {"ok": True}
    finally:
        c.close()


@router.get("/mail/gmail/connect")
def gmail_connect(u=Depends(current_user)):
    if not gmail_configured():
        raise HTTPException(503, "gmail_not_configured")
    c = db()
    try:
        if not _consented(c, u["id"]):
            raise HTTPException(403, "consent_required")
    finally:
        c.close()
    q = urlencode({"client_id": os.getenv("GOOGLE_CLIENT_ID"), "redirect_uri": _redirect_uri(), "response_type": "code",
                   "scope": GMAIL_SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
                   "state": _sign_state(u["id"])})
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + q}


_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Fikarta</title>
<body style="font-family:system-ui;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;background:#F3EEF9;color:#1B1340">
<div style="max-width:340px;padding:28px;text-align:center"><h2 style="font-weight:500">{title}</h2><p style="line-height:1.5">{msg}</p></div></body>"""


@router.get("/mail/gmail/callback", response_class=HTMLResponse)
def gmail_callback(code: str = "", state: str = "", error: str = ""):
    uid = _check_state(state)
    if error or not code or not uid:
        return HTMLResponse(_PAGE.format(title="Not connected", msg="Gmail was not connected. You can close this page and try again from the Fikarta app."), status_code=400)
    try:
        r = httpx.post("https://oauth2.googleapis.com/token", data={"code": code, "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
                       "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""), "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"}, timeout=15)
        j = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        j = {}
    if not j.get("refresh_token"):
        return HTMLResponse(_PAGE.format(title="Not connected", msg="Google did not grant access. Please try again from the Fikarta app."), status_code=400)
    email = None
    try:
        info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {j['access_token']}"}, timeout=10).json()
        email = info.get("email")
    except (httpx.HTTPError, ValueError):
        pass
    c = db()
    try:
        c.execute("INSERT INTO gmail_links(user_id,email,refresh_enc,access_enc,expires_at,connected_at) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET email=excluded.email, refresh_enc=excluded.refresh_enc, access_enc=excluded.access_enc, "
                  "expires_at=excluded.expires_at, connected_at=excluded.connected_at",
                  (uid, email, enc(j["refresh_token"]), enc(j["access_token"]), time.time() + int(j.get("expires_in", 3600)), now()))
        audit(c, uid, "gmail_connected", email or "")
        c.commit()
    finally:
        c.close()
    return HTMLResponse(_PAGE.format(title="Gmail connected", msg="You can close this page and go back to the Fikarta app. Fikarta will read emails from your banks and lenders to answer your questions."))


@router.post("/mail/gmail/sync")
def gmail_sync_now(u=Depends(current_user)):
    c = db()
    try:
        if not c.execute("SELECT 1 FROM gmail_links WHERE user_id=?", (u["id"],)).fetchone():
            raise HTTPException(404, "Gmail is not connected")
        if not _consented(c, u["id"]):
            raise HTTPException(403, "consent_required")
        res = gmail_sync(c, u["id"])
        c.commit()
        return res
    finally:
        c.close()


@router.delete("/mail/gmail")
def gmail_disconnect(u=Depends(current_user)):
    c = db()
    try:
        g = c.execute("SELECT refresh_enc FROM gmail_links WHERE user_id=?", (u["id"],)).fetchone()
        if g:
            try:
                httpx.post("https://oauth2.googleapis.com/revoke", params={"token": dec(g["refresh_enc"])}, timeout=10)
            except httpx.HTTPError:
                pass
        purge_gmail(c, u["id"])
        audit(c, u["id"], "gmail_disconnected")
        c.commit()
        return {"ok": True}
    finally:
        c.close()


def purge_gmail(c, uid: int):
    c.execute("DELETE FROM gmail_links WHERE user_id=?", (uid,))
    c.execute("DELETE FROM bank_emails WHERE user_id=?", (uid,))
    c.execute("DELETE FROM user_docs WHERE user_id=? AND doc_type='bank_email'", (uid,))
