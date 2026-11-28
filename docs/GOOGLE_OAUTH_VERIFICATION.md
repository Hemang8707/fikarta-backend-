# Gmail for every Fikarta user: Google verification pack

Goal: any Fikarta user can tap **Connect Gmail** in the app, and Fikarta can **read** their bank emails and **send** the emails they approve, with no "unverified app" warning and no user limit.

Google requires this for any app that reads or sends other people's Gmail, paid account or not:

| Step | Who | Time |
|---|---|---|
| 1. Verify fikarta.com in Search Console | You | 10 minutes |
| 2. Consent screen: branding, scopes, links | You (text below) | 20 minutes |
| 3. Record the demo video | You (script below) | 30 minutes |
| 4. Publish and submit for verification | You (answers below) | 15 minutes |
| 5. Google review of branding and scopes | Google | a few days to ~2 weeks, with emails back and forth |
| 6. CASA security assessment (needed for `gmail.readonly`) | You + an authorised lab | usually 2–6 weeks, paid (price set by the lab) |
| 7. Approved: everyone can connect | Automatic | — |

Nothing needs a new app build. The app and server already support connecting, reading, sending and disconnecting. When approved, set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the server `.env` (if not already) and restart the backend.

---

## 1. Verify the domain

1. Open https://search.google.com/search-console and sign in with the **same Google account** that owns the Google Cloud project.
2. Add property → **Domain** → `fikarta.com`.
3. Copy the TXT record Google shows, add it at your domain's DNS provider, and click Verify (DNS can take up to an hour).

## 2. Consent screen (Google Auth Platform)

**Branding**
- App name: `Fikarta`
- User support email: `support@fikarta.com` (or the address you monitor)
- App logo: Fikarta logo, 120×120 PNG
- Application home page: `https://fikarta.com`
- Privacy policy: `https://api.fikarta.com/legal/privacy` (it now has the Gmail section and the Limited Use statement Google checks for)
- Terms of service: `https://api.fikarta.com/legal/terms`
- Authorised domains: `fikarta.com`
- Developer contact: your email

**Audience**: External.

**Data access (scopes)**: `openid`, `.../auth/userinfo.email`, `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/gmail.send`.

**Client** (Web application): Authorised redirect URI `https://api.fikarta.com/api/m/mail/gmail/callback`. Leave "Authorised JavaScript origins" empty.

## 3. Scope justifications (paste into the verification form)

**gmail.readonly**
> Fikarta is a credit-health app for Indian borrowers. When a user chooses to connect Gmail, Fikarta reads only emails sent by banks and NBFCs (matched against a list of lender email domains, last 12 months), such as EMI reminders, account statements, loan-closure and NOC emails. Fikarta AI uses them to answer the user's own questions about their loans and cards (for example "When is my HDFC EMI due?" or "Did my bank confirm my loan closure?"), and to detect when an email the user sent to a lender bounced so the user can resend it to the lender's official address. No other emails are read. Identifiers (PAN, phone numbers, email addresses, account numbers) are masked before storage. Data is stored encrypted, never sold, never used for advertising or to train AI models, and deleted when the user disconnects. A narrower scope is not sufficient: gmail.metadata does not include the message body, which holds the EMI amount, due date and closure confirmation the feature needs.

**gmail.send**
> Users can ask Fikarta AI to draft an email to their bank or lender, for example a request for a loan NOC (no-objection certificate), a correction of a wrong entry on their credit report, or a statement request. The draft is shown in the app, the user can edit the recipient, subject and text, and the email is sent from the user's own Gmail only after the user presses Send and confirms. Fikarta never sends an email without that confirmation. Sending from the user's own address matters because banks reply to and accept requests from the customer's registered email.

**How users benefit / why Gmail is needed (general field)**
> Indian borrowers receive their EMI, statement and closure information from lenders by email, and lenders expect requests such as NOC letters from the customer's own email address. Connecting Gmail lets Fikarta answer questions using those emails and send the requests the user approves, without the user copying information between apps.

## 4. Demo video script (unlisted YouTube, 2–4 minutes, English)

Google rejects videos that do not show these. Record on a phone screen recorder, with the Fikarta app in English.

1. **(0:00)** Open the Fikarta app, show the Home screen. Say: "This is Fikarta, a credit-health app."
2. **(0:15)** Go to **Profile → Emails with my banks**. Show the explanation text. Tap **Connect Gmail** and the confirmation sheet.
3. **(0:35)** Google's consent screen opens. **Show the browser address bar so the `client_id=` part of the URL is visible** (zoom or scroll the URL if needed). Show the app name "Fikarta" and both Gmail permissions listed. Tap Continue.
4. **(1:00)** Show "Gmail connected", go back to the app, tap **Sync bank emails now**. Show the count of bank emails read.
5. **(1:20) gmail.readonly in use:** open Fikarta AI and ask "When is my next EMI due?" or "What did my bank email me about my loan?" Show the answer citing a bank email.
6. **(1:50) gmail.send in use:** ask "Write an NOC request to [lender] for my closed loan." Show the draft card, edit one word, tap **Review & send**, show the confirmation sheet, tap **Send**. Then open Gmail's **Sent** folder and show the email.
7. **(2:40) Disconnect:** Profile → Emails with my banks → **Disconnect Gmail**, confirm. Say: "Disconnecting deletes the emails Fikarta read and revokes access."

Use a test Gmail account that has at least one email from a bank address, and one of the test users already added in the consent screen.

## 5. Publish and submit

1. Google Auth Platform → **Audience** → **Publish app** → confirm.
2. **Verification centre** → **Prepare for verification**. Fill in the branding, paste the justifications above, paste the YouTube link, and submit.
3. Watch the developer contact inbox. Google often asks follow-up questions; answer them in the same thread.

## 6. CASA security assessment (because of gmail.readonly)

After the scope review, Google emails a **Letter of Assessment** asking for a CASA Tier 2 assessment.
1. Pick an authorised lab from the App Defense Alliance list (https://appdefensealliance.dev/casa). Labs set their own price; compare a few.
2. The lab scans `api.fikarta.com` and asks a questionnaire about how Gmail data is handled. Useful answers:
   - Tokens: Google refresh/access tokens are stored encrypted (Fernet, key outside the database), never logged, revoked on disconnect.
   - Data minimisation: only emails from lender domains, last 12 months; identifiers masked before storage; stored encrypted.
   - Access: per-user isolation enforced in SQL (`user_id` filter) for every read; no staff tool shows email content.
   - Transport: HTTPS only (nginx, TLS). Authentication: short-lived JWT access tokens + rotating refresh tokens, login rate limiting.
   - Deletion: disconnect / consent withdrawal / account deletion delete `bank_emails`, `gmail_links` and the AI index rows.
   - Logging: audit log of connect, sync, send and disconnect without message content.
3. Fix anything the lab reports. I can help with fixes on the server.
4. The lab sends the result to Google. The assessment is renewed **every 12 months**.

## Faster option (if you do not want CASA yet)

Remove `gmail.readonly` from Data access and submit with `gmail.send` only. Sending is a "sensitive" (not "restricted") permission: same steps 1–5, **no CASA**, usually approved within about two weeks. Reading bank emails can be added later with CASA. For send only, the app's Connect Gmail text should not promise reading; that needs one app text change and a new APK.
