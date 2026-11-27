"""Daily: tell users when a fresh CRIF report can be fetched (bureau data updates monthly).

Only users who fetched before, still have the credit-report consent on, and have not been reminded since
their last fetch. Fikarta never fetches automatically: each pull needs the user's own tap and consent.
Run from cron (deploy/fikarta.cron)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import ai_assistant  # noqa: E402,F401  (loads the bureau settings from .env)
import bureau_fetch  # noqa: E402
import clock  # noqa: E402
import surepass_crif as sp  # noqa: E402
from mobile_api import db, notify, now  # noqa: E402
from mobile_privacy import consent_status  # noqa: E402


def main() -> int:
    if not sp.configured():
        print("bureau not configured; nothing to do")
        return 0
    cutoff = clock.epoch() - bureau_fetch.cooldown_days() * 86400
    c = db()
    sent = 0
    try:
        rows = c.execute("""SELECT user_id, MAX(epoch) AS last, MAX(at) AS last_at FROM bureau_pulls WHERE status='success'
                            AND user_id IN (SELECT id FROM users) GROUP BY user_id HAVING MAX(epoch) <= ?""", (cutoff,)).fetchall()
        for r in rows:
            u = c.execute("SELECT * FROM users WHERE id=?", (r["user_id"],)).fetchone()
            if consent_status(c, u, "credit_report") != "granted":
                continue
            if c.execute("SELECT 1 FROM notifications WHERE user_id=? AND kind='refresh' AND at>=?", (r["user_id"], r["last_at"])).fetchone():
                continue
            notify(c, r["user_id"], "refresh", "Your new monthly report is ready",
                   "Credit bureaus update every month. Fetch your latest CRIF report to see what changed.")
            sent += 1
        c.commit()
    finally:
        c.close()
    print(f"reminders sent: {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
