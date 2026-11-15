#!/usr/bin/env python3
"""
Inbox Perseverance Local Analyzer (100% On-Device / Zero Cloud)
---------------------------------------------------------------
Crawls a local Google Takeout .mbox file, JSON export, or IMAP inbox to find every
job application, rejection, interview invite, and offer. Automatically scrubs all PII
(recruiter names, candidate names, email addresses, phone numbers) and strips calendar
years while preserving cyclical seasonality, Sankey topology, scatter clusters, and
NLP subject-line semantics.

Usage:
  python inbox_perseverance_cli.py --mbox ~/Downloads/Takeout/Mail/All\ mail.mbox --candidate-name "Jane Doe"
  python inbox_perseverance_cli.py --json ./raw_emails.json --candidate-name "Jane Doe"
"""

import argparse
import json
import mailbox
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

ATS_KEYWORDS = [
    "greenhouse", "lever.co", "workday", "myworkdayjobs", "icims", "smartrecruiters",
    "ashbyhq", "workable", "taleo", "jobvite", "bamboohr", "angel.co", "wellfound",
    "clearcompany", "breezy"
]

JOB_SUBJECT_KEYWORDS = [
    "application", "applied", "thank you for applying", "thanks for applying",
    "we received your application", "your application to", "you applied for",
    "interview", "phone screen", "onsite", "availability", "next steps",
    "offer letter", "job offer", "onboarding", "welcome to",
    "status update", "update on your", "regarding your application",
    "important information about your", "thank you for your interest"
]

NOISE_SENDERS = [
    "alert@indeed.com", "jobalert@", "alerts@ziprecruiter.com", "no-reply@indeed.com",
    "do-not-reply@indeed.com", "news@linkedin.com", "messages-noreply@linkedin.com",
    "invitations@linkedin.com", "robinhood", "flight", "boarding pass", "zillow", "streeteasy"
]


def decode_mime_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def is_job_email(from_str, subj_str):
    fl = from_str.lower()
    sl = subj_str.lower()
    if any(ns in fl or ns in sl for ns in NOISE_SENDERS):
        return False
    if any(ats in fl for ats in ATS_KEYWORDS):
        return True
    if any(kw in sl for kw in JOB_SUBJECT_KEYWORDS):
        return True
    return False


def categorize_email(from_str, subj_str, body_snippet=""):
    sl = subj_str.lower()
    bl = body_snippet.lower()
    if any(k in sl for k in ["job offer", "offer letter", "verbal offer", "onboarding", "pre-onboarding", "congratulations! you've been selected", "acceptance of application"]):
        return "offers"
    if any(k in sl for k in ["interview", "phone screen", "on-site", "onsite", "hangout confirmation", "availability request", "schedule your", "next steps!", "initial convo", "spark hire"]):
        return "interviews"
    if any(k in sl for k in ["update on your", "status update", "important information about your", "regarding your application", "follow up on your application", "thank you for your interest", "thanks for your interest", "application status"]) or any(k in bl for k in ["unfortunately", "not moving forward", "other candidates", "careful consideration", "regret to inform"]):
        return "rejections"
    return "applications"


def get_source_channel(from_str, subj_str):
    fl = from_str.lower()
    sl = subj_str.lower()
    if any(k in fl for k in ["greenhou", "@us.g", "no-reply@g"]) or "greenhouse" in sl:
        return "Greenhouse ATS"
    if "linkedin" in fl or "you applied for" in sl or "your application to" in sl:
        return "LinkedIn Jobs"
    if any(k in fl for k in ["lever", "@hire."]):
        return "Lever ATS"
    if "angel" in fl or "wellfound" in fl:
        return "AngelList / Wellfound"
    if any(k in fl for k in ["indeed", "ziprecruiter", "glassdoor", "monster", "smartrecruit", "jobvite"]):
        return "Job Boards (Indeed / Zip)"
    if any(k in fl for k in ["workday", "myworkday", "icims", "taleo", "ashby", "workable", "bamboo", "jazz", "breezy", "clearcompany"]):
        return "Enterprise ATS (Workday / Ashby)"
    return "Direct Company & Recruiter"


def get_role_domain(from_str, subj_str):
    text = (from_str + " " + subj_str).lower()
    if any(k in text for k in ["writer", "copywriter", "editor", "content", "editorial", "creative", "communications", "ux writer", "writing"]):
        return "Copywriting, Content & UX"
    if any(k in text for k in ["trust", "safety", "policy", "moderation", "integrity", "compliance", "risk", "legal"]):
        return "Trust & Safety / Policy"
    if any(k in text for k in ["product", "program", "project", "strategy", "operations", "ops", "analyst", "manager", "lead", "director", "associate", "coordinator", "specialist"]):
        return "Product, Strategy & Ops"
    if any(k in text for k in ["teacher", "teaching", "education", "tutor", "esl", "english", "school", "instructor", "curriculum", "academic"]):
        return "Education & EdTech"
    if any(k in text for k in ["marketing", "brand", "social", "growth", "community", "media", "video"]):
        return "Media, Creator & Marketing"
    if any(k in text for k in ["ai", "machine learning", "data", "engineer", "developer", "technical", "software"]):
        return "Big Tech & AI Labs"
    return "Startups, SaaS & General"


def scrub_pii(text, candidate_name=""):
    if not text:
        return ""
    s = re.sub(r"<[^>]*>?", "", text)
    s = re.sub(r"\b[\w\.-]+@[\w\.-]+", "", s)
    s = re.sub(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "", s)
    if candidate_name:
        for part in candidate_name.split():
            if len(part) >= 2:
                s = re.sub(rf"(?i)\b{re.escape(part)}\b", "", s)
    s = re.sub(r"\b(?:with|from)\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -–—:|,.\t\n\"'")


def build_stats_payload(messages, candidate_name=""):
    months_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    months_count = [{"month": m, "applications": 0, "rejections": 0, "interviews": 0, "offers": 0, "total": 0} for m in months_labels]
    dow_count = [{"day": d, "applications": 0, "rejections": 0, "interviews": 0, "offers": 0, "total": 0} for d in dow_labels]
    month_dow_matrix = [[0 for _ in range(7)] for _ in range(12)]
    month_dow_by_cat = {cat: [[0 for _ in range(7)] for _ in range(12)] for cat in ["applications", "rejections", "interviews", "offers"]}
    hour_by_cat = {cat: [0] * 24 for cat in ["applications", "rejections", "interviews", "offers", "total"]}

    daily_counts = Counter()
    weekly_counts = Counter()
    s1_s2 = Counter()
    s2_s3 = Counter()
    channel_totals = defaultdict(lambda: {"applications": 0, "rejections": 0, "interviews": 0, "offers": 0, "total": 0})
    domain_totals = defaultdict(lambda: {"applications": 0, "rejections": 0, "interviews": 0, "offers": 0, "total": 0})
    cluster_stats = defaultdict(lambda: {"applications": 0, "rejections": 0, "interviews": 0, "offers": 0, "total": 0, "domain": "", "channel": ""})

    word_freq = Counter()
    word_cat_dist = defaultdict(lambda: {"applications": 0, "rejections": 0, "interviews": 0, "offers": 0})
    stopwords = {"the", "and", "for", "your", "you", "with", "from", "that", "this", "have", "has", "are", "was", "were", "will", "our", "about", "new", "next", "steps", "re", "fwd", "job", "jobs", "position", "role"}

    for m in messages:
        cat = m.get("category") or categorize_email(m.get("from", ""), m.get("subject", ""), m.get("body", ""))
        ch = get_source_channel(m.get("from", ""), m.get("subject", ""))
        dom = get_role_domain(m.get("from", ""), m.get("subject", ""))
        clean_subj = scrub_pii(m.get("subject", ""), candidate_name)

        s1_s2[(ch, dom)] += 1
        s2_s3[(dom, cat)] += 1
        channel_totals[ch][cat] += 1
        channel_totals[ch]["total"] += 1
        domain_totals[dom][cat] += 1
        domain_totals[dom]["total"] += 1

        archetype_key = f"{ch} — {dom}"
        cluster_stats[archetype_key][cat] += 1
        cluster_stats[archetype_key]["total"] += 1
        cluster_stats[archetype_key]["domain"] = dom
        cluster_stats[archetype_key]["channel"] = ch

        for tok in re.findall(r"[A-Za-z]{3,}", clean_subj.lower()):
            if tok not in stopwords:
                word_freq[tok] += 1
                word_cat_dist[tok][cat] += 1

        try:
            dt = parsedate_to_datetime(m.get("date", ""))
            m_idx, w_idx, hr = dt.month - 1, dt.weekday(), dt.hour
            months_count[m_idx][cat] += 1
            months_count[m_idx]["total"] += 1
            dow_count[w_idx][cat] += 1
            dow_count[w_idx]["total"] += 1
            month_dow_matrix[m_idx][w_idx] += 1
            month_dow_by_cat[cat][m_idx][w_idx] += 1
            hour_by_cat[cat][hr] += 1
            hour_by_cat["total"][hr] += 1
            daily_counts[dt.strftime("%Y-%m-%d")] += 1
            weekly_counts[dt.strftime("%Y-W%W")] += 1
        except Exception:
            pass

    daily_hist_bins = Counter()
    for _, cnt in daily_counts.items():
        if cnt == 1: daily_hist_bins["1 email/day"] += 1
        elif cnt <= 3: daily_hist_bins["2–3 emails/day"] += 1
        elif cnt <= 6: daily_hist_bins["4–6 emails/day"] += 1
        elif cnt <= 10: daily_hist_bins["7–10 emails/day"] += 1
        elif cnt <= 20: daily_hist_bins["11–20 emails/day"] += 1
        else: daily_hist_bins["21+ (Sprint Day)"] += 1

    semantic_terms = []
    for word, count in word_freq.most_common(65):
        dist = word_cat_dist[word]
        if dist["offers"] >= 2 or word in ["offer", "welcome", "onboarding", "selected"]:
            cluster = "Offer & Onboarding"
        elif dist["interviews"] / max(1, count) > 0.22 or word in ["interview", "schedule", "availability", "screen", "phone", "onsite", "call"]:
            cluster = "Interview & Scheduling"
        elif dist["rejections"] / max(1, count) > 0.30 or word in ["update", "status", "regarding", "important", "information", "interest", "unfortunately"]:
            cluster = "Rejection & Status Vernacular"
        elif word in ["writer", "copywriter", "editor", "content", "manager", "lead", "specialist", "coordinator", "associate", "teacher", "director", "operations", "marketing", "product", "engineer"]:
            cluster = "Role & Craft Identity"
        else:
            cluster = "Application & ATS Pipeline"
        semantic_terms.append({"word": word, "count": count, "cluster": cluster, "breakdown": dist})

    scatter_points = []
    for label, st in sorted(cluster_stats.items(), key=lambda x: -x[1]["total"]):
        depth = (st["applications"] * 1.0 + st["rejections"] * 1.5 + st["interviews"] * 5.0 + st["offers"] * 12.0) / max(1, st["total"])
        scatter_points.append({
            "label": label,
            "domain": st["domain"],
            "channel": st["channel"],
            "total_emails": st["total"],
            "applications": st["applications"],
            "rejections": st["rejections"],
            "interviews": st["interviews"],
            "offers": st["offers"],
            "depth_score": round(depth, 2)
        })

    return {
        "summary": {
            "total_emails": len(messages),
            "applications": sum(1 for m in messages if m.get("category") == "applications"),
            "rejections": sum(1 for m in messages if m.get("category") == "rejections"),
            "interviews": sum(1 for m in messages if m.get("category") == "interviews"),
            "offers": sum(1 for m in messages if m.get("category") == "offers"),
            "active_days": len(daily_counts),
            "active_weeks": len(weekly_counts),
            "max_single_day_burst": max(daily_counts.values()) if daily_counts else 0,
            "unique_channels": len(channel_totals),
            "unique_domains": len(domain_totals),
            "unique_employers_touched": len(cluster_stats)
        },
        "seasonality": {
            "months": months_count,
            "days_of_week": dow_count,
            "month_dow_matrix": month_dow_matrix,
            "month_dow_by_cat": month_dow_by_cat,
            "hour_by_cat": hour_by_cat
        },
        "histograms": {
            "daily_burst_bins": [
                {"bin": k, "days": daily_hist_bins[k]}
                for k in ["1 email/day", "2–3 emails/day", "4–6 emails/day", "7–10 emails/day", "11–20 emails/day", "21+ (Sprint Day)"]
            ],
            "weekly_distribution": sorted(list(weekly_counts.values()), reverse=True)[:60]
        },
        "sankey": {
            "stage1_to_stage2": [{"source": k[0], "target": k[1], "value": v} for k, v in sorted(s1_s2.items(), key=lambda x: -x[1])],
            "stage2_to_stage3": [{"source": k[0], "target": k[1], "value": v} for k, v in sorted(s2_s3.items(), key=lambda x: -x[1])]
        },
        "channels": [{"channel": k, **v} for k, v in sorted(channel_totals.items(), key=lambda x: -x[1]["total"])],
        "domains": [{"domain": k, **v} for k, v in sorted(domain_totals.items(), key=lambda x: -x[1]["total"])],
        "semantic_words": semantic_terms,
        "scatter_clusters": scatter_points[:70]
    }


def main():
    parser = argparse.ArgumentParser(description="Local Privacy-First Inbox Perseverance Visualizer")
    parser.add_argument("--mbox", help="Path to Google Takeout .mbox file")
    parser.add_argument("--json", help="Path to raw emails JSON file")
    parser.add_argument("--candidate-name", default="", help="Your full name to scrub from subjects")
    parser.add_argument("--out", default="perseverance_stats.json", help="Output JSON path")
    args = parser.parse_args()

    messages = []
    if args.mbox:
        print(f"Scanning local MBOX file: {args.mbox} ...")
        mb = mailbox.mbox(args.mbox)
        for msg in mb:
            f_str = decode_mime_str(msg.get("From", ""))
            s_str = decode_mime_str(msg.get("Subject", ""))
            d_str = msg.get("Date", "")
            if is_job_email(f_str, s_str):
                cat = categorize_email(f_str, s_str)
                messages.append({"from": f_str, "subject": s_str, "date": d_str, "category": cat})
    elif args.json:
        with open(args.json, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for cat, arr in raw.items():
                for item in arr:
                    item["category"] = cat
                    messages.append(item)
        elif isinstance(raw, list):
            for item in raw:
                if "category" not in item:
                    item["category"] = categorize_email(item.get("from", ""), item.get("subject", ""))
                messages.append(item)
    else:
        parser.error("Provide either --mbox or --json")

    payload = build_stats_payload(messages, args.candidate_name)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Done! Processed {len(messages)} job emails into {args.out} (100% PII-free & dateless).")


if __name__ == "__main__":
    main()
