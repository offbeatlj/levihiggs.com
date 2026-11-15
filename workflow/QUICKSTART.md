# Inbox Audit Kit — Quick-Start Guide

Zero inbox login. Zero OAuth. Zero cloud uploads. Everything runs locally on your machine.

## 1. Export Your Data (Pick Either)
- **Google Takeout (`.mbox`)**: Go to [takeout.google.com](https://takeout.google.com), select **Mail** only, and download the `.mbox` file.
- **Spreadsheet (`.csv`)**: Export your job tracker CSV with columns `company, role, sector, date, outcome, days_elapsed, notes`.

## 2. Parse Locally (Python 3, No Dependencies)
```bash
# From Google Takeout .mbox:
python parse_inbox.py --mbox ~/Downloads/Takeout/Mail/All\ mail.mbox --email you@gmail.com --out .

# Or from a CSV (test right away with sample_applications.csv):
python parse_inbox.py --csv sample_applications.csv --out .
```
This creates two clean files in your folder:
- `data.json` — powers the interactive web app (`app.html`)
- `audit_table.csv` — full audit table (`company, role, sector, date, stage, outcome, days_elapsed`)

## 3. Open the Visualization App
Open `app.html` in any browser.
- If opened directly via `file://`, click **Load `data.json` / `.csv`** in the top bar (or drag-and-drop `data.json` onto the page).
- Or run a 1-line local server:
  ```bash
  python -m http.server 8000
  ```
  and open `http://localhost:8000/app.html`.

## 4. Export & Print
- **PNG Export**: High-DPI raster export for sharing.
- **SVG Export**: Editable vector export.
- **Print-Ready PDF & Poster Mode**: Switch to **Poster Layout (18×24 / 24×36)** and press `Print / Save PDF`. See `PRINT_SPEC.md` for paper and margin specs.
