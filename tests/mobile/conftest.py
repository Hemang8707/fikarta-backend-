"""Isolated test setup for the mobile API: a throwaway database and secret per test session, no real data."""
import datetime as dt
import os
import random
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="fikarta-mobile-test-")
os.environ["MOBILE_DATABASE_PATH"] = os.path.join(_TMP, "mobile.db")
os.environ["FIKARTA_JWT_SECRET"] = "test-secret-" + "x" * 40
os.environ.pop("ANTHROPIC_API_KEY", None)  # AI calls must never leave the test process
os.environ["FIKARTA_AI_DISABLED"] = "1"

SERVER = os.path.join(os.path.dirname(__file__), "..", "..", "server")
sys.path.insert(0, os.path.abspath(SERVER))

from fastapi.testclient import TestClient  # noqa: E402

import clock  # noqa: E402
import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "crif_synthetic")
PASSWORD = "Fikarta2026x"


@pytest.fixture(scope="session")
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _release_clock():
    yield
    clock.freeze(None)


def signup(client, consent_bureau=True, ai=False, reminders=False):
    email = f"t{random.randint(1, 10**9)}@example.com"
    r = client.post("/api/m/auth/signup", json={"email": email, "password": PASSWORD, "first_name": "Test", "accept_terms": True})
    assert r.status_code == 201, r.text
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    body = {"pan": "ABCDE1234F", "dob": "1990-01-01", "onboarded": True}
    if consent_bureau:
        body["consent_bureau"] = True
    assert client.put("/api/m/me", headers=h, json=body).status_code == 200
    for purpose, on in (("ai_chat", ai), ("reminders", reminders)):
        if on:
            assert client.post("/api/m/privacy/consents", headers=h, json={"purpose": purpose, "granted": True}).status_code == 200
    return email, h, r.json()


def fixture_path(name):
    return os.path.join(FIXTURES, name)


def days_from_now(days):
    return clock.utcnow() + dt.timedelta(days=days)
