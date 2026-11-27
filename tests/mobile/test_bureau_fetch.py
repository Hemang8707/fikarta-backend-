"""Surepass CRIF fetch: mapping from the documented sample response, consent, PAN lock and limits.
The Surepass HTTP call is replaced with the documented sample: no network, no real PAN."""
import json
import os

import pytest

import mobile_api
import surepass_crif as sp
from conftest import signup

with open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "surepass", "crif_sample.json"), encoding="utf-8") as _f:
    SAMPLE = json.load(_f)


def _set_pan(email, pan):
    c = mobile_api.db()
    c.execute("UPDATE users SET pan_enc=? WHERE email=?", (mobile_api.enc(pan), email))
    c.commit()
    c.close()


def test_mapper_reads_documented_response():
    d = sp.map_surepass_crif(SAMPLE)
    assert d["bureau"] == "CRIF High Mark" and d["score"] == 750 and d["report_date"] == "2025-07-25"
    by = {a["lender"]: a for a in d["accounts"]}
    assert "AXIS BANK" not in by  # secondary match is excluded and reported
    home, pl, card = by["HDFC BANK"], by["ICICI BANK"], by["SBI CARD"]
    assert home["type"] == "Home Loan" and home["status"] == "Active" and home["balance"] == "280000" and home["sanctioned"] == "300000"
    assert home["opened"] == "2023-05-10" and home["tenure_months"] == 240 and home["dpd_events"] == 0 and home["account_no"] == "XXXX4756"
    assert pl["status"] == "Closed" and pl["balance"] == "0"
    assert card["type"] == "Credit Card" and card["limit"] == "100000" and card["balance"] == "45000"
    assert card["overdue"] is None  # blank stays unknown, never zero
    assert card["dpd_events"] == 1  # one 30-DPD month; the XXX month is not counted either way
    assert [q["date"] for q in d["enquiries"]] == ["2025-05-10", "2025-04-15"]
    codes = {i["code"] for i in d["issues"]}
    assert {"SECONDARY_MATCHES", "INQUIRY_DATE_MISSING"} <= codes


def test_mapper_score_mismatch_and_bad_input():
    s = json.loads(json.dumps(SAMPLE))
    s["data"]["credit_score"] = "700"
    d = sp.map_surepass_crif(s)
    assert d["score"] == 750 and any(i["code"] == "SCORE_MISMATCH" for i in d["issues"])
    with pytest.raises(sp.BureauError):
        sp.map_surepass_crif({"data": {}})


def test_late_months_parser():
    assert sp.late_months("") is None
    assert sp.late_months("Jul:2025,XXX/XXX|") is None
    assert sp.late_months("Jul:2025,000/STD|Jun:2025,090/SUB|May:2025,015/SMA|") == 2


@pytest.fixture
def fake_surepass(monkeypatch):
    calls = []
    monkeypatch.setenv("SUREPASS_TOKEN", "test-token")

    def fake(first, last, mobile, pan, timeout=45.0):
        calls.append((first, last, mobile, pan))
        if mobile == "9000000000":
            raise sp.BureauError("no_match", "verification_failed")
        return SAMPLE
    monkeypatch.setattr(sp, "fetch_report", fake)
    return calls


def test_not_configured(client, monkeypatch):
    monkeypatch.delenv("SUREPASS_TOKEN", raising=False)
    _, h, _ = signup(client)
    assert client.get("/api/m/credit/fetch/status", headers=h).json()["available"] is False
    assert client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True}).status_code == 503


def test_fetch_saves_report_uses_account_pan_and_locks_it(client, fake_surepass):
    email, h, _ = signup(client)
    _set_pan(email, "LOCPD1234A")
    assert client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": False}).status_code == 400
    r = client.post("/api/m/credit/fetch", headers=h, json={"mobile": "98765 43210", "consent": True})
    assert r.status_code == 201, r.text
    assert r.json()["score"] == 750 and r.json()["source"] == "crif_api"
    assert fake_surepass[-1][3] == "LOCPD1234A" and fake_surepass[-1][2] == "9876543210"  # PAN from the account, never the request
    assert client.get("/api/m/credit/report", headers=h).json()["bureau"] == "CRIF High Mark"
    assert client.put("/api/m/me", headers=h, json={"pan": "ZZZPZ9999Z"}).status_code == 403  # PAN locked
    assert client.put("/api/m/me", headers=h, json={"pan": "LOCPD1234A"}).status_code == 200  # same PAN is fine
    assert client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True}).status_code == 429  # cooldown
    st = client.get("/api/m/credit/fetch/status", headers=h).json()
    assert st["available"] and st["next_allowed"] and st["last_fetched"] and st["pan"] != "LOCPD1234A"


def test_same_pan_cannot_be_fetched_by_another_account(client, fake_surepass):
    e1, h1, _ = signup(client)
    e2, h2, _ = signup(client)
    _set_pan(e1, "SHAPE1234B")
    _set_pan(e2, "SHAPE1234B")
    assert client.post("/api/m/credit/fetch", headers=h1, json={"mobile": "9876543210", "consent": True}).status_code == 201
    assert client.post("/api/m/credit/fetch", headers=h2, json={"mobile": "9876543210", "consent": True}).status_code == 409


def test_no_match_and_attempt_limit(client, fake_surepass):
    email, h, _ = signup(client)
    _set_pan(email, "NOMPT1234C")
    for _ in range(3):
        assert client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9000000000", "consent": True}).status_code == 422
    assert client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True}).status_code == 429


def test_requires_credit_report_consent(client, fake_surepass):
    email, h, _ = signup(client, consent_bureau=False)
    _set_pan(email, "NOCPN1234D")
    assert client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True}).status_code == 403


def test_response_shape_has_paths_but_no_values():
    shape = sp.response_shape(SAMPLE)
    assert "data.credit_report.RESPONSES.RESPONSE[].LOAN-DETAILS.CURRENT-BAL:str" in shape
    blob = " ".join(shape)
    for value in ("AXYPR5678L", "9876543210", "RAHUL", "2,80,000", "HDFC BANK"):
        assert value not in blob


def test_non_personal_pan_is_refused_before_calling_the_bureau(client, fake_surepass):
    email, h, _ = signup(client)
    _set_pan(email, "ABCDE1234F")  # 4th letter D: not a personal PAN
    r = client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True})
    assert r.status_code == 400 and "4th letter" in r.json()["detail"]
    assert fake_surepass == []  # no paid bureau call was made


def test_invalid_input_and_placeholder_dates(client, monkeypatch):
    import copy
    s = copy.deepcopy(SAMPLE)
    s["data"]["credit_report"]["HEADER"]["DATE-OF-ISSUE"] = "01-01-2000"
    s["data"]["credit_report"]["HEADER"]["DATE-OF-REQUEST"] = ""
    d = sp.map_surepass_crif(s)
    import datetime as dt
    assert d["report_date"] == dt.date.today().isoformat()  # placeholder date never shown as the report date
    assert any(i["code"] == "REPORT_DATE_MISSING" for i in d["issues"])

    monkeypatch.setenv("SUREPASS_TOKEN", "test-token")
    monkeypatch.setattr(sp, "fetch_report", lambda *a, **k: (_ for _ in ()).throw(sp.BureauError("invalid_input", "Input payload validation failed")))
    email, h, _ = signup(client)
    _set_pan(email, "VALPD1234A")
    r = client.post("/api/m/credit/fetch", headers=h, json={"mobile": "9876543210", "consent": True})
    assert r.status_code == 422 and r.json()["detail"] == "bureau_invalid_input"
