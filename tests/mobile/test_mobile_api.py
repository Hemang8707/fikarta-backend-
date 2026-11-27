"""Behaviour tests for the Fikarta mobile API. Synthetic data only (see tests/fixtures/crif_synthetic)."""
import json
from decimal import Decimal

import pytest

import clock
import crif_ingest
import mobile_api
from conftest import PASSWORD, days_from_now, fixture_path, signup


# --------------------------------------------------------------------------- auth
class TestAuth:
    def test_weak_password_rejected(self, client):
        r = client.post("/api/m/auth/signup", json={"email": "weak@example.com", "password": "short", "first_name": "A", "accept_terms": True})
        assert r.status_code == 422

    def test_terms_must_be_accepted(self, client):
        r = client.post("/api/m/auth/signup", json={"email": "terms@example.com", "password": PASSWORD, "first_name": "A", "accept_terms": False})
        assert r.status_code == 400

    def test_duplicate_email_rejected_case_insensitive(self, client):
        email, _, _ = signup(client)
        r = client.post("/api/m/auth/signup", json={"email": email.upper(), "password": PASSWORD, "first_name": "A", "accept_terms": True})
        assert r.status_code == 409

    def test_new_passwords_use_argon2id(self, client):
        email, _, _ = signup(client)
        c = mobile_api.db()
        h = c.execute("SELECT pw_hash FROM users WHERE email=?", (email,)).fetchone()["pw_hash"]
        c.close()
        assert h.startswith("$argon2id$")

    def test_legacy_bcrypt_hash_upgrades_on_signin(self, client):
        import bcrypt
        email, _, _ = signup(client)
        c = mobile_api.db()
        c.execute("UPDATE users SET pw_hash=? WHERE email=?", (bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(), email))
        c.commit()
        assert client.post("/api/m/auth/signin", json={"email": email, "password": PASSWORD}).status_code == 200
        h = c.execute("SELECT pw_hash FROM users WHERE email=?", (email,)).fetchone()["pw_hash"]
        c.close()
        assert h.startswith("$argon2id$")

    def test_wrong_password_is_generic_and_locks_out(self, client):
        email, _, _ = signup(client)
        for _ in range(5):
            r = client.post("/api/m/auth/signin", json={"email": email, "password": "Wrongpass1"})
            assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password"
        r = client.post("/api/m/auth/signin", json={"email": email, "password": PASSWORD})
        assert r.status_code == 429  # locked even with the right password

    def test_refresh_token_rotation_and_reuse_detection(self, client):
        _, _, s = signup(client)
        r1 = client.post("/api/m/auth/refresh", json={"refresh_token": s["refresh_token"]})
        assert r1.status_code == 200
        reuse = client.post("/api/m/auth/refresh", json={"refresh_token": s["refresh_token"]})
        assert reuse.status_code == 401
        # reuse revokes every session, including the token just issued
        me = client.get("/api/m/me", headers={"Authorization": "Bearer " + r1.json()["access_token"]})
        assert me.status_code == 401

    def test_access_token_expires(self, client):
        _, h, _ = signup(client)
        clock.freeze(days_from_now(1))
        assert client.get("/api/m/me", headers=h).status_code == 401

    def test_pan_is_encrypted_and_masked(self, client):
        email, h, _ = signup(client)
        assert client.get("/api/m/me", headers=h).json()["pan_masked"] == "ABXXXXX34F"
        c = mobile_api.db()
        enc = c.execute("SELECT pan_enc FROM users WHERE email=?", (email,)).fetchone()["pan_enc"]
        c.close()
        assert "ABCDE1234F" not in enc


# --------------------------------------------------------------------------- unknown != zero, Decimal money
class TestUnknownIsNotZero:
    def test_no_cards_means_utilisation_not_enough_data(self, client):
        _, h, _ = signup(client)
        r = client.post("/api/m/credit/report", headers=h, json={"score": 712, "accounts": [{"lender": "Synth Bank", "type": "Personal Loan", "balance": "150000"}]})
        assert r.status_code == 201, r.text
        f = {x["key"]: x for x in r.json()["factors"]}
        assert f["utilisation"]["grade"] == mobile_api.NO_DATA and f["utilisation"]["score"] is None
        assert r.json()["utilisation"] is None

    def test_missing_amounts_stay_missing(self, client):
        _, h, _ = signup(client)
        r = client.post("/api/m/credit/report", headers=h, json={"score": 700, "accounts": [{"lender": "Synth Bank", "type": "Personal Loan"}]})
        a = r.json()["accounts"][0]
        assert a["balance"] is None and a["overdue"] is None
        assert r.json()["summary"]["balance_missing"] == 1

    def test_money_is_exact_decimal(self):
        accts = [{"lender": "A", "type": "Credit Card", "status": "Active", "limit": "0.30", "balance": "0.10", "ref": "1"},
                 {"lender": "B", "type": "Credit Card", "status": "Active", "limit": "0.30", "balance": "0.20", "ref": "2"}]
        res = mobile_api.compute(accts, 0)
        assert res["utilisation"] == 50.0  # 0.30/0.60 exactly; float sums would not be exact
        assert mobile_api.D(0.1) == Decimal("0.1")

    def test_pdf_reader_never_turns_missing_into_zero(self):
        d = mobile_api.parse_report_text("CIBIL SCORE 734\nMember Name: SYNTH BANK  Account Type: Personal Loan  Date Opened: 05-03-2019")
        a = d["accounts"][0]
        assert d["score"] == 734 and a["balance"] is None and a["sanctioned"] is None and a["dpd_events"] is None

    def test_simulator_returns_direction_not_points(self, client):
        _, h, _ = signup(client)
        client.post("/api/m/credit/report", headers=h, json={"score": 700, "accounts": [
            {"lender": "Synth Bank", "type": "Credit Card", "limit": "100000", "balance": "80000", "opened": "2018-01-01", "dpd_events": 0}]})
        r = client.post("/api/m/credit/simulate", headers=h, json={"pay_card_balance_pct": 80}).json()
        assert r["direction"] == "up" and "projected" not in r
        r = client.post("/api/m/credit/simulate", headers=h, json={"miss_payment": True}).json()
        assert r["direction"] == "down"


# --------------------------------------------------------------------------- CRIF ingestion (creditsage-lab adapter)
class TestCrif:
    @pytest.mark.parametrize("name", ["01_multi_account_standard.json", "02_multi_revolving_account.json", "05_mixed_payment_tokens.json"])
    def test_synthetic_reports_map(self, name):
        d = crif_ingest.map_crif(open(fixture_path(name), encoding="utf-8").read())
        assert d["source"] == "crif" and d["accounts"]
        assert 300 <= d["score"] <= 900

    def test_unmappable_status_is_unknown_not_guessed(self):
        d = crif_ingest.map_crif(open(fixture_path("07_malformed_unmappable_records.json"), encoding="utf-8").read())
        a = d["accounts"][0]
        assert a["status"] == "Unknown" and a["type"] == "Other" and d["parse_status"] == "PARTIAL_SUCCESS" and d["issues"]

    def test_upload_parse_then_save(self, client):
        _, h, _ = signup(client)
        with open(fixture_path("01_multi_account_standard.json"), "rb") as f:
            r = client.post("/api/m/credit/report/parse", headers=h, files={"file": ("report.json", f, "application/json")})
        assert r.status_code == 200, r.text
        draft = r.json()
        assert client.get("/api/m/credit/report", headers=h).status_code == 404  # parsing never saves
        payload = {k: draft[k] for k in ("source", "bureau", "score", "report_date", "accounts", "enquiries", "issues")}
        s = client.post("/api/m/credit/report", headers=h, json=payload)
        assert s.status_code == 201, s.text
        assert s.json()["score"] == draft["score"] and s.json()["bureau"] == "CRIF High Mark"

    def test_rejects_non_report_files(self, client):
        _, h, _ = signup(client)
        r = client.post("/api/m/credit/report/parse", headers=h, files={"file": ("x.txt", b"hello", "text/plain")})
        assert r.status_code == 400


# --------------------------------------------------------------------------- consent (DPDP)
class TestConsent:
    def test_report_requires_consent(self, client):
        _, h, _ = signup(client, consent_bureau=False)
        r = client.post("/api/m/credit/report", headers=h, json={"score": 700})
        assert r.status_code == 403 and r.json()["detail"] == "consent_required"

    def test_consent_stores_notice_digest(self, client):
        _, h, _ = signup(client)
        d = client.get("/api/m/privacy/consents", headers=h).json()
        assert d["notice_digest"].startswith("sha256:") and len(d["notice_digest"]) == 71
        assert all(x["notice_digest"] == d["notice_digest"] for x in d["history"])

    def test_bureau_consent_expires_after_six_months(self, client):
        _, h, _ = signup(client)
        assert client.post("/api/m/credit/report", headers=h, json={"score": 700}).status_code == 201
        clock.freeze(days_from_now(184))
        # the old access token has expired at the frozen time; issue a fresh one for the same user
        c = mobile_api.db()
        u = c.execute("SELECT * FROM users ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        tok = mobile_api.make_access(u)
        hh = {"Authorization": "Bearer " + tok}
        st = {x["key"]: x for x in client.get("/api/m/privacy/consents", headers=hh).json()["consents"]}
        assert st["credit_report"]["status"] == "expired" and st["credit_report"]["granted"] is False
        r = client.post("/api/m/credit/report", headers=hh, json={"score": 700})
        assert r.status_code == 403 and r.json()["detail"] == "consent_expired"

    def test_required_consent_cannot_be_withdrawn(self, client):
        _, h, _ = signup(client)
        assert client.post("/api/m/privacy/consents", headers=h, json={"purpose": "service", "granted": False}).status_code == 400

    def test_withdrawing_ai_consent_deletes_chat(self, client):
        email, h, _ = signup(client, ai=True)
        c = mobile_api.db()
        uid = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        c.execute("INSERT INTO chat_messages(user_id,role,content,at) VALUES(?,?,?,?)", (uid, "user", "hi", mobile_api.now()))
        c.commit()
        client.post("/api/m/privacy/consents", headers=h, json={"purpose": "ai_chat", "granted": False})
        assert c.execute("SELECT COUNT(*) FROM chat_messages WHERE user_id=?", (uid,)).fetchone()[0] == 0
        c.close()

    def test_loans_need_reminders_consent(self, client):
        _, h, _ = signup(client)
        loan = {"lender": "Synth Bank", "account_no": "12345678", "principal": 100000, "outstanding": 50000, "emi": 5000, "due_day": 5}
        assert client.post("/api/m/loans", headers=h, json=loan).json()["detail"] == "reminders_consent_required"


# --------------------------------------------------------------------------- AI fails closed
class TestAIChat:
    def test_chat_needs_consent(self, client):
        _, h, _ = signup(client)
        assert client.post("/api/m/chat", headers=h, json={"message": "How is my score?"}).json()["detail"] == "ai_consent_required"

    def test_chat_fails_closed_without_model(self, client):
        _, h, _ = signup(client, ai=True)
        r = client.post("/api/m/chat", headers=h, json={"message": "How can I improve my score?"})
        assert r.status_code == 503 and r.json()["detail"] == "assistant_unavailable"
        assert client.get("/api/m/chat", headers=h).json() == []  # nothing stored when no answer was produced

    def test_no_keyword_reply_function_remains(self):
        assert not hasattr(mobile_api, "rule_reply")

    def test_facts_have_no_identifiers(self, client):
        _, h, _ = signup(client)
        client.post("/api/m/credit/report", headers=h, json={"score": 700, "accounts": [{"lender": "Synth Bank", "type": "Personal Loan", "balance": "1000"}]})
        c = mobile_api.db()
        rep = mobile_api.latest_report(c, c.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()["id"], raw=True)
        c.close()
        blob = json.dumps(mobile_api.report_facts(rep))
        assert "ABCDE1234F" not in blob and "ABXXXXX34F" not in blob and "personal" not in blob


# --------------------------------------------------------------------------- rights: export & erasure
class TestRights:
    def test_export_and_erasure(self, client):
        _, h, _ = signup(client)
        client.post("/api/m/credit/report", headers=h, json={"score": 700})
        assert "consents" in client.get("/api/m/me/export", headers=h).json()
        assert client.post("/api/m/privacy/erase-credit-data", headers=h).status_code == 200
        assert client.get("/api/m/credit/report", headers=h).status_code == 404
        assert client.get("/api/m/me", headers=h).json()["has_pan"] is False

    def test_delete_account(self, client):
        _, h, _ = signup(client)
        assert client.post("/api/m/me/delete", headers=h, json={"password": PASSWORD}).status_code == 200
        assert client.get("/api/m/me", headers=h).status_code == 401


def test_only_clock_reads_time():
    import pathlib
    import re
    root = pathlib.Path(mobile_api.__file__).parent
    bad = []
    for f in ("mobile_api.py", "mobile_privacy.py", "crif_ingest.py"):
        src = (root / f).read_text(encoding="utf-8")
        for pat in (r"datetime\.utcnow\(", r"datetime\.now\(", r"date\.today\(", r"time\.time\("):
            if re.search(pat, src):
                bad.append((f, pat))
    assert not bad, bad


# --------------------------------------------------------------------------- retrieval-augmented assistant
class TestRAG:
    def test_retrieval_finds_relevant_guides(self):
        import os
        import ai_assistant
        if not os.path.exists(ai_assistant.CONTENT_DB):
            pytest.skip("site database not present")
        hits = ai_assistant.KB.search("recovery agent harassment calling my family")
        assert hits and any("Kavach" in h["kind"] or "harass" in (h["title"] + h["text"]).lower() for h in hits)
        hits = ai_assistant.KB.search("what is credit utilisation ratio")
        assert any("utili" in (h["title"] + h["text"]).lower() for h in hits)

    def test_answer_validates_citations_and_uses_facts(self, monkeypatch):
        import ai_assistant
        seen = {}

        def fake_groq(system, msgs):
            seen["system"] = system
            return "Your score is 712 [S1]. See also [S9]."

        monkeypatch.setenv("FIKARTA_AI_DISABLED", "0")
        monkeypatch.setenv("GROQ_API_KEY", "test")
        monkeypatch.setattr(ai_assistant, "_call_groq", fake_groq)
        monkeypatch.setattr(ai_assistant.KB, "search", lambda q, k=5, lang="en": [{"title": "CIBIL score", "kind": "Glossary", "url": "/glossary/cibil", "text": "A 3-digit number"}])
        res = ai_assistant.answer([], "How is my score?", {"Has a credit report on Fikarta": True, "Credit score": 712}, "en")
        assert "[S9]" not in res["reply"] and res["citations"] == [{"n": 1, "title": "CIBIL score", "kind": "Glossary", "url": "/glossary/cibil"}]
        assert '"Credit score": 712' in seen["system"] and "ABCDE1234F" not in seen["system"]

    def test_provider_error_fails_closed(self, monkeypatch):
        import ai_assistant
        monkeypatch.setenv("FIKARTA_AI_DISABLED", "0")
        monkeypatch.setenv("GROQ_API_KEY", "test")

        def boom(system, msgs):
            raise ai_assistant.AssistantUnavailable()

        monkeypatch.setattr(ai_assistant, "_call_groq", boom)
        with pytest.raises(ai_assistant.AssistantUnavailable):
            ai_assistant.answer([], "hello", {"Has a credit report on Fikarta": False}, "en")


def test_email_check_and_rate_limit(client):
    email, _, _ = signup(client)
    assert client.post("/api/m/auth/check", json={"email": email}).json() == {"exists": True}
    assert client.post("/api/m/auth/check", json={"email": "nobody-here@example.com"}).json() == {"exists": False}
    for _ in range(30):
        r = client.post("/api/m/auth/check", json={"email": "x@example.com"})
    assert r.status_code == 429


def test_payments_are_self_reported_until_a_gateway_exists(client):
    _, h, _ = signup(client, reminders=True)
    r = client.post("/api/m/loans", headers=h, json={"lender": "HDFC Bank", "account_no": "XX1234", "principal": 100000, "outstanding": 50000, "emi": 5000, "due_day": 5})
    assert r.status_code == 201, r.text
    loan_id = r.json()["id"]
    before = client.get("/api/m/rewards", headers=h).json()["balance"]
    # no gateway: UPI/card "payments" and the points wallet are refused
    assert client.post("/api/m/payments", headers=h, json={"target": "loan", "target_id": loan_id, "amount": 5000, "method": "upi"}).status_code == 503
    assert client.post("/api/m/payments", headers=h, json={"target": "loan", "target_id": loan_id, "amount": 5000, "method": "wallet"}).status_code == 400
    # recording a payment made elsewhere updates the balance but earns no points
    r = client.post("/api/m/payments", headers=h, json={"target": "loan", "target_id": loan_id, "amount": 5000, "method": "self"})
    assert r.status_code == 201 and r.json()["self_reported"] is True and r.json()["points"] == 0
    assert client.get("/api/m/rewards", headers=h).json()["balance"] == before
    loan = next(l for l in client.get("/api/m/loans", headers=h).json() if l["id"] == loan_id)
    assert loan["outstanding"] == 45000
    due = next(r for r in client.get("/api/m/reminders", headers=h).json() if r["type"] == "loan" and r["id"] == loan_id)
    assert due["paid"] is True


def test_credit_builder_is_a_waitlist(client):
    _, h, _ = signup(client)
    r = client.post("/api/m/builder", headers=h, json={"amount": 10000, "tenure_months": 12, "monthly_income": 30000})
    assert r.status_code == 201 and r.json()["status"] == "waitlist"
    assert client.post("/api/m/builder", headers=h, json={"amount": 10000, "tenure_months": 12, "monthly_income": 30000}).status_code == 409


def test_public_legal_pages(client):
    for path, must in (("/legal/privacy", "Privacy notice"), ("/legal/terms", "Terms of use")):
        r = client.get(path)
        assert r.status_code == 200 and must in r.text and "text/html" in r.headers["content-type"]
    assert "Argon2id" in client.get("/legal/privacy").text
