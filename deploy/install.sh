#!/bin/sh
set -eu

# Run on the user's Linux VPS after copying the project and configuring .env.
APP=/opt/tender-tracker
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo on the VPS.' >&2
  exit 1
fi
if [ ! -f "$APP/requirements.txt" ] || [ ! -f "$APP/.env" ]; then
  echo 'Copy the project to /opt/tender-tracker and configure .env first; see deploy/README.md.' >&2
  exit 1
fi
if ! id tendertracker >/dev/null 2>&1; then
  useradd --system --home-dir "$APP" --shell /usr/sbin/nologin tendertracker
fi
if [ -d "$APP/.venv/Scripts" ]; then
  echo 'Windows .venv detected. Omit it when transferring; Linux requires a fresh virtual environment.' >&2
  exit 1
fi
python3 -m venv "$APP/.venv"
chown -R tendertracker:tendertracker "$APP"
chmod 600 "$APP/.env"
runuser -u tendertracker -- "$APP/.venv/bin/python" -m pip install -r "$APP/requirements.txt"
"$APP/.venv/bin/python" -m playwright install-deps chromium
runuser -u tendertracker -- "$APP/.venv/bin/python" -m playwright install chromium
cd "$APP"
runuser -u tendertracker -- "$APP/.venv/bin/python" -m tender_tracker --check-config
for unit in "$APP"/deploy/systemd/*; do
  install -m 644 "$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now tender-light.timer tender-discovery.timer tender-enrich.timer tender-outbox.timer tender-health.timer tender-backup.timer tender-dashboard.service
echo 'Timers enabled. Check systemctl list-timers tender-* and journalctl -u tender-light.service.'
