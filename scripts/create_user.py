"""Create a Fikarta account from the server (for team members). The password is typed by the person running
the command, hidden, twice - it is never passed on the command line, so it does not end up in shell history.

    sudo -u www-data venv/bin/python scripts/create_user.py --email a@b.com --first Asha --last Verma --phone 9876543210

The new user accepts the privacy notice and Terms on first login (consent is never recorded on their behalf),
then completes PAN and the rest from the Home checklist.
"""
import argparse
import getpass
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

from mobile_api import PHONE_RE, PW_RE, audit, db, hash_pw, notify, now  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a Fikarta account")
    ap.add_argument("--email", required=True)
    ap.add_argument("--first", required=True, help="first name as on PAN")
    ap.add_argument("--last", default="", help="last name as on PAN")
    ap.add_argument("--phone", default="", help="10-digit mobile")
    a = ap.parse_args()
    email = a.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        print("Invalid email")
        return 2
    phone = re.sub(r"\D", "", a.phone)[-10:]
    if phone and not PHONE_RE.match(phone):
        print("Invalid mobile number")
        return 2
    pw = getpass.getpass(f"Password for {email}: ")
    if not PW_RE.match(pw):
        print("Password must be 8+ characters with a letter and a number")
        return 2
    if getpass.getpass("Type it again: ") != pw:
        print("Passwords do not match")
        return 2
    c = db()
    try:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            print("An account with this email already exists")
            return 1
        cur = c.execute("INSERT INTO users(email,pw_hash,first_name,last_name,phone,referral_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (email, hash_pw(pw), a.first.strip(), a.last.strip(), phone or None, "FK" + secrets.token_hex(3).upper(), now(), now()))
        uid = cur.lastrowid
        c.execute("INSERT INTO rewards(user_id,points,reason,at) VALUES(?,?,?,?)", (uid, 50, "Welcome bonus", now()))
        notify(c, uid, "welcome", "Welcome to Fikarta", "Complete your profile on Home to see your full credit picture.")
        audit(c, uid, "created_by_admin", "terms pending first login")
        c.commit()
        print(f"Created account {uid} for {email}. They accept the Terms on first login, then complete the Home checklist.")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
