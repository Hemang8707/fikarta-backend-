"""
Purge all occurrences of GoCredit from Ghost CMS via Admin API
--------------------------------------------------------------
Updates all existing posts in Ghost so that:
- Any mention of 'GoCredit' is replaced with 'Fikarta'
- Any mention of 'gocredit.money' is replaced with 'fikarta.money'
"""

import jwt
import datetime
import urllib.request
import urllib.error
import json
import time
import re

GHOST_ADMIN_KEY = "6af2f27e9d0ef10001e3b77d:606ac1405cb90a454ecedf097e4f73c52b7352839c9de5205579b4ffe1abea2b"
GHOST_ADMIN_URL = "https://fikarta.ghost.io/ghost/api/admin"

def generate_jwt_token(admin_key: str) -> str:
    key_id, secret = admin_key.split(":")
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(minutes=5)).timestamp()),
        "aud": "/admin/",
    }
    return jwt.encode(payload, bytes.fromhex(secret), algorithm="HS256", headers={"kid": key_id})

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'gocredit\.money', 'fikarta.money', text, flags=re.IGNORECASE)
    text = re.sub(r'gocredit\.in', 'fikarta.money', text, flags=re.IGNORECASE)
    text = re.sub(r'money\.gocredit\.ai', 'fikarta.money', text, flags=re.IGNORECASE)
    text = re.sub(r'raunak@gocredit\.money', 'support@fikarta.money', text, flags=re.IGNORECASE)
    text = re.sub(r'support@gocredit\.money', 'support@fikarta.money', text, flags=re.IGNORECASE)
    text = re.sub(r'GoCredit\'s', 'Fikarta\'s', text)
    text = re.sub(r'gocredit\'s', 'fikarta\'s', text, flags=re.IGNORECASE)
    text = re.sub(r'GoCredit', 'Fikarta', text)
    text = re.sub(r'GOCREDIT', 'FIKARTA', text)
    text = re.sub(r'gocredit', 'fikarta', text, flags=re.IGNORECASE)
    return text

def purge_ghost():
    token = generate_jwt_token(GHOST_ADMIN_KEY)
    url = f"{GHOST_ADMIN_URL}/posts/?limit=100&formats=html"
    req = urllib.request.Request(url, headers={"Authorization": f"Ghost {token}"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        posts = json.loads(resp.read()).get("posts", [])

    print(f"[GhostCleaner] Found {len(posts)} posts in Ghost to inspect.")
    cleaned_count = 0

    for p in posts:
        pid = p["id"]
        title = p.get("title", "")
        slug = p.get("slug", "")
        html = p.get("html", "")
        excerpt = p.get("custom_excerpt", "") or p.get("excerpt", "")
        updated_at = p.get("updated_at")

        new_title = clean_text(title)
        new_slug = clean_text(slug)
        new_html = clean_text(html)
        new_excerpt = clean_text(excerpt)

        if (
            new_title != title
            or new_slug != slug
            or new_html != html
            or new_excerpt != excerpt
        ):
            update_payload = {
                "posts": [
                    {
                        "id": pid,
                        "title": new_title,
                        "slug": new_slug,
                        "html": new_html,
                        "custom_excerpt": new_excerpt,
                        "updated_at": updated_at,
                    }
                ]
            }

            token = generate_jwt_token(GHOST_ADMIN_KEY)
            put_url = f"{GHOST_ADMIN_URL}/posts/{pid}/?source=html"
            put_req = urllib.request.Request(
                put_url,
                data=json.dumps(update_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Ghost {token}",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            try:
                with urllib.request.urlopen(put_req, timeout=10) as put_resp:
                    if put_resp.status in (200, 201):
                        cleaned_count += 1
                        print(f"Purged GoCredit from Ghost post: {new_title[:50]}")
            except Exception as e:
                print(f"Error updating post {pid}: {e}")
            time.sleep(0.3)

    print(f"\n[GhostCleaner] Successfully cleaned {cleaned_count} posts in Ghost.")

if __name__ == "__main__":
    purge_ghost()

