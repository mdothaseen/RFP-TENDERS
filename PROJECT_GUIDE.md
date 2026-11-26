# SSC Tender Tracker — Complete Project Guide

> Written for a person **or another AI** who must understand, run, change or debug this project without prior context.
> Repo: https://github.com/mdothaseen/RFP-TENDERS · Language: Python 3.13 · Platform: Windows 11 (Linux VPS supported)
> Last updated: 2026-11-27 (code at commit `fb333fb`)

---

## 0. One-paragraph summary

The tracker watches **190 websites** (Sector Skill Councils, NSDC, NCVET, awarding bodies, universities, State Skill
Development Missions, and 23 government e-procurement portals) for new **tenders, RFPs, EOIs, empanelments,
corrigenda and notices**. Each scan crawls every site, saves new links in SQLite, downloads the documents, extracts
their text (OCR for scanned PDFs), has an AI (Claude Code CLI on the laptop, OpenRouter on the 24x7 desktop) fill a
fixed form for each tender (dates, EMD, fee, value, eligibility, scope, relevance for the company), updates Excel
files, and sends **one HTML summary email per scan**, grouped by site category (Focused / Regular / Low).

---

## 1. Business context (why it exists)

| Item | Value |
|---|---|
| Owner | CEO of a skilling-sector company (communicates in Hinglish) |
| Company profile used by the AI | **CeeVision**, an assessment agency in India; also takes skill-training work (full text in `config/settings.yaml → company_profile`) |
| High relevance | Assessment-agency empanelment/selection, third-party assessment, CBT/online/practical exams, assessor deployment, RPL; training-partner empanelment, skilling programmes, ToT/ToA, DDU-GKY, apprenticeship |
| Medium | Exam management, question banks, evaluation, monitoring / third-party verification, skill-gap studies, surveys |
| Low | Mobilisation/placement-only, office procurement, IT/hardware, printing, events, construction, repair, audits, staff recruitment |
| Email recipients | Configured in `.env → EMAIL_TO` (currently 2 Gmail addresses) |

### Owner's standing rules (must be respected by anyone changing the project)
1. **No paid Anthropic API.** The laptop reads documents with the Claude **plan** through the Claude Code CLI. The
   desktop uses OpenRouter (free models first, very cheap paid models as fallback).
2. **Secrets are filled in by the owner only** (`.env`). Never paste, print, log or commit passwords/keys.
3. **Ask before `git push`.** After a change: run tests → commit → tell the owner → push only after a "haan".
4. The email/AI text must **never name or refer to any other company** (competitors).
5. Never re-download / re-OCR / re-read a document that was already processed (everything is saved per item).
6. Do not install unknown packages without checking them first.
7. Do not bypass CAPTCHAs, logins or `robots.txt`.

---

## 2. Two machines, one codebase

| | **Laptop** (owner's, Lenovo) | **Desktop** "ALPHINE_ADMIN" (HP ProDesk 600 G4, Win 11 Pro) |
|---|---|---|
| Path | `D:\Codes\RFPTenders Checkers` | `E:\Code\RFP Tender` |
| Role | Development + manual scans | 24x7 "home VPS", automatic scans |
| Who starts a scan | Hourly popup "scan karun?" → **Haan**, or double-click `START_SCAN.bat` | Windows Task Scheduler, **08:00 / 16:00 / 00:00** daily |
| Scheduler | `setup_scheduler.ps1` (popup mode) | `setup_scheduler.ps1 -Auto -From 08:00 -EveryHours 8 -User …` (task runs as `Admin`, "run whether user is logged on or not", `StartWhenAvailable`, `WakeToRun`, `IgnoreNew`) |
| AI reader | Claude Code CLI (Claude plan) | OpenRouter (`.env`: `AI_READER=openrouter`) |
| AI docs per scan | 25 (`settings.yaml → ai.max_docs_per_run`) | 15 (`.env → OPENROUTER_DOCS_PER_RUN`) |
| Email | After every scan | After every scan (same recipients → duplicate emails are expected while both run) |
| Code updates | Edit here, commit, push | `git fetch https://github.com/mdothaseen/RFP-TENDERS.git main` then `git reset --hard FETCH_HEAD` (the clone has no tracking branch configured) |

**Machine-specific behaviour lives only in `.env`** (not in git), so `git reset --hard` on the desktop never changes
its AI reader or email setting. `config/*.yaml` is shared: edit it on the laptop, then push.

---

## 3. What happens in one scan (end-to-end pipeline)

Entry: `python -m tender_tracker --force` → `tender_tracker/main.py::main()` → `_run()`.

```
main()                                          main.py
 ├─ load settings/keywords/sites                config.py
 ├─ "today's run already done?" (skip unless --force)
 ├─ internet_ok()                               system.py           (exit 4 if offline)
 ├─ locking.acquire(data/run.lock)              locking.py          (exit 3 if another scan runs)
 ├─ keep_awake(True)                            system.py
 └─ _run()
     ├─ mark older "running" runs as interrupted (their new items join this email)
     ├─ store.start_run()                       store.py  → runs table
     ├─ scanning.scan_sites()                   scanning.py
     │    ├─ 6 sites in parallel (crawl.workers), each with a hard deadline (site_max_minutes=20 + margin)
     │    ├─ per site: discover/crawler.py::SiteScanner.run()
     │    │     ├─ robots.txt
     │    │     ├─ if site.nic_orgs → discover/nic_portal.py::scan_portal()   (e-procurement portals only)
     │    │     ├─ else crawl from url + seeds (depth 3 / 150 pages; JS sites via Playwright Chromium)
     │    │     ├─ sitemaps (discover/sitemap.py)
     │    │     └─ WordPress REST API: media library, posts, search (discover/wordpress.py)
     │    ├─ save_scan(): classify each Found → store.upsert()  (first scan of a site = baseline, not "new")
     │    ├─ page_texts diff → "Page updated: <url>" Notice items (changes.py::meaningful_diff)
     │    └─ store.record_site_run()            → site_runs table (status ok/partial/failed + errors)
     ├─ changes.revalidate_documents()          same URL, new file bytes → new revision
     ├─ enrich.enrich()                         enrich.py
     │    ├─ ai_extract.retire_stale()          closed / too-old tenders leave the queue
     │    ├─ documents.process_documents()      download ≤60 docs, text extraction, OCR (eng+hin), 5-min budget
     │    ├─ duplicates.mark_duplicates()       same sha256 / same upload path → duplicate_of
     │    ├─ documents.fill_upload_dates()      HEAD requests for upload dates
     │    ├─ read_with_claude_plan()
     │    │     ├─ ai_extract.export_queue()    → data/ai_queue.json
     │    │     ├─ claude_reader.read_queue()   (laptop)  OR  openrouter_reader.read_queue() (desktop)
     │    │     └─ ai_extract.import_results()  ← data/ai_results.json → items.ai_json
     │    └─ relations.link_corrigenda()
     ├─ excel_report.write_reports()            MASTER + per-org + New_<date>.xlsx
     ├─ alerts.publish()                        alerts.py
     │    ├─ site_status() (every site's latest check, for the email)
     │    ├─ events.queue_run_digest()          build_digest → email_html.build_email → outbox row per recipient
     │    ├─ health.queue_health()
     │    └─ events.drain_outbox()              SMTP send (notify/email_notify.py), Telegram, Windows toast
     └─ store.finish_run()  "Run N finished in X min: …"
```

Exit codes: `0` done · `2` scan partial (too many sites failed) · `3` another scan holds the lock · `4` no internet.

Typical duration: **~2 hours** for 190 sites (crawl ~90 min, documents ~5 min, AI 5–7 min, Excel 13–27 min).

---

## 4. Directory map

```
RFPTenders Checkers/
├─ tender_tracker/            Python package (all logic)          → section 5
│  ├─ discover/               crawling layers
│  ├─ notify/                 email / Telegram / Windows popup
│  └─ web/dashboard.html      local dashboard UI (--serve)
├─ config/
│  ├─ sites.yaml              190 websites + nic_orgs_default      → section 6.1
│  ├─ keywords.yaml           classification regexes               → section 6.2
│  └─ settings.yaml           crawl/doc/AI/excel/notify limits     → section 6.3
├─ tests/                     pytest, 152 tests                    → section 12
├─ deploy/                    Linux VPS systemd units + install.sh (not used today; desktop uses Task Scheduler)
├─ .github/workflows/daily.yml  manual-only GitHub Actions run (auto schedule disabled to avoid duplicate emails)
├─ data/                      RUNTIME (git-ignored except tessdata)
│  ├─ tracker.db (+ -wal)     SQLite, the single source of truth     → section 7
│  ├─ text/<id>_r<rev>.txt    extracted document text (AI input)
│  ├─ nic/<tender_id>.txt     e-procurement tender detail pages
│  ├─ ai_queue.json / ai_results.json   AI hand-off files
│  ├─ run.lock(.guard), enrich.lock, light.lock   OS locks
│  └─ tessdata/               Tesseract eng/hin/osd models (committed)
├─ output/                    RUNTIME
│  ├─ MASTER_All_SSC.xlsx     all organisations (~15 MB)
│  ├─ <CODE>/<CODE>_Updates.xlsx + <CODE>/Documents/   per organisation
│  └─ New_Updates/New_<date>.xlsx   items found that day (attached to email)
├─ logs/run_<date>.log        full log per day; last_scan_warnings.log; scheduler.log
├─ backups/                   --backup snapshots
├─ .env                       SECRETS + machine switches (never committed)  → section 6.4
├─ START_SCAN.bat             manual full scan (window shows progress)
├─ run_daily.bat              headless scan used by Task Scheduler (logs to logs\)
├─ setup_scheduler.ps1        installs the popup task or the 24x7 (-Auto) task
├─ tracker_click.pyw          handles ssctracker://scan|skip links from the popup buttons
├─ requirements.txt
├─ README.md                  user-facing setup (Hinglish/English)
└─ PROJECT_GUIDE.md           this file
```

Untracked helper files on the laptop (not in git): `Site_Categories.xlsx` (the owner's category sheet),
`email_formats/` (HTML mock-ups of the email), `graphify-out/` (code graph).

---

## 5. Module reference (`tender_tracker/`)

| Module | Lines | Responsibility | Key functions |
|---|---|---|---|
| `__main__.py` | 3 | `python -m tender_tracker` | → `main.main()` |
| `main.py` | 346 | CLI parsing, modes, run orchestration | `parse_args`, `main`, `_run`, `_maintenance`, `_ask` |
| `config.py` | 197 | Load YAML + `.env`; paths | `Site`, `Keywords`, `Settings`, `load_sites`, `_tier`, `_nic_orgs`, `env`, `env_bool`, `site_dir` |
| `scanning.py` | 158 | Parallel site scans with deadline; save results | `scan_sites`, `run_with_deadline`, `save_scan`, `_light_seeds` |
| `discover/crawler.py` | 546 | One site: crawl, sitemaps, WP API → `Found` items | `SiteScanner.run`, `_add_item`, `scan_site` |
| `discover/extract.py` | 230 | HTML → links, ticker notices, context, main text | `parse_html`, `main_text`, `make_soup` |
| `discover/sitemap.py` | 66 | XML sitemaps | `collect` |
| `discover/wordpress.py` | 139 | WP REST: media, posts, search | `WordPressAPI` |
| `discover/nic_portal.py` | 165 | NIC GePNIC e-procurement portals (department-filtered) | `scan_portal`, `parse_org_list`, `parse_tenders`, `_save_detail`, `is_portal_tender`, `detail_file` |
| `fetcher.py` | 272 | Polite HTTP per site: delay, retries, SSL fallback, legacy-TLS adapter, robots | `Fetcher.get/download/head/robots`, `_LegacyTLSAdapter` |
| `renderer.py` | 96 | Playwright headless Chromium for JS sites | `Renderer.render` |
| `urls.py` | 178 | URL cleaning, dedupe keys, document detection | `canonical_key`, `base_domain`, `is_document_url`, `title_from_filename` |
| `dates.py` | 196 | Indian date parsing from text/URLs/file names | `listing_dates`, `date_from_url`, `upload_date_from_url` |
| `classify.py` | 119 | Keyword category + first relevance guess | `classify`, `relevance`, `content_category`, `is_vague` |
| `store.py` | 387 | SQLite schema, migrations, all DB access | `Store.upsert`, `update_item_revision`, `site_history`, `kv_get/set` |
| `documents.py` | 655 | Download, text extraction (PDF/DOCX/XLSX/PPTX/ZIP/HTML), OCR | `plan_documents`, `run_documents`, `process_item`, `extract_text`, `ocr_pdf` |
| `ocr_worker.py` | 36 | OCR in a child process (killable) | `read_pdf` |
| `duplicates.py` | 58 | Same file under several URLs | `mark_duplicates` |
| `changes.py` | 141 | Page-text diffs; same-URL file revalidation | `meaningful_diff`, `revalidate_documents` |
| `relations.py` | 36 | Link corrigenda to tenders (exact ref + org) | `link_corrigenda` |
| `enrich.py` | 133 | Documents + AI step | `enrich`, `process_documents`, `read_with_claude_plan` |
| `ai_extract.py` | 249 | AI form (24 fields), queue export, result import | `TenderInfo`, `export_queue`, `import_results`, `retire_stale`, `select_items` |
| `claude_reader.py` | 80 | Runs `claude -p` headless on the queue (strips ANTHROPIC_*/CLAUDE_* env so the plan login is used, not API billing) | `read_queue`, `find_claude` |
| `openrouter_reader.py` | 185 | Same queue via OpenRouter; ordered model fallback; 429 handling | `read_queue`, `_ask_models`, `enabled`, `docs_per_run` |
| `excel_report.py` | 649 | Append/update Excel rows (never rebuilds data sheets) | `write_reports`, `build_records`, `summary_report` |
| `alerts.py` | 120 | What the user is told after a scan | `publish`, `site_status`, `failure_reason`, `pending_email`, `test_notify` |
| `events.py` | 270 | Durable events + per-recipient outbox with retries | `queue_run_digest`, `drain_outbox`, `record_event`, `enqueue` |
| `notify/__init__.py` | 141 | Digest object, send to all channels | `build_digest`, `send_all` |
| `notify/email_html.py` | 368 | The HTML summary email (category layout) | `build_email`, `_Category`, `_facts` |
| `notify/email_notify.py` | 65 | SMTP (Gmail app password) | `send` |
| `notify/telegram_notify.py` | 57 | Telegram bot | `send`, `print_chat_ids` |
| `notify/windows_notify.py` | 55 | Toasts + hourly "scan?" popup | `ask_to_scan`, `say`, `send` |
| `health.py` | 80 | Health snapshot, incident mails, heartbeat URL | `snapshot`, `queue_health`, `heartbeat` |
| `locking.py` | 146 | OS-owned non-blocking locks (no stale-age guessing) | `RunLock`, `acquire`, `run_in_progress` |
| `backup.py` | 126 | SQLite snapshot + deduplicated blobs; verify; restore | `create_backup`, `verify_backup`, `restore_backup` |
| `dashboard.py` + `web/dashboard.html` | 156 | Loopback, Basic-auth dashboard; mark tenders interested/applied/ignored | `serve` |
| `reclassify.py` | 54 | Re-apply keywords.yaml to all saved items | `reclassify` |
| `add_site.py` | 65 | `--add-site URL "Name"` | `add_site` |
| `system.py` | 32 | Keep Windows awake, internet check | `keep_awake`, `internet_ok` |

### 5.1 Discovery details (crawler)
- **Found** dataclass = one candidate item: `key` (dedupe key from `urls.canonical_key`), `url`, `title`, `context`
  (≤600 chars of surrounding text), `source_page`, `via` (crawl/sitemap/wp-media/wp-post/nic-portal/change),
  `linked`, `is_document`, `published` + `date_source`, `deadline`, `uploaded`.
- A link counts when it is a **document** (`pdf/doc/xls/zip/…`, Google Drive, flipbook query docs) or a **page whose
  text reads like a notice** (`_looks_like_item`). Ticker text without a link becomes a text item.
- **Site ownership**: `Site.owns(host)` = same `base_domain` (gov.in/nic.in/org.in etc. are two-level suffixes, so
  `asdm.assam.gov.in` → `assam.gov.in`) or a host in `extra_hosts`.
- **Timeouts**: 3 time-outs in a row stop that site; the scan-level deadline drops a hung site
  ("website hung (timeout)").
- **SSL**: certificate errors → retry unverified; `UNSAFE_LEGACY_RENEGOTIATION` (old government servers such as
  pbssd.gov.in, uksds.uk.gov.in) → retry with `_LegacyTLSAdapter`.
- **Baseline**: the first time a site is scanned, everything found is saved with `baseline=1` and is **not** "new"
  (no email flood). Same for a brand-new database (`--baseline`).

### 5.2 E-procurement portals (`discover/nic_portal.py`)
- Only for sites with `nic_orgs` (23 NIC GePNIC portals: CPPP + states/UTs, codes `EP…`).
- Reads the public page `<app>?page=FrontEndTendersByOrganisation&service=page` (no CAPTCHA), keeps departments
  whose names match `nic_orgs` regexes (default list at the top of `sites.yaml`: skill, kaushal, labour,
  employment, technical education, vocational, livelihood, ITI/industrial training, apprentice, entrepreneur),
  opens each in the **same HTTP session**, parses the table (published, closing, opening, `[title] [ref][tender id]`),
  follows `linkFwd` pagination (≤10 pages).
- Portal links contain a session token and expire, so each tender is stored under
  `…FrontEndTendersByOrganisation…#nic-tender=<TenderID>` (the site has `keep_fragments` forced on). The title gets
  `(Ref: …; Tender ID: …)`.
- The tender's **detail page** (value, EMD, fee, dates, description, location) is fetched once in-session and saved
  to `data/nic/<TenderID>.txt`; `documents.process_item` hands that text to the AI. Tender PDFs need a CAPTCHA and
  are **not** downloaded.
- Not covered: CAPTCHA portals (Arunachal, Assam, Manipur, Meghalaya, Nagaland, GeM), non-NIC portals (AP, Bihar,
  Chhattisgarh, Gujarat nProcure, Karnataka KPPP, Telangana), Andaman (unreachable).

### 5.3 Documents step (`documents.py`)
- `plan_documents`: priority = downloadable categories (Tender, RFP, EOI, Empanelment, Corrigendum, Notice,
  Recruitment) + AI-category pages; uncertain = vague/unknown file names (small budget). Skips stale items and
  duplicates. Order: AI categories, still-open deadline, freshest, category rank.
- Per scan: `max_per_run` 60 (Sunday deep: 300), `unknown_per_run` 10, wall budget 300 s.
- Text extraction: PyMuPDF for PDF; scanned PDFs → Tesseract OCR (`eng+hin`, 30 pages, 90 s, child process);
  DOCX/XLSX/PPTX/ZIP/HTML handled. Text → `data/text/<id>_r<rev>.txt`; files → `output/<CODE>/Documents/`
  named `<date>_<title>.<ext>`.
- `content_category` can upgrade a vague link to Tender/RFP/EOI from the document's first pages.

### 5.4 AI step
- Form = 24 string fields (`ai_extract._FIELDS` / `TenderInfo`): doc_type, title, issuing_organization, reference_no,
  issue_date, last_date_submission, last_time_submission, prebid_date, bid_opening_date, other_key_dates, emd,
  tender_fee, estimated_value, contract_period, who_can_apply, eligibility_summary, scope_summary, location,
  submission_mode, contact, corrigendum_of, relevance (High/Medium/Low), relevance_reason, action_needed.
- Queue selection: AI categories (Tender, RFP, EOI, Empanelment, Corrigendum), text ≥500 chars, first found on/after
  `ai.read_from` (2026-10-02), not older than `recent_days` (120), last date not passed, ≤`max_docs_per_run`.
  `past_decisions` (dashboard interested/applied/ignored) are sent as examples.
- **Laptop**: `claude -p --allowedTools Read Write --permission-mode acceptEdits` reads `data/ai_queue.json` and
  writes `data/ai_results.json` (prompt in `claude_reader.PROMPT`; prompt-injection guard: document text is DATA).
  A logged-out CLI is reported in every email (`kv claude_problem`).
- **Desktop**: `openrouter_reader` sends one request per document (text ≤60 000 chars; scanned PDFs without OCR text
  are skipped), tries the `OPENROUTER_MODEL` entries in order, moves to the next model after HTTP 429/errors, stops
  reading for this scan when every model is busy or on 401/402/403. Current list:
  `google/gemma-4-31b-it:free, nvidia/nemotron-3.5-super-vl:free, qwen/qwen3.7-flash, deepseek/deepseek-v4.1-flash`
  (paid ones cost about $0.0005–0.0009 per document).
- `import_results` validates with pydantic, normalises doc_type/relevance, saves only if the item's revision did not
  change meanwhile (`update_item_revision`), and sets `ai_model` (`claude-app` or `openrouter:<models>`).

### 5.5 Email (`notify/email_html.py`)
- Subject: `[Action needed] SSC Tender Update | <dd Mon yyyy> | Focused: N new tenders · X orgs with news · M closing in 7 days`
  (the prefix only when there is something to act on). First run: "First run complete".
- Body (full width, inline-styled tables, ~250–300 KB → Gmail shows "View entire message"):
  1. Header + verdict box (ACTION NEEDED / …).
  2. Summary table: one row per category (websites, read OK, not read, orgs with news, new tenders, other updates, closing ≤7d).
  3. "Closing within 7 days" (all categories, top 25).
  4. Per category (Focused → Regular → Low): status row of **every** organisation; org-by-org details
     (Focused 15 / Regular 8 / Low 5 items) with AI facts (scope, who can apply, Ref/EMD/Fee/Value chips, action,
     relevance reason); "Checked, nothing new" chips; "Could not be read" rows with URL + reason.
  5. Notes (Claude logout, AI problems, Excel locked) and footer.
- "Page updated: …" items are collapsed to one count per organisation. Items whose last date already passed are
  left out (one line says how many).
- Delivery: `events.queue_run_digest` → `notification_outbox` (one row per recipient, retried until accepted) →
  `drain_outbox` → SMTP. Event key `digest:run:<run_id>` makes it idempotent.
- `EMAIL_ONLY_WHEN_NEW=1` (or `notify.only_when_new`) suppresses an email when nothing is new (not used now).

### 5.6 Excel (`excel_report.py`)
- `output/MASTER_All_SSC.xlsx`: sheets Dashboard, Tenders-RFP-EOI, All Updates, Site Status.
- `output/<CODE>/<CODE>_Updates.xlsx`: Tenders-RFP-EOI, All Updates.
- `output/New_Updates/New_<date>.xlsx`: only that day's new items (email attachment).
- Data sheets are **appended/updated, never rebuilt** (row hash in `items.xl_master_hash / xl_site_hash`).
  A file that is open in Excel is skipped and retried next run ("Excel files were open" note).
- Tender sheet columns: S.No, Found On, Org, Category, Title, Published, Uploaded On, Last Date, Days Left, Status,
  Time, Pre-bid, EMD, Fee, Who Can Apply, Eligibility, Summary, What To Do, Ref No, How To Submit, Contact,
  Relevance, Why, Document, Local File, Found On Page, Found Via, Hidden?, Notes, ID.

---

## 6. Configuration

### 6.1 `config/sites.yaml`
Top-level keys: `nic_orgs_default` (list of regexes) and `sites` (list). Per site:

| Key | Type | Meaning |
|---|---|---|
| `code` | str, unique | Short id; folder name `output/<CODE>/`; shown in email/Excel |
| `name` | str | Full organisation name |
| `tier` | Focused / Regular / Low (default Regular) | Email order + detail depth; **all tiers are scanned** |
| `url` | str | Start page (for portals: the `…/nicgep/app` endpoint) |
| `seeds` | list[str] | Pages crawled first every run (tender/notice pages) |
| `js` | bool | Render with Playwright Chromium |
| `ssl_verify` | bool | false = known broken certificate |
| `extra_hosts` | list[str] | Other hosts whose documents belong to the site |
| `exclude` | list[str] | URL substrings to skip |
| `keep_fragments` | bool | Keep `#fragment` in dedupe keys (forced true for portals) |
| `nic_orgs` | `default` or list[regex] | Makes the site an e-procurement portal (section 5.2) |
| `notes` | str | Free text |

Counts today: **190 sites**; Focused 107 (59 original + 25 State Skill Missions + 23 portals), Regular 57, Low 26.
Add a site: `python -m tender_tracker --add-site https://example.org "Name (ABC)"` (then set `tier:`), or copy a block.

### 6.2 `config/keywords.yaml`
`categories` (Corrigendum, EOI, RFP, Tender, Empanelment, Recruitment, Notice, ToT-ToA/Result, News → regex lists),
`other_patterns`, `tender_categories` (the 5 tender-type categories), `relevance_high/medium/low`,
`attachment_titles` (e.g. "Price Bid" = attachment, not a new tender), `vague_titles` ("click here"),
`content_phrases` (phrases in document text that confirm a category), `crawl_exclude`, `crawl_priority`,
`external_doc_hosts` (e.g. Drive/S3 hosts whose files belong to the linking site).
After changing rules run `python -m tender_tracker --reclassify` to apply them to saved items.

### 6.3 `config/settings.yaml` (main knobs)
| Section | Key | Value | Meaning |
|---|---|---|---|
| `company_profile` | | text | What the AI judges relevance against |
| `crawl` | workers / delay_seconds / timeout_seconds | 6 / 1.2 / 30 | Parallel sites, politeness, HTTP timeout |
| | respect_robots | true | Obey robots.txt |
| | daily / light / deep | depth 3, 150 pages, 50 JS / depth 1, 6 / depth 5, 350, 150 | Crawl limits |
| | site_max_minutes | 20 | Per-site crawl cap |
| | deep_weekday | 6 | Sunday deep crawl |
| `documents` | max_download_mb 50, download_categories, baseline_days 365, max_per_run 60, unknown_per_run 10, deep_*, time_budget_seconds 300, ocr {enabled, max_pages 30, language eng+hin, timeout 90} | | |
| `ai` | read_from 2026-10-02, categories, max_docs_per_run 25, baseline_days 45, recent_days 120, max_text_chars 400000, max_pdf_pages 100, max_attempts 3, timeout_minutes 20, (optional) claude_command | | |
| `excel` | include_all, baseline_max_age_days 365, closing_soon_days 7 | | |
| `notify` | separate_emails false, when_empty false, only_when_new false, instant_*, reminder_days [7,3,1], daily_digest_after, attach_master_daily | | |
| `monitoring` | light/document workers, document_check_minutes, health_emails false, failure_threshold 3, min_free_disk_mb | | VPS-style monitoring |
| `backup` | enabled, directory backups, keep_snapshots 7 | | |

### 6.4 `.env` (never committed; template `.env.example`)
| Variable | Used by | Notes |
|---|---|---|
| SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO | email | Gmail app password; `EMAIL_TO` is a comma list |
| TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID | Telegram | optional |
| WINDOWS_TOAST | popup | |
| DASHBOARD_USER, DASHBOARD_PASSWORD | `--serve` | ≥12 chars |
| HEARTBEAT_URL | health | optional dead-man URL |
| AI_READER=openrouter | desktop only | switches the reader |
| OPENROUTER_API_KEY | desktop only | the owner fills it |
| OPENROUTER_MODEL | desktop only | comma list, tried in order |
| OPENROUTER_DOCS_PER_RUN | desktop only | 15 (3 scans/day × 15 ≈ free daily limit) |
| OPENROUTER_MAX_CHARS | optional | default 60000 |
| EMAIL_ONLY_WHEN_NEW | optional | 1 = no email when nothing is new |

---

## 7. Database (`data/tracker.db`, SQLite WAL)

| Table | Purpose / important columns |
|---|---|
| `items` | One row per thing found. `key` UNIQUE (dedupe), `site_code`, `url`, `title`, `context`, `source_page`, `found_via`, `linked_seen`, `hidden`, `is_document`, `category`, `relevance`, `published_date`+`date_source`, `deadline_hint`, `first_seen/last_seen`, `first_run_id`, `baseline`, `removed`, `notified`, document fields (`doc_status` ok/html/error, `doc_attempts`, `local_path`, `text_path`, `sha256`, `text_chars`, `scanned`), `content_category`, AI fields (`ai_status`, `ai_json`, `ai_model`, `ai_at`, `ai_error` — prefix `stale:` = retired), `revision`, `duplicate_of`, `uploaded_date/source`, Excel hashes |
| `runs` | id, started, finished, mode (full/light/sites:…/enrich), baseline, deep, status (running/done/partial/failed/interrupted), new_items, notes |
| `site_runs` | Per site per run: status ok/partial/failed, method, pages, items, new_items, wp, sitemap_urls, errors (JSON list), ssl_fallback |
| `page_texts` | Last text of watched pages (for "Page updated" diffs) + revision |
| `document_versions` | Immutable history of a URL's file bytes (sha256 per revision) |
| `change_events` | Durable events (new item, page_changed, digest…), `event_key` UNIQUE |
| `notification_outbox` | One delivery per event × channel × recipient; status pending/accepted, attempts, next_attempt, lease_until, last_error |
| `ai_usage` | Legacy API token/cost log |
| `kv` | Small state: `last_full_run_date`, `email_pending`, `claude_problem`, … |

A populated database migrating to a new schema first writes `data/tracker_pre_monitor_*.db`.

---

## 8. Command line (`python -m tender_tracker …`; on Windows use `.venv\Scripts\python.exe`)

| Option | What it does |
|---|---|
| *(none)* | Full scan unless today's full run already finished |
| `--force` | Full scan even if today's run is done (used by START_SCAN.bat, the popup and the scheduler) |
| `--site CODE …` | Only these sites (test); add `--notify` to email |
| `--baseline` | Treat the run as the first run |
| `--deep` | Deeper crawl (Sunday automatically) |
| `--no-ai` / `--no-download` / `--no-notify` | Skip that step |
| `--max-pages N`, `--workers N` | Testing limits |
| `--light` | Quick check of known tender pages only (no sitemap/OCR/AI) |
| `--enrich` | Documents + AI + Excel + email from the saved backlog, no crawl |
| `--defer-enrichment` | Scan only; a separate `--enrich` worker does documents/AI |
| `--excel-only` | Rebuild Excel from the database |
| `--reclassify` | Re-apply keywords.yaml to all items |
| `--ai-export` / `--ai-import FILE` / `--send-pending-email` | Manual AI hand-off and delayed email |
| `--test-notify` | Send the latest real summary on every channel, marked [TEST] |
| `--check-config` | Non-secret readiness flags |
| `--health [--notify]` | Health JSON (exit 2 = degraded) |
| `--drain-outbox` | Retry pending deliveries |
| `--backup`, `--verify-backup PATH`, `--restore-backup PATH --restore-to DIR` | Backups |
| `--serve [--port 8765]` | Local dashboard |
| `--add-site URL "Name" [CODE]`, `--list-sites` | Site management |
| `--telegram-setup` | Show Telegram chat ids |
| `--ask` | Show the "scan now?" popup (used by the hourly task) |
| `-v` | Verbose log |

---

## 9. Scheduling & operations

### Laptop (popup mode)
```
powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1            # hourly popup 09:00–21:00
powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -Remove
```
Popup "Haan" → `ssctracker://scan` → `tracker_click.pyw` → `main(["--from-popup"])` (= `--force`).
Manual: double-click `START_SCAN.bat`. Stop: Ctrl+C in that window, or kill the python processes whose command line
contains `tender_tracker`.

### Desktop (24x7)
```
# Administrator PowerShell
powershell -ExecutionPolicy Bypass -File "E:\Code\RFP Tender\setup_scheduler.ps1" -Auto -From 08:00 -EveryHours 8 -User tenderbot
schtasks /run   /tn "SSC Tender Tracker"      # start now
schtasks /end   /tn "SSC Tender Tracker"      # stop (also kill python processes with tender_tracker in the command line)
schtasks /query /tn "SSC Tender Tracker" /fo list | findstr /i "Status"
Get-Content "E:\Code\RFP Tender\logs\run_<yyyy-mm-dd>.log" -Tail 20 -Wait   # live progress (no window: it runs in the background)
```
The current task was registered for **Admin**: a bug made `-User` fall back to the signed-in user (fixed in commit
`70ab3d3`; the owner chose to keep Admin). If Admin's password changes, re-run the `-Auto` command. Power settings:
`powercfg /change standby-timeout-ac 0` and `hibernate-timeout-ac 0` were applied. BIOS "After Power Loss = Power On"
is recommended (F10 → Advanced → Power-On Options). Folder permission for `tenderbot` was granted with
`icacls "E:\Code\RFP Tender" /grant tenderbot:(OI)(CI)M /T /Q`.

### Updating the desktop
```
cd /d "E:\Code\RFP Tender"
git fetch https://github.com/mdothaseen/RFP-TENDERS.git main
git reset --hard FETCH_HEAD
git log -1 --oneline
.venv\Scripts\python.exe -m pip install -r requirements.txt   # only when requirements changed
```
A running scan keeps the code it started with; the next scan uses the new code.

### Linux VPS (prepared, not in use)
`deploy/` has systemd units (light every 15 min, discovery daily 02:15, enrich, outbox, health, backup, dashboard)
and `install.sh`; see `deploy/README.md`.

---

## 10. Logs & how to read them

`logs/run_<date>.log` lines (INFO):
- `Run N started: 190 sites, baseline=False, deep=False`
- `[CODE] scanning <url>` … `[CODE] done: ok|partial|failed, P pages (static|browser), I items, E errors`
- `[CODE] first scan of this site: saved as its baseline`
- `[CODE] SSL certificate problem, retrying without verification` / `old TLS server, retrying with legacy renegotiation allowed`
- `[CODE] blocked (HTTP 403) … switching to browser`
- `Documents 49/60 saved | #id ok|html|error | 38s elapsed` → `Documents: 60/60 processed, 57 ready, 3 errors; N still pending`
- `Duplicates: N items re-pointed to the first copy`
- `AI backlog: N old documents will not be read`
- `25 documents queued; starting Claude Code` / `starting OpenRouter` → `Claude Code: …` / `OpenRouter: read X of Y documents (model: n, …)`
- Silence for 13–27 min after the AI line = Excel writing, not a hang (check the python process's CPU and Excel mtimes)
- `Outbox: {'accepted': 2, 'failed': 0}` → email sent
- `Run N finished in X min: A new items, B new tender-type, sites failed: …`

`failure_reason()` wording in the email: website hung (timeout) · blocks automatic reading (403) · address not
found (DNS) · too slow (timeout) · certificate error · some pages could not be read · could not be opened.

---

## 11. Known limitations (by design or not yet solved)

- **Never done**: CAPTCHA solving, logging in, ignoring robots.txt, bidding/applying.
- E-procurement: no permanent per-tender link (session links expire) → the email links to the portal's department
  page and shows the Tender ID; tender PDFs are not downloaded (CAPTCHA); the AI reads the portal detail page instead.
- Sites failing at the latest check: IASC, INFOSYS, KSOUMK, MH (403), GTRTCK, PSEB (partial 403), MM, NAVY, ASAP
  (timeout), BSEAP (hangs), SBTETAP, DFCCIL, MSSDMN, SDEAR, OSDA (cannot open). They are retried every scan.
- State missions without their own website: Punjab (its tenders are on eproc.punjab, covered by EPPB), Sikkim,
  Meghalaya, Ladakh, Delhi, Puducherry, Chandigarh, Lakshadweep, DNH&DD.
- Backlog: ~31 000 old documents pending; 60 processed per scan. The AI skips documents older than 120 days, found
  before 2026-10-02, or past their last date.
- The desktop (OpenRouter) cannot read scanned PDFs without OCR text; free models are often rate-limited (429).
- The email is large (Gmail clips >102 KB → "View entire message").
- Both machines email the same recipients → duplicate emails while both scan.
- An Excel file open in Excel during a scan gets its new rows in the next scan.

---

## 12. Tests

`.venv\Scripts\python.exe -m pytest -q` → **152 passed**. Files:

| File | Covers |
|---|---|
| test_pipeline.py | scan → save → report end-to-end with fixtures |
| test_classify_extract.py | categories, relevance, HTML extraction |
| test_urls_dates.py | URL keys, Indian dates |
| test_documents_reuse.py | no re-download / reuse of left-over files |
| test_ai_extract.py, test_reader_guard.py | AI queue/import, Claude reader guard |
| test_openrouter_reader.py | OpenRouter format, 429 stop, fallback to a paid model, not set up |
| test_email_tiers.py | category email, every site's status, failure wording |
| test_nic_portal.py | department filter, tender rows, detail page → AI text |
| test_monitoring.py, test_lock.py, test_tuning.py | outbox/health, locks, limits |

Rule: every change ships with tests; run the full suite before committing.

---

## 13. How-to recipes

| Task | Steps |
|---|---|
| Add a website | `--add-site URL "Name (CODE)"` → set `tier:` in sites.yaml → test `--site CODE --no-notify` → commit → ask → push → update the desktop |
| Add an e-procurement portal | Copy an `EP…` block, `url: https://<host>/nicgep/app`, `nic_orgs: default` |
| Watch more/other departments on portals | Edit the `nic_orgs_default` regex list (or give one portal its own `nic_orgs: [..]`) |
| Change the category of a site | Edit its `tier:` |
| Change relevance rules | `company_profile` (AI) and `keywords.yaml relevance_*` (first guess) → `--reclassify` |
| Change scan times on the desktop | Re-run `setup_scheduler.ps1 -Auto -From HH:MM -EveryHours N -User …` |
| Change OpenRouter models | Desktop `.env → OPENROUTER_MODEL=a:free,b:free,cheap-paid-1,cheap-paid-2` |
| Re-send / test the email | `--test-notify` |
| Email did not arrive | Look for `Outbox:` in the log; `--drain-outbox`; check `notification_outbox.last_error` |
| Scan looks stuck | Check the python process's CPU, Excel file mtimes, last log line; the Excel step is silent for up to ~27 min |
| Rebuild Excel | `--excel-only` (close Excel first) |

---

## 14. Change history (recent, newest first)

| Commit | Change |
|---|---|
| fb333fb | Portal tenders get AI details from their detail page |
| 5a0ae15 | 23 NIC e-procurement portals, skill/labour/technical-education departments only |
| 2a9e6b5 | 25 State Skill Development Missions (Focused); legacy-TLS retry for old government servers |
| 70ab3d3 | setup_scheduler.ps1: `-User` no longer overwritten by the signed-in user |
| b50bfc0 | OpenRouter: several models in order (free first, then cheap paid) |
| 01ee9f6 | 24x7 schedule every 8 h from 08:00 as a separate Windows user |
| a1254bf | OpenRouter reader for the desktop; the laptop keeps Claude |
| 6dfd18c | Non-stop scan mode (`-Auto`), optional email-only-when-new |
| 5679f05 | Email uses the full width |
| 8f05d05 | Email grouped by site category Focused/Regular/Low |
| 6d46dd1 and earlier | Crawl/date/duplicate fixes, hung-site deadline, training work rated High |
