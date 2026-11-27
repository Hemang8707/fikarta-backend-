"""Ingest official regulator / bureau / government pages into db/official_kb.db for Fikarta AI retrieval.

Every document keeps its official URL, publisher, fetch time and a content hash, so answers can cite the source and
the text can be re-verified. robots.txt is respected, requests are slow and identified, and only the curated public
pages below are fetched. Re-run any time; unchanged pages are skipped by hash.

Usage: python scripts/ingest_official.py            (fetch all)
       python scripts/ingest_official.py --list     (show what is stored)
"""
import datetime as dt
import hashlib
import html
import io
import os
import re
import sqlite3
import sys
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.getenv("OFFICIAL_KB_PATH", os.path.join(ROOT, "db", "official_kb.db"))
UA = "FikartaKnowledgeBot/1.0 (+https://fikarta.com; compliance knowledge base)"

# (publisher, title, url, topic) — public pages only. Keep this list curated and reviewed.
# Not included: rbidocs.rbi.org.in PDFs (served behind a JavaScript bot challenge) and cibil.com (returns 403 to
# automated clients). Fikarta does not work around access controls; the HTML versions on www.rbi.org.in are used instead.
SOURCES = [
    ("Reserve Bank of India", "Compensation framework for delayed resolution of credit information complaints (CICs and credit institutions)",
     "https://rbi.org.in/Scripts/NotificationUser.aspx?Id=12554&Mode=0", "credit report disputes"),
    ("Reserve Bank of India", "Free annual credit report to individuals",
     "https://www.rbi.org.in/commonman/english/scripts/Notification.aspx?Id=1884", "credit report"),
    ("Reserve Bank of India", "Credit information reporting: free full credit report, 300-900 score scale and alerts",
     "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=10590&Mode=0", "credit report"),
    ("Reserve Bank of India", "Outsourcing of financial services: responsibilities of regulated entities employing recovery agents (2022)",
     "https://rbi.org.in/Scripts/NotificationUser.aspx?Id=12378&Mode=0", "recovery agents"),
    ("Reserve Bank of India", "Recovery agents engaged by banks",
     "https://www.rbi.org.in/commonman/english/scripts/Notification.aspx?Id=347", "recovery agents"),
    ("Reserve Bank of India", "Reserve Bank of India (Digital Lending) Directions, 2025",
     "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12848", "digital lending"),
    ("Reserve Bank of India", "Key Facts Statement (KFS) for loans and advances (April 2024)",
     "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12663&Mode=0", "loan disclosures"),
    ("Reserve Bank of India", "Reserve Bank - Integrated Ombudsman Scheme, 2026: FAQs",
     "https://www.rbi.org.in/commonman/english/scripts/FAQs.aspx?Id=3407", "complaints and ombudsman"),
    ("Reserve Bank of India", "RBI customer service guidelines",
     "https://www.rbi.org.in/commonman/english/Scripts/CustomerServiceGuidelines.aspx", "customer service"),
]


def init(c):
    c.execute("""CREATE TABLE IF NOT EXISTS official_docs(
        url TEXT PRIMARY KEY, publisher TEXT, title TEXT, topic TEXT, text TEXT, sha256 TEXT,
        fetched_at TEXT, http_status INTEGER, content_type TEXT)""")


def html_to_text(raw: str) -> str:
    # RBI wraps whole pages in <form> (ASP.NET), so forms are kept; only non-content blocks are removed.
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer)\b[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</tr>|</div>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(raw)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(ln) > 2]
    return "\n".join(lines)


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in r.pages[:80])


_robots = {}


def allowed(url: str) -> bool:
    host = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = httpx.get(host + "/robots.txt", headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        _robots[host] = rp
    return _robots[host].can_fetch(UA, url)


def fetch_all():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    init(c)
    ok = skipped = failed = 0
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.8"}, timeout=40, follow_redirects=True) as client:
        for publisher, title, url, topic in SOURCES:
            if not allowed(url):
                print(f"robots.txt disallows, skipped: {url}")
                failed += 1
                continue
            try:
                r = client.get(url)
            except httpx.HTTPError as e:
                print(f"FAILED {type(e).__name__}: {url}")
                failed += 1
                continue
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200:
                print(f"FAILED HTTP {r.status_code}: {url}")
                failed += 1
                continue
            try:
                text = pdf_to_text(r.content) if ("pdf" in ctype or url.lower().endswith(".pdf")) else html_to_text(r.text)
            except Exception as e:
                print(f"FAILED parse {type(e).__name__}: {url}")
                failed += 1
                continue
            text = text.strip()
            if len(text) < 400:
                print(f"FAILED too little text ({len(text)} chars): {url}")
                failed += 1
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            prev = c.execute("SELECT sha256 FROM official_docs WHERE url=?", (url,)).fetchone()
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            if prev and prev[0] == digest:
                c.execute("UPDATE official_docs SET fetched_at=? WHERE url=?", (now, url))
                skipped += 1
            else:
                c.execute("INSERT OR REPLACE INTO official_docs(url,publisher,title,topic,text,sha256,fetched_at,http_status,content_type) VALUES(?,?,?,?,?,?,?,?,?)",
                          (url, publisher, title, topic, text, digest, now, r.status_code, ctype[:80]))
                ok += 1
            c.commit()
            print(f"ok {len(text):>7} chars  {publisher}: {title[:70]}")
            time.sleep(2)  # be gentle with official servers
    c.close()
    print(f"\nstored/updated {ok}, unchanged {skipped}, failed {failed}")
    return failed


def show():
    c = sqlite3.connect(DB)
    init(c)
    for row in c.execute("SELECT publisher, title, length(text), fetched_at FROM official_docs ORDER BY publisher, title"):
        print(row)


if __name__ == "__main__":
    if "--list" in sys.argv:
        show()
    else:
        sys.exit(1 if fetch_all() == len(SOURCES) else 0)
