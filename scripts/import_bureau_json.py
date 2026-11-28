"""Load a bureau report JSON (Experian or CRIF) into an existing Fikarta account, in Fikarta's one report format.

    sudo -u www-data venv/bin/python scripts/import_bureau_json.py --email someone@fikarta.com --file report.json
    sudo -u www-data venv/bin/python scripts/import_bureau_json.py --email someone@fikarta.com --file report.json --own

Default (demo / team data): the report is loaded DE-IDENTIFIED. Its name, PAN, phone, email, address, IDs and
account numbers are dropped; every other field is stored (encrypted); the account keeps its own name, email,
password, phone and PAN.

--attach-raw: store the report's fields for a report already imported (no second report is created).

--own: the report belongs to this account holder. The report's PAN must match the PAN on the account (or the
account has none yet, in which case it is saved, encrypted). The complete raw report is kept encrypted.

Either way: the account's login (email, password) is never touched, the AI index is rebuilt for this user only,
and the action is written to the user's audit log.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import bureau_formats as bf  # noqa: E402
import user_rag  # noqa: E402
from mobile_api import audit, db, dec, enc, latest_report, mask_pan, migrate, now, save_bureau_raw, save_report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a bureau report JSON into an account")
    ap.add_argument("--email", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--own", action="store_true", help="the report is the account holder's own (PAN must match)")
    ap.add_argument("--attach-raw", action="store_true", help="only store the report's fields for the latest report (no new report)")
    a = ap.parse_args()
    with open(a.file, encoding="utf-8") as f:
        raw = json.load(f)
    try:
        draft = bf.map_any(raw)
    except (bf.FormatError, ValueError) as e:
        print(f"Cannot read this report: {e}")
        return 2
    migrate()
    c = db()
    try:
        u = c.execute("SELECT * FROM users WHERE lower(email)=?", (a.email.strip().lower(),)).fetchone()
        if not u:
            print("No account with that email")
            return 2
        who = bf.identity(raw)
        if a.own:
            acct_pan = dec(u["pan_enc"])
            if not who["pan"]:
                print("The report has no PAN, so it cannot be confirmed as this person's own. Run without --own to load it de-identified.")
                return 2
            if acct_pan and acct_pan != who["pan"]:
                print(f"PAN mismatch: account has {mask_pan(acct_pan)}, report has {mask_pan(who['pan'])}. Not imported.")
                return 2
            if not acct_pan:
                c.execute("UPDATE users SET pan_enc=?, onboarded=1 WHERE id=?", (enc(who["pan"]), u["id"]))
                u = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if a.attach_raw:
            rep = latest_report(c, u["id"], raw=True) or {}
            if rep.get("bureau") != draft["bureau"] or rep.get("score") != draft.get("score"):
                print("The latest report on this account is not this report. Not attached.")
                return 2
        else:
            rep = save_report(c, u, draft)
        # every field is kept: the complete report for the holder's own (--own), otherwise every field except identity
        save_bureau_raw(c, u["id"], draft["bureau"], draft.get("report_id"), raw if a.own else bf.sanitize(raw))
        c.execute("UPDATE users SET onboarded=1 WHERE id=?", (u["id"],))
        audit(c, u["id"], "report_imported", f"{draft['source']}:{'own' if a.own else 'deidentified'}:{draft.get('score')}")
        stats = user_rag.sync_user_docs(c, u["id"], latest_report(c, u["id"], raw=True), now())
        c.commit()
    finally:
        c.close()
    print(f"Imported {draft['bureau']} report ({'own, full report kept encrypted' if a.own else 'de-identified, every non-identity field kept encrypted'}): score {rep.get('score')}, "
          f"{len(draft['accounts'])} accounts, {len(draft['enquiries'])} enquiries. AI index: {stats['docs']} chunks, {stats.get('embedded', 0)} embedded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
