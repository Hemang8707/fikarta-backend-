"""
Fikarta Autonomous Content Engine & Auto-Publisher (v2.0)
--------------------------------------------------------
Autonomous flow that generates, verifies, and publishes core-domain
financial protection articles and blogs directly to Fikarta without
requiring any human editor or manual admin portal.

Core Domain Guardrails:
- Strict topic filter: RBI rules, NBFC compliance, credit scores (CIBIL),
  loan recovery harassment rights, fake loan app safety, and personal loan EMIs.
- Discards any topic not directly related to consumer credit protection.
- Automatically writes to SQLite (fikarta.db), generates static JSON files,
  and updates frontend search indexes for instant zero-rebuild publishing.
"""

import os
import sys
import json
import sqlite3
import random
import re
from datetime import datetime

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
DB_PATH = os.path.join(BACKEND_DIR, "db", "fikarta.db")
FRONTEND_BLOGS_DIR = os.path.join(PROJECT_ROOT, "fikarta-web", "public", "data", "blogs")
FRONTEND_NEWS_DIR = os.path.join(PROJECT_ROOT, "fikarta-web", "public", "data", "news")
BLOGS_INDEX_PATH = os.path.join(PROJECT_ROOT, "fikarta-web", "src", "data", "all_blogs_index.json")
NEWS_INDEX_PATH = os.path.join(PROJECT_ROOT, "fikarta-web", "src", "data", "all_news_index.json")

# Mandatory Core Domain Keywords (Strict whitelist)
CORE_KEYWORDS = {
    "rbi", "nbfc", "cibil", "credit score", "bureau", "personal loan",
    "recovery agent", "digital lending", "ombudsman", "emi", "interest rate",
    "banking", "loan settlement", "fintech", "default", "scam", "fraud",
    "key fact statement", "harassment", "penal charges", "moratorium"
}

# Domain Topics Blueprints
BLOG_TOPIC_TEMPLATES = [
    {
        "title_template": "RBI Directives on Loan Prepayment Charges: What Borrowers Must Know in {year}",
        "kicker": "Statutory Borrower Rights • {year}",
        "category": "RBI Rules",
        "author": "Fikarta Regulatory Desk",
        "focus": "rbi_prepayment",
        "summary": "The Reserve Bank of India has prohibited commercial banks and NBFCs from levying foreclosure penalties on floating-rate individual loans. Here is how borrowers can exercise their rights.",
    },
    {
        "title_template": "How to Legally Stop Recovery Agent Harassment: 5 Immediate Legal Remedies",
        "kicker": "Borrower Protection Playbook • {year}",
        "category": "Debt Recovery",
        "author": "Fikarta Legal & Advisory Team",
        "focus": "recovery_harassment",
        "summary": "Unsolicited recovery calls outside 8 AM – 7 PM or threats to family members violate statutory RBI Fair Practices Code. Discover your 5 actionable defenses under the 2026 regulations.",
    },
    {
        "title_template": "Removing 'Settled' Status from CIBIL: The 90-Day Credit Bureau Restoration Guide",
        "kicker": "Credit Advisory & Score Repair • {year}",
        "category": "Credit Advisory",
        "author": "Credit Bureau Intelligence Group",
        "focus": "cibil_settled_repair",
        "summary": "A 'Settled' remark drags your CIBIL score down for up to 7 years. Learn how to obtain a formal No Dues Certificate (NDC) and convert settled status to closed.",
    },
    {
        "title_template": "Fake Loan Apps Blackmail Scam: How to Verify RBI Registration in 60 Seconds",
        "kicker": "Cyber Safety & Scam Alert • {year}",
        "category": "Loan Safety",
        "author": "Fikarta Fraud Prevention Cell",
        "focus": "fake_loan_apps",
        "summary": "Predatory digital lending apps harvest private contacts to blackmail borrowers. Use this verification checklist to inspect licensed NBFC partners before sharing Aadhaar or PAN.",
    },
    {
        "title_template": "Personal Loan Balance Transfer: When Does Moving Your Loan Actually Save Money?",
        "kicker": "Strategic Debt Optimization • {year}",
        "category": "Credit Advisory",
        "author": "Fikarta Research Desk",
        "focus": "balance_transfer",
        "summary": "Refinancing high-cost debt to an RBI-approved prime lender can lower monthly EMIs significantly. Here is the mathematical formula to calculate net savings after processing fees.",
    },
    {
        "title_template": "RBI Mandates Key Fact Statements (KFS): No Hidden Processing Fees Allowed",
        "kicker": "Consumer Protection Directive • {year}",
        "category": "RBI Rules",
        "author": "Fikarta Regulatory Watch",
        "focus": "kfs_mandate",
        "summary": "Lenders must now disclose the total Annual Percentage Rate (APR), recovery partner names, and grievance redressal officers upfront in a standardized Key Fact Statement.",
    }
]

def is_core_domain(text: str) -> bool:
    """Verifies that the content strictly pertains to Fikarta's financial domain."""
    lower = text.lower()
    return any(kw in lower for kw in CORE_KEYWORDS)

def slugify(text: str) -> str:
    """Generate a clean URL slug."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")

def generate_blog_content(template: dict, current_year: str = "2026"):
    title = template["title_template"].format(year=current_year)
    slug = slugify(title)
    kicker = template["kicker"].format(year=current_year)
    category = template["category"]
    author = template["author"]
    summary = template["summary"]

    # In-depth structured sections strictly aligned to Fikarta domain kit
    sections = [
        {
            "heading": "Statutory Context & Latest Regulatory Developments",
            "body": f"In recent supervisory reviews, the Reserve Bank of India has underscored that retail lending practices must strictly adhere to fair-lending directives. Across India, borrowers navigating personal loans and credit cards often face aggressive third-party collection practices or opaque compounding calculations.\n\nUnder current {current_year} regulatory guidance, regulated entities—including Scheduled Commercial Banks and registered Non-Banking Financial Companies (NBFCs)—are legally responsible for the actions of their outsourced agents. Borrowers possessing clear awareness of these statutory boundaries can resolve credit disputes swiftly while preserving their bureau standing."
        },
        {
            "heading": "Critical Borrower Rights & Statutory Protections",
            "body": "Every borrower in India is protected by fundamental consumer and financial statutes:\n\n1. **Explicit Consent & Data Privacy:** Lenders are prohibited from accessing mobile contact lists or personal photo galleries under the RBI Digital Lending Guidelines.\n\n2. **Calling Hour Restrictions:** Authorized recovery agents may contact borrowers only between 8:00 AM and 7:00 PM. Calls placed beyond these hours constitute actionable harassment under consumer protection jurisprudence.\n\n3. **Prohibition of Intimidation:** Calling relatives, employers, or colleagues regarding an individual's personal debt is strictly unlawful and warrants immediate grievance escalation.\n\n4. **Mandatory Key Fact Statement (KFS):** Borrowers are legally entitled to receive a standardized breakdown of all upfront deductions, insurance premiums, and total APR before signing any loan agreement."
        },
        {
            "heading": "Step-by-Step Blueprint to Exercise Your Legal Remedies",
            "body": "If you experience statutory violations or credit bureau reporting discrepancies, execute the following protocol:\n\n**Step 1: Formal Written Grievance**\nSubmit an official dispute in writing to the Principal Grievance Redressal Officer (GRO) of the bank or NBFC. Always retain email receipts or registered post acknowledgment slips.\n\n**Step 2: 30-Day Resolution Window**\nUnder the Reserve Bank - Integrated Ombudsman Scheme (RB-IOS), regulated entities must respond and rectify legitimate grievances within 30 statutory business days.\n\n**Step 3: Escalate to the RBI Ombudsman**\nIf the lender fails to address the dispute or rejects your lawful request, file an online complaint at cms.rbi.org.in. The Ombudsman has the power to award compensation up to Rs 20 Lakhs for financial losses and up to Rs 1 Lakh for mental harassment."
        },
        {
            "heading": "Impact on CIBIL Score and Proactive Bureau Management",
            "body": "Unresolved disputes often trigger adverse reporting across the four registered credit bureaus (TransUnion CIBIL, Experian, CRIF High Mark, and Equifax). Proactive borrowers should request an updated Credit Information Report (CIR) quarterly.\n\nFikarta enables borrowers to assess their pre-approved loan eligibility and simulate score changes via soft inquiries that have zero negative impact on their credit rating."
        }
    ]

    faqs = [
        {
            "q": f"How quickly can an RBI complaint be resolved under {current_year} guidelines?",
            "a": "Once escalated to the RBI Integrated Ombudsman portal (cms.rbi.org.in), regulated entities are mandated to furnish their response within 15 to 30 business days."
        },
        {
            "q": "Can recovery agents visit my home without prior written notice?",
            "a": "No. Under statutory RBI Fair Practices Code, lenders must provide advance written intimation with the designated agent's identity and authorization badge before any field visit."
        },
        {
            "q": "Does checking my loan offers on Fikarta impact my CIBIL score?",
            "a": "No. Fikarta utilizes soft inquiries to verify pre-approved offers across 100+ RBI-registered lenders, meaning your credit score is completely protected."
        }
    ]

    return {
        "slug": slug,
        "title": title,
        "kicker": kicker,
        "category": category,
        "author": author,
        "read_time": "6 min read",
        "published_date": datetime.now().strftime("%B %Y"),
        "summary": summary,
        "sections": sections,
        "sections_json": json.dumps(sections, ensure_ascii=False),
        "faqs": faqs,
        "faqs_json": json.dumps(faqs, ensure_ascii=False),
    }

def publish_blog_article(article_data: dict) -> bool:
    """
    Saves the article directly to SQLite fikarta.db, generates the public JSON file,
    and prepends the article to all_blogs_index.json.
    """
    slug = article_data["slug"]

    # 1. Save to SQLite database
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO blog_articles (
                slug, title, kicker, category, author, read_time,
                published_date, summary, sections_json, faqs_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                slug,
                article_data["title"],
                article_data["kicker"],
                article_data["category"],
                article_data["author"],
                article_data["read_time"],
                article_data["published_date"],
                article_data["summary"],
                article_data["sections_json"],
                article_data["faqs_json"],
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[AutoPublisher] Error saving to SQLite: {e}")
        return False

    # 2. Write individual JSON file into public/data/blogs/
    os.makedirs(FRONTEND_BLOGS_DIR, exist_ok=True)
    file_path = os.path.join(FRONTEND_BLOGS_DIR, f"{slug}.json")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(article_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[AutoPublisher] Error writing public JSON: {e}")
        return False

    # 3. Update all_blogs_index.json
    try:
        index_data = {}
        if os.path.exists(BLOGS_INDEX_PATH):
            with open(BLOGS_INDEX_PATH, "r", encoding="utf-8") as f:
                index_data = json.load(f)

        # Prepend new article metadata at the top of the index
        new_entry = {
            slug: {
                "slug": slug,
                "title": article_data["title"],
                "kicker": article_data["kicker"],
                "category": article_data["category"],
                "author": article_data["author"],
                "read_time": article_data["read_time"],
                "published_date": article_data["published_date"],
                "summary": article_data["summary"],
            }
        }
        # Merge new at top
        updated_index = {**new_entry, **index_data}
        with open(BLOGS_INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(updated_index, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[AutoPublisher] Error updating blog index: {e}")

    print(f"[AutoPublisher] Successfully published blog: /blog/{slug}")
    return True

def run_auto_publisher(count: int = 1):
    """Executes the autonomous publisher for N core-domain articles."""
    published = []
    # Pick randomly from available templates or rotate
    available = [t for t in BLOG_TOPIC_TEMPLATES if is_core_domain(t["title_template"])]
    random.shuffle(available)

    for template in available[:count]:
        article = generate_blog_content(template)
        success = publish_blog_article(article)
        if success:
            published.append(article["slug"])

    return published

if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"[AutoPublisher] Running autonomous content flow for {count} core-domain articles...")
    results = run_auto_publisher(count)
    print(f"[AutoPublisher] Done! Published {len(results)} articles: {results}")

