"""One report format for Experian and CRIF, the richer RAG chunks built from it, hybrid retrieval and isolation."""
import json
import os
import subprocess
import sys

import bureau_formats as bf
import embeddings
import user_rag
from conftest import fixture_path, signup
from mobile_api import db, latest_report, now, save_bureau_raw, save_report

HERE = os.path.dirname(__file__)
EXP = os.path.join(HERE, "..", "fixtures", "experian_synthetic.json")


def _exp():
    with open(EXP, encoding="utf-8") as f:
        return json.load(f)


def _uid(client, h):
    return client.get("/api/m/me", headers=h).json()["id"]


def test_experian_maps_everything_without_identifiers():
    raw = _exp()
    d = bf.map_any(raw)
    assert d["bureau"] == "Experian" and d["score"] == 761 and d["report_date"] == "2026-06-01" and d["report_id"] == "TESTRPT001"
    pl, card, closed = d["accounts"]
    assert (pl["type"], pl["status"], pl["emi"], pl["interest_rate"], pl["dpd_events"]) == ("Personal Loan", "Active", "9500", "13.5", 1)
    assert pl["payment_history"][0] == {"month": "2026-05", "dpd": 0, "asset": "S"} and pl["last_payment_date"] == "2026-05-05"
    assert card["type"] == "Credit Card" and card["limit"] == "100000" and card["sanctioned"] is None
    assert closed["status"] == "Closed" and closed["closed_date"] == "2026-02-10"
    assert pl["account_no"] == "XXXX1234"
    kinds = {e["kind"] for e in d["enquiries"]}
    assert kinds == {"credit", "non_credit"} and d["enquiries"][0]["amount"] == "300000"
    assert d["profile"]["city"] == "Pune" and d["profile"]["stated_income"] == "55000" and d["profile"]["gender"] == "Male"
    assert d["bureau_summary"]["accounts_total"] == 3 and d["bureau_summary"]["outstanding_total"] == "150000"
    blob = json.dumps(d)
    for ident in ("TESTP1234T", "9000012345", "test.person@example.com", "TEST ROAD", "TESTACCT00001234", "TEST PERSON"):
        assert ident not in blob, ident
    assert bf.identity(raw) == {"name": "TEST PERSON", "pan": "TESTP1234T", "mobile": "9000012345"}


def test_crif_b2c_detected_and_enriched():
    with open(fixture_path("01_multi_account_standard.json"), encoding="utf-8") as f:
        raw = json.load(f)
    if not bf.is_crif_b2c(raw):
        import pytest
        pytest.skip("fixture is not in B2C layout")
    d = bf.map_any(raw)
    assert d["bureau"] == "CRIF High Mark" and "bureau_summary" in d and "profile" in d


def test_rich_chunks_reach_the_index_and_the_facts(client):
    _, h, _ = signup(client, ai=True)
    uid = _uid(client, h)
    c = db()
    try:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        save_report(c, u, bf.map_any(_exp()))
        user_rag.sync_user_docs(c, uid, latest_report(c, uid, raw=True), now())
        c.commit()
        docs = {r["doc_key"]: r["text"] for r in c.execute("SELECT doc_key, text FROM user_docs WHERE user_id=?", (uid,))}
    finally:
        c.close()
    acct = next(t for k, t in docs.items() if k.startswith("report:account:") and "TEST BANK ONE" in t)
    assert "EMI Rs 9,500" in acct and "Interest rate 13.5%" in acct
    pays = next(t for k, t in docs.items() if k.startswith("report:payments:") and "TEST BANK ONE" in t)
    assert "2026-04 (30 days)" in pays
    assert any(k == "report:profile" for k in docs) and any(k == "report:bureau_totals" for k in docs)
    assert all("TESTP1234T" not in t and "9000012345" not in t for t in docs.values())


def test_latest_report_of_each_bureau_is_indexed(client):
    _, h, _ = signup(client, ai=True)
    uid = _uid(client, h)
    c = db()
    try:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        save_report(c, u, bf.map_any(_exp()))
        crif = {"source": "crif", "bureau": "CRIF High Mark", "score": 702, "report_date": "2026-06-10",
                "accounts": [{"lender": "OTHER BANK", "type": "Personal Loan", "status": "Active", "balance": "1000"}], "enquiries": []}
        save_report(c, u, crif)
        user_rag.sync_user_docs(c, uid, latest_report(c, uid, raw=True), now())
        c.commit()
        keys = [r[0] for r in c.execute("SELECT doc_key FROM user_docs WHERE user_id=?", (uid,))]
    finally:
        c.close()
    assert "report:summary" in keys and "bureau:experian:summary" in keys


def test_hybrid_retrieval_uses_vectors_and_never_crosses_users(client, monkeypatch):
    # a deterministic fake embedder: a vector per keyword family
    words = ["interest", "payment", "card", "income", "loan"]

    def vec(t):
        t = t.lower()
        v = [1.0 if w in t else 0.0 for w in words] + [0.1]
        return embeddings._norm(v)
    monkeypatch.setattr(embeddings, "available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_docs", lambda texts: [vec(t) for t in texts])
    monkeypatch.setattr(embeddings, "embed_query", lambda t: vec(t))
    _, ha, _ = signup(client, ai=True)
    _, hb, _ = signup(client, ai=True)
    a, b = _uid(client, ha), _uid(client, hb)
    c = db()
    try:
        ua = c.execute("SELECT * FROM users WHERE id=?", (a,)).fetchone()
        save_report(c, ua, bf.map_any(_exp()))
        st = user_rag.sync_user_docs(c, a, latest_report(c, a, raw=True), now())
        assert st["embedded"] >= st["docs"]
        c.commit()
        got_a = user_rag.retrieve(c, a, ["what interest am I paying", "interest rate on my loan"], k=6)
        got_b = user_rag.retrieve(c, b, ["what interest am I paying", "interest rate on my loan"], k=6)
    finally:
        c.close()
    assert any("Interest rate 13.5%" in d["text"] for d in got_a)
    assert all("TEST BANK ONE" not in d["text"] for d in got_b)  # user B sees nothing of user A, vectors included
    assert all("_vec" not in d for d in got_a)


def test_raw_report_is_encrypted_and_erased(client):
    _, h, _ = signup(client, ai=True)
    uid = _uid(client, h)
    c = db()
    try:
        save_bureau_raw(c, uid, "Experian", "TESTRPT001", _exp())
        c.commit()
        row = c.execute("SELECT data_enc FROM bureau_raw WHERE user_id=?", (uid,)).fetchone()
    finally:
        c.close()
    assert row and "TESTP1234T" not in row["data_enc"]
    assert client.post("/api/m/privacy/erase-credit-data", headers=h).status_code == 200
    c = db()
    try:
        assert c.execute("SELECT COUNT(*) FROM bureau_raw WHERE user_id=?", (uid,)).fetchone()[0] == 0
    finally:
        c.close()


def test_import_script_deidentified_keeps_login_and_pan(client, tmp_path):
    email, h, _ = signup(client, ai=True)
    before = client.get("/api/m/me", headers=h).json()
    env = {**os.environ}
    script = os.path.join(HERE, "..", "..", "scripts", "import_bureau_json.py")
    r = subprocess.run([sys.executable, script, "--email", email, "--file", EXP], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    after = client.get("/api/m/me", headers=h).json()  # same session token still works: login untouched
    assert after["pan_masked"] == before["pan_masked"] and after["phone"] == before["phone"] and after["email"] == before["email"]
    rep = client.get("/api/m/credit/report", headers=h).json()
    assert rep["bureau"] == "Experian" and rep["score"] == 761
    c = db()
    try:
        assert c.execute("SELECT COUNT(*) FROM bureau_raw WHERE user_id=?", (after["id"],)).fetchone()[0] == 0  # raw not kept
    finally:
        c.close()
