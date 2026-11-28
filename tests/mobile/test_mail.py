"""Emails to lenders: official addresses, drafting without identifiers reaching the model, confirm-to-send,
mail-app fallback, Gmail send, bounce recovery and the chat offer."""
import json

import pytest

import ai_assistant
import mail_assist
import mail_store
from conftest import signup
from mobile_api import db


def _uid(client, h):
    return client.get("/api/m/me", headers=h).json()["id"]


@pytest.fixture
def contacts():
    """A small directory of official addresses (the real one is copied from the website database on migration)."""
    c = db()
    try:
        for name, email, web in (("Kotak Mahindra Bank Limited", "service.bank@kotak.com", "https://www.kotak.com"),
                                 ("Kotak Mahindra Bank Limited", "nodal.officer@kotak.com", None),
                                 ("Test Finance Limited", "care@testfin.in", "https://www.testfin.in")):
            c.execute("INSERT OR IGNORE INTO lender_contacts(lender_key,name,email,role,website,source,added_at) VALUES(?,?,?,?,?,?,?)",
                      (mail_store.lender_key(name), name, email, mail_store._role(email), web, "directory", "2026-11-28T00:00:00Z"))
            c.execute("UPDATE lender_contacts SET status='ok' WHERE email=?", (email,))
        c.commit()
    finally:
        c.close()


def test_lender_names_from_reports_find_official_addresses(contacts):
    c = db()
    try:
        got = mail_store.contacts_for(c, "KOTAK MAHINDRA BANK")
    finally:
        c.close()
    assert [g["email"] for g in got] == ["service.bank@kotak.com", "nodal.officer@kotak.com"]  # care before nodal escalation


def test_real_directory_is_copied_once():
    import os
    if not os.path.exists(mail_store.CONTENT_DB):
        pytest.skip("website database not present")
    c = db()
    try:
        before = c.execute("SELECT COUNT(*) FROM lender_contacts WHERE source='directory'").fetchone()[0]
        mail_store.seed_contacts(c)
        after = c.execute("SELECT COUNT(*) FROM lender_contacts WHERE source='directory'").fetchone()[0]
        c.commit()
        assert after >= before and after > 1000
        assert mail_store.seed_contacts(c) == 0  # idempotent
        assert mail_store.contacts_for(c, "HDFC Bank")
    finally:
        c.close()


def test_draft_fills_identity_after_generation_and_never_sends_it_to_the_model(client, monkeypatch, contacts):
    seen = {}

    def fake(system, user, max_tokens=300):
        seen["user"] = user
        return {"subject": "Wrong late payment on account {ACCOUNT}", "body": "Dear Sir,\n\nPlease correct my account {ACCOUNT}.\n\nRegards,\n{NAME}\n{PHONE}"}
    monkeypatch.setattr(ai_assistant, "_groq_json", fake)
    _, h, _ = signup(client, ai=True)
    client.put("/api/m/me", headers=h, json={"phone": "9876543210", "last_name": "Verma"})
    r = client.post("/api/m/mail/draft", headers=h, json={"lender": "KOTAK MAHINDRA BANK", "problem": "My PAN ABCPE1234F shows a late payment I never made"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["to_addr"] == "service.bank@kotak.com" and d["other_addresses"] == ["nodal.officer@kotak.com"]
    assert "Test Verma" in d["body"] and "9876543210" in d["body"] and "{NAME}" not in d["body"]
    assert "ABCPE1234F" not in seen["user"] and "9876543210" not in seen["user"] and "Verma" not in seen["user"]


def test_nothing_is_sent_without_confirmation_and_mail_app_fallback(client, monkeypatch, contacts):
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda *a, **k: {"subject": "Hi", "body": "Body {NAME}"})
    _, h, _ = signup(client, ai=True)
    d = client.post("/api/m/mail/draft", headers=h, json={"lender": "Test Finance Ltd", "problem": "Need NOC"}).json()
    assert client.post(f"/api/m/mail/drafts/{d['id']}/send", headers=h, json={"confirm": False}).status_code == 400
    assert client.put(f"/api/m/mail/drafts/{d['id']}", headers=h, json={"to_addr": "not an email"}).status_code == 400
    r = client.post(f"/api/m/mail/drafts/{d['id']}/send", headers=h, json={"confirm": True}).json()
    assert r["status"] == "open_mail_app" and r["mailto"].startswith("mailto:care@testfin.in?subject=Hi")
    assert client.post(f"/api/m/mail/drafts/{d['id']}/send", headers=h, json={"confirm": True}).status_code == 404  # sent once only


def test_gmail_send_after_confirmation(client, monkeypatch, contacts):
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda *a, **k: {"subject": "Hi", "body": "Body"})
    sent = []
    monkeypatch.setattr(mail_assist, "gmail_send", lambda c, uid, to, subj, body: sent.append((to, subj)) or {"id": "m1", "thread": "t1"})
    _, h, _ = signup(client, ai=True)
    uid = _uid(client, h)
    client.post("/api/m/privacy/consents", headers=h, json={"purpose": "email_access", "granted": True})
    c = db()
    c.execute("INSERT INTO gmail_links(user_id,email,refresh_enc,connected_at) VALUES(?,?,?,?)", (uid, "u@gmail.com", "x", "2026-11-28"))
    c.commit()
    c.close()
    d = client.post("/api/m/mail/draft", headers=h, json={"lender": "Test Finance", "problem": "Need NOC"}).json()
    r = client.post(f"/api/m/mail/drafts/{d['id']}/send", headers=h, json={"confirm": True}).json()
    assert r == {"status": "sent", "via": "gmail", "to": "care@testfin.in"} and sent == [("care@testfin.in", "Hi")]
    # withdrawing the consent disconnects Gmail and deletes its data
    client.post("/api/m/privacy/consents", headers=h, json={"purpose": "email_access", "granted": False})
    c = db()
    try:
        assert c.execute("SELECT COUNT(*) FROM gmail_links WHERE user_id=?", (uid,)).fetchone()[0] == 0
    finally:
        c.close()


def test_bounce_uses_the_next_official_address_and_asks_again(client, contacts):
    _, h, _ = signup(client, ai=True)
    uid = _uid(client, h)
    c = db()
    try:
        cur = c.execute("INSERT INTO email_drafts(user_id,lender,to_addr,subject,body,status,via,created_at,sent_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (uid, "Kotak Mahindra Bank", "service.bank@kotak.com", "S", "B", "sent", "gmail", "x", "x"))
        s = dict(c.execute("SELECT * FROM email_drafts WHERE id=?", (cur.lastrowid,)).fetchone())
        assert mail_assist._handle_bounce(c, uid, s) == 1
        c.commit()
        rows = [dict(r) for r in c.execute("SELECT to_addr, status, note FROM email_drafts WHERE user_id=? ORDER BY id", (uid,))]
    finally:
        c.close()
    assert rows[0]["status"] == "bounced"
    assert rows[1] == {"to_addr": "nodal.officer@kotak.com", "status": "draft", "note": "Resend: service.bank@kotak.com bounced"}  # waits for confirmation


def test_website_discovery_accepts_only_the_lenders_own_domain(monkeypatch):
    class R:
        status_code = 200
        text = "Write to grievance@testfin.in or help@testfin.in. Partner: sales@other.com, phish@testfin.in.evil.com"
    monkeypatch.setattr(mail_store.httpx, "get", lambda *a, **k: R())
    got = mail_store.discover_from_website("https://www.testfin.in")
    assert "grievance@testfin.in" in got and "help@testfin.in" in got
    assert all(not g.endswith("other.com") and "evil" not in g for g in got)


def test_chat_offers_a_draft_for_lender_problems(client, monkeypatch):
    monkeypatch.setattr(ai_assistant, "provider", lambda: "groq")
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda *a, **k: None)
    monkeypatch.setattr(ai_assistant, "_call_groq", lambda s, m: (
        "You can ask Kotak to correct it and escalate to the RBI Ombudsman after 30 days.\nDRAFT_FOR: Kotak Mahindra Bank\n"
        "RELATED: What is the RBI Ombudsman? | How long does a correction take? | Will my score go up?"))
    _, h, _ = signup(client, ai=True)
    r = client.post("/api/m/chat", headers=h, json={"message": "Kotak is showing a late payment I never made"}).json()
    assert r["action"] == {"type": "offer_draft", "lender": "Kotak Mahindra Bank", "problem": "Kotak is showing a late payment I never made"}
    assert "DRAFT_FOR" not in r["reply"] and len(r["suggestions"]) == 3


def test_google_keys_are_read_from_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GOOGLE_CLIENT_ID=cid.apps.googleusercontent.com\nGOOGLE_CLIENT_SECRET=sec\n", encoding="utf-8")
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    import os
    import inspect
    src = inspect.getsource(ai_assistant.load_ai_env)
    assert "GOOGLE_CLIENT_ID" in src and "GOOGLE_CLIENT_SECRET" in src
    if "path" in inspect.signature(ai_assistant.load_ai_env).parameters:
        ai_assistant.load_ai_env(str(env))
        assert os.environ["GOOGLE_CLIENT_ID"].startswith("cid") and mail_assist.gmail_configured()



def test_named_people_are_never_suggested(contacts):
    for e in ("satish.shimpi@bajajfinserv.in", "tms.muhammad@muthootgroup.com", "prince.kumar@krazybee.com", "a.sharma@lender.in"):
        assert mail_store.is_personal(e), e
    for e in ("wecare@bajajfinserv.in", "service.bank@kotak.com", "email.services@axis.bank.in", "customer.care@icicibank.com",
              "pno@indusind.com", "principalnodalofficer@rbl.bank.in", "contactcentre@sbi.co.in", "yestouch@yesbank.in", "support@hdfc.bank.in", "secretarial@payu.in", "cco@shriramfinance.in"):
        assert not mail_store.is_personal(e), e
    c = db()
    try:
        for email in ("satish.shimpi@bajajfinserv.in", "wecare@bajajfinserv.in"):
            c.execute("INSERT OR IGNORE INTO lender_contacts(lender_key,name,email,role,website,source,added_at) VALUES(?,?,?,?,?,?,?)",
                      (mail_store.lender_key("Bajaj Finance Ltd."), "Bajaj Finance Ltd.", email, "care", None, "directory", "x"))
        got = [g["email"] for g in mail_store.contacts_for(c, "BAJAJ FINANCE LIMITED")]
    finally:
        c.close()
    assert got[0] == "wecare@bajajfinserv.in" and "satish.shimpi@bajajfinserv.in" not in got


def test_only_named_people_in_directory_falls_back_to_the_website(monkeypatch):
    class R:
        status_code = 200
        text = "Customer care: care@onlyperson.in, CEO: ravi.mehta@onlyperson.in"
    monkeypatch.setattr(mail_store.httpx, "get", lambda *a, **k: R())
    c = db()
    try:
        c.execute("INSERT OR IGNORE INTO lender_contacts(lender_key,name,email,role,website,source,added_at) VALUES(?,?,?,?,?,?,?)",
                  (mail_store.lender_key("Only Person Finance Ltd"), "Only Person Finance Ltd", "anil.verma@onlyperson.in", "care",
                   "https://www.onlyperson.in", "directory", "x"))
        got = [g["email"] for g in mail_store.generic_contacts(c, "ONLY PERSON FINANCE LIMITED")]
    finally:
        c.close()
    assert got == ["care@onlyperson.in"]
