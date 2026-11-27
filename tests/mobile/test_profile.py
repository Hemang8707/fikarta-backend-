"""Customer details shared in chat: extraction -> validation -> versioned storage -> used in answers -> user control."""
import json

import pytest

import ai_assistant
import mobile_api
import user_profile
from conftest import signup


def _uid(email):
    c = mobile_api.db()
    uid = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    c.close()
    return uid


def test_validation_rules():
    v = user_profile.validate
    assert v("monthly_income", "85,000") == (True, "85000")
    assert v("monthly_income", "1.2 lakh") == (True, "120000")
    assert v("monthly_income", "50k") == (True, "50000")
    assert v("monthly_income", "abc")[0] is False and v("monthly_income", 10)[0] is False
    assert v("employment_type", "Salaried") == (True, "salaried")
    assert v("employment_type", "astronaut")[0] is False
    assert v("city", "Pune") == (True, "Pune")
    assert v("city", "ABCDE1234F")[0] is False and v("employer", "call 9876543210")[0] is False and v("employer", "[PAN]")[0] is False
    assert v("dependents", "2") == (True, "2") and v("dependents", "40")[0] is False
    assert v("favourite_colour", "blue")[0] is False



@pytest.fixture
def chat_ready(client, monkeypatch):
    monkeypatch.setattr(ai_assistant, "provider", lambda: "groq")
    state = {"profile": {}}

    def fake_json(system, user, max_tokens=300):
        if "standalone" in system:
            return {"standalone": "question", "keywords": []}
        return {"memories": [{"kind": "goal", "text": "The user wants to buy a car next year"}], "profile": state["profile"]}

    def fake_groq(system, msgs):
        state["system"] = system
        return "Noted [S1]."
    monkeypatch.setattr(ai_assistant, "_groq_json", fake_json)
    monkeypatch.setattr(ai_assistant, "_call_groq", fake_groq)
    email, h, _ = signup(client, ai=True)
    return email, h, state


def test_chat_details_are_stored_versioned_and_used(client, chat_ready):
    email, h, state = chat_ready
    state["profile"] = {"monthly_income": 50000, "employment_type": "salaried", "employer": "Infosys", "city": "Pune",
                        "dependents": 2, "housing": "rented", "monthly_rent": "18000", "phone": "9876543210", "pan": "ABCDE1234F"}
    assert client.post("/api/m/chat", headers=h, json={"message": "I earn 50000 a month at Infosys in Pune, pay 18000 rent, 2 kids"}).status_code == 200
    v = {f["key"]: f for f in client.get("/api/m/ai/profile", headers=h).json()["fields"]}
    assert v["monthly_income"]["value"] == "50000" and v["monthly_income"]["display"] == "Rs 50,000" and v["monthly_income"]["source"] == "chat"
    assert v["employer"]["value"] == "Infosys" and v["dependents"]["value"] == "2" and v["housing"]["value"] == "rented"
    assert "phone" not in v and "pan" not in v  # not part of the schema: identifiers never come from chat

    # a raise: the new value is current, the old one is kept with dates
    state["profile"] = {"monthly_income": "65000"}
    client.post("/api/m/chat", headers=h, json={"message": "I got a raise, now 65000"})
    v = {f["key"]: f for f in client.get("/api/m/ai/profile", headers=h).json()["fields"]}
    assert v["monthly_income"]["value"] == "65000" and v["monthly_income"]["previous"][0]["value"] == "Rs 50,000"

    # the next answer sees the details, labelled as self-reported
    state["profile"] = {}
    client.post("/api/m/chat", headers=h, json={"message": "Can I afford a car EMI?"})
    assert "Details the user told Fikarta (self-reported, not verified)" in state["system"] and "Rs 65,000" in state["system"]
    assert "Infosys" in state["system"]


def test_user_can_correct_and_delete(client, chat_ready):
    email, h, state = chat_ready
    assert client.put("/api/m/ai/profile/city", headers=h, json={"value": "Bengaluru"}).status_code == 200
    assert client.put("/api/m/ai/profile/monthly_income", headers=h, json={"value": "lots"}).status_code == 400
    assert client.put("/api/m/ai/profile/pan", headers=h, json={"value": "ABCDE1234F"}).status_code == 404
    v = {f["key"]: f for f in client.get("/api/m/ai/profile", headers=h).json()["fields"]}
    assert v["city"]["value"] == "Bengaluru" and v["city"]["source"] == "user_edit"
    client.delete("/api/m/ai/profile/city", headers=h)
    v = {f["key"]: f for f in client.get("/api/m/ai/profile", headers=h).json()["fields"]}
    assert v["city"]["value"] is None and v["city"]["previous"] == []
    assert "user_profile_facts" in client.get("/api/m/me/export", headers=h).json()


def test_withdrawing_ai_consent_deletes_details(client, chat_ready):
    email, h, state = chat_ready
    state["profile"] = {"city": "Pune", "monthly_income": 40000}
    client.post("/api/m/chat", headers=h, json={"message": "I live in Pune and earn 40000"})
    c = mobile_api.db()
    assert c.execute("SELECT COUNT(*) FROM user_profile_facts WHERE user_id=?", (_uid(email),)).fetchone()[0] == 2
    c.close()
    client.post("/api/m/privacy/consents", headers=h, json={"purpose": "ai_chat", "granted": False})
    c = mobile_api.db()
    assert c.execute("SELECT COUNT(*) FROM user_profile_facts WHERE user_id=?", (_uid(email),)).fetchone()[0] == 0
    c.close()


def test_chat_messages_are_kept(client, chat_ready):
    email, h, state = chat_ready
    client.post("/api/m/chat", headers=h, json={"message": "I work as a nurse in Kochi"})
    hist = client.get("/api/m/chat", headers=h).json()
    assert hist[-2]["role"] == "user" and hist[-2]["content"] == "I work as a nurse in Kochi"
