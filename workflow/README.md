# Inbox Perseverance Local Workflow (`inbox_perseverance_cli.py`)

A 100% local, zero-cloud privacy workflow that transforms a user's job-search email history (Google Takeout `.mbox` or Gmail CLI JSON export) into an interactive **Perseverance & Truth** visualization suite:
- **Sheer Funnel Counts & Conversion Ratios**
- **3-Stage Interactive Sankey Diagram** (`Source Channel / ATS -> Target Role Domain -> Pipeline Outcome`)
- **Organization Engagement Scatter Plot & Daily Burst Histograms**
- **Dateless Cyclical Seasonality Matrix** (`12 Months x 7 Weekdays` + `24-Hour Circadian Inbox Clock`)
- **NLP Subject-Line Semantic Word Map**
- **The Wall of Perseverance** (Every single email rendered as an interactive cell)

## Privacy & Sanitization Guarantees
1. **100% On-Device Execution**: No email content, headers, or metadata ever leaves the user's computer.
2. **Automatic PII Scrubbing**: Strips all personal/recruiter names, email addresses (`<...>`), phone numbers, and candidate names from all outputs.
3. **Dateless Seasonality**: Strips all calendar years (`2019`, `2022`, `2025`, etc.) so nothing reveals *when* you applied to a role (protecting employed job seekers), while preserving Month-of-Year, Day-of-Week, and Hour-of-Day seasonality.

## Quickstart for Customers

### Option 1: From a Google Takeout `.mbox` Export
```bash
python workflow/inbox_perseverance_cli.py \
  --mbox ~/Downloads/Takeout/Mail/All\ mail.mbox \
  --candidate-name "Your Full Name" \
  --out my_perseverance_stats.json
```

### Option 2: Load Directly in the Web Dashboard
1. Open `index.html` in any browser.
2. Click **"Visualize Your Inbox ->"** in the top-right corner.
3. Select your generated `my_perseverance_stats.json` file. All 6 interactive visualizations update immediately in-memory inside your browser.
