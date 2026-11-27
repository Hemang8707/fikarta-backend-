"""Per-user retrieval for Fikarta AI: a knowledge store of the user's own data, long-term memory, and traces.

Pipeline for one chat turn (see ai_assistant.answer):
  1. sync_user_docs   - the user's data -> small text documents with metadata (one per account, factor, task ...).
                        Idempotent: unchanged documents are not rewritten; removed ones are deleted.
  2. rewrite          - (ai_assistant) a follow-up question is rewritten into a standalone query + keywords.
  3. retrieve         - BM25 over this user's documents and memories, with intent boosts, for every query
                        variant, fused with reciprocal-rank fusion. Strictly filtered by user_id.
  4. generate         - (ai_assistant) numbers only from FACTS; user documents cited as [U#], guides as [S#].
  5. remember         - (ai_assistant, after the reply) durable facts the user shared become memories.
  6. trace            - which documents were retrieved, with scores, and timings. Never the text.

Privacy: documents carry no name, PAN, phone, email or account numbers. Memories pass a PII filter before
they are stored. All tables cascade on account deletion; erasure and consent withdrawal clear them too.
"""
import hashlib
import json
import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

_TOKEN = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")
_STOP = set("a an and are as at be but by for from has have how i if in into is it its me my of on or our so that the their them then there these this to was "
            "we what when where which who why will with you your can do does not no should would could am".split())

MAX_MEMORIES = 60


def tokens(text: str) -> List[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS user_docs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      doc_key TEXT NOT NULL, doc_type TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, meta TEXT,
      content_hash TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id, doc_key));
    CREATE INDEX IF NOT EXISTS ix_ud_user ON user_docs(user_id, doc_type);
    CREATE TABLE IF NOT EXISTS user_memories(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind TEXT NOT NULL, text TEXT NOT NULL, meta TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, times_seen INTEGER DEFAULT 1);
    CREATE INDEX IF NOT EXISTS ix_um_user ON user_memories(user_id);
    CREATE TABLE IF NOT EXISTS rag_traces(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      query_chars INTEGER, rewritten INTEGER, retrieved TEXT, sources TEXT, model TEXT, provider TEXT,
      latency_ms INTEGER, outcome TEXT, at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_rt_user ON rag_traces(user_id, at);
    """)


# ------------------------------------------------------------------ 1. user documents
def _inr(v) -> str:
    if v is None or v == "":
        return "not shown in report"
    try:
        n = round(float(v))
    except (TypeError, ValueError):
        return "not shown in report"
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    return ("-" if n < 0 else "") + "Rs " + s


def build_docs(c, uid: int, rep: Optional[dict]) -> List[Dict[str, Any]]:
    """The user's current data as documents. Pure read; identifiers are never included."""
    docs: List[Dict[str, Any]] = []

    def add(key, typ, title, text, **meta):
        docs.append({"doc_key": key, "doc_type": typ, "title": title, "text": text, "meta": meta})

    if rep:
        s = rep.get("summary", {})
        add("report:summary", "report", f"Your {rep.get('bureau')} report summary",
            f"{rep.get('bureau')} credit report dated {rep.get('report_date')}. Credit score {rep.get('score') if rep.get('score') is not None else 'not shown'}"
            f" ({rep.get('band') or 'no band'}). Credit card usage {str(rep.get('utilisation')) + ' percent of limits' if rep.get('utilisation') is not None else 'not known'}."
            f" Active accounts {s.get('active_accounts')} of {s.get('total_accounts')}. Total overdue {_inr(s.get('total_overdue'))}."
            f" Hard enquiries in last 6 months {rep.get('enquiries_6m')}. Source: {rep.get('source')}.",
            bureau=rep.get("bureau"), report_date=rep.get("report_date"), score=rep.get("score"), source=rep.get("source"))
        for i, a in enumerate(rep.get("accounts", [])):
            card = a.get("type") == "Credit Card"
            amounts = (f"Credit limit {_inr(a.get('limit'))}. Current balance {_inr(a.get('balance'))}." if card
                       else f"Sanctioned {_inr(a.get('sanctioned'))}. Outstanding {_inr(a.get('balance'))}.")
            late = a.get("dpd_events")
            add(f"report:account:{a.get('ref') or i}", "account", f"Your {a.get('lender')} {a.get('type')}",
                f"{a.get('lender')} {a.get('type')} account, status {a.get('status')}. Opened {a.get('opened') or 'date not shown'}. {amounts}"
                f" Overdue {_inr(a.get('overdue'))}. Late months in last 24: {late if late is not None else 'payment history not shown'}."
                f" {'Secured' if a.get('secured') else 'Unsecured'} credit." + (" This is a credit card." if card else " This is a loan."),
                lender=a.get("lender"), type=a.get("type"), status=a.get("status"), opened=a.get("opened"), card=card)
        enq = rep.get("enquiries", [])
        if enq:
            lines = "; ".join(f"{e.get('lender')} for {e.get('purpose')} on {e.get('date')}" for e in enq[:30])
            add("report:enquiries", "enquiries", "Your credit enquiries",
                f"{len(enq)} credit enquiries on the report, {rep.get('enquiries_6m')} in the last 6 months. Hard enquiries (applications for loans or cards): {lines}.",
                count=len(enq), last_6m=rep.get("enquiries_6m"))
        for f in rep.get("factors", []):
            add(f"report:factor:{f.get('key')}", "factor", f"Score factor: {f.get('name')}",
                f"Score factor {f.get('name')}: grade {f.get('grade')}. What the report shows: {f.get('value')}. Guidance: {f.get('tip')}",
                factor=f.get("key"), grade=f.get("grade"))
        for i, e in enumerate(rep.get("errors", [])):
            add(f"report:error:{i}", "error", f"Possible error: {e.get('title')}",
                f"Possible error in the report: {e.get('title')} for {e.get('lender')}, severity {e.get('severity')}. {e.get('detail') or ''} It can be disputed in the Disputes screen.",
                lender=e.get("lender"), severity=e.get("severity"))
    # ---- time: earlier reports, month-to-month changes, per-account history (exact, computed in code)
    import report_history as rh
    snaps = rh.snapshots(c, uid)
    for old in snaps[1:]:
        so = old.get("summary", {})
        add(f"report:summary:{old.get('snapshot_id')}", "report_old", f"Your earlier report of {rh._label(old)}",
            f"Earlier {old.get('bureau')} report dated {rh._label(old)}: score {old.get('score') if old.get('score') is not None else 'not shown'}, "
            f"card usage {str(old.get('utilisation')) + ' percent' if old.get('utilisation') is not None else 'not known'}, "
            f"active accounts {so.get('active_accounts')} of {so.get('total_accounts')}, enquiries in 6 months {old.get('enquiries_6m')}. This is not the latest report.",
            report_date=rh._label(old), is_latest=False, score=old.get("score"))
    for pair in rh.timeline(snaps)[:12]:
        if not pair["changes"]:
            add(f"change:{pair['to']}:none", "change", f"No changes between {pair['from']} and {pair['to']}",
                f"Comparing the reports of {pair['from']} and {pair['to']}: nothing changed.", **{"from": pair["from"], "to": pair["to"], "kind": "none"})
        for i, e in enumerate(pair["changes"][:20]):
            add(f"change:{pair['to']}:{i}", "change", f"Change by {pair['to']}: {e['title']}", e["text"], **{**e["meta"], "kind": e["kind"]})
    for k, h in rh.account_histories(snaps).items():
        if len(h["points"]) < 2:
            continue
        pts = "; ".join(f"{p['date']}: {p['status']}, balance {_inr(p['balance'])}, overdue {_inr(p['overdue'])}, late months {p['late_months'] if p['late_months'] is not None else 'not shown'}"
                        for p in h["points"][-12:])
        add(f"history:account:{k}", "account_history", f"Your {h['lender']} {h['type']} month by month",
            f"{h['lender']} {h['type']} across your reports (oldest first): {pts}.", lender=h["lender"], type=h["type"], points=len(h["points"]))
    hist = [dict(r) for r in c.execute("SELECT month, score FROM score_history WHERE user_id=? ORDER BY month", (uid,))]
    if hist:
        add("history:score", "history", "Your score history", "Score history by month: " + ", ".join(f"{h['month']} {h['score']}" for h in hist[-24:]) + ".",
            points=len(hist))
    for t in c.execute("SELECT id, title, detail, impact, week, done FROM plan_tasks WHERE user_id=?", (uid,)):
        add(f"plan:{t['id']}", "plan", f"Plan step: {t['title']}",
            f"90-day plan step (week {t['week']}, {t['impact']} impact): {t['title']}. {t['detail']} Status: {'done' if t['done'] else 'not done yet'}.",
            impact=t["impact"], done=bool(t["done"]))
    for l in c.execute("SELECT id, lender, kind, outstanding, emi, due_day, autopay FROM loans WHERE user_id=?", (uid,)):
        add(f"tracked:loan:{l['id']}", "tracked_loan", f"Your tracked {l['lender']} {l['kind']}",
            f"Loan the user added for reminders: {l['lender']} {l['kind']}. EMI {_inr(l['emi'])} due on day {l['due_day']} of each month. Outstanding {_inr(l['outstanding'])}."
            f" Autopay {'on' if l['autopay'] else 'off'}.", lender=l["lender"], due_day=l["due_day"])
    for k in c.execute("SELECT id, issuer, name, credit_limit, balance, min_due, due_day FROM cards WHERE user_id=?", (uid,)):
        add(f"tracked:card:{k['id']}", "tracked_card", f"Your tracked {k['issuer']} card",
            f"Credit card the user added for reminders: {k['issuer']} {k['name'] or ''}. Limit {_inr(k['credit_limit'])}, balance {_inr(k['balance'])}, minimum due {_inr(k['min_due'])},"
            f" bill due on day {k['due_day']}.", issuer=k["issuer"], due_day=k["due_day"])
    pays = [dict(r) for r in c.execute("SELECT lender, amount, provider, on_time, at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 12", (uid,))]
    if pays:
        add("payments:recent", "payments", "Your recorded payments",
            "Recent payments the user recorded: " + "; ".join(
                f"{_inr(p['amount'])} to {p['lender']} on {str(p['at'])[:10]}{' (paid outside Fikarta)' if p['provider'] == 'self_reported' else ''}" for p in pays) + ".",
            count=len(pays))
    for d in c.execute("SELECT id, lender, issue, bureau, status, created_at, due_by FROM disputes WHERE user_id=?", (uid,)):
        add(f"dispute:{d['id']}", "dispute", f"Your dispute with {d['bureau'] or 'the bureau'}",
            f"Dispute about {d['lender']}: {d['issue']}. Sent to {d['bureau'] or 'bureau'}. Status {d['status']}. Created {str(d['created_at'])[:10]}."
            + (f" Bureau reply due by {d['due_by']}." if d["due_by"] else ""), status=d["status"], lender=d["lender"])
    return docs


def sync_user_docs(c, uid: int, rep: Optional[dict], now: str) -> Dict[str, int]:
    """Upserts changed documents and deletes stale ones. Returns counts (for tests and traces)."""
    docs = build_docs(c, uid, rep)
    existing = {r["doc_key"]: r["content_hash"] for r in c.execute("SELECT doc_key, content_hash FROM user_docs WHERE user_id=?", (uid,))}
    written = 0
    keys = set()
    for d in docs:
        keys.add(d["doc_key"])
        h = hashlib.sha256((d["title"] + "\n" + d["text"] + json.dumps(d["meta"], sort_keys=True, default=str)).encode()).hexdigest()
        if existing.get(d["doc_key"]) == h:
            continue
        c.execute("INSERT INTO user_docs(user_id,doc_key,doc_type,title,text,meta,content_hash,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(user_id,doc_key) DO UPDATE SET doc_type=excluded.doc_type, title=excluded.title, text=excluded.text, "
                  "meta=excluded.meta, content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                  (uid, d["doc_key"], d["doc_type"], d["title"], d["text"], json.dumps(d["meta"], default=str), h, now))
        written += 1
    stale = [k for k in existing if k not in keys]
    for k in stale:
        c.execute("DELETE FROM user_docs WHERE user_id=? AND doc_key=?", (uid, k))
    return {"docs": len(docs), "written": written, "deleted": len(stale)}


# ------------------------------------------------------------------ 3. retrieval
INTENTS = [  # (question words, doc types / account flag to boost)
    ({"card", "cards", "limit", "utilisation", "utilization", "usage", "कार्ड"}, {"account:card", "tracked_card", "factor:utilisation", "change:card"}),
    ({"loan", "loans", "emi", "emis", "outstanding", "लोन", "ईएमआई"}, {"account:loan", "tracked_loan"}),
    ({"enquiry", "enquiries", "inquiry", "inquiries", "applied", "application", "applications", "पूछताछ"}, {"enquiries", "factor:enquiries", "change:enquiry"}),
    ({"late", "missed", "overdue", "dpd", "delay", "delayed", "default", "payment", "payments", "paid"}, {"factor:payment", "payments", "account:any"}),
    ({"score", "cibil", "crif", "स्कोर", "band", "drop", "dropped", "fell", "increase", "improve", "badha", "badhe"}, {"report", "factor:any", "history", "change:score"}),
    ({"error", "errors", "wrong", "mistake", "dispute", "disputes", "गलती"}, {"error", "dispute"}),
    ({"plan", "steps", "task", "tasks", "next", "should"}, {"plan"}),
    ({"history", "trend", "month", "months", "last"}, {"history"}),
]
TEMPORAL = {"change", "changed", "changes", "previous", "before", "earlier", "since", "history", "trend", "compare", "compared", "last", "month",
            "months", "ago", "drop", "dropped", "fell", "fall", "rose", "increased", "decreased", "went", "new", "why", "pichle", "pehle", "badla",
            "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "january", "february", "march", "april", "june",
            "july", "august", "september", "october", "november", "december", "बदला", "पिछले", "पहले"}
HISTORY_TYPES = {"change", "account_history", "report_old", "history"}


def _doc_tags(d) -> set:
    tags = {d["doc_type"]}
    meta = d.get("meta") or {}
    if d["doc_type"] == "account":
        tags |= {"account:any", "account:card" if meta.get("card") else "account:loan"}
    if d["doc_type"] == "factor":
        tags |= {"factor:any", f"factor:{meta.get('factor')}"}
    if d["doc_type"] == "change":
        k = meta.get("kind") or ""
        tags |= {"change:any", f"change:{k}"}
        tags |= {"change:score"} if k == "score" else {"change:card"} if k == "utilisation" else {"change:enquiry"} if k == "enquiries" else set()
    return tags


def _bm25(query_toks: List[str], corpus: List[Dict[str, Any]]) -> List[float]:
    N = len(corpus)
    if not N or not query_toks:
        return [0.0] * N
    tfs = [Counter(tokens(d["title"] + " " + d["title"] + " " + d["text"])) for d in corpus]
    lens = [sum(t.values()) for t in tfs]
    avg = (sum(lens) / N) or 1.0
    df = Counter()
    for t in tfs:
        df.update(t.keys())
    k1, b = 1.4, 0.7
    out = []
    for tf, ln in zip(tfs, lens):
        s = 0.0
        for q in query_toks:
            f = tf.get(q)
            if f:
                idf = math.log(1 + (N - df[q] + 0.5) / (df[q] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * ln / avg))
        out.append(s)
    return out


def retrieve(c, uid: int, queries: Iterable[str], k: int = 6) -> List[Dict[str, Any]]:
    """Top-k of this user's documents + memories for the query variants (reciprocal-rank fusion).
    The SQL filter on user_id is the isolation boundary: nothing from another user can be a candidate."""
    corpus = [{"key": r["doc_key"], "doc_type": r["doc_type"], "title": r["title"], "text": r["text"], "meta": json.loads(r["meta"] or "{}")}
              for r in c.execute("SELECT doc_key, doc_type, title, text, meta FROM user_docs WHERE user_id=?", (uid,))]
    corpus += [{"key": f"memory:{r['id']}", "doc_type": "memory", "title": f"Something you told Fikarta AI ({r['kind']})", "text": r["text"], "meta": {"kind": r["kind"]}}
               for r in c.execute("SELECT id, kind, text FROM user_memories WHERE user_id=?", (uid,))]
    if not corpus:
        return []
    fused: Dict[str, float] = {}
    best: Dict[str, float] = {}
    for q in [q for q in queries if q and q.strip()]:
        qt = tokens(q)
        qset = set(qt)
        boosts = set()
        for words, tags in INTENTS:
            if qset & words:
                boosts |= tags
        temporal = bool(qset & TEMPORAL)
        scores = _bm25(qt, corpus)
        ranked = []
        for d, s in zip(corpus, scores):
            tags = _doc_tags(d)
            if boosts & tags:
                s = s * 1.6 + 0.8  # intent match: also surfaces docs with no word overlap (e.g. 'why did my score drop')
            if d["doc_type"] in HISTORY_TYPES:
                # time questions want the computed changes first, then history; other questions see the latest month first
                if temporal:
                    s = s * 2.2 + 1.5 if d["doc_type"] == "change" else s * 1.4 + 0.5
                else:
                    s = s * (0.6 if d["doc_type"] == "change" else 0.25)
            if d["doc_type"] == "memory":
                s += 0.3  # small prior: things the user told us are usually relevant to their own questions
            if s > 0:
                ranked.append((s, d["key"]))
        ranked.sort(key=lambda x: -x[0])
        for rank, (s, key) in enumerate(ranked[:20]):
            fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank)
            best[key] = max(best.get(key, 0.0), s)
    by_key = {d["key"]: d for d in corpus}
    top = sorted(fused, key=lambda key: -fused[key])[:k]
    if "report:summary" in by_key and "report:summary" not in top:
        # anchor: every personal answer can see the latest summary, without pushing better matches down
        top = (top[:k - 1] if len(top) >= k else top) + ["report:summary"]
        fused.setdefault("report:summary", 0.0)
    return [{**by_key[key], "score": round(best.get(key, 0.0), 3), "rrf": round(fused[key], 5)} for key in top]


# ------------------------------------------------------------------ 5. memory
_PII = [
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.I),               # PAN
    re.compile(r"\b(?:\+?91[\s-]?)?[6-9]\d{9}\b"),            # Indian mobile
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),                    # email
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),                  # Aadhaar-like
    re.compile(r"\b\d{9,18}\b"),                               # account / card numbers
    re.compile(r"\b(otp|password|cvv|pin)\b", re.I),
]
MEMORY_KINDS = {"goal", "plan", "situation", "preference", "concern"}


def clean_memory(text: str) -> Optional[str]:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not (8 <= len(t) <= 240):
        return None
    if any(p.search(t) for p in _PII):
        return None
    return t


def _similar(a: str, b: str) -> float:
    x, y = set(tokens(a)), set(tokens(b))
    return len(x & y) / len(x | y) if x and y else 0.0


def save_memories(c, uid: int, items: List[Dict[str, str]], now: str) -> int:
    """Validated, de-duplicated upsert. Near-duplicates refresh the existing memory instead of adding one."""
    saved = 0
    existing = [dict(r) for r in c.execute("SELECT id, kind, text FROM user_memories WHERE user_id=?", (uid,))]
    for it in items[:5]:
        kind = str(it.get("kind", "")).lower().strip()
        text = clean_memory(str(it.get("text", "")))
        if kind not in MEMORY_KINDS or not text:
            continue
        dup = next((e for e in existing if _similar(e["text"], text) >= 0.6), None)
        if dup:
            c.execute("UPDATE user_memories SET text=?, kind=?, updated_at=?, times_seen=times_seen+1 WHERE id=?", (text, kind, now, dup["id"]))
        else:
            cur = c.execute("INSERT INTO user_memories(user_id,kind,text,meta,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            (uid, kind, text, json.dumps({"source": "chat"}), now, now))
            existing.append({"id": cur.lastrowid, "kind": kind, "text": text})
        saved += 1
    # keep the newest MAX_MEMORIES
    c.execute("DELETE FROM user_memories WHERE user_id=? AND id NOT IN (SELECT id FROM user_memories WHERE user_id=? ORDER BY updated_at DESC, id DESC LIMIT ?)",
              (uid, uid, MAX_MEMORIES))
    return saved


def forget_all(c, uid: int, what=("user_docs", "user_memories", "rag_traces")):
    for t in what:
        c.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))


def trace(c, uid: int, now: str, *, query: str, rewritten: bool, retrieved: List[Dict[str, Any]], sources: List[Dict[str, Any]],
          provider: str, model: str, latency_ms: int, outcome: str):
    c.execute("INSERT INTO rag_traces(user_id,query_chars,rewritten,retrieved,sources,model,provider,latency_ms,outcome,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (uid, len(query), int(rewritten), json.dumps([{"key": d["key"], "type": d["doc_type"], "score": d.get("score"), "rrf": d.get("rrf")} for d in retrieved]),
               json.dumps([{"url": s.get("url"), "score": s.get("score")} for s in sources]), model, provider, latency_ms, outcome, now))


_SCRUB = [
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.I), "[PAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[Aadhaar]"),
    (re.compile(r"\b(?:\+?91[\s-]?)?[6-9]\d{9}\b"), "[phone]"),
    (re.compile(r"\b\d{9,18}\b"), "[number]"),
]


def scrub(text: str) -> str:
    """Masks identifiers a user may type (PAN, email, Aadhaar, phone, account/card numbers) before any text
    leaves Fikarta for the AI provider. The user's own chat record keeps what they wrote."""
    t = text or ""
    for rx, repl in _SCRUB:
        t = rx.sub(repl, t)
    return t

