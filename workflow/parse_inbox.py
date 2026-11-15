#!/usr/bin/env python3
"""
Inbox Audit — 100% Local .mbox & .csv Parser
Zero inbox login. Zero OAuth. Zero cloud uploads.

Parses Google Takeout (.mbox) or CSV job trackers into:
  1. audit_table.csv  (company, role, date, stage, outcome, days_elapsed)
  2. data.json        (powers app.html visualizations & exports)
"""

import argparse
import csv
import email.utils
import json
import mailbox
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

REJECTION_PHRASES = [
    "unfortunately",
    "careful consideration",
    "other candidates",
    "not moving forward",
    "keep your resume on file",
    "at this time",
    "high volume",
    "different direction",
    "more closely match",
    "position has been filled",
]

ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "icims.com", "smartrecruiters.com", "workable.com", "bamboohr.com",
    "jobvite.com", "taleo.net", "breezy.hr", "recruitee.com",
    "rippling.com", "jazzhr.com", "linkedin.com", "indeed.com",
}

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "me.com", "proton.me", "protonmail.com", "aol.com",
}

STAGE_RANK = {
    "built": 5,
    "i_passed": 4,
    "interviewed": 3,
    "they_passed": 2,
    "silence": 1,
}

OUTCOME_LABELS = {
    "built": "Accepted",
    "i_passed": "I Passed",
    "interviewed": "Interviewed",
    "they_passed": "They Passed",
    "silence": "Silence",
}


def clean_subject(subj: str) -> str:
    s = re.sub(r"^(re|fw|fwd)\s*:\s*", "", (subj or "").strip(), flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def scrub_pii(text: str, candidate_name: str = "") -> str:
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[redacted]", text or "")
    t = re.sub(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", "[redacted]", t)
    if candidate_name:
        for part in candidate_name.split():
            if len(part) > 2:
                t = re.sub(re.escape(part), "[candidate]", t, flags=re.I)
    return t


def extract_company(sender: str, subject: str) -> str:
    name, addr = email.utils.parseaddr(sender or "")
    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    if domain and domain not in PERSONAL_DOMAINS and domain not in ATS_DOMAINS:
        base = domain.split(".")[0]
        if base not in {"mail", "email", "jobs", "careers", "recruiting", "talent", "notifications", "no-reply", "noreply"}:
            return base.capitalize()
    # Fallback to sender display name or subject hints ("at Company", "with Company")
    m = re.search(r"\b(?:at|with|to|from)\s+([A-Z][A-Za-z0-9&.\- ]{1,30})", subject or "")
    if m:
        return m.group(1).strip(" .,:-")
    cleaned = re.sub(r"\b(recruiting|talent|careers|team|hr|hiring|jobs|notifications)\b", "", name, flags=re.I).strip(" -|@<>\"'")
    return cleaned[:32] if cleaned else (domain or "Unknown")


def classify_message(subject: str, body: str, is_outbound: bool) -> str:
    blob = f"{subject}\n{body}".lower()
    if is_outbound and any(k in blob for k in ("withdraw", "accepted another", "competing offer", "declining", "cancel the interview")):
        return "i_passed"
    if any(k in blob for k in ("offer letter", "pleased to offer", "verbal offer", "compensation package", "welcome to the team")):
        return "offer_signal"
    if any(k in blob for k in ("schedule an interview", "interview invitation", "phone screen", "technical screen", "onsite", "calendly.com", "zoom.us/j")):
        return "interview_signal"
    if any(k in blob for k in REJECTION_PHRASES) or any(k in blob for k in ("regret to inform", "will not be moving forward", "unable to move forward")):
        return "rejection_signal"
    if any(k in blob for k in ("thank you for applying", "application received", "we have received your application", "thanks for applying")):
        return "ack_signal"
    return "other"


def extract_body(msg) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
                except Exception:
                    pass
        return "\n".join(parts)[:4000]
    try:
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")[:4000] if payload else ""
    except Exception:
        return str(msg.get_payload() or "")[:4000]


def parse_mbox(mbox_path: str, candidate_email: str = "", candidate_name: str = ""):
    mbox = mailbox.mbox(mbox_path)
    threads = defaultdict(list)
    phrase_counts = Counter()

    for msg in mbox:
        subj = str(msg.get("subject", "") or "")
        sender = str(msg.get("from", "") or "")
        date_str = str(msg.get("date", "") or "")
        dt = email.utils.parsedate_to_datetime(date_str) if date_str else None
        if dt and dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        body = extract_body(msg)
        blob_lower = f"{subj}\n{body}".lower()

        is_outbound = bool(candidate_email and candidate_email.lower() in sender.lower())
        sig = classify_message(subj, body, is_outbound)
        if sig == "other" and not any(k in blob_lower for k in ("application", "applied", "interview", "candidate", "recruiting")):
            continue

        for p in REJECTION_PHRASES:
            if p in blob_lower:
                phrase_counts[p] += 1

        company = extract_company(sender, subj)
        threads[company].append({
            "dt": dt,
            "sig": sig,
            "subject": clean_subject(subj),
            "is_outbound": is_outbound,
        })

    records = []
    for company, msgs in threads.items():
        msgs_dated = [m for m in msgs if m["dt"] is not None]
        msgs_dated.sort(key=lambda x: x["dt"])
        first_dt = msgs_dated[0]["dt"] if msgs_dated else None
        sigs = {m["sig"] for m in msgs}

        if "i_passed" in sigs:
            outcome = "i_passed"
            stage = "Declined / Withdrew"
        elif "offer_signal" in sigs:
            outcome = "built"
            stage = "Offer"
        elif "interview_signal" in sigs:
            outcome = "interviewed"
            stage = "Interview"
        elif "rejection_signal" in sigs:
            outcome = "they_passed"
            stage = "Rejected"
        else:
            outcome = "silence"
            stage = "Applied"

        resp_dt = None
        for m in msgs_dated[1:]:
            if not m["is_outbound"]:
                resp_dt = m["dt"]
                break
        days_elapsed = max(0, (resp_dt - first_dt).days) if (first_dt and resp_dt) else None

        records.append({
            "company": company,
            "role": "Applied Role",
            "sector": "General",
            "date": first_dt.strftime("%Y-%m-%d") if first_dt else "",
            "stage": stage,
            "outcome": outcome,
            "outcome_label": OUTCOME_LABELS[outcome],
            "stage_depth": STAGE_RANK[outcome],
            "days_elapsed": days_elapsed,
            "acknowledged": bool(len(msgs) > 1 or "ack_signal" in sigs or outcome != "silence"),
        })
    return records, phrase_counts


def parse_csv(csv_path: str):
    records = []
    phrase_counts = Counter()
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            norm = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            company = norm.get("company") or norm.get("organization") or "Unknown"
            role = norm.get("role") or norm.get("title") or "Applied Role"
            sector = norm.get("sector") or norm.get("industry") or "General"
            date_val = norm.get("date") or norm.get("applied_date") or ""
            raw_out = (norm.get("outcome") or norm.get("status") or "silence").lower()
            notes = (norm.get("notes") or norm.get("rejection_text") or "").lower()

            for p in REJECTION_PHRASES:
                if p in notes:
                    phrase_counts[p] += 1

            if any(k in raw_out for k in ("accept", "built", "hired")):
                outcome, stage = "built", "Accepted"
            elif any(k in raw_out for k in ("i passed", "declined", "withdrew", "turned down")):
                outcome, stage = "i_passed", "Declined / Withdrew"
            elif any(k in raw_out for k in ("interview", "screen", "final")):
                outcome, stage = "interviewed", "Interview"
            elif any(k in raw_out for k in ("reject", "they passed", "closed")):
                outcome, stage = "they_passed", "Rejected"
            else:
                outcome, stage = "silence", "Applied"

            days_raw = norm.get("days_elapsed") or norm.get("days") or ""
            days_elapsed = int(days_raw) if days_raw.isdigit() else None

            records.append({
                "company": company,
                "role": role,
                "sector": sector,
                "date": date_val,
                "stage": stage,
                "outcome": outcome,
                "outcome_label": OUTCOME_LABELS[outcome],
                "stage_depth": STAGE_RANK[outcome],
                "days_elapsed": days_elapsed,
                "acknowledged": outcome != "silence",
            })
    return records, phrase_counts


def build_bundle(records, phrase_counts, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    records.sort(key=lambda r: (-r["stage_depth"], r["days_elapsed"] if r["days_elapsed"] is not None else 9999, r["company"]))

    # Write full audit_table.csv
    csv_out = os.path.join(out_dir, "audit_table.csv")
    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "role", "sector", "date", "stage", "outcome", "days_elapsed"])
        for r in records:
            w.writerow([
                r["company"], r["role"], r["sector"], r["date"],
                r["stage"], r["outcome_label"],
                "" if r["days_elapsed"] is None else r["days_elapsed"],
            ])

    total = len(records)
    acks = sum(1 for r in records if r["acknowledged"])
    interviews = sum(1 for r in records if r["outcome"] in ("interviewed", "i_passed", "built"))
    finals_offers = sum(1 for r in records if r["outcome"] in ("i_passed", "built"))
    accepted = sum(1 for r in records if r["outcome"] == "built")

    rt_buckets = {"0-3d": 0, "4-7d": 0, "8-14d": 0, "15-30d": 0, "31+d": 0, "Never": 0}
    for r in records:
        d = r["days_elapsed"]
        if d is None or r["outcome"] == "silence":
            rt_buckets["Never"] += 1
        elif d <= 3:
            rt_buckets["0-3d"] += 1
        elif d <= 7:
            rt_buckets["4-7d"] += 1
        elif d <= 14:
            rt_buckets["8-14d"] += 1
        elif d <= 30:
            rt_buckets["15-30d"] += 1
        else:
            rt_buckets["31+d"] += 1

    timeline_map = defaultdict(lambda: {"applied": 0, "interviews": 0, "rejections": 0})
    for r in records:
        ym = r["date"][:7] if len(r["date"]) >= 7 else "Undated"
        timeline_map[ym]["applied"] += 1
        if r["outcome"] in ("interviewed", "i_passed", "built"):
            timeline_map[ym]["interviews"] += 1
        elif r["outcome"] == "they_passed":
            timeline_map[ym]["rejections"] += 1

    timeline = [{"period": k, **timeline_map[k]} for k in sorted(timeline_map.keys())]
    phrases = [{"phrase": p, "count": c} for p, c in phrase_counts.most_common()]

    payload = {
        "summary": {
            "total_companies": total,
            "accepted": accepted,
            "i_passed": sum(1 for r in records if r["outcome"] == "i_passed"),
            "interviewed": sum(1 for r in records if r["outcome"] == "interviewed"),
            "they_passed": sum(1 for r in records if r["outcome"] == "they_passed"),
            "silence": sum(1 for r in records if r["outcome"] == "silence"),
        },
        "funnel": [
            {"stage": "Applications", "count": total},
            {"stage": "Acknowledgments", "count": acks},
            {"stage": "Interviews", "count": interviews},
            {"stage": "Finals & Offers", "count": finals_offers},
            {"stage": "Accepted", "count": accepted},
        ],
        "response_times": [{"bucket": k, "count": v} for k, v in rt_buckets.items()],
        "rejection_phrases": phrases,
        "timeline": timeline,
        "companies": records,
    }

    json_out = os.path.join(out_dir, "data.json")
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(records)} deduplicated companies to {csv_out} and {json_out}")


def main():
    ap = argparse.ArgumentParser(description="100% local .mbox / .csv job search inbox parser.")
    ap.add_argument("--mbox", help="Path to Google Takeout .mbox file")
    ap.add_argument("--csv", help="Path to job applications .csv file")
    ap.add_argument("--email", default="", help="Your email address (to detect outbound withdrawals)")
    ap.add_argument("--name", default="", help="Your full name (scrubbed from outputs)")
    ap.add_argument("--out", default=".", help="Output directory for data.json and audit_table.csv")
    args = ap.parse_args()

    if not args.mbox and not args.csv:
        ap.error("Provide either --mbox path/to/All_mail.mbox or --csv path/to/applications.csv")

    if args.mbox:
        records, phrases = parse_mbox(args.mbox, args.email, args.name)
    else:
        records, phrases = parse_csv(args.csv)

    build_bundle(records, phrases, args.out)


if __name__ == "__main__":
    main()
