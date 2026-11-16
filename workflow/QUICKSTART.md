# Application Tracker & Resume Comparison — Quick-Start Guide

Zero inbox login. Zero OAuth. Zero cloud uploads. Everything runs locally on your machine.

## 1. Export Your Data (Pick Any Source)
- **Google Takeout (`.mbox`)**: Go to [takeout.google.com](https://takeout.google.com), select **Mail** only, and download the `.mbox` archive.
- **Spreadsheet (`.csv`)**: Export your tracker with columns `company, role, sector, appliedDate, lastTouchDate, outcome, days_elapsed, resume_variant, notes`.
- **JSON Log (`.json`)**: Export structured application arrays (`id, company, roleLevel, department, appliedDate, lastTouchDate, status, resumeVariant, daysToResponse`).

## 2. Parse Locally (Python 3, Standard Library Only)
```bash
# From Google Takeout .mbox:
python parse_inbox.py --mbox ~/Downloads/Takeout/Mail/All\ mail.mbox --email you@gmail.com --out .

# From CSV (test right away with the included sample):
python parse_inbox.py --csv sample_applications.csv --out .

# Mask non-accepted company names before sharing:
python parse_inbox.py --csv sample_applications.csv --hash-names --out .
```
This creates three clean files locally:
- `data.json` — application data and resume comparison metrics for `app.html`
- `audit_table.csv` — full spreadsheet of applications, stages, and reply times
- `llm-brief.md` — anonymized summary ready to paste into ChatGPT or Claude

## 3. Open the Offline View (`app.html`)
Open `app.html` in any browser:
- Starts immediately with the animated **Ink-Drop Fan Canvas**, **Pipeline Flow**, **Response Time**, and **Resume Version Comparison** charts.
- Directly via `file://`: click **Load Your CSV / JSON** (or drag-and-drop `data.json` onto the page).
- Or run a 1-line static server:
  ```bash
  python -m http.server 8000
  ```
  and visit `http://localhost:8000/app.html`.

## 4. Export & Print
- **PNG / SVG Export**: Vector and 2x PNG exports (`SVG ↓`, `PNG ↓`) ready for Figma, Illustrator, or sharing.
- **Print Layout + Themes**: Toggle **Print Layout** and **Paper Mode** (`#FFFFFF`) or **Dark Mode** (`#0F172A`). See `PRINT_SPEC.md`.
