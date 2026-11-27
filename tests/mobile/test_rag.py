"""Fikarta AI retrieval pipeline: user knowledge store, isolation, relevance, memory, traces, privacy.
The LLM is replaced by deterministic fakes, so these tests check the pipeline, not the model."""
import json
import os

import pytest

import ai_assistant
import mobile_api
import surepass_crif as sp
import user_rag
from conftest import PASSWORD, signup

with open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "surepass", "crif_sample.json"), encoding="utf-8") as _f:
    SAMPLE = json.load(_f)


@pytest.fixture
def with_report(client, monkeypatch):
    """A signed-up user whose report is the documented Surepass sample (HDFC home loan, ICICI PL, SBI card)."""
    monkeypatch.setenv("SUREPASS_TOKEN", "test-token")
    monkeypatch.setattr(sp, "fetch_report", lambda *a, **k: SAMPLE)

    def make(pan, ai=True, reminders=False):
        email, h, j = signup(client, ai=ai, reminders=reminders)
        c = mobile_api.db()
        c.execute("UPDATE users SET pan_enc=? WHERE email=?", (mobile_api.enc(pan), email))
        c.commit()
        uid = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
        c.close()
        r = client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True})
        assert r.status_code == 201, r.text
        return email, h, uid
    return make


def _sync(uid):
    c = mobile_api.db()
    rep = mobile_api.latest_report(c, uid, raw=True)
    out = user_rag.sync_user_docs(c, uid, rep, mobile_api.now())
    c.commit()
    docs = [dict(r) for r in c.execute("SELECT doc_key, doc_type, title, text, meta FROM user_docs WHERE user_id=?", (uid,))]
    c.close()
    return out, docs


def _retrieve(uid, *queries):
    c = mobile_api.db()
    out = user_rag.retrieve(c, uid, list(queries))
    c.close()
    return out


def test_store_has_one_doc_per_item_and_no_identifiers(with_report):
    email, _, uid = with_report("RAGPA1234A")
    first, docs = _sync(uid)
    keys = {d["doc_key"] for d in docs}
    assert "report:summary" in keys and "report:enquiries" in keys
    assert sum(1 for d in docs if d["doc_type"] == "account") == 3
    assert any(d["doc_type"] == "factor" for d in docs) and any(d["doc_type"] == "plan" for d in docs)
    blob = json.dumps(docs)
    for secret in ("RAGPA1234A", email, "9876543210", "HWPL99384756"):
        assert secret not in blob
    home = next(d for d in docs if d["doc_type"] == "account" and "HDFC" in d["title"])
    assert "Rs 2,80,000" in home["text"] and json.loads(home["meta"])["lender"] == "HDFC BANK"
    again, _ = _sync(uid)
    assert again["written"] == 0 and again["deleted"] == 0  # idempotent


def test_store_follows_data_changes(client, with_report):
    _, h, uid = with_report("RAGPB1234B", reminders=True)
    _sync(uid)
    r = client.post("/api/m/loans", headers=h, json={"lender": "Axis Bank", "account_no": "XX9911", "principal": 50000, "outstanding": 20000, "emi": 2500, "due_day": 7})
    loan_id = r.json()["id"]
    out, docs = _sync(uid)
    assert out["written"] >= 1 and any(d["doc_key"] == f"tracked:loan:{loan_id}" for d in docs)
    client.delete(f"/api/m/loans/{loan_id}", headers=h)
    out, docs = _sync(uid)
    assert out["deleted"] >= 1 and not any(d["doc_key"] == f"tracked:loan:{loan_id}" for d in docs)


def test_retrieval_finds_the_right_records(with_report):
    _, _, uid = with_report("RAGPC1234C")
    _sync(uid)
    top = _retrieve(uid, "what is the balance on my HDFC home loan")
    assert top[0]["doc_type"] == "account" and "HDFC" in top[0]["title"]
    cards = _retrieve(uid, "is my card usage too high")
    assert any(d["doc_type"] == "account" and d["meta"].get("card") for d in cards[:3])
    enq = _retrieve(uid, "how many times did I apply for credit recently")
    assert any(d["doc_type"] == "enquiries" for d in enq[:3])
    assert any(d["key"] == "report:summary" for d in _retrieve(uid, "hello"))  # summary is always available


def test_users_never_see_each_others_data(with_report):
    _, _, a = with_report("RAGPD1234D")
    _, _, b = with_report("RAGPE1234E")
    c = mobile_api.db()
    user_rag.save_memories(c, a, [{"kind": "goal", "text": "The user plans to buy a farmhouse in Nashik next year"}], mobile_api.now())
    c.commit()
    c.close()
    _sync(a)
    _sync(b)
    hits_b = _retrieve(b, "farmhouse Nashik")
    assert all(not d["key"].startswith("memory:") for d in hits_b)
    c = mobile_api.db()
    a_keys = {r[0] for r in c.execute("SELECT id FROM user_docs WHERE user_id=?", (a,))}
    c.close()
    assert a_keys and all("farmhouse" not in d["text"] for d in hits_b)
    assert any("farmhouse" in d["text"] for d in _retrieve(a, "farmhouse Nashik"))


def test_memory_filters_identifiers_and_deduplicates(with_report):
    _, _, uid = with_report("RAGPF1234F")
    c = mobile_api.db()
    now = mobile_api.now()
    n = user_rag.save_memories(c, uid, [
        {"kind": "goal", "text": "The user wants to buy a house next year"},
        {"kind": "situation", "text": "The user's PAN is ABCDE1234F"},
        {"kind": "situation", "text": "The user's number is 9876543210"},
        {"kind": "preference", "text": "The user's email is a@b.com"},
        {"kind": "weird", "text": "This kind is not allowed here"},
    ], now)
    user_rag.save_memories(c, uid, [{"kind": "goal", "text": "The user wants to buy a house next year."}], now)
    rows = [dict(r) for r in c.execute("SELECT kind, text, times_seen FROM user_memories WHERE user_id=?", (uid,))]
    c.close()
    assert n == 1 and len(rows) == 1 and rows[0]["times_seen"] == 2


@pytest.fixture
def fake_llm(monkeypatch):
    monkeypatch.setattr(ai_assistant, "provider", lambda: "groq")
    seen = {}

    def fake_json(system, user, max_tokens=300):
        if "standalone" in system:
            return {"standalone": "What is the outstanding balance on my HDFC home loan?", "keywords": ["home loan", "HDFC", "outstanding"]}
        return {"memories": [{"kind": "goal", "text": "The user wants to close the home loan early"}]}

    def fake_groq(system, msgs):
        seen["system"] = system
        return "Your HDFC home loan shows Rs 2,80,000 outstanding [U1]. Prepaying reduces interest [S1]. Bogus [S99] [U42]."
    monkeypatch.setattr(ai_assistant, "_groq_json", fake_json)
    monkeypatch.setattr(ai_assistant, "_call_groq", fake_groq)
    return seen


def test_chat_pipeline_end_to_end(client, with_report, fake_llm):
    _, h, uid = with_report("RAGPG1234G")
    client.post("/api/m/chat", headers=h, json={"message": "Hi"})
    r = client.post("/api/m/chat", headers=h, json={"message": "and what about that loan, can I close it early?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "[S99]" not in body["reply"] and "[U42]" not in body["reply"]  # invalid citations removed
    kinds = {cit["kind"] for cit in body["citations"]}
    assert "Your data" in kinds
    assert "USER CONTEXT" in fake_llm["system"] and "HDFC" in fake_llm["system"]
    assert "RAGPG1234G" not in fake_llm["system"]  # identifiers never reach the model
    c = mobile_api.db()
    tr = c.execute("SELECT rewritten, retrieved, outcome, latency_ms FROM rag_traces WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    c.close()
    assert tr["outcome"] == "answered" and tr["rewritten"] == 1 and "report:" in tr["retrieved"] and "HDFC" not in tr["retrieved"]
    mems = client.get("/api/m/ai/memories", headers=h).json()
    assert len(mems) == 1 and "home loan" in mems[0]["text"]
    assert client.delete(f"/api/m/ai/memories/{mems[0]['id']}", headers=h).status_code == 200
    assert client.get("/api/m/ai/memories", headers=h).json() == []
    exp = client.get("/api/m/me/export", headers=h).json()
    assert "user_memories" in exp and "rag_traces" in exp


def test_withdrawing_ai_consent_and_erasure_clear_the_ai_store(client, with_report, fake_llm):
    _, h, uid = with_report("RAGPH1234H")
    client.post("/api/m/chat", headers=h, json={"message": "I want to buy a car next year"})

    def count(t):
        c = mobile_api.db()
        n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (uid,)).fetchone()[0]
        c.close()
        return n
    assert count("user_docs") > 0 and count("user_memories") > 0
    client.post("/api/m/privacy/consents", headers=h, json={"purpose": "ai_chat", "granted": False})
    assert count("user_docs") == 0 and count("user_memories") == 0 and count("rag_traces") == 0


def test_account_deletion_cascades_and_releases_pan(client, with_report):
    email, h, uid = with_report("RAGPI1234I")
    _sync(uid)
    assert client.post("/api/m/me/delete", headers=h, json={"password": PASSWORD}).status_code == 200
    c = mobile_api.db()
    left = c.execute("SELECT COUNT(*) FROM user_docs WHERE user_id=?", (uid,)).fetchone()[0]
    c.close()
    assert left == 0
    _, _, uid2 = with_report("RAGPI1234I")  # same PAN on a new account works after the old one is deleted
    assert uid2 != uid
