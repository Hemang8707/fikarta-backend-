from bs4 import BeautifulSoup
import re
import json

def parse_gocredit_page(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")
    
    # 1. Page Title & Meta
    title = soup.title.string.strip() if soup.title else ""
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta_desc_tag["content"].strip() if meta_desc_tag and "content" in meta_desc_tag.attrs else ""

    # 2. Extract Hero Info
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True) if h1 else ""

    # Extract Breadcrumbs
    crumbs = []
    nav_crumbs = soup.find("nav", attrs={"aria-label": lambda x: x and "breadcrumb" in x.lower()})
    if nav_crumbs:
        for a in nav_crumbs.find_all(["a", "span"]):
            t = a.get_text(strip=True)
            if t and t not in ["›", "/", ">", "→"]:
                crumbs.append(t)

    # 3. Extract All Sections & Headings
    sections_data = []
    for s in soup.find_all("section"):
        heading = s.find(["h1", "h2", "h3", "h4"])
        h_text = heading.get_text(strip=True) if heading else ""
        text_content = s.get_text(" ", strip=True)
        
        # Check for tables inside section
        tables_data = []
        for table in s.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if cells:
                    rows.append(cells)
            if rows:
                tables_data.append({"headers": headers, "rows": rows})

        # Check for FAQ accordions or Q&As
        faqs = []
        for dt, dd in zip(s.find_all("dt"), s.find_all("dd")):
            faqs.append({"q": dt.get_text(strip=True), "a": dd.get_text(strip=True)})
        
        sections_data.append({
            "heading": h_text,
            "text": text_content[:1000], # Preview
            "tables": tables_data,
            "faqs": faqs,
        })

    # 4. Extract Structured JSON-LD if present
    json_lds = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            json_lds.append(json.loads(script.string))
        except Exception:
            pass

    return {
        "url": url,
        "title": title,
        "description": meta_desc,
        "h1": h1_text,
        "crumbs": crumbs,
        "sections": sections_data,
        "json_lds": json_lds,
    }

