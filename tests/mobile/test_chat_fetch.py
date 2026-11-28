"""Fikarta AI and personal details: ID questions get a fixed reply, and a score request with no report shows an
in-chat fetch form. Neither path calls the AI model, and nothing typed in the form reaches it."""
import random

import pytest

import ai_assistant
import surepass_crif as sp
from conftest import PASSWORD
from test_bureau_fetch import SAMPLE


@pytest.fixture
def no_model(monkeypatch):
    called = []
    monkeypatch.setattr(ai_assistant, "provider", lambda: "groq")
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda *a, **k: called.append("json") or None)
    monkeypatch.setattr(ai_assistant, "_call_groq", lambda s, m: called.append(m) or "x")
    return called


def _fresh_user(client, ai=True):
    email = f"c{random.randint(1, 10**9)}@example.com"
    r = client.post("/api/m/auth/signup", json={"email": email, "password": PASSWORD, "first_name": "Asha", "accept_terms": True})
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    if ai:
        assert client.post("/api/m/privacy/consents", headers=h, json={"purpose": "ai_chat", "granted": True}).status_code == 200
    return h


def test_personal_id_questions_are_guarded():
    for q, word in [("what is my pan", "PAN"), ("What's my PAN number?", "PAN"), ("tell me my aadhaar no", "Aadhaar"),
                    ("can you tell me my phone number", "phone number"), ("show me my password", "password"),
                    ("do you know my account number", "account number"), ("what is my OTP", "OTP")]:
        r = ai_assistant.guard(q)
        assert r and f"don't have access to your {word}" in r, q
    for q in ["what is my score", "how do I update my pan", "why did my score drop", "personal loan rate in delhi"]:
        assert ai_assistant.guard(q) is None, q


def test_pan_question_never_reaches_the_model(client, no_model):
    h = _fresh_user(client)
    client.put("/api/m/me", headers=h, json={"pan": "LOCPD1234A"})
    r = client.post("/api/m/chat", headers=h, json={"message": "What is my PAN?"})
    assert r.status_code == 200 and "don't have access to your PAN" in r.json()["reply"]
    assert "LOCPD1234A" not in r.json()["reply"] and "1234" not in r.json()["reply"] and no_model == []


def test_score_request_without_report_shows_form(client, no_model, monkeypatch):
    monkeypatch.setenv("SUREPASS_TOKEN", "test-token")
    h = _fresh_user(client)
    r = client.post("/api/m/chat", headers=h, json={"message": "What is my credit score?"})
    assert r.status_code == 200, r.text
    a = r.json()["action"]
    assert a["type"] == "fetch_report" and a["needs"] == ["pan", "mobile", "consent"] and a["consent_text"]
    assert no_model == []
    # leaving and reopening the chat keeps the form on the last prompt
    assert client.get("/api/m/chat", headers=h).json()[-1]["action"]["type"] == "fetch_report"
    # once the PAN is saved, the form asks only for what is still missing
    client.put("/api/m/me", headers=h, json={"pan": "LOCPD1234A"})
    a = client.post("/api/m/chat", headers=h, json={"message": "check my score"}).json()["action"]
    assert a["needs"] == ["mobile", "consent"] and a["prefill"]["pan_masked"] != "LOCPD1234A"


def test_form_submission_fetches_and_chat_never_sees_the_pan(client, no_model, monkeypatch):
    monkeypatch.setenv("SUREPASS_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(sp, "fetch_report", lambda f, l, m, p, timeout=45.0: calls.append(p) or SAMPLE)
    h = _fresh_user(client)
    # what the in-chat form does: save PAN + consent, then fetch
    assert client.put("/api/m/me", headers=h, json={"pan": "LOCPE4321B", "consent_bureau": True}).status_code == 200
    r = client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True})
    assert r.status_code == 201, r.text and calls == ["LOCPE4321B"]
    # with a report saved, a score question goes to the normal answer path (no form)
    monkeypatch.setattr(ai_assistant, "_call_groq", lambda s, m: no_model.append(s) or "Your score is shown on Home.")
    r = client.post("/api/m/chat", headers=h, json={"message": "what is my score"})
    assert "action" not in r.json()
    assert all("LOCPE4321B" not in str(x) for x in no_model)


def test_pan_typed_in_chat_is_masked_in_the_log(client, no_model, monkeypatch):
    h = _fresh_user(client)
    monkeypatch.setattr(ai_assistant, "_call_groq", lambda s, m: no_model.append(m) or "Thanks.")
    client.post("/api/m/chat", headers=h, json={"message": "my pan is LOCPZ9999Q, is that fine for a loan?"})
    log = client.get("/api/m/chat", headers=h).json()
    assert "LOCPZ9999Q" not in str(log) and "LOCPZ9999Q" not in str(no_model)
