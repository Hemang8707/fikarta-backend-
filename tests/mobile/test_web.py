"""fikarta.com: web sign-ups, minimal web summary, one-step report upload into the same AI memory, model lock."""
import json
import os
import random

import ai_assistant
from conftest import PASSWORD, signup
from mobile_api import db

EXP = os.path.join(os.path.dirname(__file__), "..", "fixtures", "experian_synthetic.json")


def _web_signup(client, phone=None):
    email = f"w{random.randint(1, 10**9)}@example.com"
    body = {"email": email, "password": PASSWORD, "first_name": "Web", "accept_terms": True, "origin": "web"}
    if phone:
        body["phone"] = phone
    r = client.post("/api/m/auth/signup", json=body)
    assert r.status_code == 201, r.text
    return email, {"Authorization": "Bearer " + r.json()["access_token"]}


def test_web_signup_marks_origin_and_phone_is_optional(client):
    e1, h1 = _web_signup(client)
    e2, h2 = _web_signup(client, phone="+91 98765 43210")
    c = db()
    try:
        r1 = c.execute("SELECT origin, phone FROM users WHERE email=?", (e1,)).fetchone()
        r2 = c.execute("SELECT origin, phone FROM users WHERE email=?", (e2,)).fetchone()
    finally:
        c.close()
    assert r1["origin"] == "web" and not r1["phone"]
    assert r2["origin"] == "web" and r2["phone"] == "9876543210"


def test_web_session_shows_only_a_minimal_summary(client):
    _, h = _web_signup(client)
    s = client.post("/api/m/web/session", headers=h).json()
    assert set(s) == {"first_name", "guest", "web_user", "ai_on", "report_consent", "report"} and s["web_user"] and s["report"] is None


def test_existing_app_user_can_use_the_website(client):
    email, h, _ = signup(client, ai=True)
    s = client.post("/api/m/web/session", headers=h).json()
    assert s["web_user"] is False and s["ai_on"] is True
    c = db()
    try:
        assert c.execute("SELECT last_web_at FROM users WHERE email=?", (email,)).fetchone()[0]
    finally:
        c.close()


def test_upload_needs_consent_then_saves_and_indexes_for_the_ai(client):
    _, h = _web_signup(client)
    with open(EXP, "rb") as f:
        data = f.read()
    r = client.post("/api/m/web/report", headers=h, files={"file": ("report.json", data, "application/json")})
    assert r.status_code == 400
    r = client.post("/api/m/web/report", headers=h, files={"file": ("report.json", data, "application/json")}, data={"consent": "true"})
    assert r.status_code == 201, r.text
    assert r.json() == {"bureau": "Experian", "report_date": "2026-06-01", "score": 761, "accounts": 3, "enquiries": 2}
    s = client.post("/api/m/web/session", headers=h).json()
    assert s["report"]["score"] == 761 and s["report"]["source"] == "web_upload" and s["report_consent"]
    uid = client.get("/api/m/me", headers=h).json()["id"]
    c = db()
    try:
        kinds = {r[0] for r in c.execute("SELECT doc_type FROM user_docs WHERE user_id=?", (uid,))}
        kept = c.execute("SELECT COUNT(*) FROM bureau_raw WHERE user_id=?", (uid,)).fetchone()[0]
    finally:
        c.close()
    assert {"report", "account", "account_payments", "bureau_field"} <= kinds and kept == 1


def test_bad_file_is_refused(client):
    _, h = _web_signup(client)
    r = client.post("/api/m/web/report", headers=h, files={"file": ("x.txt", b"hello", "text/plain")}, data={"consent": "true"})
    assert r.status_code == 400 and "PDF or JSON" in r.json()["detail"]


def test_only_claude_haiku_5_5_is_used(monkeypatch):
    monkeypatch.setenv("FIKARTA_AI_MODEL", "claude-opus-5-5")
    import importlib
    m = importlib.reload(ai_assistant)
    try:
        assert m.ANTHROPIC_MODEL == "claude-haiku-5-5"
    finally:
        monkeypatch.delenv("FIKARTA_AI_MODEL", raising=False)
        importlib.reload(ai_assistant)



def test_advisor_works_without_signing_in(client, monkeypatch):
    r = client.post("/api/m/web/guest")
    assert r.status_code == 201
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    s = client.post("/api/m/web/session", headers=h).json()
    assert s["guest"] and s["web_user"] and s["ai_on"] and s["first_name"] is None
    with open(EXP, "rb") as f:
        up = client.post("/api/m/web/report", headers=h, files={"file": ("r.json", f.read(), "application/json")}, data={"consent": "true"})
    assert up.status_code == 201 and up.json()["score"] == 761
    seen = {}
    monkeypatch.setattr(ai_assistant, "provider", lambda: "groq")
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda *a, **k: None)
    monkeypatch.setattr(ai_assistant, "_call_groq", lambda sy, m: seen.setdefault("s", sy) and "Your score is 761.")
    assert client.post("/api/m/chat", headers=h, json={"message": "what is my score"}).status_code == 200
    assert "Guest" not in seen["s"]  # no made-up name in the prompt
    # an optional phone number is saved when given
    assert client.put("/api/m/me", headers=h, json={"phone": "9876543210"}).status_code == 200


def test_web_user_without_report_is_asked_to_upload(client, monkeypatch):
    monkeypatch.setenv("SUREPASS_TOKEN", "x")
    r = client.post("/api/m/web/guest")
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    out = client.post("/api/m/chat", headers=h, json={"message": "What is my credit score?"}).json()
    assert "Upload" in out["reply"] and out["action"]["type"] == "fetch_report" and out["action"]["needs"] == ["upload"]
