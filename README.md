# SSC Tender & Notice Tracker

## 24x7 monitoring aur VPS setup

Ab light scan, discovery aur document/AI worker alag chal sakte hain. Naya alert AI ka wait nahi karta;
failed email recipient-wise retry hota hai. Same URL par PDF badle to naya revision aur alert banta hai.
Dashboard mein Seen / Applied / Ignore, linked corrigenda aur delivery/site health dikhte hain.
Deadline reminders 7, 3 aur 1 din pehle aur daily digest configuration mein hain.

Linux installation, systemd timers, dashboard password aur backup/restore ke liye **[VPS guide](deploy/README.md)** padhein.
`python -m tender_tracker --help` mein `--light`, `--enrich`, `--drain-outbox`, `--health`, `--backup`
aur `--serve` commands milenge. VPS par workers deploy karne ke baad live website/email checks karein.

Roz 41 websites check karta hai: 36 Sector Skill Councils, NSDC, NCVET, DC-MSME, MSBSVET aur NIELIT (poori list: `--list-sites`). Wahan se naye **tenders, RFP, RFQ, EOI, empanelment, corrigendum, notices, circulars aur recruitment** nikaalta hai.

Kya-kya karta hai:
- Documents download karta hai.
- Claude AI se har document ki key details nikaalta hai: last date, EMD, eligibility, kaun apply kar sakta hai, summary.
- Har SSC ka alag folder (uski Excel + PDFs) aur ek MASTER Excel; roz sirf nayi entries judti hain.
- Email, Telegram aur Windows popup se alert bhejta hai.

## Chhupe hue tenders kaise pakadta hai

Har site par 7 tareeke use hote hain:

| Tareeka | Kya milta hai |
|---|---|
| Homepage + tender/notice pages | Seedhe links |
| Deep crawl (3 level; Sunday ko 5 level) | Menu ke andar ke pages |
| Scrolling ticker / popup text | Woh notices jinka link nahi hota (Google Form wale bhi) |
| Sitemap (`sitemap.xml`) | Aise pages/PDF jo menu mein nahi hain |
| WordPress media library | Site par upload hui **har** PDF, chahe kahin link ho ya na ho |
| WordPress posts + site search | "tender", "rfp", "eoi" search karke |
| PDF ke andar ka text | "Notice.pdf" jaise naam wali file jo asal mein EOI ho |

Jo item sirf sitemap / media library / search se mila, us par Excel mein **Hidden? = Yes** aata hai (sheet: *Hidden Finds*).

## Pehli baar setup

Python aur packages is folder ke `.venv` mein pehle se install hain. Aapko sirf ye karna hai:

**1. `.env` file banayein**

`.env.example` ko copy karke usi folder mein `.env` naam se save karein. Is file mein passwords aur keys hote hain, ise kisi ko na bhejein.

**2. Claude login** (documents padhne ke liye, koi API key nahi)
- Terminal mein `claude` chalayein, `/login` likhein aur apne Claude plan se sign in karein.
- Documents aapke Claude plan se padhe jaate hain; alag API bill nahi aata.
- Login na ho to bhi tracker chalega; email ke Notes mein "Claude Code is not logged in" aayega.

**3. Email** (Gmail)
- Google Account → **Security** → **2-Step Verification** on karein → **App passwords** mein naya password banayein.
- `.env` mein bharein:
  - `SMTP_USER=aapka@gmail.com`
  - `SMTP_PASSWORD=<app password>`
  - `EMAIL_TO=jisko bhejna hai (comma se kai address)`

**4. Telegram**
- Telegram mein **@BotFather** kholein → `/newbot` → naam dein → jo token mile use `.env` mein `TELEGRAM_BOT_TOKEN=` ke aage daalein.
- Apne naye bot ko "hi" bhejein, phir ye command chalayein. Jo chat id dikhe use `.env` mein `TELEGRAM_CHAT_ID=` ke aage daalein.

```bash
.venv\Scripts\python -m tender_tracker --telegram-setup
```

**5. Alerts test karein.** Email, Telegram aur popup teeno aane chahiye:

```bash
.venv\Scripts\python -m tender_tracker --test-notify
```

**6. Company profile check karein.** `config/settings.yaml` mein `company_profile` aapki company ke kaam ke hisaab se likhein. AI isi se High / Medium / Low relevance deta hai.

**7. Roz ka automatic run.** PowerShell mein ye command chalayein. Isse roz 09:30 ka task register ho jaata hai. Laptop band tha to on hone ke baad chalega.

```bash
powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1
```

## Excel kahan milegi

```
output\
  MASTER_All_SSC.xlsx              <- sab SSCs ek jagah
  CGSC\
    CGSC_Updates.xlsx              <- sirf CGSC ki entries
    Documents\                     <- CGSC ke PDF / Word files, jaise "2026-06-16_CGSSC _ EOI _ For TNSDC Project.pdf"
  ASDC\ ...                        <- har SSC ka apna folder
  New_Updates\
    New_2026-10-02.xlsx            <- sirf us din mili nayi entries (jis din kuch naya mile)
```

**Roz sirf naya data judta hai:**
- Excel files har din dobara nahi banti. Nayi entries neeche nayi rows ke roop mein judti hain.
- Jo entry badli ho (jaise AI details aa gayi, last date badh gayi), sirf wahi row update hoti hai.
- Jis SSC mein kuch naya nahi, uski file chhui bhi nahi jaati.
- PDF sirf nayi wali download hoti hain. Jo file ek baar download/padhi ja chuki hai, woh dobara download nahi hoti aur uska OCR dobara nahi chalta, chahe run beech mein ruk gaya ho (har file ka kaam turant save hota hai; OCR text `data/ocr_cache/` mein rehta hai).
- Excel mein apne columns jod sakte hain (jaise "Applied?", "Remarks"); tracker unhe kabhi nahi chhedta.
- `ID` column mat hatayein: isi se tracker row pehchaanta hai.

Sheets:

| Sheet | Kahan | Kya hai |
|---|---|---|
| Dashboard | Master | Summary: aaj ke naye, open tenders last date ke hisaab se, websites ka status, AI kharcha (har run refresh) |
| Tenders-RFP-EOI | Master + har SSC | Tender / RFP / EOI / Empanelment / Corrigendum. Red = 3 din ya kam, orange = 7 din ya kam, grey = band. `Found On` = kab mila (aaj wala peela) |
| All Updates | Master + har SSC | Sab kuch: notices, circulars, recruitment, ToT/ToA, news. Filter ke saath |
| Site Status | Master | Har website khuli ya nahi, kitne pages padhe, errors |

`Hidden? = Yes` wali entries woh hain jo kisi page par link nahi thi (media library / sitemap / site search se mili). Filter lagakar dekh sakte hain.

Agar run ke waqt koi Excel khuli ho, to us file ki nayi entries agle run mein jud jaati hain. Kuch chhoot-ta nahi, aur alert mein iska note aata hai.

## Commands

```bash
.venv\Scripts\python -m tender_tracker
```

Roz wala poora run. Ek din mein ek hi baar chalta hai; dobara ke liye `--force` lagayein. Baaki options:

| Option | Kya karta hai |
|---|---|
| `--site CGSC ASDC` | Sirf ye sites (test ke liye; alert nahi jaata) |
| `--deep` | Gehra crawl (normally Sunday ko apne aap hota hai) |
| `--no-ai` | AI se documents nahi padhe jaate |
| `--no-notify` | Email, Telegram aur popup nahi bheje jaate |
| `--excel-only` | Sirf Excel dobara banti hai |
| `--list-sites` | Saari sites ke code dikhata hai |
| `--test-notify` | Test alert bhejta hai |

Logs `logs\` folder mein hain.

## Nayi website jodna

Ek command, bas link aur naam:

```bash
.venv\Scripts\python -m tender_tracker --add-site https://www.example.org "Organisation Ka Naam (ABC)"
```

- Short code apne aap ban jaata hai (bracket wala naam, jaise `ABC`). Chahein to teesra shabd dekar khud code de sakte hain.
- Tracker check kar leta hai ki site khulti hai ya nahi, aur JavaScript wali hai ya nahi.
- Same website dobara add nahi hoti.
- Agle daily run se nayi site apne aap check hogi. Uska apna folder `output\<CODE>\` banega.
- Nayi site ki pehli scan uska "baseline" hoti hai: purane items Excel mein aate hain, par email mein "naye" sirf pichhle 30 din wale dikhte hain. Claude bhi sirf haal ke (45 din) tender documents padhta hai.

Turant test karna ho to (sirf wahi site chalegi):

```bash
.venv\Scripts\python -m tender_tracker --site ABC
```

Hataana ho to `config/sites.yaml` mein us site ka block delete kar dein.

Advanced: `config/sites.yaml` mein `seeds` (tender page ka link) jodne se wo page har baar sabse pehle padha jaata hai.

Har site ki `tier` (`Focused`, `Regular` ya `Low`) batati hai ki summary email mein wo kis section mein aayegi. Sabhi sites roz scan hoti hain; email mein teeno category ka status dikhta hai: har organisation ki row (website padhi gayi ya nahi, aur nahi to kyun), naye tender/updates ki poori detail (Focused ke 15 items per org, Regular ke 8, Low ke 5), aur jin sites mein kuch naya nahi aaya unki list. Nayi site ki `tier` na likhein to wo `Regular` maani jaati hai.

Keywords (kaunsa link tender hai, kaunsa notice) `config/keywords.yaml` mein hain.

## Roz kaise chalta hai (har ghante popup)

Koi fixed time ka bada scan nahi hota. Subah 09:00 se raat 21:00 tak **har ghante** laptop par popup aata hai:

> **SSC websites scan karun?** [Haan, scan karo] [Abhi nahi]

**Haan** dabane par sab apne aap hota hai (~25–30 min):
1. Websites scan hoti hain, nayi PDFs download hoti hain aur unki **Uploaded On** date li jaati hai.
2. **Claude aapke Claude plan se nayi PDFs padhta hai.** API ka alag bill nahi aata. Isme last date, EMD, eligibility, summary aur relevance nikalte hain.
3. Excel mein nayi rows judti hain.
4. Email aur Windows popup aata hai.

Scan chalte waqt ya scan ke 50 minute baad tak popup nahi aata. Kisi bhi waqt khud chalana ho to project folder mein **`START_SCAN.bat`** par double-click karein:
- Ek window khulegi aur poora scan turant shuru ho jaayega (popup ke "Haan" jaisa).
- Window scan khatam hone tak khuli rakhein. Aakhir mein likha aata hai ki scan poora hua, internet nahi tha, ya scan pehle se chal raha tha.
- Window beech mein band kar di to scan ruk jaata hai. Agli baar wahin se kaam aage badhta hai: jo files ho chuki, dobara nahi hoti.

Terminal se yahi command:

```bash
.venv\Scripts\python -m tender_tracker --force
```

Setup ya time badalna: `powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -From 10:00 -Hours 8`

Zaroori baatein:
- Sirf `ai.read_from` (2026-10-02) ke baad mili PDFs padhi jaati hain; ek scan mein zyada se zyada 25 (`ai.max_docs_per_run`).
- Claude plan ki limit khatam ho gayi to bache documents agle scan mein padhe jaate hain; email ke "Notes" mein likha aata hai.
- AI sirf Tender / RFP / EOI / Empanelment / Corrigendum documents padhta hai.

## Document queue aur progress

`300 documents` naye tenders ki count nahi thi: purane pending / unknown documents bhi us batch mein aate the.
Ab normal scan mein tender/RFP/EOI aur configured notice categories pehle process hoti hain. Total maximum
60 documents aur unknown files ka maximum 10-document batch hai. Unknown files delete/ignore nahi hoti;
baaki backlog next scan ke liye pending rehta hai. Sunday / `--deep` mein limits 300 total aur 100 unknown hain.

Console par queue ka total, priority/unknown split, har current file aur `Documents 3/10 saved` progress aati hai.
Slow file par bhi har 15 seconds heartbeat aata hai. Har completed file ka result turant database mein save hota hai;
successful files agle run mein phir download/read nahi hoti.

Document stage 5 minutes ke baad nayi files shuru nahi karta; jo files already chal rahi hain woh finish/save hoti hain.
Yeh poore scan ka hard timeout nahi hai. Settings `config/settings.yaml` → `documents` mein hain.

OCR alag process mein chalta hai, maximum 90 seconds (OCR slot ke wait ke saath). Time limit ya native error par
tracker baaki documents par aage badhta hai. PDF disk par rehti hai; incomplete reading next scan mein usi saved file
se retry hoti hai, maximum 3 attempts. Incomplete OCR ko successful cache nahi maana jaata.

## Scanned PDF (OCR)

Jo PDF sirf photo/scan hoti hai (text nahi hota), use **Tesseract OCR** se padha jaata hai: English + Hindi, pehle 30 page, laptop par hi, free. Isse:
- "Public Notice.pdf" jaise naam wali scanned file bhi pehchaani jaati hai ki asal mein EOI/RFP hai.
- Claude ko text milta hai, jisse plan ki usage kam lagti hai.

Language files `data/tessdata/` mein hain; setting `config/settings.yaml` → `documents.ocr` mein hai.

## Upload date

`Uploaded On` = website par file kab upload hui. Yeh date kahan se aati hai, sabse sahi pehle:
1. WordPress media library ki upload date
2. File naam mein chhupa time-stamp (jaise AMHSSC, LSSSDC)
3. Server ki file date (Last-Modified)
4. Sirf mahina, `/uploads/2026/09/` jaise folder se. Aisi entry par Notes mein "upload month only" likha aata hai.

Kuch sites koi date nahi batati; wahan yeh column khaali rehta hai.

## Problems

- **Site Status mein "failed"**: website band ho sakti hai ya URL badal gaya ho. Browser mein kholkar dekhein, zarurat ho to `sites.yaml` mein URL theek karein.
- **Email nahi aaya**: `--test-notify` chalayein; Gmail ka *app password* hi use karein, normal password nahi.
- **AI details khaali hain**: terminal mein `claude` chalakar login check karein; Excel ke *Notes* column mein reason likha hota hai. Jin tenders ki last date nikal chuki hai ya jo bahut purane hain, unhe Claude nahi padhta.

## GitHub par shift karna (baad mein)

`.github/workflows/daily.yml` taiyaar hai.
1. Folder ko GitHub repo mein push karein. `.env` push nahi hoti.
2. Repo → **Settings → Secrets and variables → Actions** mein ye secrets daalein: `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Workflow roz 09:30 IST chalega:
- Database repo mein save karega.
- Excel ko *Artifacts* mein rakhega.
- Alerts email/Telegram par bhejega.

Kuch sarkari sites videshi servers ko block karti hain. Pehle cloud run ke baad *Site Status* sheet zaroor dekhein.
