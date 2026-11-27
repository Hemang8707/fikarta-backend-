"""First-party, consented usage events (purpose 'analytics' in the privacy notice).

What is stored: event name, screen, a few whitelisted properties, app version, platform/OS and device model,
session id and time. What is NOT stored: IP address (kept only in the security audit log), contacts, SMS,
call logs, files, location or anything typed into the app. Events are kept 180 days (scripts/retention.py),
deleted when the user withdraws the consent or deletes the account, and never sent to the AI provider.

Used for: ordering the user's Home shortcuts by what they actually use, and product health (screens that fail
or are never reached).
"""
import json
import re
from typing import Any, Dict, List, Optional

from fastapi import Depends
from pydantic import BaseModel, Field

from mobile_api import audit, current_user, db, now, router

RETENTION_DAYS = 180
DAILY_CAP = 2000
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
PROP_KEYS = {"tool", "tab", "screen", "result", "source", "kind", "count", "lang", "ms"}


def _init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS app_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      session_id TEXT, name TEXT NOT NULL, screen TEXT, props TEXT, app_version TEXT, platform TEXT, os TEXT, device TEXT,
      client_at TEXT, at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_ev_user ON app_events(user_id, at);
    CREATE INDEX IF NOT EXISTS ix_ev_name ON app_events(user_id, name);
    """)
    c.close()


_init()


def consented(c, uid: int) -> bool:
    r = c.execute("SELECT granted FROM consents WHERE user_id=? AND purpose='analytics' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    return bool(r and r["granted"])


def _props(p: Optional[Dict[str, Any]]) -> str:
    out = {}
    for k, v in (p or {}).items():
        if k in PROP_KEYS and isinstance(v, (str, int, float, bool)):
            out[k] = v if not isinstance(v, str) else v[:60]
    return json.dumps(out)


class EventIn(BaseModel):
    name: str = Field(max_length=41)
    screen: Optional[str] = Field(None, max_length=60)
    props: Optional[Dict[str, Any]] = None
    at: Optional[str] = Field(None, max_length=40)


class BatchIn(BaseModel):
    session_id: str = Field(max_length=40)
    app_version: str = Field(max_length=20)
    platform: str = Field(max_length=20)
    os: Optional[str] = Field(None, max_length=60)
    device: Optional[str] = Field(None, max_length=60)
    events: List[EventIn] = Field(max_length=50)


@router.post("/events", status_code=202)
def ingest(body: BatchIn, u=Depends(current_user)):
    c = db()
    try:
        if not consented(c, u["id"]):
            return {"accepted": 0, "reason": "no_consent"}  # the app may send before it knows; nothing is kept
        today = now()[:10]
        used = c.execute("SELECT COUNT(*) FROM app_events WHERE user_id=? AND at>=?", (u["id"], today)).fetchone()[0]
        room = max(0, DAILY_CAP - used)
        rows = [e for e in body.events if NAME_RE.match(e.name)][:room]
        ts = now()
        c.executemany("INSERT INTO app_events(user_id,session_id,name,screen,props,app_version,platform,os,device,client_at,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      [(u["id"], body.session_id, e.name, e.screen, _props(e.props), body.app_version, body.platform, body.os, body.device, e.at, ts) for e in rows])
        c.commit()
        return {"accepted": len(rows)}
    finally:
        c.close()


@router.get("/engagement")
def engagement(u=Depends(current_user)):
    """The user's own usage, for personalising the app (e.g. Home shortcuts). Empty without consent."""
    c = db()
    try:
        if not consented(c, u["id"]):
            return {"consented": False, "top_tools": [], "last_active": None}
        tools = [r[0] for r in c.execute(
            "SELECT json_extract(props,'$.tool') t FROM app_events WHERE user_id=? AND name='tool_open' AND t IS NOT NULL "
            "GROUP BY t ORDER BY COUNT(*) DESC, MAX(id) DESC LIMIT 8", (u["id"],))]
        last = c.execute("SELECT MAX(at) FROM app_events WHERE user_id=?", (u["id"],)).fetchone()[0]
        return {"consented": True, "top_tools": tools, "last_active": last}
    finally:
        c.close()


def purge(c, uid: int):
    c.execute("DELETE FROM app_events WHERE user_id=?", (uid,))
    audit(c, uid, "analytics_deleted")
