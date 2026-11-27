"""Check what Surepass returns for YOUR details, and what Fikarta reads from it - side by side.

Run it yourself (it asks for your details; nothing is saved or sent anywhere except Surepass):

    cd D:\\fikarta-web\\fikarta-backend
    python scripts\\check_surepass.py

It prints:
  1. Surepass's raw answer: status, the name on the report, score, report dates and every account.
  2. Fikarta's reading of the same answer: score, report date, accounts and any warnings.
If the name or accounts are not yours, Surepass returned test data (sandbox) rather than your bureau report.
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import httpx  # noqa: E402

import surepass_crif as sp  # noqa: E402


def ask(prompt, secret=False):
    v = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    if not v:
        sys.exit("Stopped: nothing entered.")
    return v


def main():
    env = (input("Environment [sandbox/production] (Enter = sandbox): ").strip().lower() or "sandbox")
    if env not in sp.BASES:
        sys.exit("Environment must be sandbox or production")
    token = os.getenv("SUREPASS_TOKEN") or ask("Surepass token (hidden): ", secret=True)
    first = ask("First name as on PAN: ")
    last = input("Last name as on PAN (Enter if none): ").strip()
    mobile = ask("Mobile number: ")
    pan = ask("PAN: ").upper()
    print("\nAsking Surepass…")
    try:
        r = httpx.post(sp.BASES[env] + sp.PATH, timeout=60,
                       json={"first_name": first, "last_name": last or first, "mobile": mobile, "pan": pan, "consent": "Y"},
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        sys.exit(f"Could not reach Surepass: {type(e).__name__}")
    try:
        j = r.json()
    except ValueError:
        sys.exit(f"HTTP {r.status_code}: not a JSON answer")
    import json
    out = os.path.abspath("surepass_response.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(j, f, indent=2, ensure_ascii=False)
    print(f"\n=== Full JSON from Surepass (also saved to {out}) ===")
    print(json.dumps(j, indent=2, ensure_ascii=False))
    print(f"\n=== 1. What Surepass returned ({env}) ===")
    print(f"HTTP {r.status_code} | success={j.get('success')} | message={j.get('message')} | code={j.get('message_code')}")
    d = j.get("data") or {}
    if not d:
        return
    cr = d.get("credit_report") or {}
    hdr = cr.get("HEADER") or {}
    print(f"Name on report : {d.get('first_name')} {d.get('last_name')}")
    print(f"credit_score   : {d.get('credit_score')}")
    print(f"SCORES block   : {(cr.get('SCORES') or {}).get('SCORE')}")
    print(f"Report dates   : issue={hdr.get('DATE-OF-ISSUE')} request={hdr.get('DATE-OF-REQUEST')} status={hdr.get('STATUS')}")
    resp = (cr.get("RESPONSES") or {}).get("RESPONSE")
    resp = resp if isinstance(resp, list) else ([resp] if resp else [])
    print(f"Accounts       : {len(resp)}")
    for x in resp:
        ld = (x or {}).get("LOAN-DETAILS") or {}
        print(f"  - {ld.get('CREDIT-GUARANTOR')} | {ld.get('ACCT-TYPE')} | {ld.get('ACCOUNT-STATUS')} | balance {ld.get('CURRENT-BAL')} | "
              f"limit {ld.get('CREDIT-LIMIT')} | opened {ld.get('DISBURSED-DT')} | match {ld.get('MATCHED-TYPE')}")
    print(f"Top-level report sections: {list(cr.keys())}")

    print("\n=== 2. What Fikarta reads from it ===")
    try:
        m = sp.map_surepass_crif(j)
    except sp.BureauError as e:
        print(f"Fikarta could not read this answer: {e.kind} {e.detail}")
        return
    print(f"Score: {m['score']} | report date: {m['report_date']} | model: {m.get('score_model')}")
    for a in m["accounts"]:
        print(f"  - {a['lender']} | {a['type']} | {a['status']} | balance {a['balance']} | limit {a['limit']} | late months {a['dpd_events']}")
    for i in m["issues"]:
        print(f"  ! {i['code']}: {i['message']}")


if __name__ == "__main__":
    main()
