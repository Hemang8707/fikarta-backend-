"""Exact answers from Fikarta's own website database for Fikarta AI.

Text search is fuzzy; questions like "personal loan in Delhi" or "Axis Bank vs HDFC Bank interest" need the
exact row. This module spots the entities a question names (city, lender, two lenders, loan type, loan
amount, salary, loan app) and turns the matching database rows into source documents with exact figures.
They are passed to the model as SOURCES, so every figure is cited and passes the figure check.

Only public website data is read here (cities, lenders, comparisons, loan amounts, salaries, loan-type
calculators, loan apps). Nothing about any user.
"""
import json
import os
import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT_DB = os.getenv("DATABASE_PATH", os.path.join(BACKEND_DIR, "db", "fikarta.db"))
MAX_DOCS = 4

# common short names people type for lenders (slug -> extra aliases)
ALIASES = {
    "sbi": ["sbi", "state bank"], "hdfc-bank": ["hdfc"], "icici-bank": ["icici"], "axis-bank": ["axis"],
    "kotak-mahindra-bank": ["kotak"], "idfc-first-bank": ["idfc"], "indusind-bank": ["indusind"], "yes-bank": ["yes bank"],
    "bajaj-finserv": ["bajaj finserv", "bajaj"], "tata-capital": ["tata capital"], "poonawalla-fincorp": ["poonawalla"],
    "moneyview": ["money view", "moneyview"], "kreditbee": ["kreditbee", "kredit bee"], "navi": ["navi"], "cashe": ["cashe"], "fibe": ["fibe", "earlysalary"],
}
LOAN_TYPES = {"home-loan": ["home loan", "housing loan"], "car-loan": ["car loan", "auto loan", "vehicle loan"],
              "personal-loan": ["personal loan"], "gold-loan": ["gold loan"], "education-loan": ["education loan", "student loan"],
              "two-wheeler-loan": ["two wheeler", "two-wheeler", "bike loan", "scooter loan"]}


def _j(v) -> list:
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v or "[]")
        return x if isinstance(x, list) else []
    except (TypeError, ValueError):
        return []


def _has(v) -> bool:
    return v not in (None, "", "None", "null")


def _inr(n) -> str:
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    return "₹" + s


def _amounts_in(text: str) -> List[int]:
    """Every amount in the text: '3 lakh', '60k', '₹75,000', '60000'."""
    t = text.lower().replace(",", "")
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(lakh|lac|l\b|k\b|thousand|crore|cr\b)", t):
        out.append(int(float(m.group(1)) * {"lakh": 1e5, "lac": 1e5, "l": 1e5, "k": 1e3, "thousand": 1e3, "crore": 1e7, "cr": 1e7}[m.group(2).strip()]))
    rest = re.sub(r"(\d+(?:\.\d+)?)\s*(lakh|lac|l\b|k\b|thousand|crore|cr\b)", " ", t)
    out += [int(x) for x in re.findall(r"\b(\d{4,9})\b", rest) if not (1900 <= int(x) <= 2100)]  # skip years
    seen = []
    for v in out:
        if v not in seen:
            seen.append(v)
    return seen[:4]


def _amount_in(text: str) -> Optional[int]:
    t = text.lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(lakh|lac|l\b|k\b|thousand|crore|cr\b)", t)
    if m:
        mult = {"lakh": 1e5, "lac": 1e5, "l": 1e5, "k": 1e3, "thousand": 1e3, "crore": 1e7, "cr": 1e7}[m.group(2).strip()]
        return int(float(m.group(1)) * mult)
    m = re.search(r"(?:₹|rs\.?\s?|inr\s?)(\d{4,9})", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{5,8})\b", t)
    return int(m.group(1)) if m else None


class SiteData:
    def __init__(self, db_path: str = CONTENT_DB):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.mtime = None
        self.cities: List[Dict[str, Any]] = []
        self.lenders: Dict[str, Dict[str, Any]] = {}
        self.pairs: Dict[frozenset, Dict[str, Any]] = {}
        self.amounts: List[Dict[str, Any]] = []
        self.salaries: List[Dict[str, Any]] = []
        self.types: Dict[str, Dict[str, Any]] = {}
        self.apps: List[Dict[str, Any]] = []

    def ensure(self):
        if not os.path.exists(self.db_path):
            return
        m = os.path.getmtime(self.db_path)
        with self.lock:
            if m == self.mtime:
                return
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            try:
                self.cities = [dict(r) for r in c.execute("SELECT slug,name,state,tier,lowest_rate,max_amount,avg_loan,disbursal,city_lenders_json FROM cities")]
                self.lenders = {r["slug"]: dict(r) for r in c.execute(
                    "SELECT slug,name,category,type,rating,rate_range,amount_range,tenure_range,fee_range,prepayment_charge,min_cibil,disbursal_speed,best_for,min_salary,min_rate FROM lenders")}
                self.pairs = {frozenset((r["lender_a_slug"], r["lender_b_slug"])): dict(r) for r in c.execute("SELECT slug,lender_a_slug,lender_b_slug,verdict FROM comparisons")}
                self.amounts = [dict(r) for r in c.execute("SELECT slug,amount_num,title,lowest_rate,best_tenure,min_salary,emi_matrix_json,top_lenders_json FROM loan_amounts")]
                self.salaries = [dict(r) for r in c.execute("SELECT slug,amount_num,label,max_loan,max_emi,foir_percent,lowest_rate,disbursal FROM salaries")]
                self.types = {r["slug"]: dict(r) for r in c.execute(
                    "SELECT slug,name,default_rate,min_rate,max_rate,min_amount,max_amount,min_tenure,max_tenure,processing_fee FROM calculators WHERE calc_type='product'")}
                self.apps = [dict(r) for r in c.execute("SELECT slug,name,status_verdict,apr_range,min_amount,max_amount,developer FROM loan_apps")]
            finally:
                c.close()
            self.mtime = m

    # -------------------------------------------------------------- matching
    def _lenders_in(self, t: str) -> List[str]:
        found = []
        for slug, l in self.lenders.items():
            names = [l["name"].lower()] + ALIASES.get(slug, [])
            pos = min((t.find(n) for n in names if re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", t)), default=-1)
            if pos >= 0:
                found.append((pos, slug))
        return [s for _, s in sorted(found)]

    def _city_in(self, t: str) -> Optional[Dict[str, Any]]:
        best = None
        for c in self.cities:
            n = c["name"].lower()
            if re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", t) and (best is None or len(n) > len(best["name"])):
                best = c
        return best

    def _app_in(self, t: str) -> Optional[Dict[str, Any]]:
        if not re.search(r"\b(app|apps|safe|fake|registered|genuine|legit)\b", t):
            return None
        best = None
        for a in self.apps:
            n = (a["name"] or "").lower()
            if len(n) >= 4 and re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", t) and (best is None or len(n) > len(best["name"])):
                best = a
        return best

    # -------------------------------------------------------------- documents
    def _lender_text(self, l: Dict[str, Any]) -> str:
        parts = [f"{l['name']} ({l.get('type') or l.get('category')})"]
        for label, k in (("interest rate", "rate_range"), ("loan amount", "amount_range"), ("tenure", "tenure_range"), ("processing fee", "fee_range"),
                         ("prepayment charge", "prepayment_charge"), ("minimum CIBIL", "min_cibil"), ("disbursal", "disbursal_speed"), ("best for", "best_for")):
            if _has(l.get(k)):
                parts.append(f"{label} {l[k]}")
        if _has(l.get("min_salary")):
            parts.append(f"minimum salary {_inr(l['min_salary'])} a month")
        return "; ".join(parts) + "."

    def lookup(self, queries: List[str]) -> List[Dict[str, Any]]:
        self.ensure()
        t = " ".join(q for q in queries if q).lower()
        if not t.strip():
            return []
        docs: List[Dict[str, Any]] = []

        def add(title, url, text):
            if not any(d["url"] == url for d in docs):
                docs.append({"kind": "Fikarta data", "title": title, "url": url, "text": text, "official": False})

        lenders = self._lenders_in(t)
        if len(lenders) >= 2:  # comparison of the first two named lenders
            a, b = self.lenders[lenders[0]], self.lenders[lenders[1]]
            pair = self.pairs.get(frozenset((a["slug"], b["slug"])))
            text = f"Side-by-side published terms. {self._lender_text(a)} {self._lender_text(b)}"
            if pair and _has(pair.get("verdict")):
                text += f" Fikarta comparison: {pair['verdict']}"
            add(f"{a['name']} vs {b['name']}", f"/compare/{pair['slug']}" if pair else f"/lenders/{a['slug']}", text)
        for slug in lenders[:2]:
            l = self.lenders[slug]
            add(f"{l['name']} personal loan", f"/lenders/{slug}", f"Published terms: {self._lender_text(l)}")

        city = self._city_in(t)
        if city:
            refs = [r.get("slug") if isinstance(r, dict) else r for r in _j(city.get("city_lenders_json"))]
            ls = [self.lenders[s] for s in refs if s in self.lenders]
            ls.sort(key=lambda x: x.get("min_rate") or 99)
            lt = "; ".join(f"{x['name']} {x['rate_range']}" for x in ls[:6])
            text = (f"Personal loans in {city['name']}, {city['state']}: lowest rate from {city['lowest_rate']}%, maximum amount {city['max_amount']}, "
                    f"average loan {city['avg_loan']}, disbursal {city['disbursal']}." + (f" Lenders serving {city['name']}: {lt}." if lt else ""))
            add(f"Personal loans in {city['name']}", f"/personal-loan/{city['slug']}", text)

        for slug, words in LOAN_TYPES.items():
            if slug in self.types and any(w in t for w in words) and not (slug == "personal-loan" and (city or lenders)):
                ty = self.types[slug]
                add(ty["name"].replace(" EMI Calculator", ""), f"/emi-calculator/{slug}",
                    f"{ty['name'].replace(' EMI Calculator', '')}: typical rate {ty['default_rate']}% (range {ty['min_rate']}%–{ty['max_rate']}%), "
                    f"amount {_inr(ty['min_amount'])} to {_inr(ty['max_amount'])}, tenure {ty['min_tenure']}–{ty['max_tenure']} months, processing fee {ty['processing_fee']}.")

        salary_q = bool(re.search(r"salary|earn|income|per month|a month|monthly", t))
        for amt in _amounts_in(t):
            # a salary-sized amount next to salary words -> salary tier; anything else -> loan-amount page
            s = min(self.salaries, key=lambda r: abs((r["amount_num"] or 0) - amt), default=None) if salary_q else None
            if s and abs((s["amount_num"] or 0) - amt) <= max(5000, amt * 0.15):
                add(f"Personal loan on {s['label']} salary", f"/personal-loan/salary/{s['slug']}",
                    f"On a monthly salary of {s['label']}: maximum loan about {s['max_loan']}, maximum EMI {_inr(s['max_emi'])} (EMIs up to {s['foir_percent']}% of salary), "
                    f"lowest rate from {s['lowest_rate']}%, disbursal {s['disbursal']}.")
                continue
            if not re.search(r"loan|emi|borrow|need", t):
                continue
            a = min(self.amounts, key=lambda r: abs((r["amount_num"] or 0) - amt), default=None)
            if a and abs((a["amount_num"] or 0) - amt) <= amt * 0.1:
                emis = "; ".join(f"{e.get('tenure')}: EMI {_inr(e.get('emi'))}" for e in _j(a.get("emi_matrix_json"))[:5] if isinstance(e, dict) and _has(e.get("emi")))
                add(a["title"], f"/personal-loan-amount/{a['slug']}",
                    f"{a['title']}: lowest rate from {a['lowest_rate']}%, best tenure {a['best_tenure']}." + (f" EMIs by tenure: {emis}." if emis else ""))

        app = self._app_in(t)
        if app:
            add(f"{app['name']} loan app check", f"/loan-app/{app['slug']}",
                f"{app['name']} (by {app.get('developer') or 'developer not listed'}): RBI directory status: {app.get('status_verdict') or 'not stated'}; "
                f"APR {app.get('apr_range') or 'not stated'}; amount {app.get('min_amount') or '?'} to {app.get('max_amount') or '?'}.")
        return docs[:MAX_DOCS]


DATA = SiteData()
