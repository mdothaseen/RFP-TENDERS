# VPS deployment

This folder prepares Linux VPS deployment. It does not provision a server or publish the dashboard externally.

## Jobs

| Job | Schedule | Purpose |
|---|---|---|
| `tender-light` | Every 15 min | Home/priority listing checks + rolling active-document revalidation; no sitemap/OCR/AI |
| `tender-discovery` | 02:15 IST daily | Wider discovery; configured Sunday deep crawl |
| `tender-enrich` | 5 min after previous worker finishes | Saved PDFs, bounded OCR, optional AI, Excel |
| `tender-outbox` | 1 min after previous run | Durable per-recipient retries |
| `tender-health` | Every 5 min | Incident/recovery alerts, not repeated unchanged-state mail |
| `tender-backup` | 03:45 IST + 10 min after boot | Verified SQLite snapshot and deduplicated assets |
| `tender-dashboard` | Always | Authenticated loopback dashboard on port 8765 |

The scan, enrichment and delivery jobs have separate OS locks. Light and discovery can fetch concurrently;
SQLite serializes their short writes. A timer does not start a second instance of its already-active service.
If the network is slow, jobs can exceed their target interval. Document revalidation has a rolling per-cycle
budget, so the 15-minute listing interval is not a guarantee that every PDF is rechecked every 15 minutes.

## Setup

1. Start with a pilot Linux VPS. 2 vCPU / 4 GiB RAM / 50–80 GB disk is an estimate, not a measured requirement.
   Existing runtime assets are roughly 9.5 GiB; an initial backup needs another copy of unique assets. Measure RAM,
   disk growth and scan duration, then adjust workers/limits. Check access to all target websites from the VPS.
2. Install a supported Python, `python3-venv` and normal OS updates. Copy this project to `/opt/tender-tracker`.
   **Do not copy Windows `.venv`.** Stop every scanner/worker before transferring runtime data, then copy `data/`
   (including SQLite WAL if present) and `output/` together. Copying only `tracker.db` from a running process is unsafe.
   Keep a separate verified backup before moving anything. Never discard the original laptop copy during setup.
3. Copy `.env.example` to `.env`; configure SMTP + recipients and a unique `DASHBOARD_PASSWORD` of at least 12 chars.
   Secrets stay in `.env` with mode 600. `/etc/tender-tracker.env` can optionally supply overriding service variables.
4. Choose `ai.reader` in `config/settings.yaml`. Existing `claude_app` mode requires a supported Claude CLI installation
   and login **for the `tendertracker` service user**, available via PATH or `ai.claude_command`. Do not copy Windows
   authentication files to Linux. Alternatively configure API mode and an API key; billing is separate. Scans/alerts
   work without AI. `ai.daily_cost_limit_usd` stops starting API requests after recorded estimated cost reaches its
   threshold; it is not a provider billing hard cap and one request may exceed the remaining estimate.
5. Run `sudo sh /opt/tender-tracker/deploy/install.sh` on the VPS. It installs Python/browser dependencies, checks
   prerequisites, installs units and starts timers. It does not send a test message itself, but enabled monitoring
   jobs will send detected alerts to the configured recipients. Installation needs network/package permissions.
6. Inspect `systemctl list-timers 'tender-*'`, `journalctl -u tender-light.service -f`, and `journalctl -u tender-enrich.service`.
   Run `.venv/bin/python -m tender_tracker --health` for JSON (exit 2 = degraded), and `--check-config` for non-secret
   readiness flags. SMTP credentials being present does not prove delivery; explicitly run `--test-notify` when ready.

The first populated database migration automatically writes a consistent `data/tracker_pre_monitor_*.db` snapshot.
No items or downloaded documents are deleted by migration.

## Dashboard

It always binds **127.0.0.1:8765**, uses HTTP Basic authentication and rejects cross-origin update requests.
Use an SSH tunnel from your computer:

```sh
ssh -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_VPS
```

Open `http://127.0.0.1:8765` locally and enter `DASHBOARD_USER` / `DASHBOARD_PASSWORD`. For public access,
configure a TLS reverse proxy to loopback; never expose Basic authentication over unencrypted public HTTP.
No public port is opened by the installer. Dashboard status changes are local DB actions and do not submit bids.

## Notifications and relevance

Priority categories with High/Medium/unassessed relevance generate a discovery alert before OCR/AI.
Low-relevance and other categories remain in the daily digest/dashboard. Change events have their own alert.
Daily digest is queued at most once per date after 18:00 IST. Known 7/3/1-day deadlines get deduplicated reminders;
Applied/Ignored and removed items are excluded from reminders. Unknown dates are never guessed.

Every channel/recipient has its own outbox row. Errors and missing credentials leave it pending with backoff
(1 min, 2 min, ... up to 1 h). A crashed delivery lease is retried after 15 min. “Accepted” means server acceptance,
not inbox placement. An ambiguous SMTP timeout/crash can produce a duplicate; deterministic Message-ID helps
identify it but does not provide exactly-once delivery. No alert is marked notified merely for being attempted.

## Backups and restore drill

```sh
.venv/bin/python -m tender_tracker --backup
.venv/bin/python -m tender_tracker --verify-backup backups/snapshot-YYYYMMDDTHHMMSS-ID
.venv/bin/python -m tender_tracker --restore-backup backups/snapshot-YYYYMMDDTHHMMSS-ID --restore-to /NEW/EMPTY/DIRECTORY
```

The backup includes a consistent SQLite database, referenced PDFs and extracted text, OCR cache/languages, and YAML
configuration. **It excludes `.env` and application code**; retain source code/dependencies and provision secrets
separately. Restore refuses a nonempty destination. Verification checks manifest/database/blob hashes and SQLite
integrity. Seven completed manifests/DB snapshots are retained; deduplicated blobs are retained for historical
document versions and off-site sync. Monitor their growth; copy the entire `backups/` directory off the VPS using
your preferred storage/backup tool. Local-only copies cannot protect against loss of the server.

Configure an external dead-man monitor's URL in `HEARTBEAT_URL`; successful full light cycles ping it. The external
service must be configured to alert on missed pings. An email process on the same stopped VPS cannot report its own outage.

## References

- [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
- [Playwright Python browser/system dependencies](https://playwright.dev/python/docs/browsers)
- [SQLite online backup](https://www.sqlite.org/backup.html)
