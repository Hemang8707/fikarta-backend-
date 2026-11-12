import os
import sys
import sqlite3
import json
import re
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request, Body, status
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(BACKEND_DIR, "db", "fikarta.db"))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Ensure submission tables and indexes exist on launch."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        phone TEXT NOT NULL,
        loan_amount REAL,
        tenure_months INTEGER,
        purpose TEXT,
        employment_type TEXT,
        monthly_income REAL,
        city TEXT,
        cibil_score_band TEXT,
        consent INTEGER DEFAULT 1,
        ip_address TEXT,
        user_agent TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS inquiries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        mobile TEXT NOT NULL,
        email TEXT NOT NULL,
        topic TEXT NOT NULL,
        message TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        status TEXT DEFAULT 'open',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS scam_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_name TEXT NOT NULL,
        apk_url TEXT,
        issue_type TEXT NOT NULL,
        description TEXT,
        complainant_phone TEXT,
        status TEXT DEFAULT 'pending_verification',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        source TEXT DEFAULT 'website',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inquiries_email ON inquiries(email);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scam_reports_app ON scam_reports(app_name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers(email);")
    conn.commit()
    conn.close()

# Auto-initialize database on module import
init_db()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="Fikarta Programmatic Engine",
    description="Internal High-Performance Gateway",
    version="2.1.0",
    lifespan=lifespan,
    docs_url=None,      # Public REST API Status: Unknown / Closed
    redoc_url=None,     # Public REST API Status: Unknown / Closed
    openapi_url=None,   # Public REST API Status: Unknown / Closed
)

# Parse allowed origins from environment variable
raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5177,http://127.0.0.1:5177,*")
allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def parse_json_fields(data: dict, fields: List[str]) -> dict:
    """Safely unpack JSON text columns into python dictionaries/lists."""
    for f in fields:
        clean_key = f.replace("_json", "")
        raw_val = data.get(f)
        if raw_val:
            try:
                data[clean_key] = json.loads(raw_val)
            except Exception:
                data[clean_key] = []
        else:
            data[clean_key] = []
    return data

# -------------------------------------------------------------
# PYDANTIC DATA TRANSFER OBJECTS (INPUT SCHEMAS)
# -------------------------------------------------------------
class LeadSubmission(BaseModel):
    name: Optional[str] = Field(None, description="Full name of applicant")
    phone: str = Field(..., description="10-digit Indian mobile number")
    loan_amount: Optional[float] = Field(None, ge=1000, le=100000000, description="Requested loan amount in INR")
    tenure_months: Optional[int] = Field(None, ge=1, le=360, description="Tenure in months")
    purpose: Optional[str] = Field(None, description="Purpose of loan")
    employment_type: Optional[str] = Field(None, description="Salaried, Self-Employed, etc.")
    monthly_income: Optional[float] = Field(None, ge=0, description="Monthly net take-home salary")
    city: Optional[str] = Field(None, description="City of residence")
    cibil_score_band: Optional[str] = Field(None, description="Credit score band (excellent, good, fair, etc.)")
    consent: bool = Field(True, description="Statutory borrower consent for credit evaluation")

class ContactSubmission(BaseModel):
    name: str = Field(..., min_length=2, max_length=120, description="Full name")
    mobile: str = Field(..., description="10-digit contact mobile number")
    email: EmailStr = Field(..., description="Valid borrower email address")
    topic: str = Field(..., min_length=2, max_length=150, description="Inquiry category")
    message: str = Field(..., min_length=5, max_length=5000, description="Inquiry details")

class ScamReportSubmission(BaseModel):
    app_name: str = Field(..., min_length=2, max_length=150, description="Suspicious or predatory app name")
    apk_url: Optional[str] = Field(None, description="Play Store link or APK download source")
    issue_type: str = Field(..., min_length=3, max_length=200, description="Type of harassment or illegality")
    description: Optional[str] = Field(None, max_length=5000, description="Detailed account of incident")
    complainant_phone: Optional[str] = Field(None, description="Complainant phone number for verification")

class NewsletterSubscription(BaseModel):
    email: EmailStr = Field(..., description="Subscriber email address")
    source: Optional[str] = Field("website", description="Subscription entry point")

# -------------------------------------------------------------
# CORE SYSTEM & HEALTH ENDPOINTS
# -------------------------------------------------------------
@app.get("/healthz")
@app.get("/api/v1/health")
def health_check():
    """Liveness probe for Docker, Kubernetes, and cloud load balancers."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        conn.close()
        return {"status": "healthy", "service": "Fikarta Backend Engine", "database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unhealthy: {str(e)}")

@app.get("/")
def root():
    return {"status": "active"}

@app.get("/api/v1/stats")
def get_stats():
    conn = get_db()
    cur = conn.cursor()

    counts = {}
    tables = [
        "cities", "nbfcs", "loan_apps", "calculators", "loan_amounts", "loan_kavach",
        "news_articles", "hindi_articles", "blog_articles", "tips", "comparisons",
        "glossary_terms", "lenders", "research_reports", "purposes", "salaries",
        "cibil_pages", "advisor_pages", "leads", "inquiries", "scam_reports", "subscribers"
    ]

    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{t}];")
            counts[t] = cur.fetchone()[0]
        except Exception:
            counts[t] = 0

    conn.close()

    total_programmatic = sum(counts[k] for k in counts if k not in ["leads", "inquiries", "scam_reports", "subscribers"])

    return {
        "architecture": "dynamic_pattern_routing",
        "url_table_status": "deprecated_and_dropped",
        "total_programmatic_entities": total_programmatic,
        "by_entity_type": counts,
        "submissions": {
            "leads": counts.get("leads", 0),
            "inquiries": counts.get("inquiries", 0),
            "scam_reports": counts.get("scam_reports", 0),
            "subscribers": counts.get("subscribers", 0),
        }
    }

# -------------------------------------------------------------
# DYNAMIC ROUTE RESOLVER (ZERO-URL-TABLE ARCHITECTURE)
# -------------------------------------------------------------
@app.get("/api/v1/resolve")
def resolve_route(path: str = Query(..., description="Route path or URL to resolve, e.g. /nbfc/bajaj-finance or /loan-app-safe/kreditbee")):
    clean_path = path.strip().split("?")[0].rstrip("/")
    if not clean_path.startswith("/"):
        clean_path = "/" + clean_path

    parts = [p for p in clean_path.strip("/").split("/") if p]
    if not parts:
        return {"resolved": True, "route_type": "home", "template": "Home", "slug": ""}

    prefix = parts[0].lower()
    slug = "/".join(parts[1:]) if len(parts) > 1 else ""

    conn = get_db()
    cur = conn.cursor()
    try:
        # 1. NBFC directory: /nbfc/:slug or /nbfcs/:slug
        if prefix in ["nbfc", "nbfcs"]:
            is_grievance = slug.endswith("/grievance") or prefix == "grievance"
            lookup_slug = slug.replace("/grievance", "") if is_grievance else slug
            cur.execute("SELECT * FROM nbfcs WHERE slug = ? OR slug = ?;", (lookup_slug.lower(), slug.lower()))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM nbfcs WHERE slug LIKE ? LIMIT 1;", (f"{lookup_slug.lower()}%",))
                row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["features_json", "deployed_apps_json"])
                return {"resolved": True, "route_type": "nbfc", "template": "DynamicNbfcTemplate", "slug": slug, "data": data}

        # 2. Loan App: /loan-app/:slug or /loan-app-safe/:slug or /loan-apps/:slug
        elif prefix in ["loan-app", "loan-apps", "loan-app-safe"]:
            cur.execute("SELECT * FROM loan_apps WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM loan_apps WHERE slug LIKE ? LIMIT 1;", (f"{slug.lower()}%",))
                row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["nbfc_partners_json", "pros_json", "cons_json", "rbi_records_json"])
                return {"resolved": True, "route_type": "loan_app", "template": "DynamicLoanAppTemplate", "slug": slug, "data": data}

        # 3. Calculators: /emi-calculator/:type or /calculators/:type
        elif prefix in ["emi-calculator", "calculator", "calculators", "ai-emi-calculator"]:
            cur.execute("SELECT * FROM calculators WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["faqs_json"])
                return {"resolved": True, "route_type": "calculator", "template": "DynamicCalculatorTemplate", "slug": slug, "data": data}

        # 4. News Articles: /news/:slug or /article/:slug
        elif prefix in ["news", "articles", "article"]:
            cur.execute("SELECT * FROM news_articles WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["takeaways_json", "paragraphs_json", "references_json", "related_news_json"])
                return {"resolved": True, "route_type": "news", "template": "DynamicArticlesTemplate", "slug": slug, "data": data}

        # 5. Blogs: /blog/:slug or /blogs/:slug
        elif prefix in ["blog", "blogs"]:
            cur.execute("SELECT * FROM blog_articles WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["sections_json", "faqs_json"])
                return {"resolved": True, "route_type": "blog", "template": "BlogPost", "slug": slug, "data": data}

        # 6. Hindi Hub: /hi/:slug or /hindi/:slug
        elif prefix in ["hi", "hindi"]:
            cur.execute("SELECT * FROM hindi_articles WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["takeaways_json", "paragraphs_json", "emi_table_json", "sources_json"])
                return {"resolved": True, "route_type": "hindi", "template": "DynamicHindiTemplate", "slug": slug, "data": data}

        # 7. City Personal Loan: /personal-loan/:city or /personal-loan/salary/:sal
        elif prefix == "personal-loan":
            if len(parts) > 2 and parts[1].lower() == "salary":
                sal_slug = parts[2].lower()
                cur.execute("SELECT * FROM salaries WHERE slug = ?;", (sal_slug,))
                row = cur.fetchone()
                if row:
                    data = dict(row)
                    data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
                    return {"resolved": True, "route_type": "salary", "template": "DynamicSalaryTemplate", "slug": sal_slug, "data": data}
            else:
                cur.execute("SELECT * FROM cities WHERE slug = ?;", (slug.lower(),))
                row = cur.fetchone()
                if row:
                    data = dict(row)
                    data = parse_json_fields(data, ["pincodes_json", "related_cities_json", "faqs_json"])
                    return {"resolved": True, "route_type": "city", "template": "FikartaCityPageExact", "slug": slug, "data": data}

        elif prefix in ["city", "cities"]:
            cur.execute("SELECT * FROM cities WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["pincodes_json", "related_cities_json", "faqs_json"])
                return {"resolved": True, "route_type": "city", "template": "FikartaCityPageExact", "slug": slug, "data": data}

        # 8. Loan Amount: /loan-amount/:slug or /amount/:slug
        elif prefix in ["loan-amount", "amount", "loan-amounts"]:
            cur.execute("SELECT * FROM loan_amounts WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["top_lenders_json", "emi_matrix_json", "faqs_json"])
                return {"resolved": True, "route_type": "amount", "template": "DynamicAmountTemplate", "slug": slug, "data": data}

        # 9. Loan Kavach: /loan-kavach/:slug or /kavach/:slug
        elif prefix in ["loan-kavach", "kavach"]:
            cur.execute("SELECT * FROM loan_kavach WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["rights_json", "escalation_steps_json", "evidence_checklist_json", "faqs_json"])
                return {"resolved": True, "route_type": "kavach", "template": "DynamicKavachTemplate", "slug": slug, "data": data}

        # 10. Tips: /tips/:slug or /tip/:slug
        elif prefix in ["tips", "tip"]:
            cur.execute("SELECT * FROM tips WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["action_steps_json"])
                return {"resolved": True, "route_type": "tip", "template": "TipsPage", "slug": slug, "data": data}

        # 11. Glossary: /glossary/:slug
        elif prefix == "glossary":
            cur.execute("SELECT * FROM glossary_terms WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["related_terms_json", "faqs_json"])
                return {"resolved": True, "route_type": "glossary", "template": "Glossary", "slug": slug, "data": data}

        # 12. Lenders: /lenders/:slug or /lender/:slug
        elif prefix in ["lenders", "lender"]:
            cur.execute("SELECT * FROM lenders WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["pros_json", "cons_json", "features_json", "faqs_json", "similar_lenders_json", "compare_pairs_json"])
                return {"resolved": True, "route_type": "lender", "template": "DynamicLenderTemplate", "slug": slug, "data": data}

        # 13. Loan Purposes: /personal-loan-for/:slug or /purpose/:slug or /purposes/:slug
        elif prefix in ["personal-loan-for", "purpose", "purposes", "loans-for"]:
            cur.execute("SELECT * FROM purposes WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
                return {"resolved": True, "route_type": "purpose", "template": "DynamicPurposeTemplate", "slug": slug, "data": data}

        # 14. Salaries: /salary/:slug or /salaries/:slug
        elif prefix in ["salary", "salaries", "salary-loan"]:
            cur.execute("SELECT * FROM salaries WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
                return {"resolved": True, "route_type": "salary", "template": "DynamicSalaryTemplate", "slug": slug, "data": data}

        # 15. CIBIL Score Pages: /cibil-score/:slug or /cibil/:slug
        elif prefix in ["cibil-score", "cibil", "credit-score"]:
            cur.execute("SELECT * FROM cibil_pages WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
                return {"resolved": True, "route_type": "cibil", "template": "DynamicCibilTemplate", "slug": slug, "data": data}

        # 16. Fikarta AI Advisor Pages: /ai-advisor/:slug or /advisor/:slug or /tara/:slug
        elif prefix in ["ai-advisor", "advisor", "tara"]:
            cur.execute("SELECT * FROM advisor_pages WHERE slug = ? OR slug = ?;", (slug.lower(), f"ai-advisor/{slug.lower()}"))
            row = cur.fetchone()
            if not row and "/" in slug:
                cur.execute("SELECT * FROM advisor_pages WHERE slug = ?;", (slug.lower(),))
                row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
                return {"resolved": True, "route_type": "advisor", "template": "DynamicAdvisorTemplate", "slug": slug, "data": data}

        # 17. Lender Comparisons: /compare/:slug or /comparisons/:slug
        elif prefix in ["compare", "comparisons", "comparison"]:
            cur.execute("SELECT * FROM comparisons WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["paragraphs_json", "faqs_json"])
                return {"resolved": True, "route_type": "comparison", "template": "DynamicComparisonTemplate", "slug": slug, "data": data}

        # 18. Research Reports: /research/:slug or /reports/:slug
        elif prefix in ["research", "reports", "report"]:
            cur.execute("SELECT * FROM research_reports WHERE slug = ?;", (slug.lower(),))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data = parse_json_fields(data, ["sections_json", "key_findings_json", "citations_json"])
                return {"resolved": True, "route_type": "research", "template": "DynamicResearchTemplate", "slug": slug, "data": data}

        raise HTTPException(status_code=404, detail=f"Route path '{path}' could not be dynamically resolved.")
    finally:
        conn.close()

# -------------------------------------------------------------
# ROTATOR AND OUTBOUND TRAFFIC NORMALIZER (0ms FAST PATH)
# -------------------------------------------------------------
@app.get("/r/{slug}")
@app.get("/out/{slug}")
def url_rotator(slug: str):
    clean_slug = slug.lower().strip()

    # Fast path: Lenders and Banks -> Instant 302 to pre-approved eligibility flow
    if clean_slug.endswith("-bank") or any(k in clean_slug for k in ["sbi", "hdfc", "icici", "axis", "kotak", "iifl", "bob", "pnb", "bajaj", "tata", "muthoot"]):
        return RedirectResponse(url=f"/personal-loan/eligibility?lender={clean_slug}&utm_source=fikarta_rotator&utm_medium=referral", status_code=302)

    # Fast path: Calculators -> Instant 302
    if any(k in clean_slug for k in ["calculator", "emi", "apr", "prepay", "fd-vs-loan", "balance-transfer"]):
        return RedirectResponse(url=f"/emi-calculator/{clean_slug}?utm_source=fikarta_rotator&utm_medium=referral", status_code=302)

    # DB fast lookup with early return
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT slug FROM loan_apps WHERE slug = ? LIMIT 1;", (clean_slug,))
    if cur.fetchone():
        conn.close()
        return RedirectResponse(url=f"/loan-app-safe/{clean_slug}?utm_source=fikarta_rotator&utm_medium=referral", status_code=302)

    cur.execute("SELECT slug FROM nbfcs WHERE slug = ? LIMIT 1;", (clean_slug,))
    if cur.fetchone():
        conn.close()
        return RedirectResponse(url=f"/nbfc/{clean_slug}?utm_source=fikarta_rotator&utm_medium=referral", status_code=302)

    conn.close()
    return RedirectResponse(url=f"/loan-app-safe/{clean_slug}?utm_source=fikarta_rotator&utm_medium=referral", status_code=302)

# -------------------------------------------------------------
# UNIVERSAL SEARCH API (CROSS-ENTITY FAST QUERY)
# -------------------------------------------------------------
@app.get("/api/v1/search")
def universal_search(
    q: str = Query(..., min_length=2, description="Search keyword or term"),
    type: Optional[str] = Query(None, description="Optional entity type filter (e.g. nbfc, loan_app, calculator, city, news, hindi, blog, kavach, glossary, lender, purpose, salary, advisor, comparison)"),
    limit: int = Query(50, ge=1, le=200, description="Maximum total results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    keyword = f"%{q.strip().lower()}%"
    conn = get_db()
    cur = conn.cursor()
    results = []

    try:
        # Search NBFCs
        if not type or type in ["nbfc", "nbfcs"]:
            cur.execute("""
                SELECT slug, name, legal_name, cor_number, category, rating 
                FROM nbfcs 
                WHERE LOWER(name) LIKE ? OR LOWER(legal_name) LIKE ? OR LOWER(cor_number) LIKE ? OR LOWER(category) LIKE ?
                LIMIT 20;
            """, (keyword, keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "nbfc",
                    "title": r["name"],
                    "subtitle": f"{r['category']} • CoR: {r['cor_number']}",
                    "slug": r["slug"],
                    "url": f"/nbfc/{r['slug']}",
                    "rating": r["rating"],
                })

        # Search Loan Apps
        if not type or type in ["loan_app", "loan_apps", "app"]:
            cur.execute("""
                SELECT slug, name, tagline, developer, rating, downloads, status_verdict 
                FROM loan_apps 
                WHERE LOWER(name) LIKE ? OR LOWER(developer) LIKE ? OR LOWER(tagline) LIKE ?
                LIMIT 20;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "loan_app",
                    "title": r["name"],
                    "subtitle": f"{r['developer']} • {r['tagline'] or ''}".strip(" •"),
                    "slug": r["slug"],
                    "url": f"/loan-app-safe/{r['slug']}",
                    "rating": r["rating"],
                    "verdict": r["status_verdict"],
                })

        # Search Calculators
        if not type or type in ["calculator", "calculators", "calc"]:
            cur.execute("""
                SELECT slug, name, calc_type 
                FROM calculators 
                WHERE LOWER(name) LIKE ? OR LOWER(calc_type) LIKE ?
                LIMIT 15;
            """, (keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "calculator",
                    "title": r["name"],
                    "subtitle": f"Type: {r['calc_type']}",
                    "slug": r["slug"],
                    "url": f"/emi-calculator/{r['slug']}",
                })

        # Search Cities
        if not type or type in ["city", "cities"]:
            cur.execute("""
                SELECT slug, name, state, tier 
                FROM cities 
                WHERE LOWER(name) LIKE ? OR LOWER(state) LIKE ?
                LIMIT 15;
            """, (keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "city",
                    "title": f"Personal Loan in {r['name']}",
                    "subtitle": f"{r['state']} • Tier {r['tier']}",
                    "slug": r["slug"],
                    "url": f"/personal-loan/{r['slug']}",
                })

        # Search Loan Amounts
        if not type or type in ["amount", "loan_amounts"]:
            cur.execute("""
                SELECT slug, amount_num, title, kicker 
                FROM loan_amounts 
                WHERE LOWER(title) LIKE ? OR LOWER(kicker) LIKE ? OR CAST(amount_num AS TEXT) LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "amount",
                    "title": r["title"],
                    "subtitle": r["kicker"],
                    "slug": r["slug"],
                    "url": f"/loan-amount/{r['slug']}",
                })

        # Search Loan Kavach
        if not type or type in ["kavach", "loan_kavach"]:
            cur.execute("""
                SELECT slug, category, title, kicker 
                FROM loan_kavach 
                WHERE LOWER(title) LIKE ? OR LOWER(kicker) LIKE ? OR LOWER(category) LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "loan_kavach",
                    "title": r["title"],
                    "subtitle": f"{r['category']} • {r['kicker']}",
                    "slug": r["slug"],
                    "url": f"/loan-kavach/{r['slug']}",
                })

        # Search News Articles
        if not type or type in ["news", "news_articles"]:
            cur.execute("""
                SELECT slug, title, kicker, category, published_date 
                FROM news_articles 
                WHERE LOWER(title) LIKE ? OR LOWER(kicker) LIKE ? OR LOWER(category) LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "news",
                    "title": r["title"],
                    "subtitle": f"{r['category']} • {r['published_date']}",
                    "slug": r["slug"],
                    "url": f"/news/{r['slug']}",
                })

        # Search Hindi Hub
        if not type or type in ["hindi", "hindi_articles"]:
            cur.execute("""
                SELECT slug, title, kicker, topic 
                FROM hindi_articles 
                WHERE LOWER(title) LIKE ? OR LOWER(kicker) LIKE ? OR LOWER(topic) LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "hindi",
                    "title": r["title"],
                    "subtitle": r["topic"],
                    "slug": r["slug"],
                    "url": f"/hindi/{r['slug']}",
                })

        # Search Blogs
        if not type or type in ["blog", "blogs", "blog_articles"]:
            cur.execute("""
                SELECT slug, title, kicker, category, read_time 
                FROM blog_articles 
                WHERE LOWER(title) LIKE ? OR LOWER(kicker) LIKE ? OR LOWER(category) LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "blog",
                    "title": r["title"],
                    "subtitle": f"{r['category']} • {r['read_time']}",
                    "slug": r["slug"],
                    "url": f"/blog/{r['slug']}",
                })

        # Search Glossary
        if not type or type in ["glossary", "glossary_terms"]:
            cur.execute("""
                SELECT slug, term, acronym, short_def 
                FROM glossary_terms 
                WHERE LOWER(term) LIKE ? OR LOWER(acronym) LIKE ? OR LOWER(short_def) LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "glossary",
                    "title": r["term"],
                    "subtitle": f"({r['acronym']}) {r['short_def'][:90]}..." if r["acronym"] else f"{r['short_def'][:90]}...",
                    "slug": r["slug"],
                    "url": f"/glossary/{r['slug']}",
                })

        # Search Lenders
        if not type or type in ["lender", "lenders"]:
            cur.execute("""
                SELECT slug, name, title, category, rating 
                FROM lenders 
                WHERE LOWER(name) LIKE ? OR LOWER(title) LIKE ? OR LOWER(category) LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "lender",
                    "title": r["name"],
                    "subtitle": f"{r['title']} • {r['category']}",
                    "slug": r["slug"],
                    "url": f"/lenders/{r['slug']}",
                    "rating": r["rating"],
                })

        # Search Purposes
        if not type or type in ["purpose", "purposes"]:
            cur.execute("""
                SELECT slug, title, desc 
                FROM purposes 
                WHERE LOWER(title) LIKE ? OR LOWER(desc) LIKE ?
                LIMIT 15;
            """, (keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "purpose",
                    "title": r["title"],
                    "subtitle": f"{r['desc'][:90]}...",
                    "slug": r["slug"],
                    "url": f"/personal-loan-for/{r['slug']}",
                })

        # Search Salaries
        if not type or type in ["salary", "salaries"]:
            cur.execute("""
                SELECT slug, title, desc 
                FROM salaries 
                WHERE LOWER(title) LIKE ? OR LOWER(desc) LIKE ? OR slug LIKE ?
                LIMIT 15;
            """, (keyword, keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "salary",
                    "title": r["title"],
                    "subtitle": f"{r['desc'][:90]}...",
                    "slug": r["slug"],
                    "url": f"/personal-loan/salary/{r['slug']}",
                })

        # Search CIBIL Pages
        if not type or type in ["cibil", "cibil_pages"]:
            cur.execute("""
                SELECT slug, title, desc 
                FROM cibil_pages 
                WHERE LOWER(title) LIKE ? OR LOWER(desc) LIKE ?
                LIMIT 15;
            """, (keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "cibil",
                    "title": r["title"],
                    "subtitle": f"{r['desc'][:90]}...",
                    "slug": r["slug"],
                    "url": f"/cibil-score/{r['slug']}",
                })

        # Search Comparisons
        if not type or type in ["compare", "comparisons"]:
            cur.execute("""
                SELECT slug, title, verdict 
                FROM comparisons 
                WHERE LOWER(title) LIKE ? OR LOWER(verdict) LIKE ?
                LIMIT 15;
            """, (keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "comparison",
                    "title": r["title"],
                    "subtitle": f"{r['verdict'][:90]}...",
                    "slug": r["slug"],
                    "url": f"/compare/{r['slug']}",
                })

        # Search Advisor Pages
        if not type or type in ["advisor", "advisor_pages", "ai-advisor"]:
            cur.execute("""
                SELECT slug, title, desc 
                FROM advisor_pages 
                WHERE LOWER(title) LIKE ? OR LOWER(desc) LIKE ?
                LIMIT 15;
            """, (keyword, keyword))
            for r in cur.fetchall():
                results.append({
                    "type": "advisor",
                    "title": r["title"],
                    "subtitle": f"{r['desc'][:90]}...",
                    "slug": r["slug"],
                    "url": f"/ai-advisor/{r['slug']}",
                })

    finally:
        conn.close()

    total_matches = len(results)
    paginated_results = results[offset : offset + limit]

    return {
        "query": q,
        "type_filter": type or "all",
        "total": total_matches,
        "count": len(paginated_results),
        "offset": offset,
        "limit": limit,
        "results": paginated_results,
    }

# -------------------------------------------------------------
# INTERACTIVE SUBMISSION APIS (LEADS, CONTACT, SCAM, NEWSLETTER)
# -------------------------------------------------------------
@app.post("/api/v1/leads", status_code=status.HTTP_201_CREATED)
def submit_lead(lead: LeadSubmission, request: Request):
    """
    Captures loan inquiry lead from eligibility calculators and preview forms.
    Validates Indian 10-digit mobile number and records IP & User-Agent for audit compliance.
    """
    clean_phone = re.sub(r"\D", "", lead.phone)
    if len(clean_phone) == 12 and clean_phone.startswith("91"):
        clean_phone = clean_phone[2:]
    if not re.match(r"^[6-9]\d{9}$", clean_phone):
        raise HTTPException(
            status_code=422,
            detail="Invalid Indian mobile number. Must be a valid 10-digit number starting with 6, 7, 8, or 9."
        )

    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO leads (
            name, phone, loan_amount, tenure_months, purpose,
            employment_type, monthly_income, city, cibil_score_band,
            consent, ip_address, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        lead.name,
        clean_phone,
        lead.loan_amount,
        lead.tenure_months,
        lead.purpose,
        lead.employment_type,
        lead.monthly_income,
        lead.city,
        lead.cibil_score_band,
        1 if lead.consent else 0,
        client_ip,
        user_agent,
    ))
    lead_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "lead_id": lead_id,
        "reference_code": f"FK-LD-{lead_id:06d}",
        "message": "Loan inquiry received. Pre-approved match evaluation initiated with zero credit score impact.",
        "masked_phone": f"+91 {clean_phone[:2]}*****{clean_phone[7:]}",
    }

@app.get("/api/v1/leads")
def list_leads(limit: int = Query(50, le=100), offset: int = Query(0, ge=0)):
    """Admin inspection endpoint for captured leads."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, phone, loan_amount, tenure_months, purpose,
               employment_type, monthly_income, city, cibil_score_band, created_at
        FROM leads 
        ORDER BY id DESC 
        LIMIT ? OFFSET ?;
    """, (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "items": rows}

@app.post("/api/v1/contact", status_code=status.HTTP_201_CREATED)
def submit_contact(inquiry: ContactSubmission, request: Request):
    """
    Submits user inquiries to Fikarta Official Helpdesk and Grievance Desk.
    """
    clean_mobile = re.sub(r"\D", "", inquiry.mobile)
    if len(clean_mobile) == 12 and clean_mobile.startswith("91"):
        clean_mobile = clean_mobile[2:]
    if not re.match(r"^[6-9]\d{9}$", clean_mobile):
        raise HTTPException(
            status_code=422,
            detail="Invalid contact mobile number. Must be 10 digits starting with 6, 7, 8, or 9."
        )

    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO inquiries (name, mobile, email, topic, message, ip_address, user_agent)
        VALUES (?, ?, ?, ?, ?, ?, ?);
    """, (
        inquiry.name.strip(),
        clean_mobile,
        str(inquiry.email).lower().strip(),
        inquiry.topic.strip(),
        inquiry.message.strip(),
        client_ip,
        user_agent,
    ))
    inq_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "ticket_id": f"FK-INQ-{inq_id:06d}",
        "message": "Inquiry recorded successfully. Fikarta Helpdesk will respond via email/SMS within 24 business hours.",
        "data": {
            "name": inquiry.name,
            "email": str(inquiry.email),
            "topic": inquiry.topic,
        }
    }

@app.post("/api/v1/report-scam", status_code=status.HTTP_201_CREATED)
def report_scam(report: ScamReportSubmission):
    """
    Logs suspicious or predatory lending applications into Fikarta Anti-Scam Registry.
    Cross-checks against RBI CoR database and triggers public audit warning flag.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO scam_reports (app_name, apk_url, issue_type, description, complainant_phone)
        VALUES (?, ?, ?, ?, ?);
    """, (
        report.app_name.strip(),
        report.apk_url.strip() if report.apk_url else None,
        report.issue_type.strip(),
        report.description.strip() if report.description else None,
        report.complainant_phone.strip() if report.complainant_phone else None,
    ))
    report_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "report_id": f"FK-SCAM-{report_id:05d}",
        "app_name": report.app_name,
        "message": "Predatory lending app report logged successfully. CoR verification check has been scheduled.",
    }

@app.get("/api/v1/scam-reports")
def list_scam_reports(limit: int = Query(50, le=100), offset: int = Query(0, ge=0)):
    """Public transparency feed of audited and reported predatory lending apps."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, app_name, apk_url, issue_type, description, status, created_at
        FROM scam_reports 
        ORDER BY id DESC 
        LIMIT ? OFFSET ?;
    """, (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "items": rows}

@app.post("/api/v1/subscribe", status_code=status.HTTP_201_CREATED)
def subscribe_newsletter(sub: NewsletterSubscription):
    """Borrower protection and financial intelligence newsletter subscription."""
    conn = get_db()
    cur = conn.cursor()
    clean_email = str(sub.email).lower().strip()
    cur.execute("""
        INSERT OR IGNORE INTO subscribers (email, source)
        VALUES (?, ?);
    """, (clean_email, sub.source or "website"))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "email": clean_email,
        "message": "Subscribed to Fikarta Borrower Protection & Financial Strategy alerts.",
    }

# -------------------------------------------------------------
# PROGRAMMATIC ENTITY APIS
# -------------------------------------------------------------

# 1. CITIES
@app.get("/api/v1/cities")
def list_cities(
    q: Optional[str] = Query(None, description="Search city name or state"),
    state: Optional[str] = Query(None, description="Filter by state"),
    tier: Optional[int] = Query(None, description="Filter by city tier (1, 2, 3)"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, name, state, tier, population, lowest_rate, max_amount, avg_loan FROM cities WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(name) LIKE ? OR LOWER(state) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%"])
    if state:
        query += " AND LOWER(state) = ?"
        params.append(state.lower())
    if tier:
        query += " AND tier = ?"
        params.append(tier)
    query += " ORDER BY population DESC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/cities/{slug}")
def get_city(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cities WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"City '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["pincodes_json", "related_cities_json", "faqs_json"])
    cur.execute(
        """SELECT slug, name, type, category, rating, rate_range, min_rate, max_rate, amount_range,
                  min_loan_num, max_loan_num, tenure_range, fee_range, prepayment_charge,
                  min_cibil, min_cibil_num, min_salary, disbursal_speed, best_for, color, short
           FROM lenders ORDER BY COALESCE(min_rate, 99) ASC;"""
    )
    data["eligible_lenders"] = [dict(r) for r in cur.fetchall()]
    conn.close()
    return data

# 2. NBFCS
@app.get("/api/v1/nbfc")
@app.get("/api/v1/nbfcs")
def list_nbfcs(
    q: Optional[str] = Query(None, description="Search by name, legal name, or CoR"),
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, name, legal_name, cor_number, category, rating, rate_range, fee_range, max_loan, tenure_range FROM nbfcs WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(name) LIKE ? OR LOWER(legal_name) LIKE ? OR LOWER(cor_number) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.lower()}%"])
    if category:
        query += " AND LOWER(category) = ?"
        params.append(category.lower())
    query += " ORDER BY rating DESC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/nbfc/{slug}")
@app.get("/api/v1/nbfcs/{slug}")
def get_nbfc(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM nbfcs WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"NBFC '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["features_json", "deployed_apps_json"])
    return data

# 3. LOAN APPS
@app.get("/api/v1/loan-apps")
def list_loan_apps(
    q: Optional[str] = Query(None, description="Search by app name or developer"),
    verdict: Optional[str] = Query(None, description="Filter by status verdict (safe, caution, etc.)"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, name, tagline, developer, downloads, rating, min_amount, max_amount, apr_range, status_verdict FROM loan_apps WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(name) LIKE ? OR LOWER(developer) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%"])
    if verdict:
        query += " AND LOWER(status_verdict) = ?"
        params.append(verdict.lower())
    query += " ORDER BY rating DESC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/loan-app/{slug}")
@app.get("/api/v1/loan-apps/{slug}")
@app.get("/api/v1/loan-app-safe/{slug}")
def get_loan_app(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM loan_apps WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Loan app '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["nbfc_partners_json", "pros_json", "cons_json", "rbi_records_json"])
    return data

# 4. CALCULATORS
@app.get("/api/v1/calculators")
def list_calculators(
    q: Optional[str] = Query(None, description="Search calculator name or calc_type"),
    calc_type: Optional[str] = Query(None, description="Filter by calculator type"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, name, calc_type, default_rate, default_amount, default_tenure, processing_fee FROM calculators WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(name) LIKE ? OR LOWER(calc_type) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%"])
    if calc_type:
        query += " AND LOWER(calc_type) = ?"
        params.append(calc_type.lower())
    query += " ORDER BY name ASC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/calculators/{slug}")
def get_calculator(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM calculators WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Calculator '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["faqs_json"])
    return data

# 5. LOAN AMOUNTS
@app.get("/api/v1/loan-amounts")
def list_loan_amounts(limit: int = Query(100, le=300), offset: int = Query(0, ge=0)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT slug, amount_num, title, kicker, lowest_rate, best_tenure FROM loan_amounts ORDER BY amount_num ASC LIMIT ? OFFSET ?;", (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/loan-amounts/{slug}")
def get_loan_amount(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM loan_amounts WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Loan Amount page '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["top_lenders_json", "emi_matrix_json", "faqs_json"])
    amt = data.get("amount_num") or 100000
    cur.execute(
        """SELECT slug, name, type, rating, rate_range, min_rate, max_rate, amount_range,
                  min_loan_num, max_loan_num, tenure_range, fee_range, prepayment_charge,
                  min_cibil, min_cibil_num, min_salary, disbursal_speed, best_for, color, short
           FROM lenders WHERE min_loan_num <= ? AND max_loan_num >= ?
           ORDER BY COALESCE(min_rate, 99) ASC;""",
        (amt, amt)
    )
    data["eligible_lenders"] = [dict(r) for r in cur.fetchall()]
    conn.close()
    return data

# 6. LOAN KAVACH
@app.get("/api/v1/loan-kavach")
def list_loan_kavach(category: Optional[str] = Query(None, description="Filter by category")):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, category, title, kicker, tldr, legal_framework FROM loan_kavach WHERE 1=1"
    params = []
    if category:
        query += " AND LOWER(category) = ?"
        params.append(category.lower())
    query += " ORDER BY id ASC;"
    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "items": rows}

@app.get("/api/v1/loan-kavach/{slug}")
def get_loan_kavach(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM loan_kavach WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Loan Kavach guide '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["rights_json", "escalation_steps_json", "evidence_checklist_json", "faqs_json"])
    return data

# 7. NEWS ARTICLES
@app.get("/api/v1/news")
def list_news(
    q: Optional[str] = Query(None, description="Search news title or summary"),
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, title, kicker, category, read_time, published_date, summary FROM news_articles WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(title) LIKE ? OR LOWER(summary) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%"])
    if category:
        query += " AND LOWER(category) = ?"
        params.append(category.lower())
    query += " ORDER BY id DESC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/news/{slug}")
def get_news(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM news_articles WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"News article '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["takeaways_json", "paragraphs_json", "references_json", "related_news_json"])
    return data

# 8. HINDI HUB
@app.get("/api/v1/hindi")
def list_hindi(
    q: Optional[str] = Query(None, description="Search Hindi article title or topic"),
    topic: Optional[str] = Query(None, description="Filter by topic"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, title, kicker, desc, topic FROM hindi_articles WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(title) LIKE ? OR LOWER(desc) LIKE ? OR LOWER(topic) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.lower()}%"])
    if topic:
        query += " AND LOWER(topic) = ?"
        params.append(topic.lower())
    query += " ORDER BY id DESC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/hindi/{slug}")
def get_hindi(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM hindi_articles WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Hindi article '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["takeaways_json", "paragraphs_json", "emi_table_json", "sources_json"])
    return data

# 9. BLOG ARTICLES
@app.get("/api/v1/blog")
@app.get("/api/v1/blogs")
def list_blogs(
    q: Optional[str] = Query(None, description="Search blog title or summary"),
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, title, kicker, category, author, read_time, published_date, summary FROM blog_articles WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(title) LIKE ? OR LOWER(summary) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%"])
    if category:
        query += " AND LOWER(category) = ?"
        params.append(category.lower())
    query += " ORDER BY id DESC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/blog/{slug}")
def get_blog(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM blog_articles WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Blog article '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["sections_json", "faqs_json"])
    return data

# 10. TIPS
@app.get("/api/v1/tips")
def list_tips(
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, title, kicker, category, quick_tip, explanation FROM tips WHERE 1=1"
    params = []
    if category:
        query += " AND LOWER(category) = ?"
        params.append(category.lower())
    query += " ORDER BY id ASC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/tips/{slug}")
def get_tip(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tips WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Tip '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["action_steps_json"])
    return data

# 11. GLOSSARY TERMS
@app.get("/api/v1/glossary")
def list_glossary(
    q: Optional[str] = Query(None, description="Search glossary terms or acronyms"),
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(100, le=200),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, term, acronym, category, short_def, calculation_example, borrower_impact FROM glossary_terms WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(term) LIKE ? OR LOWER(acronym) LIKE ? OR LOWER(short_def) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.lower()}%"])
    if category:
        query += " AND LOWER(category) = ?"
        params.append(category.lower())
    query += " ORDER BY term ASC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/glossary/{slug}")
def get_glossary_term(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM glossary_terms WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Glossary term '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["related_terms_json", "faqs_json"])
    return data

# 12. LENDERS
@app.get("/api/v1/lenders")
def list_lenders(
    category: Optional[str] = Query(None, description="Filter by category"),
    min_salary: Optional[int] = Query(None, description="Filter lenders accepting this monthly salary"),
    max_cibil: Optional[int] = Query(None, description="Filter lenders accepting this CIBIL score"),
    amount: Optional[int] = Query(None, description="Filter lenders offering this loan amount"),
):
    conn = get_db()
    cur = conn.cursor()
    query = """SELECT id, slug, name, title, tagline, category, type, rating, rate_range, min_rate, max_rate,
                      amount_range, min_loan_num, max_loan_num, tenure_range, fee_range, prepayment_charge,
                      min_cibil, min_cibil_num, min_salary, disbursal_speed, age_range, employment,
                      best_for, special_offer, pros_json, cons_json, color, short, show_in_salary_matrix
               FROM lenders WHERE 1=1"""
    params = []
    if category:
        query += " AND (LOWER(category) = ? OR LOWER(type) = ?)"
        params.extend([category.lower(), category.lower()])
    if min_salary is not None:
        query += " AND show_in_salary_matrix = 1 AND min_salary <= ?"
        params.append(min_salary)
    if max_cibil is not None:
        query += " AND min_cibil_num <= ?"
        params.append(max_cibil)
    if amount is not None:
        query += " AND min_loan_num <= ? AND max_loan_num >= ?"
        params.extend([amount, amount])
    query += " ORDER BY COALESCE(min_rate, 99) ASC, rating DESC;"
    cur.execute(query, tuple(params))
    rows = [parse_json_fields(dict(r), ["pros_json", "cons_json"]) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "items": rows}

@app.get("/api/v1/lenders/{slug}")
def get_lender(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM lenders WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Lender '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["pros_json", "cons_json", "features_json", "faqs_json", "similar_lenders_json", "compare_pairs_json"])
    return data

# 13. RESEARCH REPORTS
@app.get("/api/v1/research")
@app.get("/api/v1/reports")
def list_research():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT slug, title, kicker, read_time, published_date, summary FROM research_reports ORDER BY id DESC;")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "items": rows}

@app.get("/api/v1/research/{slug}")
def get_research_report(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM research_reports WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Research report '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["sections_json", "key_findings_json", "citations_json"])
    return data

# 14. LOAN PURPOSES
@app.get("/api/v1/purposes")
def list_purposes(
    q: Optional[str] = Query(None, description="Search loan purpose by title or description"),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, url, title, desc, updated_at FROM purposes WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(title) LIKE ? OR LOWER(desc) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%"])
    query += " ORDER BY title ASC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/purposes/{slug}")
def get_purpose(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM purposes WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Loan purpose '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
    return data

# 15. SALARY BRACKETS
@app.get("/api/v1/salaries")
def list_salaries(limit: int = Query(50, le=100), offset: int = Query(0, ge=0)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT slug, url, title, desc, updated_at FROM salaries ORDER BY id ASC LIMIT ? OFFSET ?;", (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/salaries/{slug}")
def get_salary(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM salaries WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Salary guide '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
    amt = data.get("amount_num") or 25000
    cur.execute(
        """SELECT slug, name, type, category, rating, rate_range, min_rate, max_rate, amount_range,
                  min_loan_num, max_loan_num, tenure_range, fee_range, prepayment_charge,
                  min_cibil, min_cibil_num, min_salary, disbursal_speed, best_for, color, short
           FROM lenders WHERE show_in_salary_matrix = 1 AND min_salary <= ?
           ORDER BY COALESCE(min_rate, 99) ASC;""",
        (amt,)
    )
    data["eligible_lenders"] = [dict(r) for r in cur.fetchall()]
    conn.close()
    return data

# 16. CIBIL SCORE GUIDES
@app.get("/api/v1/cibil")
def list_cibil_pages(limit: int = Query(50, le=100), offset: int = Query(0, ge=0)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT slug, url, title, desc, updated_at FROM cibil_pages ORDER BY id ASC LIMIT ? OFFSET ?;", (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/cibil/{slug}")
def get_cibil_page(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cibil_pages WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"CIBIL score guide '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
    clean = slug.lower()
    m = re.search(r"(\d+)", clean)
    score = 450 if ("below-500" in clean or "400" in clean) else (int(m.group(1)) if m else 750)
    cur.execute(
        """SELECT slug, name, type, category, rating, rate_range, min_rate, max_rate, amount_range,
                  min_loan_num, max_loan_num, tenure_range, fee_range, prepayment_charge,
                  min_cibil, min_cibil_num, min_salary, disbursal_speed, best_for, color, short
           FROM lenders WHERE COALESCE(min_cibil_num, 0) <= ?
           ORDER BY COALESCE(min_rate, 99) ASC;""",
        (score,)
    )
    data["eligible_lenders"] = [dict(r) for r in cur.fetchall()]
    conn.close()
    return data

# 17. FIKARTA AI ADVISOR PAGES
@app.get("/api/v1/advisor")
@app.get("/api/v1/ai-advisor")
def list_advisor_pages(limit: int = Query(50, le=100), offset: int = Query(0, ge=0)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT slug, url, title, desc, updated_at FROM advisor_pages ORDER BY id ASC LIMIT ? OFFSET ?;", (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/advisor/{slug:path}")
@app.get("/api/v1/ai-advisor/{slug:path}")
def get_advisor_page(slug: str):
    clean_slug = slug.strip().lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM advisor_pages WHERE slug = ? OR slug = ?;", (clean_slug, f"ai-advisor/{clean_slug}"))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Advisor page '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["h2s_json", "paragraphs_json", "faqs_json"])
    return data

# 18. LENDER COMPARISONS
@app.get("/api/v1/comparisons")
def list_comparisons(
    q: Optional[str] = Query(None, description="Search comparison by lender names"),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0)
):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT slug, url, title, lender_a_slug, lender_b_slug, verdict, updated_at FROM comparisons WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(title) LIKE ? OR LOWER(lender_a_slug) LIKE ? OR LOWER(lender_b_slug) LIKE ?)"
        params.extend([f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.lower()}%"])
    query += " ORDER BY id ASC LIMIT ? OFFSET ?;"
    params.extend([limit, offset])

    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows}

@app.get("/api/v1/compare/{slug}")
@app.get("/api/v1/comparisons/{slug}")
def get_comparison(slug: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM comparisons WHERE slug = ?;", (slug.lower(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Comparison '{slug}' not found")
    data = dict(row)
    data = parse_json_fields(data, ["paragraphs_json", "faqs_json"])

    # Optionally enrich with lender_a and lender_b full metrics
    cur.execute("SELECT * FROM lenders WHERE slug = ?;", (data.get("lender_a_slug"),))
    lender_a = cur.fetchone()
    if lender_a:
        data["lender_a"] = parse_json_fields(dict(lender_a), ["pros_json", "cons_json"])

    cur.execute("SELECT * FROM lenders WHERE slug = ?;", (data.get("lender_b_slug"),))
    lender_b = cur.fetchone()
    if lender_b:
        data["lender_b"] = parse_json_fields(dict(lender_b), ["pros_json", "cons_json"])

    conn.close()
    return data

# -------------------------------------------------------------
# 100% WHITE-LABEL HEADLESS CONTENT PROXY
# Keeps Ghost and WordPress completely private inside your server.
# Visitors and crawlers only see https://api.fikarta.money/api/v1/content/...
# -------------------------------------------------------------
INTERNAL_GHOST_URL = os.getenv("INTERNAL_GHOST_URL", "http://127.0.0.1:2368").rstrip("/")
INTERNAL_GHOST_KEY = os.getenv("INTERNAL_GHOST_KEY", "")
INTERNAL_WP_URL = os.getenv("INTERNAL_WP_URL", "http://127.0.0.1:8080/wp-json/wp/v2").rstrip("/")

@app.get("/api/v1/content/blog/{slug}")
def proxy_blog_content(slug: str):
    """
    White-label blog proxy:
    Fetches content from internal headless engine with zero external trace.
    """
    clean_slug = slug.lower().strip()
    
    # 1. Check internal Ghost
    if INTERNAL_GHOST_KEY:
        try:
            import requests
            url = f"{INTERNAL_GHOST_URL}/ghost/api/content/posts/slug/{clean_slug}/?key={INTERNAL_GHOST_KEY}&include=tags,authors"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                posts = res.json().get("posts", [])
                if posts:
                    p = posts[0]
                    raw_html = p.get("html") or ""
                    clean_html = (
                        raw_html.replace("https://fikarta.ghost.io/", "/blog/")
                        .replace("https://fikarta.ghost.io", "")
                        .replace("Powered by Ghost", "Fikarta Research")
                        .replace("Published with Ghost", "Fikarta Editorial")
                    )
                    raw_summary = p.get("custom_excerpt") or p.get("excerpt") or ""
                    clean_summary = raw_summary.replace("https://fikarta.ghost.io", "").strip()
                    return {
                        "slug": p.get("slug"),
                        "title": p.get("title"),
                        "kicker": (p.get("primary_tag") or {}).get("name", "Credit Advisory"),
                        "category": (p.get("primary_tag") or {}).get("name", "Credit Strategy"),
                        "author": (p.get("primary_author") or {}).get("name", "Fikarta Editorial"),
                        "read_time": f"{p.get('reading_time', 5)} min read",
                        "summary": clean_summary,
                        "html": clean_html,
                        "published_date": "2026",
                    }
        except Exception:
            pass

    # 2. Check internal WordPress
    if INTERNAL_WP_URL:
        try:
            import requests
            url = f"{INTERNAL_WP_URL}/posts?slug={clean_slug}&_embed"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                posts = res.json()
                if isinstance(posts, list) and len(posts) > 0:
                    p = posts[0]
                    return {
                        "slug": p.get("slug"),
                        "title": (p.get("title") or {}).get("rendered", ""),
                        "kicker": "Editorial Advisory",
                        "category": "Credit Strategy",
                        "author": "Fikarta Editorial",
                        "read_time": "5 min read",
                        "summary": (p.get("excerpt") or {}).get("rendered", ""),
                        "html": (p.get("content") or {}).get("rendered", ""),
                        "published_date": "2026",
                    }
        except Exception:
            pass

    raise HTTPException(status_code=404, detail=f"Blog article '{clean_slug}' not found")

@app.get("/api/v1/content/news/{slug}")
def proxy_news_content(slug: str):
    """
    White-label news proxy:
    Fetches news from internal headless engine with zero external trace.
    """
    clean_slug = slug.lower().strip()
    
    # 1. Check internal WordPress
    if INTERNAL_WP_URL:
        try:
            import requests
            url = f"{INTERNAL_WP_URL}/posts?slug={clean_slug}&_embed"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                posts = res.json()
                if isinstance(posts, list) and len(posts) > 0:
                    p = posts[0]
                    return {
                        "slug": p.get("slug"),
                        "title": (p.get("title") or {}).get("rendered", ""),
                        "kicker": "Regulatory Dispatch",
                        "category": "Banking & Policy",
                        "author": "Fikarta News Desk",
                        "read_time": "4 min read",
                        "summary": (p.get("excerpt") or {}).get("rendered", ""),
                        "html": (p.get("content") or {}).get("rendered", ""),
                        "published_date": "2026",
                    }
        except Exception:
            pass

    # 2. Check internal Ghost
    if INTERNAL_GHOST_KEY:
        try:
            import requests
            url = f"{INTERNAL_GHOST_URL}/ghost/api/content/posts/slug/{clean_slug}/?key={INTERNAL_GHOST_KEY}&include=tags,authors"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                posts = res.json().get("posts", [])
                if posts:
                    p = posts[0]
                    return {
                        "slug": p.get("slug"),
                        "title": p.get("title"),
                        "kicker": (p.get("primary_tag") or {}).get("name", "Regulatory Dispatch"),
                        "category": (p.get("primary_tag") or {}).get("name", "Banking & Policy"),
                        "author": "Fikarta News Desk",
                        "read_time": f"{p.get('reading_time', 4)} min read",
                        "summary": p.get("custom_excerpt") or p.get("excerpt"),
                        "html": p.get("html"),
                        "published_date": "2026",
                    }
        except Exception:
            pass

    raise HTTPException(status_code=404, detail=f"News article '{clean_slug}' not found")

# -------------------------------------------------------------
# AUTONOMOUS CONTENT PIPELINE (ZERO-ADMIN SELF-PUBLISHER)
# Automatically publishes core-domain articles to website
# -------------------------------------------------------------
@app.post("/api/v1/pipeline/auto-publish")
def trigger_auto_publish(count: int = Query(1, ge=1, le=5)):
    """
    Autonomous pipeline trigger:
    Generates and self-publishes core-domain financial protection blogs
    without requiring any human intervention or admin portal.
    """
    try:
        sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from pipeline.auto_content_engine import run_auto_publisher
        published = run_auto_publisher(count=count)
        return {
            "status": "success",
            "published_count": len(published),
            "articles": [f"/blog/{slug}" for slug in published],
            "message": f"Successfully auto-published {len(published)} core-domain articles."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Autonomous publisher error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"Starting Fikarta Backend Server on http://{host}:{port}")
    uvicorn.run("main:app", host=host, port=port, reload=False)
