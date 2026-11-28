"""Fikarta website (fikarta.com): the same accounts, AI and memory as the app, with one difference in how the
report arrives: web users upload their own credit bureau report (Experian or CRIF, JSON or the bureau PDF).

- Web sign-ups are marked origin='web' (phone optional; saved when given). Existing app users can sign in on
  the website and chat with the same features; the web summary shows only name, report bureau/date/score and
  whether AI chat is on, nothing more.
- Upload: parsed with the full-detail mapper (bureau_formats) for JSON, or the PDF reader, saved as a report with
  source 'web_upload', every field kept encrypted (identity numbers removed, the holder's name kept), and indexed
  for Fikarta AI (chunks, embeddings, memory). Needs the credit_report consent, which the upload form asks for.
- Chat uses the same /api/m/chat endpoint and pipeline (Claude Haiku 5.5 → Gemini → Groq, guards, related
  questions, tables, email drafts).
"""
import json
import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from mobile_api import (_read_upload, audit, current_user, db, latest_report, now, router, save_bureau_raw, save_report)

log = logging.getLogger("fikarta.web")


def _summary(c, u) -> Dict[str, Any]:
    rep = latest_report(c, u["id"], raw=True)
    ai = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='ai_chat' ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
    cr = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='credit_report' ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
    return {
        "first_name": u["first_name"], "web_user": (u["origin"] if "origin" in u.keys() else None) == "web",
        "ai_on": bool(ai and ai["granted"]), "report_consent": bool(cr and cr["granted"]),
        "report": {"bureau": rep.get("bureau"), "report_date": rep.get("report_date"), "score": rep.get("score"),
                   "source": rep.get("source"), "test_data": bool(rep.get("test_data"))} if rep else None,
    }


@router.post("/web/session")
def web_session(u=Depends(current_user)):
    """Called by fikarta.com after sign-in: records the web visit and returns a minimal summary."""
    c = db()
    try:
        c.execute("UPDATE users SET last_web_at=? WHERE id=?", (now(), u["id"]))
        c.commit()
        return _summary(c, u)
    finally:
        c.close()


@router.post("/web/report", status_code=201)
async def web_report(request: Request, u=Depends(current_user)):
    """One step for the website: read the user's own bureau file, save it, index it for Fikarta AI."""
    form, data = await _read_upload(request)
    if str(form.get("consent") or "").lower() not in ("true", "1", "yes", "on"):
        raise HTTPException(400, "Please confirm this is your own report and allow Fikarta to process it")
    c = db()
    try:
        from mobile_privacy import record_consent
        record_consent(c, u["id"], "credit_report", True, u["language"] or "en")
        c.commit()
    finally:
        c.close()
    head = data.lstrip()[:1]
    raw = None
    if head in (b"{", b"["):
        import bureau_formats as bf
        try:
            raw = json.loads(data.decode("utf-8-sig"))
            draft = bf.map_any(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "This file is not an Experian or CRIF report we can read.")
    elif data.startswith(b"%PDF"):
        from mobile_api import _parse_pdf
        draft = _parse_pdf(data, (form.get("password") or "").strip())
        if draft.get("score") is None and not draft.get("accounts"):
            raise HTTPException(400, "We could not read this PDF. Upload the bureau's JSON file, or check the PDF password.")
    else:
        raise HTTPException(400, "Upload your report as the bureau's PDF or JSON file (Experian or CRIF).")
    draft["source"] = "web_upload"
    c = db()
    try:
        rep = save_report(c, u, draft)
        if raw is not None:
            import bureau_formats as bf
            save_bureau_raw(c, u["id"], draft.get("bureau") or "Credit bureau", draft.get("report_id"), bf.sanitize(raw, keep_name=True))
        import user_rag
        user_rag.sync_user_docs(c, u["id"], latest_report(c, u["id"], raw=True), now())
        audit(c, u["id"], "report_saved", f"web_upload:{draft.get('bureau')}:{draft.get('score')}")
        c.commit()
        return {"bureau": rep.get("bureau"), "report_date": rep.get("report_date"), "score": rep.get("score"),
                "accounts": len(rep.get("accounts") or []), "enquiries": len(rep.get("enquiries") or [])}
    finally:
        c.close()
