"""Home checklist and first-login terms for admin-created accounts."""
import subprocess
import sys

import mobile_api
from conftest import PASSWORD, signup


def test_completeness_tracks_missing_pieces(client):
    _, h, _ = signup(client, consent_bureau=False)
    j = client.get("/api/m/profile/completeness", headers=h).json()
    by = {i["key"]: i for i in j["items"]}
    assert j["terms_accepted"] is True  # signed up in the app
    assert by["pan"]["done"] is True and by["report"]["done"] is False and by["credit_consent"]["done"] is False
    assert by["phone"]["done"] is False and by["work_income"]["done"] is False
    client.put("/api/m/me", headers=h, json={"phone": "9876543210", "last_name": "Verma"})
    client.put("/api/m/ai/profile/monthly_income", headers=h, json={"value": "60000"})
    client.put("/api/m/ai/profile/employment_type", headers=h, json={"value": "salaried"})
    by = {i["key"]: i for i in client.get("/api/m/profile/completeness", headers=h).json()["items"]}
    assert by["phone"]["done"] and by["name"]["done"] and by["work_income"]["done"]


def test_admin_created_account_accepts_terms_on_first_login(client):
    c = mobile_api.db()
    email = "admin.made@example.com"
    c.execute("INSERT INTO users(email,pw_hash,first_name,last_name,phone,referral_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
              (email, mobile_api.hash_pw(PASSWORD), "Team", "Member", "9876543210", "FKTEST1", mobile_api.now(), mobile_api.now()))
    c.commit()
    c.close()
    r = client.post("/api/m/auth/signin", json={"email": email, "password": PASSWORD, "device": "t"})
    assert r.status_code == 200, r.text
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert client.get("/api/m/profile/completeness", headers=h).json()["terms_accepted"] is False
    assert client.post("/api/m/privacy/accept-terms", headers=h).json()["terms_accepted"] is True
    assert client.get("/api/m/profile/completeness", headers=h).json()["terms_accepted"] is True


def test_create_user_script_rejects_bad_input():
    import os
    script = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "create_user.py")
    r = subprocess.run([sys.executable, script, "--email", "not-an-email", "--first", "X"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "Invalid email" in r.stdout


def test_saving_a_pan_completes_setup_once(client):
    c = mobile_api.db()
    email = "pan.once@example.com"
    c.execute("INSERT INTO users(email,pw_hash,first_name,last_name,referral_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
              (email, mobile_api.hash_pw(PASSWORD), "Pan", "Once", "FKPANONE", mobile_api.now(), mobile_api.now()))
    c.commit()
    c.close()
    h = {"Authorization": "Bearer " + client.post("/api/m/auth/signin", json={"email": email, "password": PASSWORD, "device": "t"}).json()["access_token"]}
    me = client.get("/api/m/me", headers=h).json()
    assert me["onboarded"] is False and me["has_pan"] is False
    me = client.put("/api/m/me", headers=h, json={"pan": "ONCPE1234A"}).json()
    assert me["onboarded"] is True and me["has_pan"] is True and me["pan_masked"].endswith("4A")
