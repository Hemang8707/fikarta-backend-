"""Structured customer details the user shares in chat (income, job, city, household, obligations).

- Fixed schema with validation: every value is type- and range-checked before it is stored; anything that
  looks like an identifier (PAN, phone, email, Aadhaar, account numbers) is rejected - those only come from
  verified profile flows, never from chat.
- Versioned like credit reports: a changed value closes the old row (valid_to) and opens a new one, so the
  history of what the user told us is kept with dates.
- Source is recorded ('chat' or 'user_edit'), and the assistant sees these as user-stated, not verified.
- Purpose: personalise Fikarta AI's guidance (ai_chat consent). Withdrawal, credit-data erasure and account
  deletion remove it; export includes it; the user can view, correct and delete each detail.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

FIELDS: Dict[str, Dict[str, Any]] = {
    "monthly_income": {"label": "Monthly income", "type": "money", "min": 1000, "max": 10_000_000},
    "employment_type": {"label": "Work", "type": "enum", "values": ["salaried", "self-employed", "business owner", "freelancer", "student", "retired", "homemaker", "not working"]},
    "employer": {"label": "Employer or business", "type": "text", "max_len": 60},
    "industry": {"label": "Industry", "type": "text", "max_len": 40},
    "city": {"label": "City", "type": "text", "max_len": 40},
    "dependents": {"label": "People who depend on you", "type": "int", "min": 0, "max": 15},
    "housing": {"label": "Home", "type": "enum", "values": ["rented", "own home", "with family", "company provided", "other"]},
    "monthly_rent": {"label": "Monthly rent", "type": "money", "min": 0, "max": 2_000_000},
    "other_emis": {"label": "Other monthly EMIs not on your report", "type": "money", "min": 0, "max": 5_000_000},
    "monthly_savings": {"label": "Monthly savings", "type": "money", "min": 0, "max": 10_000_000},
    "loan_amount_needed": {"label": "Loan amount you need", "type": "money", "min": 1000, "max": 100_000_000},
    "loan_purpose": {"label": "What the loan is for", "type": "text", "max_len": 40},
}

_IDENT = [re.compile(p, re.I) for p in (r"\b[A-Z]{5}\d{4}[A-Z]\b", r"[\w.+-]+@[\w-]+\.[\w.]+", r"\b\d{4}\s?\d{4}\s?\d{4}\b",
                                         r"\b(?:\+?91[\s-]?)?[6-9]\d{9}\b", r"\b\d{9,18}\b", r"\[(?:PAN|phone|email|Aadhaar|number)\]")]


def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS user_profile_facts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      key TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT);
    CREATE INDEX IF NOT EXISTS ix_upf_user ON user_profile_facts(user_id, key, valid_to);
    """)


def _money(v) -> Optional[int]:
    s = str(v).lower().replace(",", "").replace("rs", "").replace("₹", "").replace("inr", "").strip()
    mult = 1
    for suffix, m in (("lakh", 100_000), ("lac", 100_000), ("l", 100_000), ("k", 1000), ("thousand", 1000), ("cr", 10_000_000), ("crore", 10_000_000)):
        if s.endswith(suffix):
            s, mult = s[: -len(suffix)].strip(), m
            break
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


def validate(key: str, value: Any) -> Tuple[bool, Optional[str]]:
    """Returns (ok, normalised string value)."""
    f = FIELDS.get(key)
    if f is None or value is None or str(value).strip() == "":
        return False, None
    raw = str(value).strip()
    if any(p.search(raw) for p in _IDENT) and f["type"] in ("text", "enum"):
        return False, None
    if f["type"] == "money":
        n = _money(raw)
        return (True, str(n)) if n is not None and f["min"] <= n <= f["max"] else (False, None)
    if f["type"] == "int":
        try:
            n = int(float(raw))
        except ValueError:
            return False, None
        return (True, str(n)) if f["min"] <= n <= f["max"] else (False, None)
    if f["type"] == "enum":
        v = raw.lower()
        return (True, v) if v in f["values"] else (False, None)
    t = re.sub(r"\s+", " ", raw)
    if len(t) < 2 or len(t) > f["max_len"] or re.search(r"\d{4,}", t):
        return False, None
    return True, t


def current(c, uid: int) -> Dict[str, Dict[str, Any]]:
    out = {}
    for r in c.execute("SELECT key, value, source, valid_from FROM user_profile_facts WHERE user_id=? AND valid_to IS NULL", (uid,)):
        out[r["key"]] = {"value": r["value"], "source": r["source"], "since": r["valid_from"]}
    return out


def upsert(c, uid: int, items: Dict[str, Any], source: str, now: str) -> List[str]:
    """Validated, versioned write. Returns the keys that changed."""
    changed = []
    cur = current(c, uid)
    for key, value in (items or {}).items():
        ok, v = validate(key, value)
        if not ok:
            continue
        if key in cur and cur[key]["value"] == v:
            continue
        c.execute("UPDATE user_profile_facts SET valid_to=? WHERE user_id=? AND key=? AND valid_to IS NULL", (now, uid, key))
        c.execute("INSERT INTO user_profile_facts(user_id,key,value,source,valid_from) VALUES(?,?,?,?,?)", (uid, key, v, source, now))
        changed.append(key)
    return changed


def remove(c, uid: int, key: str) -> int:
    return c.execute("DELETE FROM user_profile_facts WHERE user_id=? AND key=?", (uid, key)).rowcount


def history(c, uid: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in c.execute("SELECT key, value, source, valid_from, valid_to FROM user_profile_facts WHERE user_id=? ORDER BY key, valid_from", (uid,))]


def display(key: str, value: str) -> str:
    f = FIELDS[key]
    if f["type"] == "money":
        n = int(value)
        s = str(n)
        if len(s) > 3:
            head, tail = s[:-3], s[-3:]
            head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
            s = head + "," + tail
        return "Rs " + s
    return value


def facts_for_ai(c, uid: int) -> Optional[Dict[str, str]]:
    cur = current(c, uid)
    if not cur:
        return None
    return {FIELDS[k]["label"]: display(k, v["value"]) for k, v in cur.items() if k in FIELDS}


def view(c, uid: int) -> Dict[str, Any]:
    cur = current(c, uid)
    hist = history(c, uid)
    return {
        "fields": [{"key": k, "label": f["label"], "type": f["type"], "values": f.get("values"),
                    "value": cur[k]["value"] if k in cur else None, "display": display(k, cur[k]["value"]) if k in cur else None,
                    "source": cur[k]["source"] if k in cur else None, "since": cur[k]["since"] if k in cur else None,
                    "previous": [{"value": display(k, h["value"]), "from": h["valid_from"], "to": h["valid_to"]} for h in hist if h["key"] == k and h["valid_to"]]}
                   for k, f in FIELDS.items()],
    }
