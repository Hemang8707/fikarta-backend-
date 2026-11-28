# Fikarta AI: retrieval-augmented architecture

This document records how Fikarta AI is built and why. It is the reference for anyone changing the
assistant, the user data store or the bureau integration. Code lives in `server/`; tests in `tests/mobile/`.

## 1. Goals and non-negotiables

| Goal | How it is met |
|---|---|
| Answers about the user's own credit are exact | Numbers come only from deterministic code (credit engine, change engine), passed to the model as FACTS. The model never calculates. |
| The assistant knows the user, now and over time | Per-user document store (latest data + every earlier report + month-to-month changes + account histories) and long-term memory. |
| Every claim is traceable | Mandatory citations: `[U#]` for the user's own records, `[S#]` for guides/RBI texts. Invalid markers are stripped. Every turn writes a trace. |
| Users never see each other's data | `user_id` filter in SQL is the only path to candidates; tested. |
| Personal identifiers never reach the AI provider | Documents are built without name/PAN/phone/email/account numbers; the user's message is scrubbed before any model call. |
| Unknown is never zero | Missing bureau values stay `None` everywhere: mapper, engine, change engine, documents, prompts. |
| Fits the server | 2 vCPU / ~1 GB RAM: lexical retrieval with inverted index and caching; no in-process embedding model. |

## 2. Data sources

1. **Credit report snapshots** (`reports` table). Every saved report is a full, immutable snapshot:
   CRIF fetched through Surepass (`surepass_crif.py`, `bureau_fetch.py`), uploaded PDF/CRIF JSON, or manual entry.
   Bureau data changes monthly; each new fetch adds a snapshot, nothing is overwritten (up to 24 used).
2. **User activity in Fikarta**: plan steps, tracked loans and cards, recorded payments, disputes, score history.
3. **Long-term memory** (`user_memories`): durable facts the user stated in chat.
4. **Knowledge base**: Fikarta guides/articles (`db/fikarta.db`) and official RBI pages (`db/official_kb.db`,
   refreshed monthly by `scripts/ingest_official.py` into an untracked live copy).
5. **Usage events** (`app_events`, optional consent): screens/tools used, for personalising the app.
   Never sent to the AI provider.

## 3. Pipeline for one chat turn (`mobile_api.chat`)

```
user message
  -> consent check (ai_chat)
  -> scrub identifiers (user_rag.scrub)                       PAN/phone/email/Aadhaar/account numbers masked
  -> sync user documents (user_rag.sync_user_docs)            idempotent; hash per document; stale deleted
  -> exact change facts (report_history.change_facts)          latest vs previous snapshot, computed in code
  -> query rewrite + keywords (ai_assistant.rewrite_query)     fast model, JSON, fail-soft
  -> retrieve user context (user_rag.retrieve)                 BM25 + intent boosts + time awareness, RRF over query variants
  -> retrieve guides (ai_assistant.search_sources)             inverted-index BM25, RRF over query variants, cached
  -> generate (ai_assistant.answer)                            FACTS + USER CONTEXT [U#] + SOURCES [S#]; strict rules
  -> validate citations, store messages, write trace
  -> after response: extract memories from the user's own words (background task)
```

### 3.1 User document store (`user_rag.py`, table `user_docs`)

One small document per item, each with `doc_type`, `title`, `text` and JSON `meta`:

| doc_type | Content | Example meta |
|---|---|---|
| `report` | latest report summary | bureau, report_date, score, source |
| `account` | each account on the latest report | lender, type, status, opened, card |
| `enquiries` | hard enquiries list | count, last_6m |
| `factor` | each score factor with grade and guidance | factor, grade |
| `error` | possible report errors | lender, severity |
| `report_old` | each earlier report summary | report_date, is_latest=false |
| `change` | each month-to-month change | from, to, kind, old, new, delta |
| `account_history` | one account across months | lender, type, points |
| `history` | score by month | points |
| `plan`, `tracked_loan`, `tracked_card`, `payments`, `dispute` | Fikarta activity | as relevant |

Sync runs at the start of every chat turn. Each document's content hash is compared with the stored one, so
unchanged documents are not rewritten and removed ones are deleted. This avoids missed update hooks: whatever
changed (new report, paid EMI, closed dispute) is reflected on the next question.

### 3.2 Time awareness (`report_history.py`)

- Accounts are matched across snapshots by masked account number, otherwise lender + type + open date.
- `diff(prev, cur)` produces exact events: score, card usage, new/closed/disappeared accounts, status changes,
  balance and overdue changes (thresholded), new late months, new enquiries. A value that appears or disappears
  is described as such, never as a jump from or to zero.
- The latest change set is added to FACTS, so "why did my score drop" is answered from computed changes.
- Retrieval detects time questions (change, last month, before, since, month names, Hindi equivalents).
  Time questions boost `change` first, then history; other questions down-weight history so the latest month
  wins.
- `GET /api/m/credit/changes` exposes the same timeline to the app.

### 3.3 Retrieval

- **User store**: BM25 over the user's documents and memories (tens to a few hundred items), intent boosts
  (card, loan, enquiry, late payment, score, errors, plan, history), time awareness, small prior for memories,
  reciprocal-rank fusion across query variants (original, rewritten, keywords). The latest report summary is
  always included as an anchor without displacing better matches.
- **Knowledge base**: BM25 with a prebuilt inverted index (term -> postings), precomputed IDF, official RBI
  texts boosted x1.25, results fused across query variants and cached (bounded, cleared on rebuild).
  Measured: 7.7 ms -> 2.4 ms per uncached search on a dev PC; cached repeats are sub-millisecond.
- **Why not embeddings yet**: a local embedding model needs more memory than the 1 GB server has, and an
  external embeddings API would add another processor of user data. Query rewriting and keyword expansion give
  much of the semantic recall. The store keeps documents small and typed so a vector column can be added later
  (bigger instance or an approved embeddings provider) without changing the pipeline.

### 3.4 Generation rules (`ai_assistant.build_messages`)

- FACTS (structured, plain-English keys) are the only source of the user's numbers; FACTS beat USER CONTEXT.
- Every sentence using the user's records ends with `[U#]`; every rule or general fact with `[S#]`.
- No eligibility/approval/rate predictions; never recommend taking new credit; no markdown; Hindi when chosen.
- Provider: Groq `openai/gpt-oss-120b`, retry on rate limit, fallback `gpt-oss-20b`, fails closed (503).

### 3.5 Long-term memory (`user_memories`)

- Extracted after the reply, from the user's own message only (never the assistant's reply), by the fast model
  with a strict JSON schema: kinds goal, plan, situation, preference, concern.
- PII filter rejects PAN, phone, email, Aadhaar-like and long numbers, OTP/password/CVV/PIN mentions.
- De-duplicated by token overlap (near-duplicates refresh the existing memory), capped at 60 per user.
- User control: `GET/DELETE /api/m/ai/memories` and the "What Fikarta AI remembers" screen.

### 3.6 Traces (`rag_traces`)

Per turn: query length, whether it was rewritten, retrieved document keys with scores, knowledge-base URLs,
model, provider, latency and outcome. Never document text or the message. Kept 90 days (`scripts/retention.py`).

## 4. Bureau data (CRIF via Surepass)

- Request uses only the PAN and name on the account; PAN locked after a fetch; one PAN per account
  (released when an account is deleted). Explicit consent per pull, digest stored.
- Limits: one successful fetch per `SUREPASS_COOLDOWN_DAYS` (30), 3 attempts/day, daily cap across users.
- Mapper: Indian-grouped amounts read exactly, DD-MM-YYYY dates, DPD history to late months, secondary matches
  excluded and reported, score from the bureau's score block.
- The raw response is never stored; only the mapped report and the response *shape* (field paths and value
  types, no values) for checking the mapping against what Surepass really sends.
- Monthly refresh: `scripts/refresh_reminder.py` notifies users when a fresh report can be fetched. Fikarta never
  fetches without the user's tap and consent.

## 5. Privacy and lawful data use (DPDP Act 2023, RBI digital lending guidelines)

- Purpose-bound consents: credit_report (183 days), reminders, ai_chat, analytics, marketing. Each has a notice
  text; a digest of the exact notice is stored with every consent.
- Not collected, by design: contacts, SMS, call logs, files/media, precise location, or any data without a stated
  purpose. RBI's digital lending guidelines bar lending-related apps from these phone resources.
- IP address: only in the security audit log, not in analytics or AI data.
- Withdrawals and erasure: turning off ai_chat deletes chats, memories, user documents and traces; turning off
  analytics deletes usage events; credit-data erasure clears reports and the AI store; account deletion cascades.
- Export (`/api/m/me/export`) includes memories, user documents, traces and usage events.

## 6. Operations

- Nightly backups of all databases + the encryption key (`scripts/backup_db.sh`), health check every 2 minutes,
  monthly RBI refresh, daily refresh reminders and retention (`deploy/fikarta.cron`).
- Tests: `tests/mobile/test_rag.py` (store, isolation, relevance, memory, end-to-end, privacy),
  `test_history.py` (exact diffs, unknown handling, time-aware retrieval, scrubbing, index equivalence, usage
  events), `test_bureau_fetch.py` (mapping, consent, PAN lock, limits). The suite also runs on the production
  server in an isolated copy before release.

## 7. Next upgrades (need resources or decisions)

1. Dense embeddings + hybrid search: needs a 2 GB+ instance or an approved embeddings provider.
2. Mobile OTP before bureau fetch (needs an SMS provider) to prove the number belongs to the user.
3. Offline evaluation set of real (consented, anonymised) questions with expected documents, run in CI.
4. Move from SQLite to PostgreSQL when concurrent users grow; the store's schema maps directly.

## 8. Additions (27 Nov 2026)

### 8.1 Customer details from chat (`user_profile.py`, table `user_profile_facts`)
The fast model extracts structured details from the user's own message (never the assistant's reply):
monthly income, employment type, employer, industry, city, dependents, housing, rent, other EMIs, savings.
Every value is validated in code (types, ranges, enums; identifiers rejected). Values are versioned
(`valid_from`/`valid_to`), so a raise or a move keeps the earlier value with dates. The assistant receives them
in FACTS as "Details the user told Fikarta (self-reported, not verified)". Users view, correct and delete them
(`/api/m/ai/profile`, "What Fikarta AI remembers"). Money amounts are kept out of free-text memories.

### 8.2 Answer format
Markdown: a one-line answer, bullets, numbered steps, tables (at most 4 columns) for comparisons; rendered in
the app with kit colours, no bold, horizontally scrollable tables. `fix_tables` repairs a missing header divider.

### 8.3 Figure check (`grounding.py`)
Every rupee amount in a draft must come from FACTS, USER CONTEXT, SOURCES or the user's own messages, or be
simple arithmetic on them (10-60%, /12, x12, sums and differences). Earlier assistant replies are not a source,
so a wrong figure cannot be repeated. Amounts presented as estimates must match a real figure exactly.
Ungrounded draft -> one rewrite with a correction; still ungrounded -> the offending lines are removed.
The outcome is recorded in the trace (`answered`, `answered_retried`, `answered_trimmed`).

### 8.4 Model fallback
Groq `gpt-oss-120b` (retry, then `gpt-oss-20b`) -> Google Gemini `gemini-3.5-flash` (then `gemini-2.5-flash`)
when Groq fails; optional JSON steps fall back the same way. Gemini thinking is kept low so answers are never
truncated; truncated or blocked responses are never shown. Keys only in the server `.env`; the privacy notice
names both processors.

### 8.5 Profile completeness and team accounts
`GET /api/m/profile/completeness` drives the Home checklist (name as on PAN, PAN, mobile, credit consent,
report, work and income, Fikarta AI). `scripts/create_user.py` creates team accounts with a hidden password
prompt; such users accept the privacy notice and Terms themselves on first login (`/api/m/privacy/accept-terms`).

### 8.6 Exact website data, answer style and scope (28 Nov 2026)
- `site_data.py` spots the entities a question names - city, one or two lenders, loan type, loan amount, salary,
  loan app - and turns the matching rows of the website database (cities, lenders, comparisons, loan_amounts,
  salaries, loan-type calculators, loan_apps) into "Fikarta data" sources with exact figures. They are placed
  first in SOURCES, so answers like "personal loan in Delhi" or "Axis Bank vs HDFC Bank" quote the database.
- Style: friendly, plain words, answer only what was asked (usually under 90 words); ask ONE short question when
  a needed detail is missing. Details the user gives (income, loan amount needed, purpose, city, ...) are saved
  to the versioned profile and used next time.
- Scope: only credit, loans, cards, EMIs, RBI rules, loan apps and Fikarta features; other topics get a one-line
  polite refusal. A deterministic guard answers requests for internals (prompt, keys, providers) or other
  people's data with a fixed reply before any model call. Per-user retrieval stays filtered by user_id.
