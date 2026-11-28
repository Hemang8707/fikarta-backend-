"""
Fikarta mobile app API  (mounted at /api/m)

Security model
- Passwords: bcrypt (cost 12). Policy: 8-128 chars, at least one letter and one digit.
- Access tokens: JWT HS256, 30 min. Refresh tokens: random 256-bit, stored only as SHA-256 hash,
  rotated on every use, revoked on logout / password change / account deletion.
- Secret: FIKARTA_JWT_SECRET env var, otherwise generated once and kept in db/.mobile_secret.
- Brute force: 5 failed sign-ins per email (or 20 per IP) within 15 min locks sign-in for 15 min.
- PAN is encrypted at rest (Fernet, key derived from the secret) and only ever returned masked.
- User data lives in its own database file (db/fikarta_mobile.db), separate from site content.

Credit data
- Real bureau pulls need a licensed bureau partnership (Experian / CIBIL / CRIF credentials).
  BUREAU_PROVIDER selects the provider. Only "sandbox" ships here: it builds a deterministic,
  internally consistent report per user so every feature (factors, error scan, simulator,
  disputes, plan) works end to end. Every report carries provider + is_sandbox so the app labels it.
- Payments go through PAYMENT_PROVIDER; only "sandbox" ships (records the payment, no money moves).
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional

import bcrypt
import clock
from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
import jwt
from cryptography.fernet import Fernet
from fastapi import BackgroundTasks, APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOBILE_DB = os.getenv("MOBILE_DATABASE_PATH", os.path.join(BACKEND_DIR, "db", "fikarta_mobile.db"))
CONTENT_DB = os.getenv("DATABASE_PATH", os.path.join(BACKEND_DIR, "db", "fikarta.db"))
SECRET_FILE = os.getenv("MOBILE_SECRET_PATH", os.path.join(BACKEND_DIR, "db", ".mobile_secret"))
ACCESS_MIN = 30
REFRESH_DAYS = 30
BUREAU_PROVIDER = os.getenv("BUREAU_PROVIDER", "sandbox")
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "sandbox")
NOTICE_VERSION = "2026-11-27"  # DPDP privacy notice version shown at sign-up

router = APIRouter(prefix="/api/m", tags=["mobile"])


# --------------------------------------------------------------------------- secrets
def _load_secret() -> str:
    env = os.getenv("FIKARTA_JWT_SECRET")
    if env and len(env) >= 32:
        return env
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            s = f.read().strip()
            if len(s) >= 32:
                return s
    # Several server workers start at once: only one may create the secret (O_EXCL); the others read the winner's.
    # Both the JWT signing key and the PAN encryption key derive from it, so every worker must share it.
    s = secrets.token_urlsafe(48)
    try:
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(50):
            with open(SECRET_FILE, "r", encoding="utf-8") as f:
                got = f.read().strip()
            if len(got) >= 32:
                return got
            time.sleep(0.05)
        raise RuntimeError("mobile secret file exists but is empty")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(s)
        f.flush()
        os.fsync(f.fileno())
    return s


SECRET = _load_secret()
_FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("pan:" + SECRET).encode()).digest()))


def enc(v: Optional[str]) -> Optional[str]:
    return _FERNET.encrypt(v.encode()).decode() if v else None


def dec(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    try:
        return _FERNET.decrypt(v.encode()).decode()
    except Exception:
        return None


def mask_pan(p: Optional[str]) -> Optional[str]:
    return (p[:2] + "XXXXX" + p[-3:]) if p and len(p) == 10 else None


# --------------------------------------------------------------------------- db
def db() -> sqlite3.Connection:
    c = sqlite3.connect(MOBILE_DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def now() -> str:
    return clock.iso()


def init_mobile_db():
    c = db()
    c.executescript(
        """
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
      first_name TEXT NOT NULL, last_name TEXT, phone TEXT, pan_enc TEXT, dob TEXT, city TEXT,
      language TEXT DEFAULT 'en', consent_bureau INTEGER DEFAULT 0, consent_marketing INTEGER DEFAULT 0,
      consent_at TEXT, referral_code TEXT UNIQUE, referred_by TEXT, onboarded INTEGER DEFAULT 0,
      token_version INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS refresh_tokens(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash TEXT UNIQUE NOT NULL, expires_at INTEGER NOT NULL, revoked INTEGER DEFAULT 0,
      device TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS login_attempts(key TEXT, ts INTEGER);
    CREATE INDEX IF NOT EXISTS ix_la ON login_attempts(key, ts);
    CREATE TABLE IF NOT EXISTS reports(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      provider TEXT, score INTEGER, data TEXT, pulled_at TEXT);
    CREATE TABLE IF NOT EXISTS score_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      month TEXT, score INTEGER, UNIQUE(user_id, month));
    CREATE TABLE IF NOT EXISTS plan_tasks(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      code TEXT, title TEXT, detail TEXT, impact TEXT, week INTEGER, done INTEGER DEFAULT 0, done_at TEXT,
      UNIQUE(user_id, code));
    CREATE TABLE IF NOT EXISTS disputes(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      account_ref TEXT, lender TEXT, issue TEXT, details TEXT, bureau TEXT, letter TEXT,
      status TEXT DEFAULT 'draft', created_at TEXT, updated_at TEXT, due_by TEXT);
    CREATE TABLE IF NOT EXISTS dispute_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, dispute_id INTEGER NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
      kind TEXT, note TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS letters(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind TEXT, lender TEXT, body TEXT, status TEXT DEFAULT 'draft', created_at TEXT);
    CREATE TABLE IF NOT EXISTS chat_messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      role TEXT, content TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS loans(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      lender TEXT, kind TEXT, account_no TEXT, principal REAL, outstanding REAL, emi REAL, rate REAL,
      due_day INTEGER, tenure_months INTEGER, autopay INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS cards(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      issuer TEXT, name TEXT, last4 TEXT, credit_limit REAL, balance REAL, min_due REAL, due_day INTEGER,
      created_at TEXT);
    CREATE TABLE IF NOT EXISTS payments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      target TEXT, target_id INTEGER, lender TEXT, amount REAL, method TEXT, provider TEXT, ref TEXT,
      status TEXT, on_time INTEGER, points INTEGER DEFAULT 0, at TEXT);
    CREATE TABLE IF NOT EXISTS rewards(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      points INTEGER, reason TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind TEXT, title TEXT, body TEXT, read INTEGER DEFAULT 0, at TEXT);
    CREATE TABLE IF NOT EXISTS builder_applications(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      amount REAL, tenure_months INTEGER, monthly_income REAL, status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, detail TEXT, ip TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS consents(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      purpose TEXT, granted INTEGER, notice_version TEXT, language TEXT, at TEXT);
    """
    )
    c.commit()
    c.close()


def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _m2_consent_evidence(c):
    """DPDP consent evidence: SHA-256 digest of the exact notice shown, and an expiry for time-bound consents."""
    have = _cols(c, "consents")
    if "notice_digest" not in have:
        c.execute("ALTER TABLE consents ADD COLUMN notice_digest TEXT")
    if "expires_at" not in have:
        c.execute("ALTER TABLE consents ADD COLUMN expires_at TEXT")


def _m3_privacy_tables(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS nominees(
      user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, name TEXT, relation TEXT, contact TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS grievances(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      ref TEXT, category TEXT, message TEXT, status TEXT DEFAULT 'open', response TEXT, due_by TEXT, at TEXT, updated_at TEXT);
    """)


def _m7_vectors_raw(c):
    """Dense vectors stored beside each chunk (same row, same user_id filter) and the full bureau response, encrypted."""
    for t in ("user_docs", "user_memories"):
        cols = _cols(c, t)
        if "vec" not in cols:
            c.execute(f"ALTER TABLE {t} ADD COLUMN vec BLOB")
        if "vec_model" not in cols:
            c.execute(f"ALTER TABLE {t} ADD COLUMN vec_model TEXT")
    c.execute("""CREATE TABLE IF NOT EXISTS bureau_raw(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      bureau TEXT NOT NULL, report_id TEXT, data_enc TEXT NOT NULL, at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_br_user ON bureau_raw(user_id)")


def _m10_web(c):
    cols = _cols(c, "users")
    if "origin" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN origin TEXT DEFAULT 'app'")
    if "last_web_at" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN last_web_at TEXT")


def _m9_mail(c):
    import mail_store
    mail_store.init(c)
    mail_store.seed_contacts(c)


def save_bureau_raw(c, uid: int, bureau: str, report_id: Optional[str], raw: Any):
    """Keeps the complete bureau response (encrypted at rest) so nothing the bureau sent is lost. Never sent to the AI."""
    c.execute("INSERT INTO bureau_raw(user_id,bureau,report_id,data_enc,at) VALUES(?,?,?,?,?)",
              (uid, bureau, report_id, enc(json.dumps(raw, ensure_ascii=False)), now()))


# Versioned, append-only migrations. Never edit a shipped migration; add a new one.
MIGRATIONS = [
    (1, "baseline schema", lambda c: None),  # created by init_mobile_db (idempotent CREATE IF NOT EXISTS)
    (2, "consent evidence: notice digest + expiry", _m2_consent_evidence),
    (3, "privacy tables: nominees, grievances", _m3_privacy_tables),
    (4, "chat citations", lambda c: None if "citations" in _cols(c, "chat_messages") else c.execute("ALTER TABLE chat_messages ADD COLUMN citations TEXT")),
    (5, "user knowledge store, memories, retrieval traces", lambda c: __import__("user_rag").init(c)),
    (6, "versioned profile details from chat", lambda c: __import__("user_profile").init(c)),
    (7, "chunk vectors + encrypted raw bureau reports", lambda c: _m7_vectors_raw(c)),
    (8, "related questions on chat replies", lambda c: None if "suggestions" in _cols(c, "chat_messages") else c.execute("ALTER TABLE chat_messages ADD COLUMN suggestions TEXT")),
    (9, "bank emails: lender contacts (one-time directory copy), drafts, Gmail links", lambda c: _m9_mail(c)),
    (10, "web users: sign-up origin and last web visit", lambda c: _m10_web(c)),
]


def migrate():
    """Applies pending migrations. Every migration is idempotent (column checks, CREATE IF NOT EXISTS), and a
    duplicate-column error from a parallel worker that got there first is treated as already applied, so several
    server workers starting together cannot break or double-apply a migration."""
    init_mobile_db()
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    c.commit()
    done = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
    for v, name, fn in MIGRATIONS:
        if v in done:
            continue
        try:
            fn(c)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        c.execute("INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)", (v, name, clock.iso()))
        c.commit()
    c.close()


migrate()


def audit(c, uid, action, detail="", ip=""):
    c.execute("INSERT INTO audit_log(user_id,action,detail,ip,at) VALUES(?,?,?,?,?)", (uid, action, detail[:500], ip, now()))


def notify(c, uid, kind, title, body):
    c.execute("INSERT INTO notifications(user_id,kind,title,body,at) VALUES(?,?,?,?,?)", (uid, kind, title, body, now()))


# --------------------------------------------------------------------------- auth helpers
PW_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,128}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
# Argon2id for new passwords (same parameters as creditsage-lab); bcrypt hashes from earlier accounts still verify
# and are upgraded to Argon2id on the next successful sign-in.
_ARGON = Argon2Hasher(time_cost=2, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16)
_DUMMY_HASH = _ARGON.hash("timing-equaliser-1")


def hash_pw(pw: str) -> str:
    return _ARGON.hash(pw)


def check_pw(pw: str, h: str) -> bool:
    try:
        if h.startswith("$argon2"):
            return _ARGON.verify(h, pw)
        return bcrypt.checkpw(pw.encode(), h.encode())
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def needs_rehash(h: str) -> bool:
    return not h.startswith("$argon2") or _ARGON.check_needs_rehash(h)


def make_access(u) -> str:
    t = clock.epoch()
    return jwt.encode({"sub": str(u["id"]), "tv": u["token_version"], "iat": t, "exp": t + ACCESS_MIN * 60, "typ": "access"}, SECRET, algorithm="HS256")


def make_refresh(c, uid, device="") -> str:
    raw = secrets.token_urlsafe(32)
    c.execute(
        "INSERT INTO refresh_tokens(user_id,token_hash,expires_at,device,created_at) VALUES(?,?,?,?,?)",
        (uid, hashlib.sha256(raw.encode()).hexdigest(), clock.epoch() + REFRESH_DAYS * 86400, device[:120], now()),
    )
    return raw


def tokens(c, u, device=""):
    return {"access_token": make_access(u), "refresh_token": make_refresh(c, u["id"], device), "token_type": "bearer", "expires_in": ACCESS_MIN * 60}


def client_ip(req: Request) -> str:
    return (req.client.host if req.client else "") or ""


def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not signed in")
    try:
        # signature checked by PyJWT; time claims checked against the injected clock so expiry is testable
        p = jwt.decode(authorization[7:], SECRET, algorithms=["HS256"], options={"require": ["exp", "sub", "iat"], "verify_exp": False, "verify_iat": False})
        if p.get("typ") != "access":
            raise ValueError()
        if int(p["exp"]) <= clock.epoch():
            raise jwt.ExpiredSignatureError()
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired")
    except Exception:
        raise HTTPException(401, "Invalid session")
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (int(p["sub"]),)).fetchone()
    c.close()
    if not u or u["token_version"] != p.get("tv"):
        raise HTTPException(401, "Invalid session")
    return u


def public_user(u) -> Dict[str, Any]:
    return {
        "id": u["id"], "email": u["email"], "first_name": u["first_name"], "last_name": u["last_name"] or "",
        "phone": u["phone"] or "", "pan_masked": mask_pan(dec(u["pan_enc"])), "has_pan": bool(u["pan_enc"]),
        "dob": u["dob"] or "", "city": u["city"] or "", "language": u["language"] or "en",
        "consent_bureau": bool(u["consent_bureau"]), "consent_marketing": bool(u["consent_marketing"]),
        "referral_code": u["referral_code"], "onboarded": bool(u["onboarded"]), "created_at": u["created_at"],
    }


def _locked(c, email, ip) -> bool:
    since = clock.epoch() - 900
    c.execute("DELETE FROM login_attempts WHERE ts<?", (since - 3600,))
    e = c.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND ts>=?", ("e:" + email, since)).fetchone()[0]
    i = c.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND ts>=?", ("i:" + ip, since)).fetchone()[0]
    return e >= 5 or i >= 20


# --------------------------------------------------------------------------- models
class SignUp(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field("", max_length=60)
    referral_code: Optional[str] = Field(None, max_length=16)
    accept_terms: bool
    phone: Optional[str] = Field(None, max_length=20)  # optional; saved when given (web sign-ups need not give it)
    origin: Optional[str] = Field(None, max_length=10)  # 'web' for fikarta.com, else the app

    @field_validator("password")
    @classmethod
    def strong(cls, v):
        if not PW_RE.match(v):
            raise ValueError("Password needs at least 8 characters with a letter and a number")
        return v


class SignIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    device: str = Field("", max_length=120)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=200)


class ProfileIn(BaseModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=60)
    last_name: Optional[str] = Field(None, max_length=60)
    phone: Optional[str] = None
    pan: Optional[str] = None
    dob: Optional[str] = None
    city: Optional[str] = Field(None, max_length=60)
    language: Optional[str] = None
    consent_bureau: Optional[bool] = None
    consent_marketing: Optional[bool] = None
    onboarded: Optional[bool] = None


class PasswordIn(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def strong(cls, v):
        if not PW_RE.match(v):
            raise ValueError("Password needs at least 8 characters with a letter and a number")
        return v


class DeleteIn(BaseModel):
    password: str


# --------------------------------------------------------------------------- auth routes
@router.post("/auth/signup", status_code=201)
def signup(body: SignUp, req: Request):
    if not body.accept_terms:
        raise HTTPException(400, "Please accept the Terms and Privacy Policy")
    email = body.email.lower().strip()
    c = db()
    try:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "An account with this email already exists. Please sign in.")
        code = "FK" + secrets.token_hex(3).upper()
        ref = None
        if body.referral_code:
            r = c.execute("SELECT referral_code FROM users WHERE referral_code=?", (body.referral_code.strip().upper(),)).fetchone()
            ref = r["referral_code"] if r else None
        cur = c.execute(
            "INSERT INTO users(email,pw_hash,first_name,last_name,referral_code,referred_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (email, hash_pw(body.password), body.first_name.strip(), body.last_name.strip(), code, ref, now(), now()),
        )
        uid = cur.lastrowid
        phone = re.sub(r"\D", "", body.phone or "")[-10:]
        if phone and PHONE_RE.match(phone):
            c.execute("UPDATE users SET phone=? WHERE id=?", (phone, uid))
        if "origin" in _cols(c, "users"):
            c.execute("UPDATE users SET origin=? WHERE id=?", ("web" if (body.origin or "").lower() == "web" else "app", uid))
        if ref:
            rr = c.execute("SELECT id FROM users WHERE referral_code=?", (ref,)).fetchone()
            c.execute("INSERT INTO rewards(user_id,points,reason,at) VALUES(?,?,?,?)", (rr["id"], 100, "Friend joined with your code", now()))
            notify(c, rr["id"], "reward", "You earned 100 points", "A friend joined Fikarta with your referral code.")
        c.execute("INSERT INTO rewards(user_id,points,reason,at) VALUES(?,?,?,?)", (uid, 50, "Welcome bonus", now()))
        notify(c, uid, "welcome", "Welcome to Fikarta", "Check your credit score and get your free improvement plan.")
        # DPDP: the itemised notice was shown and the account purpose accepted at sign-up
        from mobile_privacy import record_consent
        record_consent(c, uid, "service", True)
        audit(c, uid, "signup", "notice " + NOTICE_VERSION, client_ip(req))
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        out = {"user": public_user(u), **tokens(c, u)}
        c.commit()
        return out
    finally:
        c.close()


class EmailCheck(BaseModel):
    email: EmailStr


@router.post("/auth/check")
def check_email(body: EmailCheck, req: Request):
    """Email-first sign-in step: tells the app whether to show 'enter password' or 'create account'.
    Rate-limited per IP (30 checks / 15 min) so the endpoint cannot be used to enumerate emails at scale."""
    ip = client_ip(req)
    c = db()
    try:
        since = clock.epoch() - 900
        n = c.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND ts>=?", ("c:" + ip, since)).fetchone()[0]
        if n >= 30:
            raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")
        c.execute("INSERT INTO login_attempts(key,ts) VALUES(?,?)", ("c:" + ip, clock.epoch()))
        exists = c.execute("SELECT 1 FROM users WHERE email=?", (body.email.lower().strip(),)).fetchone() is not None
        c.commit()
        return {"exists": exists}
    finally:
        c.close()


@router.post("/auth/signin")
def signin(body: SignIn, req: Request):
    email, ip = body.email.lower().strip(), client_ip(req)
    c = db()
    try:
        if _locked(c, email, ip):
            c.commit()
            raise HTTPException(429, "Too many attempts. Please wait 15 minutes and try again.")
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = check_pw(body.password, u["pw_hash"]) if u else (check_pw(body.password, _DUMMY_HASH) and False)
        if not ok:
            c.execute("INSERT INTO login_attempts(key,ts) VALUES(?,?)", ("e:" + email, clock.epoch()))
            c.execute("INSERT INTO login_attempts(key,ts) VALUES(?,?)", ("i:" + ip, clock.epoch()))
            c.commit()
            raise HTTPException(401, "Invalid email or password")
        c.execute("DELETE FROM login_attempts WHERE key=?", ("e:" + email,))
        if needs_rehash(u["pw_hash"]):
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(body.password), u["id"]))
        audit(c, u["id"], "signin", body.device, ip)
        out = {"user": public_user(u), **tokens(c, u, body.device)}
        c.commit()
        return out
    finally:
        c.close()


@router.post("/auth/refresh")
def refresh(body: RefreshIn):
    h = hashlib.sha256(body.refresh_token.encode()).hexdigest()
    c = db()
    try:
        r = c.execute("SELECT * FROM refresh_tokens WHERE token_hash=?", (h,)).fetchone()
        if not r or r["expires_at"] < clock.epoch():
            raise HTTPException(401, "Session expired. Please sign in again.")
        if r["revoked"]:
            # reuse of a rotated token: assume theft, revoke every session of this user
            c.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (r["user_id"],))
            c.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (r["user_id"],))
            c.commit()
            raise HTTPException(401, "Session expired. Please sign in again.")
        c.execute("UPDATE refresh_tokens SET revoked=1 WHERE id=?", (r["id"],))
        u = c.execute("SELECT * FROM users WHERE id=?", (r["user_id"],)).fetchone()
        out = {"user": public_user(u), **tokens(c, u, r["device"] or "")}
        c.commit()
        return out
    finally:
        c.close()


@router.post("/auth/logout")
def logout(body: RefreshIn, u=Depends(current_user)):
    c = db()
    c.execute("UPDATE refresh_tokens SET revoked=1 WHERE token_hash=? AND user_id=?", (hashlib.sha256(body.refresh_token.encode()).hexdigest(), u["id"]))
    c.commit()
    c.close()
    return {"ok": True}


@router.post("/auth/logout-all")
def logout_all(u=Depends(current_user)):
    c = db()
    c.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (u["id"],))
    c.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (u["id"],))
    c.commit()
    c.close()
    return {"ok": True}


@router.post("/auth/change-password")
def change_password(body: PasswordIn, req: Request, u=Depends(current_user)):
    if not check_pw(body.current_password, u["pw_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    c = db()
    c.execute("UPDATE users SET pw_hash=?, token_version=token_version+1, updated_at=? WHERE id=?", (hash_pw(body.new_password), now(), u["id"]))
    c.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (u["id"],))
    audit(c, u["id"], "change_password", "", client_ip(req))
    nu = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    out = {"user": public_user(nu), **tokens(c, nu)}
    c.commit()
    c.close()
    return out


@router.get("/me")
def me(u=Depends(current_user)):
    return public_user(u)


@router.put("/me")
def update_me(body: ProfileIn, u=Depends(current_user)):
    f: Dict[str, Any] = {}
    if body.first_name is not None:
        f["first_name"] = body.first_name.strip()
    if body.last_name is not None:
        f["last_name"] = body.last_name.strip()
    if body.phone is not None:
        p = re.sub(r"\D", "", body.phone)[-10:]
        if p and not PHONE_RE.match(p):
            raise HTTPException(400, "Enter a valid 10-digit Indian mobile number")
        f["phone"] = p
    if body.pan is not None:
        p = body.pan.strip().upper()
        if not PAN_RE.match(p):
            raise HTTPException(400, "Enter a valid PAN, for example ABCDE1234F")
        if p != (dec(u["pan_enc"]) or ""):
            # once a bureau report was fetched for this PAN it cannot be swapped (stops pulling other people's reports)
            c0 = db()
            try:
                locked = c0.execute("SELECT 1 FROM bureau_pulls WHERE user_id=? AND status='success'", (u["id"],)).fetchone() is not None
            except sqlite3.OperationalError:
                locked = False
            finally:
                c0.close()
            if locked:
                raise HTTPException(403, "Your PAN is locked because a credit report was fetched with it. Contact support to change it.")
        f["pan_enc"] = enc(p)
        f["onboarded"] = 1  # a saved PAN completes setup: the PAN step is never shown again
    if body.dob is not None:
        try:
            d = dt.date.fromisoformat(body.dob)
        except ValueError:
            raise HTTPException(400, "Date of birth must be YYYY-MM-DD")
        age = (clock.today() - d).days // 365
        if age < 18 or age > 100:
            raise HTTPException(400, "You must be at least 18 years old")
        f["dob"] = body.dob
    if body.city is not None:
        f["city"] = body.city.strip()
    if body.language is not None:
        if body.language not in ("en", "hi"):
            raise HTTPException(400, "Unsupported language")
        f["language"] = body.language
    changes = []
    if body.consent_bureau is not None:
        f["consent_bureau"] = int(body.consent_bureau)
        f["consent_at"] = now()
        changes.append(("credit_report", body.consent_bureau))
    if body.consent_marketing is not None:
        f["consent_marketing"] = int(body.consent_marketing)
        changes.append(("marketing", body.consent_marketing))
    if body.onboarded is not None:
        f["onboarded"] = int(body.onboarded)
    if not f:
        return public_user(u)
    f["updated_at"] = now()
    c = db()
    c.execute("UPDATE users SET " + ",".join(k + "=?" for k in f) + " WHERE id=?", (*f.values(), u["id"]))
    if changes:
        from mobile_privacy import record_consent
        for purpose, granted in changes:  # every consent change goes into the DPDP consent ledger
            record_consent(c, u["id"], purpose, granted, f.get("language") or u["language"] or "en")
    c.commit()
    nu = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    c.close()
    return public_user(nu)


@router.get("/me/export")
def export_me(u=Depends(current_user)):
    c = db()
    out: Dict[str, Any] = {"profile": public_user(u), "exported_at": now()}
    for t in ["reports", "score_history", "plan_tasks", "disputes", "letters", "chat_messages", "loans", "cards", "payments", "rewards", "notifications", "builder_applications", "audit_log", "consents", "grievances", "nominees", "user_memories", "user_docs", "rag_traces", "app_events", "user_profile_facts"]:
        rows = [dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE user_id=?", (u["id"],))]
        for r in rows:
            if t == "reports" and r.get("data"):
                r["data"] = json.loads(r["data"])
        out[t] = rows
    c.close()
    return out


@router.post("/me/delete")
def delete_me(body: DeleteIn, u=Depends(current_user)):
    if not check_pw(body.password, u["pw_hash"]):
        raise HTTPException(400, "Password is incorrect")
    c = db()
    c.execute("DELETE FROM users WHERE id=?", (u["id"],))
    audit(c, None, "account_deleted", "user %d" % u["id"])
    c.commit()
    c.close()
    return {"ok": True}


# --------------------------------------------------------------------------- lenders (from site DB)
def lender_names() -> List[str]:
    try:
        c = sqlite3.connect(CONTENT_DB)
        names = [r[0] for r in c.execute("SELECT name FROM lenders ORDER BY name")]
        c.close()
        if names:
            return names
    except Exception:
        pass
    return ["HDFC Bank", "ICICI Bank", "Axis Bank", "SBI", "Kotak Mahindra Bank", "Bajaj Finserv", "Tata Capital", "IDFC FIRST Bank"]


@router.get("/lenders")
def lenders(q: str = ""):
    try:
        c = sqlite3.connect(CONTENT_DB)
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT slug,name,type,rate_range,amount_range,tenure_range,fee_range,min_cibil,disbursal_speed,best_for,rating FROM lenders ORDER BY name")]
        c.close()
    except Exception:
        rows = [{"slug": n.lower().replace(" ", "-"), "name": n} for n in lender_names()]
    if q:
        rows = [r for r in rows if q.lower() in r["name"].lower()]
    return rows


# --------------------------------------------------------------------------- credit engine
# Every report shown to a user is THEIR report: a CRIF report file (parsed by creditsage-lab's adapter), their bureau
# PDF (parsed, then confirmed by them), or values they typed. Nothing is generated. The score shown is always the
# bureau's own score. Rules borrowed from creditsage-lab:
#   UNKNOWN != ZERO     a value the report does not contain stays None and is shown as "not in report".
#   Decimal money       amounts are parsed and summed as Decimal; floats appear only when serialising for display.
#   No invented grades  a factor without enough evidence is "Not enough data", never "Good".
#   No invented points  the what-if tool shows direction (helps / hurts), not a predicted score.
from decimal import Decimal, InvalidOperation

BANDS = [(740, "Excellent"), (670, "Good"), (580, "Fair"), (300, "Poor")]  # design kit v4.3, CIBIL default bands
CARD_TYPES = ("Credit Card",)
SECURED_TYPES = ("Home Loan", "Auto Loan", "Car Loan", "Two-Wheeler Loan", "Gold Loan", "Loan Against Property", "Secured Credit Card", "Loan Against FD")
ACCOUNT_TYPES = ["Credit Card", "Personal Loan", "Home Loan", "Car Loan", "Two-Wheeler Loan", "Gold Loan", "Education Loan", "Business Loan",
                 "Consumer Durable Loan", "Loan Against Property", "Overdraft", "Other"]
STATUSES = ["Active", "Closed", "Settled", "Written-Off", "Suit Filed", "Unknown"]
NO_DATA = "Not enough data"


def D(v) -> Optional[Decimal]:
    """Exact Decimal from a stored value; None stays None (never 0)."""
    if v is None or v == "":
        return None
    if isinstance(v, float):
        v = repr(v)
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def money_out(v) -> Optional[float]:
    """Presentation only: Decimal -> JSON number. Not used in any calculation."""
    d = D(v)
    return None if d is None else float(d)


def band(score: Optional[int]) -> str:
    if not score:
        return "No score"
    for lo, name in BANDS:
        if score >= lo:
            return name
    return "Poor"


def months_since(iso_date: Optional[str]) -> Optional[int]:
    try:
        d = dt.date.fromisoformat(iso_date[:10])
    except Exception:
        return None
    t = clock.today()
    return max(0, (t.year - d.year) * 12 + t.month - d.month)


def _grade(x: float) -> str:
    return "Excellent" if x >= 0.9 else "Good" if x >= 0.7 else "Fair" if x >= 0.5 else "Needs work"


def compute(accts: List[dict], enquiries_6m: int) -> Dict[str, Any]:
    """Explains the score from the accounts using the factor weights bureaus publish. Each factor is computed only
    from known values; with no usable evidence it is NO_DATA (score None). 'model' is used only for the direction of
    what-if changes and is never shown as a score."""
    active = [a for a in accts if a["status"] == "Active"]
    severe = sum(1 for a in accts if a["status"] in ("Written-Off", "Settled", "Suit Filed"))
    known_late = [int(a["dpd_events"]) for a in accts if a.get("dpd_events") is not None]
    late = sum(known_late)
    cards = [a for a in active if a["type"] in CARD_TYPES]
    cards_known = [a for a in cards if D(a.get("limit")) and D(a.get("balance")) is not None]
    lim = sum((D(a["limit"]) for a in cards_known), Decimal(0))
    bal = sum((D(a["balance"]) for a in cards_known), Decimal(0))
    util = (bal / lim) if lim else None
    ages = [m for m in (months_since(a.get("opened")) for a in accts) if m is not None]
    known_types = [a for a in accts if a["type"] != "Other"]
    secured = any(a["type"] in SECURED_TYPES for a in known_types)
    unsecured = any(a["type"] not in SECURED_TYPES for a in known_types)

    f_pay = max(0.0, 1 - late * 0.12 - severe * 0.25) if (known_late or severe) else None
    f_util = None
    if util is not None:
        u = float(util)
        f_util = 1.0 if u <= .10 else .9 if u <= .30 else .65 if u <= .50 else .4 if u <= .70 else .15
    f_age = min(1.0, (sum(ages) / len(ages)) / 84) if ages else None
    f_mix = (1.0 if (secured and unsecured) else 0.6) if known_types else None
    f_enq = max(0.0, 1 - enquiries_6m * 0.15)

    def fac(key, name, weight, f, value, missing_value, tip):
        return {"key": key, "name": name, "weight": weight, "known": f is not None, "score": None if f is None else round(f * 100),
                "grade": NO_DATA if f is None else _grade(f), "value": value if f is not None else missing_value, "tip": tip}

    util_pct = None if util is None else round(float(util * 100), 1)
    factors = [
        fac("payment", "Payment history", 35, f_pay,
            (f"{late} late month(s)" + (f", {severe} settled / written-off" if severe else "")) if (late or severe) else "No late payments in the report",
            "The report has no usable payment history", "Pay every EMI and card bill in full by the due date. Even one missed EMI can pull your score down sharply."),
        fac("utilisation", "Credit utilisation", 30, f_util, f"{util_pct}% of card limits used",
            "No active credit card with a reported limit" if not cards else "Card limit or balance missing in the report",
            "Keep card balances below 30% of the limit. Above 70% is a red flag for lenders."),
        fac("age", "Length of credit history", 15, f_age, f"{round(sum(ages) / len(ages) / 12, 1) if ages else 0} years average account age",
            "Account open dates are missing in the report", "Keep your oldest cards open, even if you rarely use them."),
        fac("mix", "Credit mix", 10, f_mix, "Secured and unsecured" if secured and unsecured else "Only one type of credit",
            "Account types are not stated in the report", "A healthy mix of secured and unsecured credit helps over time."),
        fac("enquiries", "New credit enquiries", 10, f_enq, f"{enquiries_6m} hard {'enquiry' if enquiries_6m == 1 else 'enquiries'} in 6 months", "",
            "Avoid applying with many lenders at once. Checking your own score is a soft enquiry and never hurts."),
    ]
    known = [(f["weight"], (f["score"] or 0) / 100) for f in factors if f["known"]]
    tw = sum(w for w, _ in known)
    model = (sum(w * v for w, v in known) / tw) if tw else None
    return {"model": model, "utilisation": util_pct, "factors": factors}


def scan_errors(accts: List[dict]) -> List[dict]:
    out = []
    seen: Dict[tuple, str] = {}
    for a in accts:
        amt = D(a.get("sanctioned")) or D(a.get("limit"))
        k = (a["lender"].strip().lower(), a["type"], amt, (a.get("opened") or "")[:7])
        if amt and a.get("opened") and k in seen:
            out.append({"code": "duplicate", "account_ref": a["ref"], "lender": a["lender"], "severity": "high", "title": "Same account reported twice",
                        "detail": f"{a['lender']} {a['type'].lower()} appears twice with the same amount and open date. Duplicates inflate your debt."})
        seen[k] = a["ref"]
        if a.get("closed_by_me") and a["status"] == "Active":
            out.append({"code": "closed_open", "account_ref": a["ref"], "lender": a["lender"], "severity": "high", "title": "Closed loan still shows active",
                        "detail": f"You said this {a['type'].lower()} is closed, but the bureau still lists it as active."})
        if a.get("not_mine"):
            out.append({"code": "unknown", "account_ref": a["ref"], "lender": a["lender"], "severity": "high", "title": "Account you do not recognise",
                        "detail": "You marked this account as not yours. It could be a reporting mistake or identity fraud. Dispute it quickly."})
        od = D(a.get("overdue"))
        if a["status"] == "Active" and od is not None and Decimal(0) < od <= Decimal(5000):
            out.append({"code": "small_overdue", "account_ref": a["ref"], "lender": a["lender"], "severity": "medium", "title": "Small unpaid balance",
                        "detail": f"₹{int(od):,} is overdue. Small dues like this add a late mark every month until cleared."})
        if a["status"] == "Settled":
            out.append({"code": "settled", "account_ref": a["ref"], "lender": a["lender"], "severity": "high", "title": "Account marked Settled",
                        "detail": "Settled can stay on the report for up to 7 years. If you have paid in full, ask the lender to report it as Closed and get a NOC."})
        tenure, age, bal = a.get("tenure_months"), months_since(a.get("opened")), D(a.get("balance"))
        if a["status"] == "Active" and a["type"] not in CARD_TYPES and bal == 0 and tenure and age is not None and age > int(tenure) + 2:
            out.append({"code": "dormant", "account_ref": a["ref"], "lender": a["lender"], "severity": "low", "title": "Finished loan still shows active",
                        "detail": "The tenure is over and the balance is zero, but the account is still reported active."})
        if a["status"] == "Unknown":
            out.append({"code": "unknown_status", "account_ref": a["ref"], "lender": a["lender"], "severity": "medium", "title": "Account status not readable",
                        "detail": f"The bureau reported a status we cannot map ({a.get('raw_status') or 'blank'}). Check this account in your report."})
    return out


def _sum_known(accts, field, pred) -> Dict[str, Any]:
    vals = [D(a.get(field)) for a in accts if pred(a)]
    known = [v for v in vals if v is not None]
    return {"total": sum(known, Decimal(0)), "missing": len(vals) - len(known)}


def build_report(u, raw: Dict[str, Any]) -> Dict[str, Any]:
    accts = []
    refs = set()
    for i, a in enumerate(raw.get("accounts") or []):
        a = dict(a)
        a["ref"] = a.get("ref") or a.get("account_no") or f"ACC{i + 1}"
        if a["ref"] in refs:  # masked numbers can share last 4 digits; every account needs its own reference
            a["ref"] = f"{a['ref']}-{i + 1}"
        refs.add(a["ref"])
        a["secured"] = a["type"] in SECURED_TYPES
        for f in ("sanctioned", "limit", "balance", "overdue"):
            d = D(a.get(f))
            a[f] = None if d is None else str(d)
        accts.append(a)
    six_ago = (clock.today() - dt.timedelta(days=183)).isoformat()
    enq = list(raw.get("enquiries") or [])
    enq_6m = sum(1 for e in enq if (e.get("date") or "") >= six_ago)
    res = compute(accts, enq_6m)
    score = raw.get("score")
    pan = dec(u["pan_enc"])
    bal = _sum_known(accts, "balance", lambda a: a["status"] == "Active")
    od = _sum_known(accts, "overdue", lambda a: True)
    return {
        "source": raw.get("source", "manual"), "provider": raw.get("source", "manual"), "is_sandbox": False,
        # True when the report came from the bureau partner's test environment: a fixed sample, not the user's data
        "test_data": bool(raw.get("test_data")),
        "bureau": raw.get("bureau") or "Credit bureau", "report_date": raw.get("report_date"),
        "score": score, "band": band(score), "range": [300, 900], "model": res["model"],
        "utilisation": res["utilisation"], "factors": res["factors"], "accounts": accts, "enquiries": enq, "enquiries_6m": enq_6m,
        "errors": scan_errors(accts), "parse_issues": raw.get("issues") or [],
        # full bureau detail kept in one format for every bureau (see bureau_formats.py); no identifiers
        "score_model": raw.get("score_model"), "report_id": raw.get("report_id"),
        "profile": raw.get("profile") or {}, "bureau_summary": raw.get("bureau_summary") or {}, "score_factors": raw.get("score_factors") or [],
        "personal": {"name": f"{u['first_name']} {u['last_name'] or ''}".strip(), "pan": mask_pan(pan), "dob": u["dob"], "phone": u["phone"]},
        "summary": {"active_accounts": sum(1 for a in accts if a["status"] == "Active"), "total_accounts": len(accts),
                    "total_balance": str(bal["total"]), "balance_missing": bal["missing"],
                    "total_overdue": str(od["total"]), "overdue_missing": od["missing"]},
    }


def present(rep: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise a stored report for the app: Decimal strings -> numbers (display only)."""
    out = dict(rep)
    out["accounts"] = [{**a, **{f: money_out(a.get(f)) for f in ("sanctioned", "limit", "balance", "overdue")}} for a in rep["accounts"]]
    s = dict(rep["summary"])
    s["total_balance"], s["total_overdue"] = money_out(s.get("total_balance")) or 0, money_out(s.get("total_overdue")) or 0
    out["summary"] = s
    out.pop("model", None)
    return out


def latest_report(c, uid, raw=False):
    r = c.execute("SELECT * FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if not r:
        return None
    d = json.loads(r["data"])
    if d.get("is_sandbox"):  # legacy test reports are never shown as real data
        return None
    d["pulled_at"] = r["pulled_at"]
    return d if raw else present(d)


def ensure_plan(c, uid, rep):
    tasks = []
    f = {x["key"]: x for x in rep["factors"]}
    live_codes = set()
    for e in rep["errors"]:
        code = "err_" + e["code"] + "_" + e["account_ref"]
        live_codes.add(code)
        tasks.append((code, f"Fix: {e['title']}", f"{e['lender']}: {e['detail']} Raise a dispute from the Disputes tab.", "High", 1))
    od = D(rep["summary"]["total_overdue"]) or Decimal(0)
    if od > 0:
        tasks.append(("pay_overdue", "Clear all overdue amounts", f"₹{int(od):,} is overdue across your accounts. Paying it stops new late marks.", "High", 1))
    if rep["utilisation"] is not None and rep["utilisation"] > 30:
        tasks.append(("util_30", "Bring card usage below 30%", f"You are using {rep['utilisation']}% of your card limits. Pay down balances or ask for a limit increase.", "High", 2))
    tasks += [
        ("autopay", "Turn on autopay for every EMI", "Autopay removes the risk of a missed due date.", "High", 1),
        ("reminders", "Add all loans and cards for reminders", "Fikarta reminds you 3 days before every due date.", "Medium", 1),
        ("no_new_credit", "Pause new loan or card applications", f"You have {rep['enquiries_6m']} hard {'enquiry' if rep['enquiries_6m'] == 1 else 'enquiries'} in 6 months. Space applications at least 3 months apart.", "Medium" if rep["enquiries_6m"] < 3 else "High", 2),
        ("keep_old", "Keep your oldest card open", "Account age is 15% of your score. Use the old card now and then and pay it in full.", "Medium", 4),
        ("statement_check", "Check your statement every month", "Match each card statement with your spends to catch errors early.", "Low", 6),
        ("recheck_60", "Add a fresh report after 60 days", "Lenders report monthly. Changes show 30-45 days after you act.", "Medium", 9),
        ("recheck_90", "90-day review", "Get your latest report, add it and refresh your plan.", "Medium", 13),
    ]
    if rep.get("parse_issues"):
        tasks.append(("check_parse", "Check the items we could not read", "Some fields in your report were missing or unreadable. Compare them with your report file.", "Medium", 1))
    if f["mix"]["known"] and f["mix"]["score"] < 100:
        tasks.append(("mix", "Consider a secured product later", "A small secured loan or FD-backed card improves credit mix, only if you actually need it.", "Low", 8))
    # the plan always reflects the LATEST report: open steps get the new wording and figures, open steps that no
    # longer apply are removed; completed steps are kept as history
    current = set()
    for code, title, detail, impact, week in tasks:
        current.add(code)
        c.execute("INSERT INTO plan_tasks(user_id,code,title,detail,impact,week) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(user_id,code) DO UPDATE SET title=excluded.title, detail=excluded.detail, impact=excluded.impact, week=excluded.week "
                  "WHERE plan_tasks.done=0", (uid, code, title, detail, impact, week))
    for r in c.execute("SELECT id,code FROM plan_tasks WHERE user_id=? AND done=0 AND code NOT LIKE 'err_%'", (uid,)).fetchall():
        if r["code"] not in current:
            c.execute("DELETE FROM plan_tasks WHERE id=?", (r["id"],))
    for r in c.execute("SELECT id,code FROM plan_tasks WHERE user_id=? AND code LIKE 'err_%' AND done=0", (uid,)).fetchall():
        if r["code"] not in live_codes:
            c.execute("UPDATE plan_tasks SET done=1, done_at=? WHERE id=?", (now(), r["id"]))


def save_report(c, u, raw: Dict[str, Any]) -> Dict[str, Any]:
    rep = build_report(u, raw)
    prev = latest_report(c, u["id"], raw=True)
    c.execute("INSERT INTO reports(user_id,provider,score,data,pulled_at) VALUES(?,?,?,?,?)", (u["id"], rep["source"], rep["score"], json.dumps(rep), now()))
    if rep["score"]:
        month = (rep.get("report_date") or clock.today().isoformat())[:7]
        c.execute("INSERT INTO score_history(user_id,month,score) VALUES(?,?,?) ON CONFLICT(user_id,month) DO UPDATE SET score=excluded.score", (u["id"], month, rep["score"]))
    ensure_plan(c, u["id"], rep)
    if rep["errors"]:
        notify(c, u["id"], "errors", f"{len(rep['errors'])} possible error(s) in your report", "Review them in Report → Error scan and dispute what is wrong.")
    if rep["score"]:
        msg = f"Your {rep['bureau']} score is {rep['score']} ({band(rep['score']).lower()})."
        if prev and prev.get("score"):
            d = rep["score"] - prev["score"]
            msg += f" That is {'+' if d >= 0 else ''}{d} since your last report."
        notify(c, u["id"], "score", "Report updated", msg)
    rep["pulled_at"] = now()
    return present(rep)


class AccountIn(BaseModel):
    lender: str = Field(min_length=2, max_length=80)
    type: str
    status: str = "Active"
    account_no: Optional[str] = Field(None, max_length=40)
    raw_type: Optional[str] = Field(None, max_length=80)
    raw_status: Optional[str] = Field(None, max_length=80)
    sanctioned: Optional[Decimal] = Field(None, ge=0, le=Decimal("1e10"))
    limit: Optional[Decimal] = Field(None, ge=0, le=Decimal("1e10"))
    balance: Optional[Decimal] = Field(None, ge=0, le=Decimal("1e10"))
    overdue: Optional[Decimal] = Field(None, ge=0, le=Decimal("1e10"))
    opened: Optional[str] = None
    tenure_months: Optional[int] = Field(None, ge=1, le=480)
    dpd_events: Optional[int] = Field(None, ge=0, le=36)
    closed_by_me: bool = False
    not_mine: bool = False
    field_status: Optional[Dict[str, str]] = None

    @field_validator("type")
    @classmethod
    def vtype(cls, v):
        return v if v in ACCOUNT_TYPES else "Other"

    @field_validator("status")
    @classmethod
    def vstatus(cls, v):
        return v if v in STATUSES else "Unknown"

    @field_validator("opened")
    @classmethod
    def vdate(cls, v):
        if v in (None, ""):
            return None
        dt.date.fromisoformat(v[:10])
        return v[:10]


class EnquiryIn(BaseModel):
    lender: str = Field(min_length=1, max_length=80)
    purpose: str = Field("Not stated", max_length=60)
    date: str

    @field_validator("date")
    @classmethod
    def vdate(cls, v):
        dt.date.fromisoformat(v[:10])
        return v[:10]


class ReportIn(BaseModel):
    source: str = "manual"  # crif | upload | manual
    bureau: str = Field("CIBIL", max_length=40)
    score: Optional[int] = Field(None, ge=300, le=900)
    report_date: Optional[str] = None
    accounts: List[AccountIn] = Field(default_factory=list, max_length=120)
    enquiries: List[EnquiryIn] = Field(default_factory=list, max_length=120)
    issues: List[Dict[str, str]] = Field(default_factory=list, max_length=200)

    @field_validator("report_date")
    @classmethod
    def vdate(cls, v):
        if not v:
            return clock.today().isoformat()
        d = dt.date.fromisoformat(v[:10])
        if d > clock.today():
            raise ValueError("Report date cannot be in the future")
        return v[:10]


def require_report_consent(c, u):
    """Bureau-report processing needs a current (unexpired) consent in the DPDP ledger."""
    from mobile_privacy import consent_state, consent_status  # local import: privacy module imports this one
    st = consent_status(c, u, "credit_report")
    if st == "expired":
        raise HTTPException(403, "consent_expired")
    if not consent_state(c, u)["credit_report"]:
        raise HTTPException(403, "consent_required")


@router.get("/credit/report")
def get_report(u=Depends(current_user)):
    c = db()
    try:
        rep = latest_report(c, u["id"])
        if not rep:
            raise HTTPException(404, "no_report")
        return rep
    finally:
        c.close()


@router.post("/credit/report", status_code=201)
def put_report(body: ReportIn, u=Depends(current_user)):
    c = db()
    try:
        require_report_consent(c, u)
        if body.score is None and not body.accounts:
            raise HTTPException(400, "Add your score or at least one account")
        raw = body.model_dump(mode="json")
        raw["source"] = body.source if body.source in ("crif", "upload") else "manual"
        rep = save_report(c, u, raw)
        audit(c, u["id"], "report_saved", f"{raw['source']}:{body.bureau}:{body.score}")
        c.commit()
        return rep
    finally:
        c.close()


async def _read_upload(request: Request):
    form = await request.form()
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise HTTPException(400, "Choose your credit report file")
    data = await f.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "The file is larger than 15 MB")
    return form, data


@router.post("/credit/report/parse")
async def parse_report(request: Request, u=Depends(current_user)):
    """Reads the user's own report file and returns a DRAFT. Nothing is saved until the user reviews and confirms.
    CRIF JSON is parsed by creditsage-lab's adapter; bureau PDFs by the best-effort text reader below."""
    c = db()
    try:
        require_report_consent(c, u)
    finally:
        c.close()
    form, data = await _read_upload(request)
    head = data.lstrip()[:1]
    if head in (b"{", b"["):
        import bureau_formats as bf
        try:
            draft = bf.map_any(json.loads(data.decode("utf-8-sig")))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "This JSON file is not an Experian or CRIF report we can read.")
    elif data.startswith(b"%PDF"):
        draft = _parse_pdf(data, (form.get("password") or "").strip())
    else:
        raise HTTPException(400, "Upload your report as the bureau's PDF or CRIF JSON file.")
    c = db()
    audit(c, u["id"], "report_parsed", f"{draft['bureau']}:{draft.get('parse_status', 'pdf')}")  # no report content is stored at this step
    c.commit()
    c.close()
    return draft


# ---------------- PDF text reader (best effort; the user confirms every value) -----------------
_AMT = r"(?:Rs\.?|INR|₹)?\s*([0-9][0-9,]*(?:\.\d+)?)"
_TYPE_WORDS = [("credit card", "Credit Card"), ("personal loan", "Personal Loan"), ("housing loan", "Home Loan"), ("home loan", "Home Loan"),
               ("auto loan", "Car Loan"), ("car loan", "Car Loan"), ("two-wheeler", "Two-Wheeler Loan"), ("two wheeler", "Two-Wheeler Loan"),
               ("gold loan", "Gold Loan"), ("education loan", "Education Loan"), ("business loan", "Business Loan"), ("consumer loan", "Consumer Durable Loan"),
               ("consumer durable", "Consumer Durable Loan"), ("property", "Loan Against Property"), ("overdraft", "Overdraft")]
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_INDIAN_AMOUNT = re.compile(r"^(?:\d{1,2}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def _num(s: Optional[str]) -> Optional[str]:
    """Exact amount string, or None when absent or malformed (never 0)."""
    if s is None:
        return None
    s = s.strip()
    if not _INDIAN_AMOUNT.match(s):
        return None
    return str(Decimal(s.replace(",", "")))


def _date(s: str) -> Optional[str]:
    s = s.strip()
    for pat in (r"(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4})", r"(\d{1,2})[-/. ]([A-Za-z]{3})[A-Za-z]*[-/. ,]+(\d{4})", r"(\d{4})-(\d{2})-(\d{2})"):
        m = re.search(pat, s)
        if not m:
            continue
        try:
            a, b, y = m.groups()
            if len(a) == 4:
                return dt.date(int(a), int(b), int(y)).isoformat()
            mon = int(b) if b.isdigit() else _MONTHS.get(b[:3].lower())
            return dt.date(int(y), int(mon), int(a)).isoformat()
        except Exception:
            continue
    return None


def _field(chunk: str, labels: List[str]) -> Optional[str]:
    for lab in labels:
        m = re.search(lab + r"\s*[:\-]?\s*" + _AMT, chunk, re.I)
        if m:
            return m.group(1)
    return None


def parse_report_text(text: str) -> Dict[str, Any]:
    t = re.sub(r"[ \t]+", " ", text)
    low = t.lower()
    bureau = "CIBIL" if "cibil" in low else "Experian" if "experian" in low else "CRIF High Mark" if "crif" in low else "Equifax" if "equifax" in low else "Credit bureau"
    score = None
    for pat in (r"(?:cibil|experian|crif|equifax|credit)\s*(?:transunion\s*)?(?:credit\s*)?score\s*(?:\(.*?\))?\s*[:\-]?\s*(\d{3})\b",
                r"\bscore\s*[:\-]?\s*(\d{3})\b", r"\b(\d{3})\s*(?:/\s*900|out of 900)"):
        for m in re.finditer(pat, t, re.I):
            v = int(m.group(1))
            if 300 <= v <= 900:
                score = v
                break
        if score:
            break
    rdate = None
    m = re.search(r"(?:report\s*date|date\s*of\s*report|generated\s*on|date\s*:)\s*[:\-]?\s*([0-9A-Za-z ,./-]{8,20})", t, re.I)
    if m:
        rdate = _date(m.group(1))
    starts = [mm.start() for mm in re.finditer(r"(?:member\s*name|lender\s*name|institution|subscriber\s*name|bank\s*name)\s*[:\-]", t, re.I)]
    accounts = []
    for i, s0 in enumerate(starts):
        chunk = t[s0: starts[i + 1] if i + 1 < len(starts) else s0 + 2500]
        cut = re.search(r"\benquir(?:y|ies)\b", chunk, re.I)
        if cut and cut.start() > 40:
            chunk = chunk[:cut.start()]
        lm = re.search(r"(?:member\s*name|lender\s*name|institution|subscriber\s*name|bank\s*name)\s*[:\-]\s*([A-Za-z0-9&.,'() -]{2,60}?)(?:\s{2,}|\n|account|type|$)", chunk, re.I)
        lender = (lm.group(1).strip(" .,-") if lm else "")
        lender = " ".join(w if (w.isupper() and len(w) <= 5) else w.title() for w in lender.split())
        cl = chunk.lower()
        tm = re.search(r"account\s*type\s*[:\-]?\s*([A-Za-z -]{3,40})", chunk, re.I)
        tl = tm.group(1).lower() if tm else ""
        typ = next((v for k, v in _TYPE_WORDS if k in tl), None) or next((v for k, v in _TYPE_WORDS if k in cl), None)
        if not lender or not typ:
            continue
        status = "Closed" if re.search(r"\bclosed\b|date\s*closed\s*[:\-]?\s*\d", cl) else "Active"
        if "written" in cl and "off" in cl:
            status = "Written-Off"
        elif "settled" in cl:
            status = "Settled"
        elif "suit filed" in cl:
            status = "Suit Filed"
        opened = None
        om = re.search(r"(?:date\s*opened|opened\s*on|open\s*date|date\s*of\s*opening)\s*[:\-]?\s*([0-9A-Za-z ,./-]{8,20})", chunk, re.I)
        if om:
            opened = _date(om.group(1))
        dpd_tokens = re.findall(r"(?<![\d,./:-])(\d{3}|STD|XXX|SMA|SUB|DBT|LSS)(?![\d,./:-])", chunk)
        late = sum(1 for x in dpd_tokens if (x.isdigit() and 0 < int(x) <= 900) or x in ("SMA", "SUB", "DBT", "LSS")) if dpd_tokens else None
        accounts.append({
            "lender": lender, "type": typ, "status": status,
            "sanctioned": None if typ == "Credit Card" else _num(_field(chunk, ["sanctioned amount", "sanctioned", "high credit", "loan amount", "amount financed"])),
            "limit": _num(_field(chunk, ["credit limit", "limit"])) if typ == "Credit Card" else None,
            "balance": _num(_field(chunk, ["current balance", "outstanding balance", "balance"])),
            "overdue": _num(_field(chunk, ["amount overdue", "overdue amount", "overdue"])),
            "opened": opened, "dpd_events": None if late is None else min(late, 36),
        })
    enquiries = []
    em = re.search(r"enquir(?:y|ies)(.*)$", t, re.I | re.S)
    if em:
        for mm in re.finditer(r"([A-Z][A-Za-z&.' ]{2,50}?)\s+(\d{1,2}[-/][0-9A-Za-z]{2,3}[-/]\d{4})\s+(personal loan|credit card|home loan|auto loan|consumer loan|business loan|gold loan|other)?", em.group(1), re.I):
            d = _date(mm.group(2))
            if d:
                enquiries.append({"lender": mm.group(1).strip().title(), "date": d, "purpose": (mm.group(3) or "Not stated").title()})
    return {"source": "upload", "bureau": bureau, "score": score, "report_date": rdate, "accounts": accounts, "enquiries": enquiries[:40], "issues": []}


def _parse_pdf(data: bytes, password: str) -> Dict[str, Any]:
    from pypdf import PdfReader
    import io
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            if not password or not r.decrypt(password):
                raise HTTPException(422, "password_required")
        text = "\n".join((p.extract_text() or "") for p in r.pages[:60])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "We could not read this PDF. Try downloading it again from the bureau website.")
    if len(text.strip()) < 50:
        raise HTTPException(400, "This PDF has no readable text (it may be a scanned image). Please enter your details manually.")
    draft = parse_report_text(text)
    draft["found"] = {"score": draft["score"] is not None, "accounts": len(draft["accounts"]), "enquiries": len(draft["enquiries"])}
    return draft


@router.post("/credit/refresh")
def refresh_report(u=Depends(current_user)):
    if BUREAU_PROVIDER in ("", "none", "sandbox"):
        raise HTTPException(400, "Live bureau refresh is not connected yet. Download your latest report and add it here.")
    raise HTTPException(501, f"Bureau provider '{BUREAU_PROVIDER}' is configured but its connector is not installed")


@router.get("/credit/history")
def history(u=Depends(current_user)):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT month,score FROM score_history WHERE user_id=? ORDER BY month", (u["id"],))]
    c.close()
    return rows


@router.get("/credit/meta")
def credit_meta():
    return {"account_types": ACCOUNT_TYPES, "statuses": STATUSES, "bands": [{"min": lo, "name": n} for lo, n in BANDS]}


class SimIn(BaseModel):
    pay_card_balance_pct: float = Field(0, ge=0, le=100)
    clear_overdue: bool = False
    close_oldest_card: bool = False
    new_enquiries: int = Field(0, ge=0, le=10)
    miss_payment: bool = False
    settle_loan: bool = False
    fix_errors: bool = False


@router.post("/credit/simulate")
def simulate(body: SimIn, u=Depends(current_user)):
    """Direction of change only. No predicted score is returned: Fikarta has no validated scoring model, and the design
    kit's recommendation-safety rule says predicted points must not be shown without one."""
    c = db()
    rep = latest_report(c, u["id"], raw=True)
    c.close()
    if not rep:
        raise HTTPException(404, "Add your credit report first")
    accts = [dict(a) for a in rep["accounts"]]
    if body.fix_errors:
        bad = {e["account_ref"] for e in rep["errors"] if e["code"] in ("duplicate", "unknown")}
        accts = [a for a in accts if a["ref"] not in bad]
        for a in accts:
            if a.get("closed_by_me"):
                a["status"] = "Closed"
    pay = Decimal(str(body.pay_card_balance_pct)) / Decimal(100)
    for a in accts:
        b = D(a.get("balance"))
        if a["type"] in CARD_TYPES and a["status"] == "Active" and b is not None:
            a["balance"] = str(b * (Decimal(1) - pay))
        if body.clear_overdue and D(a.get("overdue")) is not None:
            a["overdue"] = "0"
            if a.get("dpd_events"):
                a["dpd_events"] = int(a["dpd_events"]) - 1
    if body.close_oldest_card:
        cards = [a for a in accts if a["type"] in CARD_TYPES and a["status"] == "Active"]
        if cards:
            accts.remove(min(cards, key=lambda a: a.get("opened") or "9999"))
    if body.miss_payment:
        live = [a for a in accts if a["status"] == "Active"]
        if live:
            live[0]["dpd_events"] = int(live[0].get("dpd_events") or 0) + 2
    if body.settle_loan:
        loans = [a for a in accts if a["type"] not in CARD_TYPES and a["status"] == "Active"]
        if loans:
            loans[0]["status"] = "Settled"
    base = compute(rep["accounts"], rep["enquiries_6m"])
    new = compute(accts, rep["enquiries_6m"] + body.new_enquiries)
    changed = [f["name"] for f0, f in zip(base["factors"], new["factors"]) if f0["score"] != f["score"] and f["known"]]
    if base["model"] is None or new["model"] is None:
        return {"direction": "unknown", "strength": None, "changed_factors": [],
                "message": "Your report does not have enough data to judge this change."}
    delta = new["model"] - base["model"]
    direction = "up" if delta > 0.005 else "down" if delta < -0.005 else "none"
    strength = None if direction == "none" else ("large" if abs(delta) >= 0.08 else "moderate" if abs(delta) >= 0.03 else "small")
    msg = {"up": "These changes are likely to help your score.", "down": "These changes are likely to hurt your score.",
           "none": "These changes are unlikely to move your score much."}[direction]
    return {"direction": direction, "strength": strength, "changed_factors": changed, "message": msg,
            "note": "Direction only. The exact change depends on your bureau's scoring model and shows 30-45 days after lenders report."}


# --------------------------------------------------------------------------- plan
@router.get("/plan")
def get_plan(u=Depends(current_user)):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT * FROM plan_tasks WHERE user_id=? ORDER BY done, week, CASE impact WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END", (u["id"],))]
    created = c.execute("SELECT MIN(pulled_at) FROM reports WHERE user_id=?", (u["id"],)).fetchone()[0]
    c.close()
    day = 0
    if created:
        day = max(0, (clock.utcnow() - dt.datetime.fromisoformat(created.rstrip("Z"))).days)
    done = sum(r["done"] for r in rows)
    return {"day": min(day + 1, 90), "total_days": 90, "done": done, "total": len(rows), "tasks": rows}


class TaskIn(BaseModel):
    done: bool


@router.patch("/plan/tasks/{tid}")
def set_task(tid: int, body: TaskIn, u=Depends(current_user)):
    c = db()
    r = c.execute("UPDATE plan_tasks SET done=?, done_at=? WHERE id=? AND user_id=?", (int(body.done), now() if body.done else None, tid, u["id"]))
    if not r.rowcount:
        c.close()
        raise HTTPException(404, "Task not found")
    if body.done:
        c.execute("INSERT INTO rewards(user_id,points,reason,at) VALUES(?,?,?,?)", (u["id"], 10, "Completed a plan task", now()))
    c.commit()
    c.close()
    return {"ok": True}


# --------------------------------------------------------------------------- disputes & letters
ISSUES = {
    "duplicate": "the same account is reported more than once",
    "closed_open": "the account was closed but is still reported as active",
    "unknown": "I do not recognise this account and did not apply for it",
    "small_overdue": "the overdue amount reported is incorrect",
    "settled": "the account status should be Closed, not Settled, as the dues have been paid in full",
    "wrong_status": "the account status reported is incorrect",
    "wrong_dpd": "late payments are reported for months when I paid on time",
    "dormant": "the account tenure is complete and the balance is nil, but it is still reported active",
    "other": "the information reported is incorrect",
}


def dispute_letter(u, lender, account_ref, issue, details, bureau):
    name = f"{u['first_name']} {u['last_name'] or ''}".strip()
    return (
        f"To: The Dispute Resolution Team, {bureau}\nCc: Grievance Redressal Officer, {lender}\n\n"
        f"Subject: Request to correct my credit report — account {account_ref}\n\n"
        f"Dear Sir/Madam,\n\nI, {name} (PAN {mask_pan(dec(u['pan_enc'])) or 'on file'}), am writing to dispute an entry in my credit report. "
        f"For the account {account_ref} reported by {lender}, {ISSUES.get(issue, ISSUES['other'])}.\n\n"
        + (f"Details: {details}\n\n" if details else "")
        + "Under the Credit Information Companies (Regulation) Act, 2005 and RBI directions, I request you to verify this with the lender "
        "and correct or remove the entry within 30 days. Please share the outcome in writing.\n\n"
        f"Regards,\n{name}\n{u['email']}" + (f"\n{u['phone']}" if u["phone"] else "")
    )


class DisputeIn(BaseModel):
    account_ref: str = Field(min_length=2, max_length=40)
    lender: str = Field(min_length=2, max_length=80)
    issue: str
    details: str = Field("", max_length=1500)
    bureau: str = Field("Credit bureau", max_length=40)


@router.get("/disputes")
def list_disputes(u=Depends(current_user)):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT * FROM disputes WHERE user_id=? ORDER BY id DESC", (u["id"],))]
    c.close()
    return rows


@router.post("/disputes", status_code=201)
def create_dispute(body: DisputeIn, u=Depends(current_user)):
    if body.issue not in ISSUES:
        raise HTTPException(400, "Unknown issue type")
    c = db()
    letter = dispute_letter(u, body.lender, body.account_ref, body.issue, body.details, body.bureau)
    cur = c.execute("INSERT INTO disputes(user_id,account_ref,lender,issue,details,bureau,letter,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (u["id"], body.account_ref, body.lender, body.issue, body.details, body.bureau, letter, "draft", now(), now()))
    c.execute("INSERT INTO dispute_events(dispute_id,kind,note,at) VALUES(?,?,?,?)", (cur.lastrowid, "drafted", "Fikarta AI drafted your dispute letter. Review it before filing.", now()))
    c.commit()
    d = dict(c.execute("SELECT * FROM disputes WHERE id=?", (cur.lastrowid,)).fetchone())
    c.close()
    return d


@router.get("/disputes/{did}")
def get_dispute(did: int, u=Depends(current_user)):
    c = db()
    d = c.execute("SELECT * FROM disputes WHERE id=? AND user_id=?", (did, u["id"])).fetchone()
    if not d:
        c.close()
        raise HTTPException(404, "Dispute not found")
    ev = [dict(r) for r in c.execute("SELECT * FROM dispute_events WHERE dispute_id=? ORDER BY id", (did,))]
    c.close()
    return {**dict(d), "events": ev}


class DisputeAction(BaseModel):
    action: str  # file | update_letter | resolved | rejected | withdraw | note
    letter: Optional[str] = Field(None, max_length=6000)
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/disputes/{did}/action")
def dispute_action(did: int, body: DisputeAction, u=Depends(current_user)):
    c = db()
    d = c.execute("SELECT * FROM disputes WHERE id=? AND user_id=?", (did, u["id"])).fetchone()
    if not d:
        c.close()
        raise HTTPException(404, "Dispute not found")
    a = body.action
    if a == "update_letter" and body.letter and d["status"] == "draft":
        c.execute("UPDATE disputes SET letter=?, updated_at=? WHERE id=?", (body.letter, now(), did))
        c.execute("INSERT INTO dispute_events(dispute_id,kind,note,at) VALUES(?,?,?,?)", (did, "edited", "You edited the letter.", now()))
    elif a == "file" and d["status"] == "draft":
        due = (clock.today() + dt.timedelta(days=30)).isoformat()
        c.execute("UPDATE disputes SET status='filed', due_by=?, updated_at=? WHERE id=?", (due, now(), did))
        c.execute("INSERT INTO dispute_events(dispute_id,kind,note,at) VALUES(?,?,?,?)", (did, "filed", f"You approved and filed the dispute. The bureau must respond by {due}.", now()))
        notify(c, u["id"], "dispute", "Dispute filed", f"Your dispute for {d['lender']} ({d['account_ref']}) is filed. Expected answer by {due}.")
    elif a in ("resolved", "rejected") and d["status"] in ("filed", "in_review"):
        c.execute("UPDATE disputes SET status=?, updated_at=? WHERE id=?", (a, now(), did))
        c.execute("INSERT INTO dispute_events(dispute_id,kind,note,at) VALUES(?,?,?,?)", (did, a, body.note or ("Bureau confirmed the correction." if a == "resolved" else "Bureau did not accept the correction."), now()))
        if a == "resolved":
            c.execute("UPDATE plan_tasks SET done=1, done_at=? WHERE user_id=? AND code LIKE ?", (now(), u["id"], f"err_%_{d['account_ref']}"))
    elif a == "withdraw" and d["status"] in ("draft", "filed"):
        c.execute("UPDATE disputes SET status='withdrawn', updated_at=? WHERE id=?", (now(), did))
        c.execute("INSERT INTO dispute_events(dispute_id,kind,note,at) VALUES(?,?,?,?)", (did, "withdrawn", "You withdrew this dispute.", now()))
    elif a == "note" and body.note:
        if d["status"] == "filed":
            c.execute("UPDATE disputes SET status='in_review', updated_at=? WHERE id=?", (now(), did))
        c.execute("INSERT INTO dispute_events(dispute_id,kind,note,at) VALUES(?,?,?,?)", (did, "reply", body.note, now()))
    else:
        c.close()
        raise HTTPException(400, "This action is not allowed for the current status")
    c.commit()
    c.close()
    return get_dispute(did, u)


LETTERS = {
    "settlement": ("Request for one-time settlement", "Due to {reason}, I am unable to continue the EMIs on loan account {acct}. I request a one-time settlement of ₹{amount}. On acceptance, please confirm in writing that the account will be reported as closed and share the NOC."),
    "noc": ("Request for No Objection Certificate", "I have repaid loan account {acct} in full. Please issue the No Objection Certificate and update the bureau status to Closed within 30 days."),
    "hardship": ("Request for EMI restructuring due to hardship", "Due to {reason}, my income has reduced. For loan account {acct}, I request a temporary reduction of EMI or a moratorium so that I can keep paying without default."),
    "goodwill": ("Goodwill adjustment request", "My account {acct} shows a late payment caused by {reason}. I have otherwise paid on time. As a goodwill gesture, please remove the late mark reported to the bureau."),
    "harassment": ("Complaint against recovery agent conduct", "Recovery agents for loan account {acct} have {reason}. This breaches the RBI Fair Practices Code. Please stop such calls immediately and confirm the action taken."),
}


class LetterIn(BaseModel):
    kind: str
    lender: str = Field(min_length=2, max_length=80)
    account_no: str = Field(min_length=2, max_length=40)
    reason: str = Field("", max_length=500)
    amount: Optional[float] = Field(None, ge=0)


@router.post("/letters", status_code=201)
def make_letter(body: LetterIn, u=Depends(current_user)):
    if body.kind not in LETTERS:
        raise HTTPException(400, "Unknown letter type")
    subj, tmpl = LETTERS[body.kind]
    name = f"{u['first_name']} {u['last_name'] or ''}".strip()
    text = (f"To: Grievance Redressal Officer, {body.lender}\n\nSubject: {subj} — {body.account_no}\n\nDear Sir/Madam,\n\n"
            + tmpl.format(reason=body.reason or "unforeseen circumstances", acct=body.account_no, amount=f"{int(body.amount or 0):,}")
            + f"\n\nIf this is not resolved within 30 days, I will escalate to the RBI Ombudsman (cms.rbi.org.in).\n\nRegards,\n{name}\n{u['email']}"
            + (f"\n{u['phone']}" if u["phone"] else ""))
    c = db()
    cur = c.execute("INSERT INTO letters(user_id,kind,lender,body,created_at) VALUES(?,?,?,?,?)", (u["id"], body.kind, body.lender, text, now()))
    c.commit()
    r = dict(c.execute("SELECT * FROM letters WHERE id=?", (cur.lastrowid,)).fetchone())
    c.close()
    return r


@router.get("/letters")
def list_letters(u=Depends(current_user)):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT * FROM letters WHERE user_id=? ORDER BY id DESC", (u["id"],))]
    c.close()
    return rows


@router.delete("/letters/{lid}")
def del_letter(lid: int, u=Depends(current_user)):
    c = db()
    c.execute("DELETE FROM letters WHERE id=? AND user_id=?", (lid, u["id"]))
    c.commit()
    c.close()
    return {"ok": True}


# --------------------------------------------------------------------------- Fikarta AI chat
# Rules from creditsage-lab: the model explains, it never produces figures. Every number it may mention is a typed
# fact taken from the user's saved report (no name, PAN, phone or email). There is no keyword-matching fallback:
# if the model is unavailable, times out or declines, the chat fails closed with a clear "assistant unavailable".
from ai_assistant import AssistantUnavailable  # noqa: E402  (retrieval-augmented assistant; fails closed)


def _account_facts(accts: List[dict]) -> Dict[str, Any]:
    """Accounts for the AI prompt, sized to fit: every open account in full; closed accounts only when they carry a
    problem (late months, overdue, settled, written off). Each account's full detail is also in the retrieved chunks."""
    def full(a):
        return {k: v for k, v in {
            "Lender": a["lender"], "Type": a["type"], "Status": a["status"], "Opened": a.get("opened"), "Closed": a.get("closed_date"),
            "Sanctioned (rupees)": a.get("sanctioned"), "Credit limit (rupees)": a.get("limit"), "Balance (rupees)": a.get("balance"),
            "Overdue (rupees)": a.get("overdue"), "Late months in last 24": a.get("dpd_events"), "EMI (rupees)": a.get("emi"),
            "Interest rate (percent)": a.get("interest_rate"), "Last payment date": a.get("last_payment_date"),
            "Tenure (months)": a.get("tenure_months"), "Written off (rupees)": a.get("written_off_amount"),
            "Settled for (rupees)": a.get("settlement_amount"), "Suit filed": a.get("suit_filed"),
            "Last 3 months (newest first)": "; ".join(
                f"{h['month']}: " + ", ".join(x for x in (
                    f"paid {h['paid']}" if h.get("paid") not in (None, "") else "paid NA",
                    f"balance {h['balance']}" if h.get("balance") not in (None, "") else None,
                    f"{h['dpd']} days late" if h.get("dpd") is not None else None) if x)
                for h in (a.get("payment_history") or [])[:3]) or None,
        }.items() if v not in (None, "")}
    live = [a for a in accts if a["status"] not in ("Closed",)]
    closed = [a for a in accts if a["status"] == "Closed"]
    flagged = [a for a in closed if (a.get("dpd_events") or 0) > 0 or (D(a.get("overdue")) or 0) > 0
               or (D(a.get("written_off_amount")) or 0) > 0 or (D(a.get("settlement_amount")) or 0) > 0]
    dated = sorted([a for a in accts if a.get("opened")], key=lambda a: a["opened"])
    emis = [D(a.get("emi")) for a in live if a["type"] != "Credit Card"]
    known = [e for e in emis if e is not None]
    out: Dict[str, Any] = {
        "Open accounts (all details)": [full(a) for a in live],
        "Closed accounts": len(closed),
        "Closed accounts with late payments, overdue, settlement or write-off": [full(a) for a in flagged],
        "Closed accounts with a clean record": len(closed) - len(flagged),
        "Total EMI of open loans where the report shows the EMI (rupees, computed exactly)": str(sum(known)) if known else None,
        "Open loans whose EMI the report does not show": len(emis) - len(known),
    }
    cards = [a for a in live if a["type"] == "Credit Card"]
    if cards:
        lim = [D(a.get("limit")) for a in cards]
        bal = [D(a.get("balance")) for a in cards]
        out["Open credit cards"] = len(cards)
        out["Total credit limit of open cards where the limit is shown (rupees, computed exactly)"] = str(sum(x for x in lim if x is not None))
        out["Open cards whose limit the report does not show"] = sum(1 for x in lim if x is None)
        out["Total balance on open cards (rupees, computed exactly; negative balances count as 0)"] = str(sum(max(x, 0) for x in bal if x is not None))
        paid_rows = []
        for a in cards:
            months = [h for h in (a.get("payment_history") or [])[:3] if D(h.get("paid")) is not None]
            if months:
                paid_rows.append({"Card": f"{a['lender']} credit card", "Paid in last 3 reported months (rupees, computed exactly)": str(sum(D(h["paid"]) for h in months)),
                                  "Months": ", ".join(f"{h['month']} {h['paid']}" for h in months)})
        out["Credit card payments, last 3 reported months (cards only, never loans)"] = paid_rows or "The report does not show amounts paid on any card"
        if paid_rows:
            out["Total paid on all cards in the last 3 reported months (rupees, computed exactly)"] = str(sum(D(r["Paid in last 3 reported months (rupees, computed exactly)"]) for r in paid_rows))
    if dated:
        out["Oldest account"] = f"{dated[0]['lender']} {dated[0]['type']}, opened {dated[0]['opened']}, {dated[0]['status']}"
        out["Newest account"] = f"{dated[-1]['lender']} {dated[-1]['type']}, opened {dated[-1]['opened']}, {dated[-1]['status']}"
    return out


def report_facts(rep: Optional[dict]) -> Dict[str, Any]:
    """Typed facts from the user's saved report, labelled in plain English (no PAN, phone, email or address)."""
    if not rep:
        return {"Has a credit report on Fikarta": False}
    s = rep["summary"]
    return {
        "Has a credit report on Fikarta": True,
        **({"IMPORTANT": "This report is TEST DATA from the credit bureau's sandbox. It is a fixed sample, not the user's real credit report. "
                         "Tell the user this clearly whenever you mention their report."} if rep.get("test_data") else {}),
        "Bureau": rep.get("bureau"),
        "Report date": rep.get("report_date"),
        "Credit score": rep.get("score"),
        "Score band": rep.get("band"),
        "Band scale": "300 to 900. Excellent 740+, Good 670-739, Fair 580-669, Poor below 580",
        "Credit card usage (percent of limits)": rep.get("utilisation"),
        "Hard enquiries in the last 6 months": rep.get("enquiries_6m"),
        "Active accounts": s["active_accounts"],
        "Total accounts": s["total_accounts"],
        "Total overdue (rupees)": s["total_overdue"],
        "Accounts whose overdue amount is not in the report": s.get("overdue_missing", 0),
        "Score factors": [{"Factor": x["name"], "Share of the scoring model": f"{x['weight']}% for this whole factor (not a cost per item)",
                           "Grade": x["grade"], "What the report shows": x["value"], "Fikarta guidance": x["tip"]} for x in rep["factors"]],
        "Possible errors found by Fikarta's check": [{"Issue": e["title"], "Lender": e["lender"], "Severity": e["severity"]} for e in rep["errors"]],
        **_account_facts(rep["accounts"]),
        **({"Bureau's own totals": {k.replace("_", " "): v for k, v in rep["bureau_summary"].items() if v is not None}} if rep.get("bureau_summary") else {}),
        **({"Bureau's score reasons": rep["score_factors"]} if rep.get("score_factors") else {}),
        **({"Profile as reported by the bureau (no identifiers)": {k.replace("_", " "): v for k, v in rep["profile"].items() if v is not None}}
           if any(v is not None for v in (rep.get("profile") or {}).values()) else {}),
    }


def ai_reply(history: List[dict], text: str, rep: Optional[dict], lang: str) -> Dict[str, Any]:
    """Kept for callers without a user store (tests); the chat endpoint uses the full pipeline below."""
    import ai_assistant
    return ai_assistant.answer(history, text, report_facts(rep), lang)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


@router.get("/chat")
def chat_history(u=Depends(current_user)):
    c = db()
    try:
        scol = ", suggestions" if "suggestions" in _cols(c, "chat_messages") else ""
        rows = [dict(r) for r in c.execute(f"SELECT role,content,at,citations{scol} FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 100", (u["id"],))]
        for r in rows:
            r["citations"] = json.loads(r["citations"]) if r.get("citations") else []
            r["suggestions"] = json.loads(r["suggestions"]) if r.get("suggestions") else []
        # the in-chat fetch form stays available (on its last prompt) until the user has a report
        if rows and rows[0]["role"] == "assistant" and rows[0]["content"].startswith(_FETCH_PROMPT) and latest_report(c, u["id"]) is None:
            _, action = _fetch_action(c, u)
            if action:
                rows[0]["action"] = action
        return list(reversed(rows))
    finally:
        c.close()


def _remember(uid: int, question: str, reply: str):
    """Runs after the response is sent: from the user's own words, store memories and structured profile details."""
    import ai_assistant
    import user_profile
    import user_rag
    try:
        got = ai_assistant.extract_user_details(question)
        if not got["memories"] and not got["profile"]:
            return
        c = db()
        try:
            cr = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='ai_chat' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
            if cr and cr["granted"]:  # consent may have been withdrawn while the reply was being written
                ts = now()
                user_rag.save_memories(c, uid, got["memories"], ts)
                changed = user_profile.upsert(c, uid, got["profile"], "chat", ts)
                if changed:
                    audit(c, uid, "profile_from_chat", ",".join(changed))
                c.commit()
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001  (memory is optional; never break chat)
        logging.getLogger("fikarta.ai").info("memory step skipped: %s", type(e).__name__)


_WRITE_ASK = re.compile(r"\b(write|draft|compose|prepare|send|mail|email|letter|likh|likho|likhna|likhdo|bhej|bhejo|bhejna|bhejdo|"
                        r"application)\b|लिख|भेज|पत्र|मेल", re.I)

_SCORE_ASK = re.compile(
    r"\b(check|fetch|get|pull|show|see|know|tell me)\b[^.?!]{0,15}\bmy (?:credit |cibil |crif )?(?:score|report)\b"
    r"|\bwhat(?:'s| is)? my (?:credit |cibil |crif )?score\b|^\s*(?:my )?(?:credit |cibil )?score\s*\??\s*$", re.I)


_FETCH_PROMPT = "I don't have your credit report yet."


def _fetch_action(c, u):
    """The user asked for their score but has no report: Fikarta AI shows a small in-chat form for exactly what is missing.
    The form posts straight to the profile and bureau-fetch endpoints; nothing typed in it is sent to the AI model."""
    import bureau_fetch as bf
    if not bf.sp.configured():
        return ("I can't fetch credit reports right now. Please try again a little later.", None)
    needs = []
    if not u["pan_enc"]:
        needs.append("pan")
    if not (u["first_name"] or "").strip():
        needs.append("name")
    needs += ["mobile", "consent"]
    last = bf._last_success(c, u["id"])
    if last and clock.epoch() - last["epoch"] < bf.cooldown_days() * 86400:
        return ("Your report was fetched recently. Pull down on Home to refresh it, or try again tomorrow.", None)
    reply = f"{_FETCH_PROMPT} Fill in the details below and I'll fetch it securely from the credit bureau."
    return reply, {"type": "fetch_report", "needs": needs,
                   "prefill": {"mobile": u["phone"] or "", "first_name": u["first_name"] or "", "last_name": u["last_name"] or "",
                               "pan_masked": mask_pan(dec(u["pan_enc"])) if u["pan_enc"] else None},
                   "consent_text": bf.CONSENT_TEXT}


@router.post("/chat")
def chat(body: ChatIn, bg: BackgroundTasks, u=Depends(current_user)):
    import ai_assistant
    import user_rag
    c = db()
    try:
        cr = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='ai_chat' ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        if not (cr and cr["granted"]):
            raise HTTPException(403, "ai_consent_required")
        import report_history
        hist = [{"role": r["role"], "content": user_rag.scrub(r["content"])}
                for r in c.execute("SELECT role,content FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 10", (u["id"],))][::-1]
        rep = latest_report(c, u["id"], raw=True)
        lang = u["language"] or "en"
        question = user_rag.scrub(body.message)  # identifiers never leave Fikarta
        # fixed replies that never reach the model: personal-ID questions, internals, other people's data
        fixed = ai_assistant.guard(body.message, lang)
        action = None
        if not fixed and rep is None and _SCORE_ASK.search(body.message or ""):
            fixed, action = _fetch_action(c, u)
        if fixed:
            sugg = [] if action else ai_assistant.DEFAULT_RELATED[ai_assistant.reply_language(body.message, lang)]
            c.execute("INSERT INTO chat_messages(user_id,role,content,at) VALUES(?,?,?,?)", (u["id"], "user", question, now()))
            c.execute("INSERT INTO chat_messages(user_id,role,content,at,citations,suggestions) VALUES(?,?,?,?,?,?)",
                      (u["id"], "assistant", fixed, now(), "[]", json.dumps(sugg, ensure_ascii=False)))
            audit(c, u["id"], "ai_answer", "fetch_form" if action else "guard")
            c.commit()
            out = {"reply": fixed, "citations": [], "suggestions": sugg}
            if action:
                out["action"] = action
            return out
        facts = report_facts(rep)
        nm = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip()
        if nm:
            facts = {"Account holder's name (the user you are talking to)": nm, **facts}
        ch = report_history.change_facts(report_history.snapshots(c, u["id"], limit=2))
        if ch:
            facts["Changes since the previous report (computed exactly by Fikarta)"] = ch
        import user_profile
        told = user_profile.facts_for_ai(c, u["id"])
        if told:
            facts["Details the user told Fikarta (self-reported, not verified)"] = told
        # 1. keep the user's knowledge store in step with their data
        user_rag.sync_user_docs(c, u["id"], rep, now())
        c.commit()
        if ai_assistant.provider() is None:
            raise HTTPException(503, "assistant_unavailable")
        # 2-3. standalone query + keywords, then retrieval over the user's own documents and memories
        rq = ai_assistant.rewrite_query(hist, question)
        queries = [question, rq["standalone"], " ".join(rq["keywords"])]
        ctx = user_rag.retrieve(c, u["id"], queries, k=6)
        # 4. grounded answer
        try:
            res = ai_assistant.answer(hist, question, facts, lang, user_context=ctx, queries=queries)
        except AssistantUnavailable:
            user_rag.trace(c, u["id"], now(), query=body.message, rewritten=rq["rewritten"], retrieved=ctx, sources=[],
                           provider=ai_assistant.provider() or "", model="", latency_ms=0, outcome="unavailable")
            c.commit()
            raise HTTPException(503, "assistant_unavailable")
        # stored masked too: a PAN or phone typed into chat is never kept in the chat log
        c.execute("INSERT INTO chat_messages(user_id,role,content,at) VALUES(?,?,?,?)", (u["id"], "user", question, now()))
        c.execute("INSERT INTO chat_messages(user_id,role,content,at,citations,suggestions) VALUES(?,?,?,?,?,?)",
                  (u["id"], "assistant", res["reply"], now(), json.dumps(res["citations"]), json.dumps(res.get("suggestions") or [], ensure_ascii=False)))
        # 6. trace: what was retrieved and how long it took (document keys and scores, never their text)
        user_rag.trace(c, u["id"], now(), query=body.message, rewritten=rq["rewritten"], retrieved=ctx, sources=res.get("sources", []),
                       provider=res["provider"], model=res.get("model", ""), latency_ms=res.get("latency_ms", 0),
                       outcome="answered" if res.get("figure_check", "ok") == "ok" else f"answered_{res['figure_check']}")
        audit(c, u["id"], "ai_answer", res["provider"])
        c.commit()
        # 5. long-term memory, after the response
        bg.add_task(_remember, u["id"], question, res["reply"])
        out = {"reply": res["reply"], "citations": res["citations"], "suggestions": res.get("suggestions") or []}
        if res.get("draft_for"):
            if _WRITE_ASK.search(body.message or ""):
                import mail_assist
                try:
                    out["action"] = {"type": "draft", "draft": mail_assist.create_draft(c, u, res["draft_for"], question)}
                    c.commit()
                except Exception as e:  # noqa: BLE001  (drafting is optional; the offer button still works)
                    logging.getLogger("fikarta.mail").info("chat draft skipped: %s", type(e).__name__)
                    out["action"] = {"type": "offer_draft", "lender": res["draft_for"], "problem": question}
            else:
                out["action"] = {"type": "offer_draft", "lender": res["draft_for"], "problem": question}
        return out
    finally:
        c.close()


@router.get("/credit/changes")
def credit_changes(u=Depends(current_user)):
    """Month-to-month changes between the user's saved reports, computed exactly (no AI)."""
    import report_history
    c = db()
    try:
        snaps = report_history.snapshots(c, u["id"])
        tl = report_history.timeline(snaps)
        hist = {k: v for k, v in report_history.account_histories(snaps).items() if len(v["points"]) >= 2}
        return {"reports": len(snaps), "latest": tl[0] if tl else None, "timeline": tl[:12],
                "scores": [{"date": report_history._label(s), "score": s.get("score")} for s in reversed(snaps)],
                "accounts": list(hist.values())}
    finally:
        c.close()


class ProfileFactIn(BaseModel):
    value: str = Field(min_length=1, max_length=60)


@router.get("/ai/profile")
def ai_profile(u=Depends(current_user)):
    import user_profile
    c = db()
    try:
        return user_profile.view(c, u["id"])
    finally:
        c.close()


@router.put("/ai/profile/{key}")
def ai_profile_set(key: str, body: ProfileFactIn, u=Depends(current_user)):
    import user_profile
    if key not in user_profile.FIELDS:
        raise HTTPException(404, "Unknown detail")
    ok, _ = user_profile.validate(key, body.value)
    if not ok:
        raise HTTPException(400, "Please enter a valid value")
    c = db()
    try:
        user_profile.upsert(c, u["id"], {key: body.value}, "user_edit", now())
        audit(c, u["id"], "profile_edit", key)
        c.commit()
        return user_profile.view(c, u["id"])
    finally:
        c.close()


@router.delete("/ai/profile/{key}")
def ai_profile_delete(key: str, u=Depends(current_user)):
    import user_profile
    c = db()
    try:
        user_profile.remove(c, u["id"], key)
        audit(c, u["id"], "profile_delete", key)
        c.commit()
        return user_profile.view(c, u["id"])
    finally:
        c.close()


@router.get("/ai/memories")
def list_memories(u=Depends(current_user)):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT id, kind, text, created_at, updated_at FROM user_memories WHERE user_id=? ORDER BY updated_at DESC", (u["id"],))]
    c.close()
    return rows


@router.delete("/ai/memories/{mid}")
def delete_memory(mid: int, u=Depends(current_user)):
    c = db()
    n = c.execute("DELETE FROM user_memories WHERE id=? AND user_id=?", (mid, u["id"])).rowcount
    audit(c, u["id"], "memory_deleted", str(mid))
    c.commit()
    c.close()
    if not n:
        raise HTTPException(404, "Not found")
    return {"ok": True}


@router.delete("/ai/memories")
def delete_all_memories(u=Depends(current_user)):
    c = db()
    c.execute("DELETE FROM user_memories WHERE user_id=?", (u["id"],))
    audit(c, u["id"], "memories_cleared")
    c.commit()
    c.close()
    return {"ok": True}


@router.delete("/chat")
def clear_chat(u=Depends(current_user)):
    c = db()
    c.execute("DELETE FROM chat_messages WHERE user_id=?", (u["id"],))
    c.commit()
    c.close()
    return {"ok": True}


# --------------------------------------------------------------------------- loans, cards, payments, reminders
class LoanIn(BaseModel):
    lender: str = Field(min_length=2, max_length=80)
    kind: str = Field("Personal Loan", max_length=40)
    account_no: str = Field(min_length=4, max_length=30)
    principal: float = Field(gt=0, le=1e9)
    outstanding: float = Field(ge=0, le=1e9)
    emi: float = Field(gt=0, le=1e8)
    rate: float = Field(0, ge=0, le=60)
    due_day: int = Field(ge=1, le=28)
    tenure_months: int = Field(12, ge=1, le=480)
    autopay: bool = False


class CardIn(BaseModel):
    issuer: str = Field(min_length=2, max_length=80)
    name: str = Field("", max_length=60)
    last4: str = Field(pattern=r"^\d{4}$")
    credit_limit: float = Field(gt=0, le=1e8)
    balance: float = Field(ge=0, le=1e8)
    min_due: float = Field(0, ge=0, le=1e8)
    due_day: int = Field(ge=1, le=28)


def _need_reminders_consent(c, u):
    r = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='reminders' ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
    if not (r and r["granted"]):
        c.close()
        raise HTTPException(403, "reminders_consent_required")


def _mask_acct(a: str) -> str:
    return ("XXXX" + a[-4:]) if len(a) > 4 else a


def _crud_list(table, uid):
    c = db()
    rows = [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY id DESC", (uid,))]
    c.close()
    return rows


@router.get("/loans")
def loans_list(u=Depends(current_user)):
    rows = _crud_list("loans", u["id"])
    for r in rows:
        r["account_masked"] = _mask_acct(r["account_no"])
    return rows


@router.post("/loans", status_code=201)
def loans_add(body: LoanIn, u=Depends(current_user)):
    c = db()
    _need_reminders_consent(c, u)
    cur = c.execute("INSERT INTO loans(user_id,lender,kind,account_no,principal,outstanding,emi,rate,due_day,tenure_months,autopay,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (u["id"], body.lender, body.kind, body.account_no, body.principal, body.outstanding, body.emi, body.rate, body.due_day, body.tenure_months, int(body.autopay), now()))
    c.execute("UPDATE plan_tasks SET done=1, done_at=? WHERE user_id=? AND code='reminders' AND done=0", (now(), u["id"]))
    if body.autopay:
        c.execute("UPDATE plan_tasks SET done=1, done_at=? WHERE user_id=? AND code='autopay' AND done=0", (now(), u["id"]))
    c.commit()
    r = dict(c.execute("SELECT * FROM loans WHERE id=?", (cur.lastrowid,)).fetchone())
    c.close()
    return r


@router.put("/loans/{lid}")
def loans_update(lid: int, body: LoanIn, u=Depends(current_user)):
    c = db()
    r = c.execute("UPDATE loans SET lender=?,kind=?,account_no=?,principal=?,outstanding=?,emi=?,rate=?,due_day=?,tenure_months=?,autopay=? WHERE id=? AND user_id=?",
                  (body.lender, body.kind, body.account_no, body.principal, body.outstanding, body.emi, body.rate, body.due_day, body.tenure_months, int(body.autopay), lid, u["id"]))
    c.commit()
    c.close()
    if not r.rowcount:
        raise HTTPException(404, "Loan not found")
    return {"ok": True}


@router.delete("/loans/{lid}")
def loans_del(lid: int, u=Depends(current_user)):
    c = db()
    c.execute("DELETE FROM loans WHERE id=? AND user_id=?", (lid, u["id"]))
    c.commit()
    c.close()
    return {"ok": True}


@router.get("/cards")
def cards_list(u=Depends(current_user)):
    rows = _crud_list("cards", u["id"])
    for r in rows:
        r["utilisation"] = round(100 * r["balance"] / r["credit_limit"], 1) if r["credit_limit"] else 0
    return rows


@router.post("/cards", status_code=201)
def cards_add(body: CardIn, u=Depends(current_user)):
    c = db()
    _need_reminders_consent(c, u)
    cur = c.execute("INSERT INTO cards(user_id,issuer,name,last4,credit_limit,balance,min_due,due_day,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (u["id"], body.issuer, body.name, body.last4, body.credit_limit, body.balance, body.min_due, body.due_day, now()))
    c.commit()
    r = dict(c.execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone())
    c.close()
    return r


@router.put("/cards/{cid}")
def cards_update(cid: int, body: CardIn, u=Depends(current_user)):
    c = db()
    r = c.execute("UPDATE cards SET issuer=?,name=?,last4=?,credit_limit=?,balance=?,min_due=?,due_day=? WHERE id=? AND user_id=?",
                  (body.issuer, body.name, body.last4, body.credit_limit, body.balance, body.min_due, body.due_day, cid, u["id"]))
    c.commit()
    c.close()
    if not r.rowcount:
        raise HTTPException(404, "Card not found")
    return {"ok": True}


@router.delete("/cards/{cid}")
def cards_del(cid: int, u=Depends(current_user)):
    c = db()
    c.execute("DELETE FROM cards WHERE id=? AND user_id=?", (cid, u["id"]))
    c.commit()
    c.close()
    return {"ok": True}


def next_due(day: int) -> dt.date:
    t = clock.today()
    d = t.replace(day=min(day, 28))
    if d < t:
        m = t.month % 12 + 1
        d = d.replace(year=t.year + (t.month == 12), month=m)
    return d


def paid_this_cycle(c, uid, target, tid, due: dt.date) -> bool:
    start = (due - dt.timedelta(days=31)).isoformat()
    return bool(c.execute("SELECT 1 FROM payments WHERE user_id=? AND target=? AND target_id=? AND status IN ('success','recorded') AND at>=?", (uid, target, tid, start)).fetchone())


@router.get("/reminders")
def reminders(u=Depends(current_user)):
    c = db()
    out = []
    for l in c.execute("SELECT * FROM loans WHERE user_id=?", (u["id"],)):
        d = next_due(l["due_day"])
        out.append({"type": "loan", "id": l["id"], "title": f"{l['lender']} EMI", "subtitle": f"{l['kind']} · {_mask_acct(l['account_no'])}", "amount": l["emi"],
                    "due_date": d.isoformat(), "days_left": (d - clock.today()).days, "autopay": bool(l["autopay"]), "paid": paid_this_cycle(c, u["id"], "loan", l["id"], d)})
    for k in c.execute("SELECT * FROM cards WHERE user_id=?", (u["id"],)):
        d = next_due(k["due_day"])
        out.append({"type": "card", "id": k["id"], "title": f"{k['issuer']} card bill", "subtitle": f"•••• {k['last4']}", "amount": k["balance"], "min_due": k["min_due"],
                    "due_date": d.isoformat(), "days_left": (d - clock.today()).days, "autopay": False, "paid": paid_this_cycle(c, u["id"], "card", k["id"], d)})
    c.close()
    out.sort(key=lambda r: (r["paid"], r["days_left"]))
    return out


class PayIn(BaseModel):
    target: str  # loan | card | builder
    target_id: int
    amount: float = Field(gt=0, le=1e8)
    method: str  # self (paid elsewhere, recorded by the user) | upi | card | netbanking (only with a live gateway)


@router.post("/payments", status_code=201)
def pay(body: PayIn, req: Request, u=Depends(current_user)):
    if body.target not in ("loan", "card") or body.method not in ("self", "upi", "card", "netbanking"):
        raise HTTPException(400, "Invalid payment request")
    # No payment gateway is connected yet: Fikarta cannot move money. Users record payments they made in
    # their own bank/UPI app ("self"). Gateway methods are refused until a real provider is configured.
    self_reported = body.method == "self"
    if not self_reported and PAYMENT_PROVIDER == "sandbox":
        raise HTTPException(503, "payments_not_live")
    c = db()
    try:
        tbl = "loans" if body.target == "loan" else "cards"
        t = c.execute(f"SELECT * FROM {tbl} WHERE id=? AND user_id=?", (body.target_id, u["id"])).fetchone()
        if not t:
            raise HTTPException(404, "Account not found")
        lender = t["lender"] if body.target == "loan" else t["issuer"]
        due = next_due(t["due_day"])
        on_time = int(clock.today() <= due)
        # Points only for payments Fikarta can verify; a self-reported payment earns none.
        points = 0 if self_reported else (int(min(500, body.amount * 0.01)) if on_time else 0)
        provider = "self_reported" if self_reported else PAYMENT_PROVIDER
        ref = "FKP" + clock.utcnow().strftime("%y%m%d") + secrets.token_hex(4).upper()
        c.execute("INSERT INTO payments(user_id,target,target_id,lender,amount,method,provider,ref,status,on_time,points,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (u["id"], body.target, body.target_id, lender, body.amount, body.method, provider, ref, "recorded" if self_reported else "success", on_time, points, now()))
        if body.target == "loan":
            c.execute("UPDATE loans SET outstanding=MAX(0,outstanding-?) WHERE id=?", (body.amount, body.target_id))
        else:
            c.execute("UPDATE cards SET balance=MAX(0,balance-?), min_due=0 WHERE id=?", (body.amount, body.target_id))
        if points:
            c.execute("INSERT INTO rewards(user_id,points,reason,at) VALUES(?,?,?,?)", (u["id"], points, f"On-time payment to {lender}", now()))
        notify(c, u["id"], "payment", f"₹{body.amount:,.0f} {'recorded for' if self_reported else 'paid to'} {lender}", f"Reference {ref}." + (f" You earned {points} points." if points else ""))
        audit(c, u["id"], "payment", f"{body.target}:{body.target_id}:{body.amount}", client_ip(req))
        c.commit()
        return {"ref": ref, "status": "recorded" if self_reported else "success", "provider": provider, "self_reported": self_reported,
                "is_sandbox": False, "points": points, "lender": lender, "amount": body.amount, "at": now()}
    finally:
        c.close()


@router.get("/payments")
def payments(u=Depends(current_user)):
    return _crud_list("payments", u["id"])


# --------------------------------------------------------------------------- rewards, referral, builder, notifications, dashboard
@router.get("/rewards")
def rewards(u=Depends(current_user)):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT points,reason,at FROM rewards WHERE user_id=? ORDER BY id DESC", (u["id"],))]
    refs = c.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (u["referral_code"],)).fetchone()[0]
    c.close()
    return {"balance": sum(r["points"] for r in rows), "ledger": rows, "referral_code": u["referral_code"], "referrals": refs, "point_value_inr": 1}


class BuilderIn(BaseModel):
    amount: float = Field(ge=5000, le=50000)
    tenure_months: int = Field(ge=6, le=24)
    monthly_income: float = Field(ge=5000, le=1e7)


@router.post("/builder", status_code=201)
def builder(body: BuilderIn, u=Depends(current_user)):
    c = db()
    if c.execute("SELECT 1 FROM builder_applications WHERE user_id=? AND status IN ('waitlist','submitted','approved')", (u["id"],)).fetchone():
        c.close()
        raise HTTPException(409, "You are already on the Credit Builder waitlist")
    emi = round(body.amount / body.tenure_months)
    if emi > body.monthly_income * 0.2:
        c.close()
        raise HTTPException(400, "EMI would be over 20% of your income. Choose a smaller amount or longer tenure.")
    c.execute("INSERT INTO builder_applications(user_id,amount,tenure_months,monthly_income,status,created_at) VALUES(?,?,?,?,?,?)",
              (u["id"], body.amount, body.tenure_months, body.monthly_income, "waitlist", now()))
    # No lending partner is signed yet, so this is a waitlist entry, not a loan application.
    notify(c, u["id"], "builder", "You are on the Credit Builder waitlist", "Credit Builder is not live yet. We will tell you when it launches with an RBI-regulated lending partner.")
    c.commit()
    c.close()
    return {"status": "waitlist", "emi": emi}


@router.get("/builder")
def builder_status(u=Depends(current_user)):
    return _crud_list("builder_applications", u["id"])


@router.get("/notifications")
def notifications(u=Depends(current_user)):
    c = db()
    # due-date alerts are generated on read so they are always current
    for r in reminders(u):
        if not r["paid"] and 0 <= r["days_left"] <= 3:
            key = f"Due in {r['days_left']} day(s): {r['title']}"
            if not c.execute("SELECT 1 FROM notifications WHERE user_id=? AND title=? AND at>=?", (u["id"], key, (clock.today()).isoformat())).fetchone():
                notify(c, u["id"], "due", key, f"₹{r['amount']:,.0f} due on {r['due_date']}. Pay now to avoid a late mark.")
    c.commit()
    rows = [dict(r) for r in c.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100", (u["id"],))]
    c.close()
    return rows


@router.post("/notifications/read")
def notif_read(u=Depends(current_user)):
    c = db()
    c.execute("UPDATE notifications SET read=1 WHERE user_id=?", (u["id"],))
    c.commit()
    c.close()
    return {"ok": True}


@router.get("/offers")
def offers(u=Depends(current_user)):
    c = db()
    rep = latest_report(c, u["id"])
    c.close()
    score = rep["score"] if rep else None
    out = []
    for l in lenders():
        mc = l.get("min_cibil") or ""
        m = re.search(r"\d{3}", str(mc))
        need = int(m.group()) if m else 650
        out.append({**l, "min_score": need, "eligible": (score is not None and score >= need)})
    out.sort(key=lambda r: (not r["eligible"], r["min_score"]))
    return {"score": score, "offers": out}


@router.get("/dashboard")
def dashboard(u=Depends(current_user)):
    c = db()
    rep = latest_report(c, u["id"])
    hist = [dict(r) for r in c.execute("SELECT month,score FROM score_history WHERE user_id=? ORDER BY month", (u["id"],))]
    unread = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0", (u["id"],)).fetchone()[0]
    open_d = c.execute("SELECT COUNT(*) FROM disputes WHERE user_id=? AND status IN ('draft','filed','in_review')", (u["id"],)).fetchone()[0]
    pts = c.execute("SELECT COALESCE(SUM(points),0) FROM rewards WHERE user_id=?", (u["id"],)).fetchone()[0]
    tasks = c.execute("SELECT COUNT(*), COALESCE(SUM(done),0) FROM plan_tasks WHERE user_id=?", (u["id"],)).fetchone()
    c.close()
    rem = [r for r in reminders(u) if not r["paid"]][:3]
    return {
        "user": public_user(u), "has_report": bool(rep),
        "score": rep["score"] if rep else None, "band": rep["band"] if rep else None, "is_sandbox": False, "bureau": rep["bureau"] if rep else None, "report_date": rep.get("report_date") if rep else None, "source": rep.get("source") if rep else None,
        "test_data": bool(rep.get("test_data")) if rep else False,
        "pulled_at": rep.get("pulled_at") if rep else None, "change": (hist[-1]["score"] - hist[-2]["score"]) if len(hist) > 1 else 0,
        "errors": len(rep["errors"]) if rep else 0, "factors": rep["factors"] if rep else [],
        "history": hist, "unread": unread, "open_disputes": open_d, "points": pts,
        "plan_total": tasks[0], "plan_done": tasks[1], "upcoming": rem,
    }
