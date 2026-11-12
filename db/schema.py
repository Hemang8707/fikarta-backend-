import sqlite3
import os

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "fikarta.db")

def init_db(db_path=DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Cities / Rollups Table (~2,500 cities & districts)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        state TEXT NOT NULL,
        tier TEXT,
        population TEXT,
        pincodes_count INTEGER DEFAULT 0,
        lowest_rate REAL DEFAULT 10.49,
        max_amount TEXT,
        avg_loan TEXT,
        disbursal TEXT,
        pincodes_json TEXT, -- JSON array of pincodes
        areas TEXT,
        related_cities_json TEXT, -- JSON array of [{name, slug, rate}]
        narrative TEXT,
        faqs_json TEXT, -- JSON array of [{q, a}]
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 3. NBFCs & Scheduled Commercial Banks Table (~3,623 entities)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS nbfcs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        legal_name TEXT,
        cor_number TEXT,
        category TEXT, -- 'Banks', 'NBFCs', 'Fintechs'
        rating REAL DEFAULT 4.5,
        rate_range TEXT,
        fee_range TEXT,
        max_loan TEXT,
        tenure_range TEXT,
        min_cibil TEXT,
        disbursal_speed TEXT,
        best_for TEXT,
        features_json TEXT, -- JSON array of bullets
        grievance_officer TEXT,
        grievance_email TEXT,
        grievance_phone TEXT,
        address TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 4. Loan Apps & Digital Brands Table (~1,538 brands)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS loan_apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        tagline TEXT,
        developer TEXT,
        downloads TEXT,
        rating REAL DEFAULT 4.3,
        reviews_count TEXT,
        min_amount TEXT,
        max_amount TEXT,
        apr_range TEXT,
        tenure_range TEXT,
        nbfc_partners_json TEXT, -- JSON array
        pros_json TEXT,
        cons_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 5. Salary Tiers Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS salaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bracket TEXT UNIQUE NOT NULL, -- '10000', '15000', '25000', etc.
        amount_num INTEGER NOT NULL,
        label TEXT NOT NULL,
        title TEXT NOT NULL,
        kicker TEXT,
        desc TEXT,
        lowest_rate REAL,
        max_amount TEXT,
        avg_loan TEXT,
        max_emi TEXT,
        foir TEXT,
        disbursal TEXT,
        rules_json TEXT,
        faqs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 6. Loan Purposes Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS purposes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        kicker TEXT,
        desc TEXT,
        lowest_rate REAL,
        max_amount TEXT,
        avg_loan TEXT,
        faqs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 7. Calculators Table (Institutions & Products - ~1,668 entities)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS calculators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        calc_type TEXT DEFAULT 'institution', -- 'institution' or 'product'
        default_rate REAL DEFAULT 10.50,
        min_rate REAL DEFAULT 10.49,
        max_rate REAL DEFAULT 24.00,
        default_amount INTEGER DEFAULT 200000,
        min_amount INTEGER DEFAULT 10000,
        max_amount INTEGER DEFAULT 4000000,
        default_tenure INTEGER DEFAULT 36,
        min_tenure INTEGER DEFAULT 6,
        max_tenure INTEGER DEFAULT 72,
        processing_fee TEXT DEFAULT '1% - 3%',
        cor_number TEXT,
        gro_email TEXT,
        gro_phone TEXT,
        official_url TEXT,
        faqs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 8. Loan Amounts Table (~196 variations & amounts)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS loan_amounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        amount_num INTEGER NOT NULL,
        title TEXT NOT NULL,
        kicker TEXT,
        desc TEXT,
        min_salary INTEGER DEFAULT 15000,
        min_cibil INTEGER DEFAULT 650,
        lowest_rate REAL DEFAULT 10.49,
        best_tenure TEXT DEFAULT '24 to 36 Months',
        top_lenders_json TEXT,
        emi_matrix_json TEXT,
        faqs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 9. Loan Kavach Table (~38 Playbooks, Guidelines & Legal Notices)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS loan_kavach (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL, -- 'recovery-harassment', 'letters', 'default-consequences', 'settlement'
        title TEXT NOT NULL,
        kicker TEXT,
        tldr TEXT,
        legal_framework TEXT,
        rights_json TEXT,
        letter_template TEXT,
        escalation_steps_json TEXT,
        evidence_checklist_json TEXT,
        faqs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 10. News Articles Table (~768 articles)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS news_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        kicker TEXT,
        category TEXT DEFAULT 'Fintech & Economy',
        read_time TEXT DEFAULT '4 min read',
        published_date TEXT DEFAULT 'Sep 2026',
        summary TEXT,
        takeaways_json TEXT,
        paragraphs_json TEXT,
        references_json TEXT,
        related_news_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 11. Hindi Vernacular Articles Table (~400 articles)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS hindi_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        kicker TEXT,
        desc TEXT,
        topic TEXT DEFAULT 'Personal Finance',
        takeaways_json TEXT,
        paragraphs_json TEXT,
        emi_table_json TEXT,
        sources_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 12. Blog & In-Depth Guides Table (~312 articles)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS blog_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        kicker TEXT,
        category TEXT DEFAULT 'Credit Advisory',
        author TEXT DEFAULT 'Fikarta Research & Editorial Team',
        read_time TEXT DEFAULT '6 min read',
        published_date TEXT DEFAULT 'August 2026',
        summary TEXT,
        sections_json TEXT,
        faqs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 13. Financial Tips Table (~77 tips)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        kicker TEXT,
        category TEXT DEFAULT 'Actionable Credit Strategy',
        quick_tip TEXT NOT NULL,
        explanation TEXT,
        action_steps_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 14. Financial Glossary Terms Table (~46 terms)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS glossary_terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        term TEXT NOT NULL,
        acronym TEXT,
        category TEXT DEFAULT 'Regulations & Guidelines',
        short_def TEXT NOT NULL,
        full_def TEXT,
        calculation_example TEXT,
        borrower_impact TEXT,
        rbi_circular TEXT,
        related_terms_json TEXT,
        faqs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 15. Lenders Table (~20 premier bank & NBFC lenders)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lenders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        title TEXT NOT NULL,
        tagline TEXT,
        category TEXT DEFAULT 'Banks', -- 'Banks', 'NBFCs', 'Fintechs'
        rating REAL DEFAULT 4.7,
        rate_range TEXT NOT NULL,
        amount_range TEXT NOT NULL,
        tenure_range TEXT NOT NULL,
        fee_range TEXT,
        prepayment_charge TEXT,
        min_cibil TEXT DEFAULT '700+',
        disbursal_speed TEXT DEFAULT 'Instant to Same Day',
        best_for TEXT,
        special_offer TEXT,
        pros_json TEXT,
        cons_json TEXT,
        features_json TEXT,
        faqs_json TEXT,
        similar_lenders_json TEXT,
        compare_pairs_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 16. Research Reports Table (~3 deep industry papers)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS research_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        kicker TEXT,
        read_time TEXT DEFAULT '8 min read',
        published_date TEXT DEFAULT 'June 2026',
        summary TEXT,
        sections_json TEXT,
        key_findings_json TEXT,
        methodology TEXT,
        citations_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()
    conn.close()
    print(f"Database schema initialized successfully at: {db_path}")

if __name__ == "__main__":
    init_db()

