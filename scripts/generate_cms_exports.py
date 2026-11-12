import json
import os
import sqlite3
import time
from xml.sax.saxutils import escape

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BACKEND_DIR, "db", "fikarta.db")
OUTPUT_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "docs", "cms_exports")
os.makedirs(OUTPUT_DIR, exist_ok=True)

def generate_ghost_import():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM blog_articles;")
    blogs = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM news_articles;")
    news = [dict(r) for r in cur.fetchall()]
    conn.close()

    print(f"Loaded {len(blogs)} blogs and {len(news)} news articles.")

    posts = []
    tags = [{"name": "Blog", "slug": "blog"}, {"name": "News", "slug": "news"}, {"name": "Credit Advisory", "slug": "credit-advisory"}, {"name": "Regulatory Policy", "slug": "regulatory-policy"}]
    posts_tags = []

    now_ms = int(time.time() * 1000)

    # Convert Blogs
    for b in blogs:
        slug = b.get("slug")
        title = b.get("title") or slug.replace("-", " ").title()
        summary = b.get("summary") or ""
        
        # Build HTML content from sections
        html_parts = []
        if summary:
            html_parts.append(f"<p class='lead'><strong>{summary}</strong></p>")

        sections = []
        if b.get("sections_json"):
            try:
                sections = json.loads(b["sections_json"])
            except Exception:
                sections = []

        for sec in sections:
            h = sec.get("heading") or ""
            body = sec.get("body") or sec.get("content") or ""
            if h:
                html_parts.append(f"<h2>{h}</h2>")
            if body:
                html_parts.append(f"<p>{body}</p>")

        full_html = "\n".join(html_parts) if html_parts else f"<p>{summary}</p>"

        posts.append({
            "id": f"blog_{slug}",
            "title": title,
            "slug": slug,
            "mobiledoc": None,
            "html": full_html,
            "status": "published",
            "type": "post",
            "published_at": now_ms,
            "custom_excerpt": summary[:280] if summary else None,
            "visibility": "public",
        })

    # Convert News
    for n in news:
        slug = n.get("slug")
        title = n.get("title") or slug.replace("-", " ").title()
        summary = n.get("summary") or ""

        html_parts = []
        if summary:
            html_parts.append(f"<p class='lead'><strong>{summary}</strong></p>")

        paras = []
        if n.get("paragraphs_json"):
            try:
                paras = json.loads(n["paragraphs_json"])
            except Exception:
                paras = []

        for p in paras:
            html_parts.append(f"<p>{p}</p>")

        full_html = "\n".join(html_parts) if html_parts else f"<p>{summary}</p>"

        posts.append({
            "id": f"news_{slug}",
            "title": title,
            "slug": slug,
            "mobiledoc": None,
            "html": full_html,
            "status": "published",
            "type": "post",
            "published_at": now_ms,
            "custom_excerpt": summary[:280] if summary else None,
            "visibility": "public",
        })

    ghost_data = {
        "db": [
            {
                "meta": {
                    "exported_on": now_ms,
                    "version": "5.0.0"
                },
                "data": {
                    "posts": posts,
                    "tags": tags,
                }
            }
        ]
    }

    out_file = os.path.join(OUTPUT_DIR, "fikarta_ghost_import.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(ghost_data, f, ensure_ascii=False, indent=2)

    print(f"Generated Ghost 1-Click Import file: {out_file} ({len(posts)} posts, {os.path.getsize(out_file)/1024:.1f} KB)")


def generate_wordpress_wxr():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM blog_articles;")
    blogs = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM news_articles;")
    news = [dict(r) for r in cur.fetchall()]
    conn.close()

    items_xml = []
    post_id = 1000

    for b in blogs:
        post_id += 1
        slug = escape(b.get("slug") or "")
        title = escape(b.get("title") or slug)
        summary = escape(b.get("summary") or "")

        sections = []
        if b.get("sections_json"):
            try: sections = json.loads(b["sections_json"])
            except Exception: sections = []

        body_parts = []
        if summary:
            body_parts.append(f"<!-- wp:paragraph -->\n<p><strong>{summary}</strong></p>\n<!-- /wp:paragraph -->")
        for s in sections:
            h = escape(s.get("heading") or "")
            t = escape(s.get("body") or "")
            if h: body_parts.append(f"<!-- wp:heading -->\n<h2>{h}</h2>\n<!-- /wp:heading -->")
            if t: body_parts.append(f"<!-- wp:paragraph -->\n<p>{t}</p>\n<!-- /wp:paragraph -->")

        content = "\n".join(body_parts)

        items_xml.append(f"""
    <item>
        <title>{title}</title>
        <link>https://fikarta.money/blog/{slug}</link>
        <pubDate>Mon, 10 Nov 2026 12:00:00 +0000</pubDate>
        <dc:creator><![CDATA[Fikarta Editorial]]></dc:creator>
        <category><![CDATA[Blog]]></category>
        <category><![CDATA[Credit Advisory]]></category>
        <guid isPermaLink="false">https://fikarta.money/?p={post_id}</guid>
        <description></description>
        <content:encoded><![CDATA[{content}]]></content:encoded>
        <excerpt:encoded><![CDATA[{summary}]]></excerpt:encoded>
        <wp:post_id>{post_id}</wp:post_id>
        <wp:post_date>2026-11-10 12:00:00</wp:post_date>
        <wp:post_name>{slug}</wp:post_name>
        <wp:status>publish</wp:status>
        <wp:post_type>post</wp:post_type>
    </item>""")

    wxr_content = f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"
    xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:wfw="http://wellformedweb.org/CommentAPI/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:wp="http://wordpress.org/export/1.2/"
>
<channel>
    <title>Fikarta</title>
    <link>https://fikarta.money</link>
    <description>Empowering Smarter Financial Decisions in India</description>
    <pubDate>Mon, 10 Nov 2026 12:00:00 +0000</pubDate>
    <language>en-IN</language>
    <wp:wxr_version>1.2</wp:wxr_version>
    {"".join(items_xml)}
</channel>
</rss>"""

    out_file = os.path.join(OUTPUT_DIR, "fikarta_wordpress_import.xml")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(wxr_content)

    print(f"Generated WordPress WXR XML file: {out_file} ({len(blogs)} posts, {os.path.getsize(out_file)/1024:.1f} KB)")


if __name__ == "__main__":
    generate_ghost_import()
    generate_wordpress_wxr()

