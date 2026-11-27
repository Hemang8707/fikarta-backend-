"""What each user still needs to complete, so Home can ask for exactly the missing pieces.

Also the first-login Terms acceptance for accounts created by an administrator (scripts/create_user.py):
those users never ticked the sign-up checkbox, so the 'service' consent is recorded only when they accept in
the app - never on their behalf.
"""
from fastapi import Depends

from mobile_api import NOTICE_VERSION, audit, current_user, db, dec, router
from mobile_privacy import consent_status, record_consent


def terms_accepted(c, uid: int) -> bool:
    return c.execute("SELECT 1 FROM consents WHERE user_id=? AND purpose='service' AND granted=1", (uid,)).fetchone() is not None


@router.get("/profile/completeness")
def completeness(u=Depends(current_user)):
    import user_profile
    c = db()
    try:
        has_report = c.execute("SELECT 1 FROM reports WHERE user_id=?", (u["id"],)).fetchone() is not None
        facts = user_profile.current(c, u["id"])
        ai_on = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='ai_chat' ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        items = [
            {"key": "name", "title": "Name as on your PAN card", "why": "CRIF matches your name exactly", "done": bool((u["first_name"] or "").strip() and (u["last_name"] or "").strip()), "action": "profile"},
            {"key": "pan", "title": "PAN", "why": "Needed to get your credit report", "done": bool(dec(u["pan_enc"])), "action": "profile"},
            {"key": "phone", "title": "Mobile number", "why": "The number registered with your bank", "done": bool(u["phone"]), "action": "profile"},
            {"key": "credit_consent", "title": "Allow credit report processing", "why": "Your consent to read your report", "done": consent_status(c, u, "credit_report") == "granted", "action": "report"},
            {"key": "report", "title": "Get your credit report", "why": "Your score, accounts and plan", "done": has_report, "action": "report"},
            {"key": "work_income", "title": "Work and monthly income", "why": "Makes Fikarta AI's advice practical", "done": "monthly_income" in facts and "employment_type" in facts, "action": "details"},
            {"key": "ai", "title": "Turn on Fikarta AI", "why": "Ask anything about your credit", "done": bool(ai_on and ai_on["granted"]), "action": "chat"},
        ]
        done = sum(1 for i in items if i["done"])
        return {"done": done, "total": len(items), "percent": round(100 * done / len(items)), "items": items,
                "terms_accepted": terms_accepted(c, u["id"])}
    finally:
        c.close()


@router.post("/privacy/accept-terms")
def accept_terms(u=Depends(current_user)):
    c = db()
    try:
        if not terms_accepted(c, u["id"]):
            record_consent(c, u["id"], "service", True)
            audit(c, u["id"], "terms_accepted", "notice " + NOTICE_VERSION)
            c.commit()
        return {"terms_accepted": True}
    finally:
        c.close()
