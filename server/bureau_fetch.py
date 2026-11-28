"""Fetch the user's own CRIF report through Surepass.

Safeguards (a bureau pull costs money and exposes highly sensitive data):
- Only the account holder's own report: the request always uses the PAN and name saved on the account,
  never a PAN typed at fetch time. After a successful fetch the PAN is locked (see mobile_api.update_me),
  and a PAN can only ever be fetched by one Fikarta account.
- Consent: the 'credit_report' purpose must be active AND the user ticks an explicit consent for this pull.
  The exact consent text's sha256 is stored with every attempt (evidence for an audit).
- Cost and abuse limits: 1 successful fetch per user per SUREPASS_COOLDOWN_DAYS (default 30),
  3 attempts per user per 24 h, SUREPASS_DAILY_CAP attempts per day across all users (default 300).
- The raw bureau response is never stored or logged; only the mapped report is saved.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

import clock
import surepass_crif as sp
from mobile_api import (PHONE_RE, SECRET, audit, client_ip, current_user, db, dec, mask_pan, now,
                        require_report_consent, router, save_report)

CONSENT_TEXT = ("I ask Fikarta to get my credit report from CRIF High Mark. Fikarta will send my name, PAN and mobile "
                "number to its authorised credit-bureau partner, which requests the report from CRIF. "
                "This is a soft enquiry and does not affect my score. Fikarta uses the report only to show my credit "
                "information and improvement plan.")
CONSENT_SHA = "sha256:" + hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest()


def _int_env(name, default):
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        return default


def cooldown_days():
    return _int_env("SUREPASS_COOLDOWN_DAYS", 30)


def _init():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS bureau_pulls(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, pan_hash TEXT NOT NULL, bureau TEXT,
        status TEXT NOT NULL, detail TEXT, report_id TEXT, consent_sha TEXT, ip TEXT, at TEXT NOT NULL, epoch INTEGER NOT NULL)""")
    try:
        c.execute("ALTER TABLE bureau_pulls ADD COLUMN shape TEXT")  # field paths of the response, never values
    except sqlite3.OperationalError:
        pass  # already there (or added by another worker)
    c.execute("CREATE INDEX IF NOT EXISTS ix_bp_user ON bureau_pulls(user_id, epoch)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_bp_pan ON bureau_pulls(pan_hash, status)")
    c.commit()
    c.close()


_init()


def pan_hash(pan: str) -> str:
    return hmac.new(("bureau-pan:" + SECRET).encode(), pan.upper().encode(), hashlib.sha256).hexdigest()


def has_successful_pull(c, uid) -> bool:
    return c.execute("SELECT 1 FROM bureau_pulls WHERE user_id=? AND status='success'", (uid,)).fetchone() is not None


def _last_success(c, uid):
    return c.execute("SELECT at, epoch FROM bureau_pulls WHERE user_id=? AND status='success' ORDER BY epoch DESC LIMIT 1", (uid,)).fetchone()


@router.get("/credit/fetch/status")
def fetch_status(u=Depends(current_user)):
    c = db()
    try:
        last = _last_success(c, u["id"])
        nxt = None
        if last:
            nxt = (dt.datetime.fromtimestamp(last["epoch"] + cooldown_days() * 86400, dt.timezone.utc)).date().isoformat()
            if nxt <= clock.today().isoformat():
                nxt = None
        pan = dec(u["pan_enc"])
        return {
            "available": sp.configured(), "bureau": sp.BUREAU, "cooldown_days": cooldown_days(),
            "last_fetched": last["at"] if last else None, "next_allowed": nxt,
            "name": f"{u['first_name']} {u['last_name'] or ''}".strip(), "pan": mask_pan(pan) if pan else None,
            "phone": u["phone"] or None, "consent_text": CONSENT_TEXT,
        }
    finally:
        c.close()


class FetchIn(BaseModel):
    mobile: str
    consent: bool


@router.post("/credit/fetch", status_code=201)
def fetch_crif(body: FetchIn, req: Request, u=Depends(current_user)):
    if not sp.configured():
        raise HTTPException(503, "bureau_not_configured")
    if body.consent is not True:
        raise HTTPException(400, "Please tick the consent to fetch your report")
    mobile = re.sub(r"\D", "", body.mobile)[-10:]
    if not PHONE_RE.match(mobile):
        raise HTTPException(400, "Enter the 10-digit mobile number registered with your bank")
    pan = dec(u["pan_enc"])
    if not pan:
        raise HTTPException(400, "Add your PAN in your profile first")
    if len(pan) == 10 and pan[3].upper() != "P":
        raise HTTPException(400, "This PAN is not a personal PAN (its 4th letter must be P). Please check the PAN in your profile.")
    first, last = (u["first_name"] or "").strip(), (u["last_name"] or "").strip()
    if not first:
        raise HTTPException(400, "Add your name as per PAN in your profile first")
    ph = pan_hash(pan)
    ip = client_ip(req)
    epoch = clock.epoch()
    c = db()
    try:
        require_report_consent(c, u)
        if c.execute("SELECT 1 FROM bureau_pulls WHERE pan_hash=? AND status='success' AND user_id<>? AND user_id IN (SELECT id FROM users)", (ph, u["id"])).fetchone():
            raise HTTPException(409, "This PAN is already linked to another Fikarta account. Contact support if this is your PAN.")
        last_ok = _last_success(c, u["id"])
        if last_ok and epoch - last_ok["epoch"] < cooldown_days() * 86400:
            nxt = dt.datetime.fromtimestamp(last_ok["epoch"] + cooldown_days() * 86400, dt.timezone.utc).date()
            raise HTTPException(429, f"You can fetch a fresh report from {nxt.strftime('%d %b %Y')}.")
        if c.execute("SELECT COUNT(*) FROM bureau_pulls WHERE user_id=? AND epoch>=?", (u["id"], epoch - 86400)).fetchone()[0] >= 3:
            raise HTTPException(429, "Too many attempts today. Please try again tomorrow.")
        if c.execute("SELECT COUNT(*) FROM bureau_pulls WHERE epoch>=?", (epoch - 86400,)).fetchone()[0] >= _int_env("SUREPASS_DAILY_CAP", 300):
            raise HTTPException(503, "bureau_busy")
        cur = c.execute("INSERT INTO bureau_pulls(user_id,pan_hash,bureau,status,consent_sha,ip,at,epoch) VALUES(?,?,?,?,?,?,?,?)",
                        (u["id"], ph, sp.BUREAU, "pending", CONSENT_SHA, ip, now(), epoch))
        pull_id = cur.lastrowid
        audit(c, u["id"], "bureau_fetch_consent", f"{sp.BUREAU}:{CONSENT_SHA}", ip)
        c.commit()
    finally:
        c.close()

    # the network call happens with no database connection held open
    try:
        resp = sp.fetch_report(first, last, mobile, pan)
        draft = sp.map_surepass_crif(resp)
        if sp.environment() == "sandbox":
            # the sandbox answers every PAN with the same sample report; never present it as the user's own data
            draft["test_data"] = True
            draft.setdefault("issues", []).append({"severity": "WARNING", "code": "SANDBOX_TEST_DATA",
                                                   "message": "This is a sample report from the bureau's test environment, not your real CRIF report."})
    except sp.BureauError as e:
        c = db()
        c.execute("UPDATE bureau_pulls SET status=?, detail=? WHERE id=?", ("no_match" if e.kind == "no_match" else "error", f"{e.kind}:{e.detail}"[:120], pull_id))
        audit(c, u["id"], "bureau_fetch_failed", e.kind, ip)
        c.commit()
        c.close()
        if e.kind == "no_match":
            raise HTTPException(422, "bureau_no_match")
        if e.kind == "invalid_input":
            raise HTTPException(422, "bureau_invalid_input")
        raise HTTPException(503, "bureau_unavailable")

    c = db()
    try:
        rep = save_report(c, u, draft)
        from mobile_api import save_bureau_raw
        save_bureau_raw(c, u["id"], sp.BUREAU, draft.get("report_id"), resp)
        shape = sorted(sp.response_shape(resp))
        c.execute("UPDATE bureau_pulls SET status='success', report_id=?, shape=? WHERE id=?", (draft.get("report_id"), json.dumps(shape), pull_id))
        if not u["phone"]:
            c.execute("UPDATE users SET phone=? WHERE id=?", (mobile, u["id"]))
        audit(c, u["id"], "report_saved", f"crif_api:{sp.BUREAU}:{draft.get('score')}", ip)
        c.commit()
        return rep
    finally:
        c.close()
