"""Daily retention: delete usage events older than 180 days and retrieval traces older than 90 days."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import app_events  # noqa: E402
import clock  # noqa: E402
from mobile_api import db  # noqa: E402


def main() -> int:
    c = db()
    ev_cut = (clock.utcnow() - dt.timedelta(days=app_events.RETENTION_DAYS)).isoformat()
    tr_cut = (clock.utcnow() - dt.timedelta(days=90)).isoformat()
    e = c.execute("DELETE FROM app_events WHERE at < ?", (ev_cut,)).rowcount
    t = c.execute("DELETE FROM rag_traces WHERE at < ?", (tr_cut,)).rowcount
    c.commit()
    c.close()
    print(f"deleted events={e} traces={t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
