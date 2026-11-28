"""Lender email directory and email tables (no app imports, so migrations can use it while the app loads).
See mail_assist.py for the flow."""
import datetime as dt
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

CONTENT_DB = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "fikarta.db"))
if not os.path.isabs(CONTENT_DB):
    CONTENT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), CONTENT_DB)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_STOP = {"limited", "ltd", "private", "pvt", "the", "india", "co", "company", "corporation", "formerly", "known", "as", "and", "of", "services", "financial"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ schema
def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS lender_contacts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, lender_key TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL,
      role TEXT, website TEXT, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ok', added_at TEXT NOT NULL,
      UNIQUE(lender_key, email));
    CREATE INDEX IF NOT EXISTS ix_lc_key ON lender_contacts(lender_key);
    CREATE TABLE IF NOT EXISTS email_drafts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      lender TEXT NOT NULL, to_addr TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
      via TEXT, provider_id TEXT, thread_id TEXT, created_at TEXT NOT NULL, sent_at TEXT, note TEXT);
    CREATE INDEX IF NOT EXISTS ix_ed_user ON email_drafts(user_id, status);
    CREATE TABLE IF NOT EXISTS gmail_links(
      user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, email TEXT, refresh_enc TEXT NOT NULL,
      access_enc TEXT, expires_at REAL, connected_at TEXT NOT NULL, last_sync TEXT);
    CREATE TABLE IF NOT EXISTS bank_emails(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      msg_id TEXT NOT NULL, from_domain TEXT, lender TEXT, subject TEXT, received TEXT, text TEXT, UNIQUE(user_id, msg_id));
    CREATE INDEX IF NOT EXISTS ix_be_user ON bank_emails(user_id);
    """)


def lender_key(name: str) -> str:
    t = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _STOP]
    return " ".join(t[:4])


def _domain(email_or_url: str) -> str:
    s = email_or_url or ""
    host = s.split("@", 1)[1] if "@" in s and "://" not in s else (urlparse(s if "://" in s else "https://" + s).hostname or "")
    host = host.lower()
    parts = host.split(".")
    # registrable part: keep the last 3 labels for *.co.in / *.bank.in style domains, else 2
    keep = 3 if len(parts) >= 3 and parts[-2] in ("co", "bank", "com", "net", "org", "gov") and parts[-1] == "in" else 2
    return ".".join(parts[-keep:])


def _role(email: str) -> str:
    e = email.lower()
    if any(k in e for k in ("nodal", "pno", "principal")):
        return "nodal"
    if any(k in e for k in ("grievance", "complaint", "cgro", "redressal")):
        return "grievance"
    return "care"


def seed_contacts(c) -> int:
    """One-time copy of Fikarta's bank/NBFC directory into lender_contacts (idempotent)."""
    if not os.path.exists(CONTENT_DB):
        return 0
    src = sqlite3.connect(CONTENT_DB)
    n = 0
    try:
        rows = src.execute("SELECT name, grievance_email, website, rbi_registered_email FROM nbfcs").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        src.close()
    stamp = now()
    for name, g, web, rbi in rows:
        key = lender_key(name)
        if not key:
            continue
        for e in _EMAIL.findall(f"{g or ''} {rbi or ''}"):
            cur = c.execute("INSERT OR IGNORE INTO lender_contacts(lender_key,name,email,role,website,source,added_at) VALUES(?,?,?,?,?,?,?)",
                            (key, name.strip(), e.strip().lower(), _role(e), (web or "").strip() or None, "directory", stamp))
            n += cur.rowcount
    return n


def _match_key(c, lender: str) -> Optional[str]:
    key = lender_key(lender)
    if not key:
        return None
    if c.execute("SELECT 1 FROM lender_contacts WHERE lender_key=? LIMIT 1", (key,)).fetchone():
        return key
    want = set(key.split())
    best, score = None, 0.0
    first = key.split()[0]
    for (k,) in c.execute("SELECT DISTINCT lender_key FROM lender_contacts WHERE lender_key LIKE ?", (first + "%",)):
        have = set(k.split())
        s = len(want & have) / max(len(want | have), 1)
        if s > score:
            best, score = k, s
    return best if score >= 0.5 else None


_ROLE_ORDER = {"grievance": 0, "care": 1, "nodal": 2}


def contacts_for(c, lender: str) -> List[Dict[str, Any]]:
    """Official addresses for a lender, best first; bounced ones left out."""
    key = _match_key(c, lender)
    if not key:
        return []
    rows = [dict(r) for r in c.execute("SELECT email, role, website, source, name FROM lender_contacts WHERE lender_key=? AND status='ok'", (key,))]
    return sorted(rows, key=lambda r: (_ROLE_ORDER.get(r["role"] or "care", 1), r["source"] != "directory", r["email"]))


def discover_from_website(website: str, timeout: float = 8.0) -> List[str]:
    """Reads the lender's own website (contact / grievance pages) and returns addresses on the lender's domain only."""
    if not website:
        return []
    base = website if "://" in website else "https://" + website
    dom = _domain(base)
    found: List[str] = []
    for path in ("", "/contact-us", "/contact", "/grievance-redressal", "/customer-service", "/grievance", "/support"):
        try:
            r = httpx.get(base.rstrip("/") + path, timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (compatible; FikartaContactCheck/1.0)"})
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        for e in _EMAIL.findall(r.text):
            e = e.lower().rstrip(".")
            if _domain(e) == dom and e not in found and not e.endswith((".png", ".jpg")):
                found.append(e)
        if len(found) >= 6:
            break
    return found


def next_address(c, lender: str, bounced: Optional[str] = None) -> Optional[str]:
    """After a bounce: mark it, then the next official address, else addresses from the lender's own website."""
    key = _match_key(c, lender)
    if bounced:
        c.execute("UPDATE lender_contacts SET status='bounced' WHERE email=?", (bounced.lower(),))
    rest = contacts_for(c, lender)
    if rest:
        return rest[0]["email"]
    if not key:
        return None
    web = next((r[0] for r in c.execute("SELECT website FROM lender_contacts WHERE lender_key=? AND website IS NOT NULL LIMIT 1", (key,))), None)
    name = next((r[0] for r in c.execute("SELECT name FROM lender_contacts WHERE lender_key=? LIMIT 1", (key,))), lender)
    for e in discover_from_website(web or ""):
        if bounced and e == bounced.lower():
            continue
        c.execute("INSERT OR IGNORE INTO lender_contacts(lender_key,name,email,role,website,source,added_at) VALUES(?,?,?,?,?,?,?)",
                  (key, name, e, _role(e), web, "website", now()))
    rest = contacts_for(c, lender)
    return rest[0]["email"] if rest else None


