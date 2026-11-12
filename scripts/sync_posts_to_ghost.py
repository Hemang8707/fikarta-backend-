"""
Sync Fikarta Core Domain Articles to Ghost CMS via Admin API
-----------------------------------------------------------
Autonomously pushes financial protection, credit, and regulatory
articles directly into Ghost without any manual intervention.
"""

import sqlite3
import json
import jwt
import datetime
import urllib.request
import urllib.error
import time
import os
import sys

GHOST_ADMIN_KEY = "6af2f27e9d0ef10001e3b77d:606ac1405cb90a454ecedf097e4f73c52b7352839c9de5205579b4ffe1abea2b"
GHOST_ADMIN_URL = "https://fikarta.ghost.io/ghost/api/admin"

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BACKEND_DIR, "db", "fikarta.db")

def generate_jwt_token(admin_key: str) -> str:
    key_id, secret = admin_key.split(":")
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(minutes=5)).timestamp()),
        "aud": "/admin/",
    }
    return jwt.encode(payload, bytes.fromhex(secret), algorithm="HS256", headers={"kid": key_id})

def get_existing_ghost_slugs() -> set:
    slugs = set()
    page = 1
    while True:
        token = generate_jwt_token(GHOST_ADMIN_KEY)
        url = f"{GHOST_ADMIN_URL}/posts/?limit=100&page={page}&fields=id,slug"
        req = urllib.request.Request(url, headers={"Authorization": f"Ghost {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                posts = data.get("posts", [])
                if not posts:
                    break
                for p in posts:
                    slugs.add(p["slug"])
                pagination = data.get("meta", {}).get("pagination", {})
                if page >= pagination.get("pages", 1):
                    break
                page += 1
        except Exception as e:
            print(f"[GhostSync] Warning fetching existing slugs: {e}")
            break
    return slugs

def build_article_html(summary: str, sections_json: str, faqs_json: str) -> str:
    html_parts = []
    if summary:
        html_parts.append(f'<p class="fikarta-lead" style="font-size:1.15rem;line-height:1.6;font-weight:500;">{summary}</p>')

    if sections_json:
        try:
            sections = json.loads(sections_json)
            for sec in sections:
                h = sec.get("heading")
                b = sec.get("body", "")
                if h:
                    html_parts.append(f'<h2>{h}</h2>')
                if b:
                    paras = b.split("\n\n")
                    for p in paras:
                        p_clean = p.strip()
                        if p_clean:
                            html_parts.append(f'<p>{p_clean}</p>')
        except Exception:
            pass

    if faqs_json:
        try:
            faqs = json.loads(faqs_json)
            if faqs:
                html_parts.append('<h2>Frequently Asked Questions</h2>')
                for f in faqs:
                    q = f.get("q")
                    a = f.get("a")
                    if q and a:
                        html_parts.append(f'<h3>{q}</h3><p>{a}</p>')
        except Exception:
            pass

    return "\n".join(html_parts)

def push_article_to_ghost(post_obj: dict) -> bool:
    token = generate_jwt_token(GHOST_ADMIN_KEY)
    url = f"{GHOST_ADMIN_URL}/posts/?source=html"
    payload = json.dumps({"posts": [post_obj]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Ghost {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status in (200, 201)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"[GhostSync] HTTP Error for {post_obj.get('slug')}: {e.code} - {err_body[:200]}")
        return False
    except Exception as e:
        print(f"[GhostSync] Network error for {post_obj.get('slug')}: {e}")
        return False

def sync_articles(limit: int = 15):
    existing = get_existing_ghost_slugs()
    print(f"[GhostSync] Currently {len(existing)} posts in Ghost.")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT slug, title, kicker, category, summary, sections_json, faqs_json FROM blog_articles ORDER BY id ASC;")
    all_blogs = cur.fetchall()
    conn.close()

    synced_count = 0
    for row in all_blogs:
        slug, title, kicker, category, summary, sects_json, faqs_json = row
        if slug in existing:
            continue

        html = build_article_html(summary, sects_json, faqs_json)
        post_obj = {
            "title": title,
            "slug": slug,
            "custom_excerpt": (summary or "")[:300],
            "html": html,
            "status": "published",
            "tags": [{"name": category or "Credit Advisory"}],
        }

        success = push_article_to_ghost(post_obj)
        if success:
            synced_count += 1
            print(f"[{synced_count}/{limit}] Auto-published to Ghost: {title[:55]}... ({slug})")
            existing.add(slug)
        else:
            print(f"Failed to publish: {slug}")

        if synced_count >= limit:
            break
        time.sleep(0.3)

    print(f"\n[GhostSync] Complete! Successfully auto-published {synced_count} fresh articles to Ghost.")

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    sync_articles(limit=n)

