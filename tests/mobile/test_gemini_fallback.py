"""Model order: Claude Haiku 5.5 first, then Gemini, then Groq; fails closed when all fail."""
import json

import httpx

import ai_assistant
import mobile_api
from conftest import signup


def _chat(client, h):
    return client.post("/api/m/chat", headers=h, json={"message": "How is my score?"})


def _down(*a, **k):
    raise ai_assistant.AssistantUnavailable()


def _setup(monkeypatch, chain):
    monkeypatch.setattr(ai_assistant, "provider", lambda: chain[0] if chain else None)
    monkeypatch.setattr(ai_assistant, "provider_chain", lambda: list(chain))
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda *a, **k: None)


def _trace(email):
    c = mobile_api.db()
    t = c.execute("SELECT provider, model, outcome FROM rag_traces WHERE user_id=(SELECT id FROM users WHERE email=?) ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    c.close()
    return (t["provider"], t["model"], t["outcome"])


def test_claude_haiku_answers_first(client, monkeypatch):
    _setup(monkeypatch, ["anthropic", "gemini", "groq"])
    monkeypatch.setattr(ai_assistant, "_call_anthropic", lambda system, msgs: "Answer from Claude.")
    monkeypatch.setattr(ai_assistant, "_call_gemini", _down)
    monkeypatch.setattr(ai_assistant, "_call_groq", _down)
    email, h, _ = signup(client, ai=True)
    r = _chat(client, h)
    assert r.status_code == 200 and r.json()["reply"] == "Answer from Claude."
    assert _trace(email) == ("anthropic", "claude-haiku-5-5", "answered")


def test_gemini_answers_when_claude_fails(client, monkeypatch):
    _setup(monkeypatch, ["anthropic", "gemini", "groq"])
    monkeypatch.setattr(ai_assistant, "_call_anthropic", _down)
    monkeypatch.setattr(ai_assistant, "_call_gemini", lambda system, msgs: ("Answer from the backup model.", "gemini-3.5-flash"))
    monkeypatch.setattr(ai_assistant, "_call_groq", _down)
    email, h, _ = signup(client, ai=True)
    r = _chat(client, h)
    assert r.status_code == 200 and r.json()["reply"] == "Answer from the backup model."
    assert _trace(email) == ("gemini", "gemini-3.5-flash", "answered")


def test_groq_answers_when_claude_and_gemini_fail(client, monkeypatch):
    _setup(monkeypatch, ["anthropic", "gemini", "groq"])
    monkeypatch.setattr(ai_assistant, "_call_anthropic", _down)
    monkeypatch.setattr(ai_assistant, "_call_gemini", _down)
    monkeypatch.setattr(ai_assistant, "_call_groq", lambda system, msgs: "Answer from the last model.")
    email, h, _ = signup(client, ai=True)
    r = _chat(client, h)
    assert r.status_code == 200 and r.json()["reply"] == "Answer from the last model." and _trace(email)[0] == "groq"


def test_fails_closed_when_all_fail(client, monkeypatch):
    _setup(monkeypatch, ["anthropic", "gemini", "groq"])
    for f in ("_call_anthropic", "_call_gemini", "_call_groq"):
        monkeypatch.setattr(ai_assistant, f, _down)
    _, h, _ = signup(client, ai=True)
    assert _chat(client, h).status_code == 503


def test_provider_order_is_claude_gemini_groq(monkeypatch):
    monkeypatch.delenv("FIKARTA_AI_DISABLED", raising=False)
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.setenv(k, "x")
    assert ai_assistant.provider_chain() == ["anthropic", "gemini", "groq"] and ai_assistant.provider() == "anthropic"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert ai_assistant.provider_chain() == ["gemini", "groq"]


def test_claude_turns_alternate_and_start_with_the_user():
    t = ai_assistant._claude_turns([{"role": "assistant", "content": "old"}, {"role": "user", "content": "a"},
                                    {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}])
    assert [m["role"] for m in t] == ["user", "assistant", "user"] and t[0]["content"] == "a\n\nb"


def test_gemini_request_shape(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("FIKARTA_AI_DISABLED", raising=False)
    seen = {}

    class R:
        status_code = 200

        def json(self):
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hidden", "thought": True}, {"text": "Visible answer"}]}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return R()
    monkeypatch.setattr(httpx, "post", fake_post)
    text, model = ai_assistant._call_gemini("SYSTEM RULES", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "q"}])
    assert text == "Visible answer" and model == ai_assistant.GEMINI_MODELS[0]
    assert seen["headers"]["x-goog-api-key"] == "test-key" and "key=" not in seen["url"]  # key in a header, never in the URL
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "SYSTEM RULES"
    assert [c["role"] for c in seen["body"]["contents"]] == ["user", "model", "user"]
    monkeypatch.setenv("FIKARTA_AI_DISABLED", "1")


def test_fix_tables_inserts_missing_divider():
    raw = "Intro\n\n| Lender | Balance |\n| HDFC | ₹1 |\n| SBI | ₹2 |\n\nDone"
    fixed = ai_assistant.fix_tables(raw)
    assert "| Lender | Balance |\n| --- | --- |\n| HDFC | ₹1 |" in fixed
    ok = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert ai_assistant.fix_tables(ok) == ok  # an already valid table is untouched


def test_invented_amounts_are_rewritten_or_removed(client, monkeypatch):
    import grounding
    monkeypatch.setattr(ai_assistant, "provider", lambda: "groq")
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda *a, **k: None)
    calls = []

    def fake_groq(system, msgs):
        calls.append(system)
        # first draft invents an EMI; the rewrite still invents one (to exercise the last-resort trim)
        return "Your rent is ₹15,000.\n- Existing EMI around ₹12,000\n- 40% of ₹75,000 is ₹30,000"
    monkeypatch.setattr(ai_assistant, "_call_groq", fake_groq)
    email, h, _ = signup(client, ai=True)
    import user_profile
    c = mobile_api.db()
    uid = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    user_profile.upsert(c, uid, {"monthly_income": 75000, "monthly_rent": 15000}, "chat", mobile_api.now())
    c.commit()
    c.close()
    r = client.post("/api/m/chat", headers=h, json={"message": "How much EMI can I afford?"})
    body = r.json()["reply"]
    assert len(calls) == 2 and "CORRECTION" in calls[1] and "₹12,000" in calls[1]
    assert "12,000" not in body and "₹30,000" in body and "₹15,000" in body
    c = mobile_api.db()
    assert c.execute("SELECT outcome FROM rag_traces WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()[0] == "answered_trimmed"
    c.close()


def test_grounded_answer_is_untouched():
    import grounding
    src = ["Monthly income: Rs 75,000. Monthly rent: Rs 15,000"]
    assert grounding.ungrounded("40% of ₹75,000 is ₹30,000; after rent ₹15,000 you have ₹15,000.", src) == []
    assert grounding.ungrounded("Support for parents ≈ ₹10,000", src) == [10000]
