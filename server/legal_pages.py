"""Public legal pages (needed for the Play Store listing and for users who are not signed in).

GET /legal/privacy  - the same privacy notice the app shows (built from mobile_privacy.notice_content,
                      so the page and the in-app notice can never drift apart)
GET /legal/terms    - terms of use

Both are drafts written from what the product actually does today and must be reviewed by a lawyer
before launch; the pages say so.
"""
import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from mobile_privacy import DPO, NOTICE_VERSION, notice_content

router = APIRouter(prefix="/legal", tags=["legal"])

_CSS = """
:root{--ink:#211B2D;--ink2:#584E6B;--muted:#9A90AD;--line:#E0D9EA;--blue:#1744C7;--violet:#6424B8;--ivory:#FBF8F2;--lav:#F8F5FB}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(160deg,var(--ivory),var(--lav));color:var(--ink2);
font:15px/1.6 Inter,-apple-system,"Segoe UI",Roboto,"Noto Sans Devanagari",sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 64px}
h1{color:var(--ink);font-weight:500;font-size:28px;margin:0 0 4px}
h2{color:var(--ink);font-weight:500;font-size:19px;margin:28px 0 8px}
h3{color:var(--ink);font-weight:500;font-size:16px;margin:16px 0 4px}
.meta{color:var(--muted);font-size:13px;margin-bottom:20px}
.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin:10px 0}
.note{border-left:3px solid var(--violet);background:#F3EEF9;padding:10px 14px;border-radius:8px;font-size:14px}
a{color:var(--blue)}ul{padding-left:20px}li{margin:3px 0}
.brand{font-weight:500;background:linear-gradient(90deg,var(--blue),var(--violet));-webkit-background-clip:text;color:transparent;font-size:18px}
"""


def _page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · Fikarta</title>
<style>{_CSS}</style></head><body><main><div class="brand">Fikarta</div>{body}</main></body></html>"""
    return HTMLResponse(doc, headers={"Cache-Control": "public, max-age=3600"})


def _e(s) -> str:
    return html.escape(str(s))


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    n = notice_content()
    purposes = "".join(
        f"""<div class="card"><h3>{_e(p['title'])}{'' if p.get('required') else ' <span style="color:#9A90AD;font-size:13px">(optional)</span>'}</h3>
<p>{_e(p['why'])}</p><ul>{''.join(f'<li>{_e(d)}</li>' for d in p['data'])}</ul>
{f'<p style="font-size:13px;color:#9A90AD">This consent lasts {p["valid_days"]} days; we ask again after that.</p>' if p.get('valid_days') else ''}</div>"""
        for p in n["purposes"])
    rights = "".join(f"<li><b style='font-weight:500;color:#211B2D'>{_e(r['title'])}.</b> {_e(r['text'])}</li>" for r in n["rights"])
    body = f"""<h1>Privacy notice</h1>
<div class="meta">Version {_e(NOTICE_VERSION)} · Data fiduciary: {_e(n['fiduciary'])} · Under India's Digital Personal Data Protection Act, 2023</div>
<div class="note">This is the same notice shown inside the Fikarta app. It is a working draft pending legal review.</div>
<h2>What we use and why</h2>{purposes}
<h2>Your rights</h2><ul>{rights}</ul>
<p>Use these rights in the app under Profile → Privacy Centre, or write to us (below).</p>
<h2>Sharing</h2><p>{_e(n['sharing'])}</p>
<h2>How long we keep data</h2><p>{_e(n['retention'])}</p>
<h2>Security</h2><p>{_e(n['security'])}</p>
<h2>Breaches</h2><p>{_e(n['breach'])}</p>
<h2>Children</h2><p>{_e(n['children'])}</p>
<h2>Contact and grievances</h2>
<div class="card"><p>{_e(DPO['name'])}<br>Email: <a href="mailto:{_e(DPO['email'])}">{_e(DPO['email'])}</a><br>
Grievances: <a href="mailto:{_e(DPO['grievance_email'])}">{_e(DPO['grievance_email'])}</a><br>
We reply within {_e(DPO['response_days'])} days. {_e(n['board'])}</p></div>
<p><a href="/legal/terms">Terms of use</a></p>"""
    return _page("Privacy notice", body)


@router.get("/terms", response_class=HTMLResponse)
def terms_page():
    body = f"""<h1>Terms of use</h1>
<div class="meta">Version 1.0</div>
<div class="note">Working draft written from what Fikarta does today. It must be reviewed by a lawyer before launch.</div>
<h2>1. What Fikarta is</h2>
<p>Fikarta is a credit information and education service. It helps you read your own credit report, understand what affects
your score, find possible errors, draft dispute letters, keep payment reminders and ask questions to Fikarta AI.</p>
<p><b style="font-weight:500;color:#211B2D">Fikarta is not a lender, not a credit bureau and not a financial adviser.</b> It does not give
loans, does not decide loan approvals and has no lending partners at present. Lender listings in the app are for comparison only,
using figures each lender publishes. Fikarta does not take, hold or move your money; payments you record in the app are payments
you made yourself through your bank or UPI app.</p>
<h2>2. Who can use it</h2>
<p>You must be 18 or older and resident in India, and you may use Fikarta only for your own credit information.
You must not upload anyone else's report or use someone else's PAN.</p>
<h2>3. Your credit report</h2>
<p>Fikarta does not fetch reports from credit bureaus. You download your report from a bureau yourself and upload it, or type it in.
We read it automatically and you check the result before it is saved. You are responsible for checking that what is saved matches your report.</p>
<h2>4. Scores, plans and Fikarta AI</h2>
<p>Score bands, factor grades, plans and AI answers are general information based on your report and public rules, mainly from the
Reserve Bank of India. They are not a promise that your score will change by any amount, and not advice to take or close any loan or card.
AI answers can be wrong; check important points with your lender or bureau. Score bands follow CIBIL's published ranges and other bureaus may differ.</p>
<h2>5. Disputes</h2>
<p>Fikarta helps you draft dispute letters. You send them to the bureau or lender yourself. Bureaus and lenders decide the outcome.</p>
<h2>6. Reward points</h2>
<p>Points record good credit habits, such as completing plan steps. They have no cash value and cannot be redeemed at present.
We may change or end the points programme with notice in the app.</p>
<h2>7. Your account</h2>
<p>Keep your password private. Tell us at once if you think someone else has used your account. You can delete your account at any time
in Profile → Privacy Centre.</p>
<h2>8. Acceptable use</h2>
<p>Do not misuse the service: no attempts to access other users' data, overload or probe our systems, or upload harmful files.</p>
<h2>9. Liability</h2>
<p>We work to keep Fikarta accurate and available but cannot guarantee it is error-free or always available. To the extent the law allows,
Fikarta is not responsible for decisions made by lenders or bureaus, or for losses from relying on general information in the app.</p>
<h2>10. Privacy</h2>
<p>How we use your data is explained in our <a href="/legal/privacy">privacy notice</a>.</p>
<h2>11. Changes and contact</h2>
<p>We will tell you in the app before material changes take effect. Questions: <a href="mailto:{_e(DPO['grievance_email'])}">{_e(DPO['grievance_email'])}</a>.
These terms are governed by the laws of India.</p>"""
    return _page("Terms of use", body)
