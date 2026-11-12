import sqlite3
import os
import json

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BACKEND_DIR, "db", "fikarta.db")
FRONTEND_DATA_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "fikarta-web", "src", "data")

os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)

def sync_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Export Cities
    cur.execute("SELECT * FROM cities;")
    cities = {}
    for r in cur.fetchall():
        d = dict(r)
        if d.get("pincodes_json"):
            try: d["pincodes"] = json.loads(d["pincodes_json"])
            except Exception: d["pincodes"] = []
        if d.get("faqs_json"):
            try: d["faqs"] = json.loads(d["faqs_json"])
            except Exception: d["faqs"] = []
        cities[d["slug"]] = d

    city_out = os.path.join(FRONTEND_DATA_DIR, "all_cities_extended.json")
    with open(city_out, "w", encoding="utf-8") as f:
        json.dump(cities, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(cities)} cities to: {city_out}")

    # 2. Export NBFCs
    cur.execute("SELECT * FROM nbfcs;")
    nbfcs = {}
    for r in cur.fetchall():
        d = dict(r)
        if d.get("features_json"):
            try: d["deployed_apps"] = json.loads(d["features_json"])
            except Exception: d["deployed_apps"] = []
        nbfcs[d["slug"]] = d

    nbfc_out = os.path.join(FRONTEND_DATA_DIR, "all_nbfcs.json")
    with open(nbfc_out, "w", encoding="utf-8") as f:
        json.dump(nbfcs, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(nbfcs)} NBFCs to: {nbfc_out}")

    # 3. Export Loan Apps
    cur.execute("SELECT * FROM loan_apps;")
    apps = {}
    for r in cur.fetchall():
        d = dict(r)
        if d.get("pros_json"):
            try: d["pros"] = json.loads(d["pros_json"])
            except Exception: d["pros"] = []
        if d.get("cons_json"):
            try: d["cons"] = json.loads(d["cons_json"])
            except Exception: d["cons"] = []
        if d.get("nbfc_partners_json"):
            try: d["partners"] = json.loads(d["nbfc_partners_json"])
            except Exception: d["partners"] = []
        apps[d["slug"]] = d

    apps_out = os.path.join(FRONTEND_DATA_DIR, "all_loan_apps.json")
    with open(apps_out, "w", encoding="utf-8") as f:
        json.dump(apps, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(apps)} Loan Apps to: {apps_out}")

    # 4. Export Calculators
    cur.execute("SELECT * FROM calculators;")
    calcs = {}
    for r in cur.fetchall():
        d = dict(r)
        if d.get("faqs_json"):
            try: d["faqs"] = json.loads(d["faqs_json"])
            except Exception: d["faqs"] = []
        calcs[d["slug"]] = d

    calcs_out = os.path.join(FRONTEND_DATA_DIR, "all_calculators.json")
    with open(calcs_out, "w", encoding="utf-8") as f:
        json.dump(calcs, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(calcs)} Calculators to: {calcs_out}")

    # 5. Export Loan Amounts
    cur.execute("SELECT * FROM loan_amounts;")
    amounts = {}
    for r in cur.fetchall():
        d = dict(r)
        if d.get("top_lenders_json"):
            try: d["top_lenders"] = json.loads(d["top_lenders_json"])
            except Exception: d["top_lenders"] = []
        if d.get("emi_matrix_json"):
            try: d["emi_matrix"] = json.loads(d["emi_matrix_json"])
            except Exception: d["emi_matrix"] = []
        if d.get("faqs_json"):
            try: d["faqs"] = json.loads(d["faqs_json"])
            except Exception: d["faqs"] = []
        amounts[d["slug"]] = d

    amounts_out = os.path.join(FRONTEND_DATA_DIR, "all_loan_amounts.json")
    with open(amounts_out, "w", encoding="utf-8") as f:
        json.dump(amounts, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(amounts)} Loan Amounts to: {amounts_out}")

    # 6. Export Loan Kavach
    cur.execute("SELECT * FROM loan_kavach;")
    kavach = {}
    for r in cur.fetchall():
        d = dict(r)
        if d.get("rights_json"):
            try: d["rights"] = json.loads(d["rights_json"])
            except Exception: d["rights"] = []
        if d.get("escalation_steps_json"):
            try: d["escalation_steps"] = json.loads(d["escalation_steps_json"])
            except Exception: d["escalation_steps"] = []
        if d.get("evidence_checklist_json"):
            try: d["evidence_checklist"] = json.loads(d["evidence_checklist_json"])
            except Exception: d["evidence_checklist"] = []
        if d.get("faqs_json"):
            try: d["faqs"] = json.loads(d["faqs_json"])
            except Exception: d["faqs"] = []
        kavach[d["slug"]] = d

    kavach_out = os.path.join(FRONTEND_DATA_DIR, "all_loan_kavach.json")
    with open(kavach_out, "w", encoding="utf-8") as f:
        json.dump(kavach, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(kavach)} Loan Kavach playbooks to: {kavach_out}")

    # 7. Export News Articles
    cur.execute("SELECT * FROM news_articles;")
    news = {}
    for r in cur.fetchall():
        d = dict(r)
        for field in ["takeaways_json", "paragraphs_json", "references_json", "related_news_json"]:
            if d.get(field):
                try: d[field.replace("_json", "")] = json.loads(d[field])
                except Exception: d[field.replace("_json", "")] = []
        news[d["slug"]] = d

    news_out = os.path.join(FRONTEND_DATA_DIR, "all_news.json")
    with open(news_out, "w", encoding="utf-8") as f:
        json.dump(news, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(news)} News articles to: {news_out}")

    # 8. Export Hindi Articles
    cur.execute("SELECT * FROM hindi_articles;")
    hindi = {}
    for r in cur.fetchall():
        d = dict(r)
        for field in ["takeaways_json", "paragraphs_json", "emi_table_json", "sources_json"]:
            if d.get(field):
                try: d[field.replace("_json", "")] = json.loads(d[field])
                except Exception: d[field.replace("_json", "")] = []
        hindi[d["slug"]] = d

    hindi_out = os.path.join(FRONTEND_DATA_DIR, "all_hindi.json")
    with open(hindi_out, "w", encoding="utf-8") as f:
        json.dump(hindi, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(hindi)} Hindi articles to: {hindi_out}")

    # 9. Export Blog Articles
    cur.execute("SELECT * FROM blog_articles;")
    blogs = {}
    for r in cur.fetchall():
        d = dict(r)
        for field in ["sections_json", "faqs_json"]:
            if d.get(field):
                try: d[field.replace("_json", "")] = json.loads(d[field])
                except Exception: d[field.replace("_json", "")] = []
        blogs[d["slug"]] = d

    blog_out = os.path.join(FRONTEND_DATA_DIR, "all_blogs.json")
    with open(blog_out, "w", encoding="utf-8") as f:
        json.dump(blogs, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(blogs)} Blog articles to: {blog_out}")

    # 10. Export Financial Tips
    cur.execute("SELECT * FROM tips;")
    tips = {}
    for r in cur.fetchall():
        d = dict(r)
        if d.get("action_steps_json"):
            try: d["action_steps"] = json.loads(d["action_steps_json"])
            except Exception: d["action_steps"] = []
        tips[d["slug"]] = d

    tips_out = os.path.join(FRONTEND_DATA_DIR, "all_tips.json")
    with open(tips_out, "w", encoding="utf-8") as f:
        json.dump(tips, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(tips)} Financial Tips to: {tips_out}")

    conn.close()
    print("\nFrontend data sync completed successfully!")

if __name__ == "__main__":
    sync_data()

