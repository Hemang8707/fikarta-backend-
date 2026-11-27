#!/bin/sh
# Runs every 2 minutes from cron. If the API stops answering locally twice in a row,
# restart the service and log it (journalctl -t fikarta-health).
URL=http://127.0.0.1:8000/api/m/privacy/notice
STATE=/run/fikarta-health.fail
if curl -fs -m 8 -o /dev/null "$URL"; then
  rm -f "$STATE"
  exit 0
fi
if [ -f "$STATE" ]; then
  logger -t fikarta-health "API not responding twice; restarting fikarta-backend"
  systemctl restart fikarta-backend
  rm -f "$STATE"
else
  touch "$STATE"
  logger -t fikarta-health "API did not respond; will restart if it fails again"
fi
