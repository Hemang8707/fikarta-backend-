"""Month-to-month report history, time-aware retrieval, identifier scrubbing and the fast knowledge-base index."""
import copy
import json
import math
import os

import pytest

import ai_assistant
import mobile_api
import report_history as rh
import surepass_crif as sp
import user_rag
from conftest import signup

with open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "surepass", "crif_sample.json"), encoding="utf-8") as _f:
    SAMPLE = json.load(_f)


def _next_month():
    """The same person one month later: score down, card used more, a late month, a new enquiry, a new loan."""
    s = copy.deepcopy(SAMPLE)
    cr = s["data"]["credit_report"]
    cr["HEADER"]["DATE-OF-ISSUE"] = "25-08-2025"
    cr["SCORES"]["SCORE"]["SCORE-VALUE"] = "720"
    s["data"]["credit_score"] = "720"
    accts = cr["RESPONSES"]["RESPONSE"]
    accts[0]["LOAN-DETAILS"]["CURRENT-BAL"] = "2,72,000"
    accts[2]["LOAN-DETAILS"]["CURRENT-BAL"] = "80,000"
    accts[2]["LOAN-DETAILS"]["COMBINED-PAYMENT-HISTORY"] = "Aug:2025,060/SMA|Jul:2025,030/SMA|Jun:2025,000/STD|"
    accts.append({"LOAN-DETAILS": {"ACCT-NUMBER": "BAJAJ55550001", "CREDIT-GUARANTOR": "BAJAJ FINANCE", "ACCT-TYPE": "Consumer Loan", "ACCOUNT-STATUS": "Active",
                                   "DISBURSED-AMT": "40,000", "DISBURSED-DT": "05-08-2025", "CURRENT-BAL": "40,000", "MATCHED-TYPE": "PRIMARY"}})
    cr["INQUIRY-HISTORY"]["HISTORY"].append({"MEMBER-NAME": "BAJAJ FINANCE", "INQUIRY-DATE": "04-08-2025", "PURPOSE": "Consumer Loan"})
    return s


@pytest.fixture
def two_months(client):
    email, h, _ = signup(client, ai=True)
    c = mobile_api.db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    mobile_api.save_report(c, u, sp.map_surepass_crif(SAMPLE))
    mobile_api.save_report(c, u, sp.map_surepass_crif(_next_month()))
    c.commit()
    c.close()
    return h, u["id"]


def test_diff_is_exact(two_months):
    _, uid = two_months
    c = mobile_api.db()
    snaps = rh.snapshots(c, uid)
    c.close()
    assert len(snaps) == 2
    ev = {e["kind"]: e for e in rh.diff(snaps[1], snaps[0])}
    assert ev["score"]["meta"]["old"] == 750 and ev["score"]["meta"]["new"] == 720 and ev["score"]["meta"]["delta"] == -30
    assert ev["late_payment"]["meta"]["old"] == 1 and ev["late_payment"]["meta"]["new"] == 2
    assert ev["utilisation"]["meta"]["old"] == 45.0 and ev["utilisation"]["meta"]["new"] == 80.0
    assert ev["enquiries"]["meta"]["count"] == 1 and "BAJAJ FINANCE" in ev["enquiries"]["text"]
    assert ev["account_new"]["meta"]["lender"] == "BAJAJ FINANCE"
    bal = [e for e in rh.diff(snaps[1], snaps[0]) if e["kind"] == "account_balance"]
    assert any(e["meta"]["old"] == "280000" and e["meta"]["new"] == "272000" for e in bal)
    assert "Rs 2,80,000" in next(e["text"] for e in bal if "HDFC" in e["text"])


def test_unknown_is_never_a_jump_from_zero():
    a = {"report_date": "2025-07-25", "score": None, "accounts": [{"lender": "X", "type": "Personal Loan", "account_no": "XXXX1234", "status": "Active", "balance": None, "overdue": None}], "enquiries": []}
    b = {"report_date": "2025-08-25", "score": 700, "accounts": [{"lender": "X", "type": "Personal Loan", "account_no": "XXXX1234", "status": "Active", "balance": "5000", "overdue": None}], "enquiries": []}
    ev = rh.diff(a, b)
    assert [e["title"] for e in ev] == ["Score now shown"]  # no 'balance up from 0'


def test_changes_api_and_history_docs(client, two_months):
    h, uid = two_months
    j = client.get("/api/m/credit/changes", headers=h).json()
    assert j["reports"] == 2 and j["latest"]["from"] == "2025-07-25" and j["latest"]["to"] == "2025-08-25"
    assert j["scores"] == [{"date": "2025-07-25", "score": 750}, {"date": "2025-08-25", "score": 720}]
    assert any(a["lender"] == "HDFC BANK" and len(a["points"]) == 2 for a in j["accounts"])
    c = mobile_api.db()
    user_rag.sync_user_docs(c, uid, mobile_api.latest_report(c, uid, raw=True), mobile_api.now())
    c.commit()
    types = {r[0] for r in c.execute("SELECT doc_type FROM user_docs WHERE user_id=?", (uid,))}
    why = user_rag.retrieve(c, uid, ["why did my score drop last month"])
    now = user_rag.retrieve(c, uid, ["what is my SBI card balance"])
    c.close()
    assert {"change", "account_history", "report_old"} <= types
    assert any(d["doc_type"] == "change" and "Score down 30" in d["title"] for d in why[:2])  # the computed change is a top answer
    assert all(d["doc_type"] in ("change", "history", "account_history", "report_old", "factor", "report") for d in why[:4])
    assert now[0]["doc_type"] == "account" and "80,000" in now[0]["text"]  # the latest month first, not history


def test_chat_gets_exact_changes_and_never_identifiers(client, two_months, monkeypatch):
    h, _ = two_months
    seen = {}
    monkeypatch.setattr(ai_assistant, "provider", lambda: "groq")
    monkeypatch.setattr(ai_assistant, "_groq_json", lambda system, user, max_tokens=300: seen.setdefault("json", []).append(user) or None)

    def fake(system, msgs):
        seen["system"], seen["msgs"] = system, msgs
        return "Your score fell by 30 points [U1]."
    monkeypatch.setattr(ai_assistant, "_call_groq", fake)
    r = client.post("/api/m/chat", headers=h, json={"message": "My PAN is ABCDE1234F and phone 9876543210. Why did my score drop?"})
    assert r.status_code == 200, r.text
    assert "750 to 720" in seen["system"]  # exact computed change is in the facts
    everything = seen["system"] + json.dumps(seen["msgs"]) + json.dumps(seen.get("json", []))
    assert "ABCDE1234F" not in everything and "9876543210" not in everything
    assert "[PAN]" in json.dumps(seen["msgs"])


def test_scrub():
    t = user_rag.scrub("PAN ABCDE1234F, 9876543210, a@b.co, 1234 5678 9012, 4111111111111111, score 750, Rs 280000, 2027")
    assert "ABCDE1234F" not in t and "9876543210" not in t and "a@b.co" not in t and "4111111111111111" not in t
    assert "750" in t and "280000" in t and "2027" in t


def test_inverted_index_matches_brute_force():
    kb = ai_assistant.KB
    kb.ensure()
    if not kb.chunks:
        pytest.skip("no knowledge base in this environment")

    def brute(query, k=8):
        q = ai_assistant._tokens(query)
        N, k1, b = len(kb.chunks), 1.5, 0.75
        scored = []
        for ch in kb.chunks:
            s = 0.0
            for t in q:
                f = ch["tf"].get(t)
                if f:
                    idf = math.log(1 + (N - kb.df[t] + 0.5) / (kb.df[t] + 0.5))
                    s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * ch["len"] / kb.avgdl))
            if s > 0:
                s *= 1.15 if ch["lang"] == "en" else 1
                s *= 1.25 if ch.get("official") else 1
                scored.append((s, ch["url"]))
        scored.sort(key=lambda x: -x[0])
        out, seen = [], set()
        for s, u in scored:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out[:k]

    for q in ["how to improve credit score", "hard enquiry", "RBI complaint credit information company", "late payment stays how long"]:
        kb._cache.clear()
        assert [s["url"] for s in kb.search(q, k=8)] == brute(q)


def test_usage_events_need_consent_and_feed_shortcuts(client):
    email, h, _ = signup(client)
    batch = {"session_id": "s1", "app_version": "1.0.0", "platform": "android", "os": "Android 15", "device": "Pixel 6",
             "events": [{"name": "tool_open", "props": {"tool": "EMI calc", "email": "x@y.com"}}] * 3 + [{"name": "tool_open", "props": {"tool": "Disputes"}}]}
    assert client.post("/api/m/events", headers=h, json=batch).json()["accepted"] == 0  # no consent: nothing kept
    assert client.get("/api/m/engagement", headers=h).json()["top_tools"] == []
    client.post("/api/m/privacy/consents", headers=h, json={"purpose": "analytics", "granted": True})
    assert client.post("/api/m/events", headers=h, json=batch).json()["accepted"] == 4
    assert client.get("/api/m/engagement", headers=h).json()["top_tools"] == ["EMI calc", "Disputes"]
    c = mobile_api.db()
    row = c.execute("SELECT props FROM app_events ORDER BY id DESC LIMIT 1").fetchone()
    blob = json.dumps([dict(r) for r in c.execute("SELECT * FROM app_events")])
    c.close()
    assert "x@y.com" not in blob and "ip" not in json.loads(row["props"])  # only whitelisted properties
    client.post("/api/m/privacy/consents", headers=h, json={"purpose": "analytics", "granted": False})
    assert client.get("/api/m/engagement", headers=h).json()["consented"] is False
    c = mobile_api.db()
    left = c.execute("SELECT COUNT(*) FROM app_events WHERE user_id=(SELECT id FROM users WHERE email=?)", (email,)).fetchone()[0]
    c.close()
    assert left == 0  # withdrawing the consent deletes the events
