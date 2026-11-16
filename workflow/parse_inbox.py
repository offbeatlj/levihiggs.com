#!/usr/bin/env python3
"""
Local Career Telemetry & Resume A/B Ingestion Engine (parse_inbox.py).
Generic, zero-hardcoded-PII pipeline parsing .mbox, .csv, or .json application logs
into normalized ApplicationRecord telemetry, comparative Resume A/B analytics,
and headhunter diagnostics. Runs 100% locally.
"""

from __future__ import annotations

import argparse
import csv
import email
import email.header
import email.utils
import hashlib
import json
import mailbox
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any

GHOST_THRESHOLD_DAYS = 30

JOB_HINT = re.compile(
    r"\b(application|applied|applicant|candidate|interview|recruiter|hiring|position|role|"
    r"resume|cv|offer letter|written offer|moving forward|unfortunately|shortlist|background check|"
    r"onboarding|phone screen|technical screen|assessment|calendly|goodtime)\b",
    re.I,
)

STATUS_ENUM = (
    "applied",
    "recruiter_screen",
    "technical_round",
    "onsite",
    "offer",
    "rejected",
    "ghosted",
)

RESUME_VARIANTS = (
    "v1_executive_summary",
    "v2_technical_deepdive",
    "v3_minimalist",
)

STAGE_LABELS = [
    ("applied", "Applied"),
    ("recruiter_screen", "Recruiter Screen"),
    ("technical_round", "Technical Round"),
    ("onsite", "Onsite / Final"),
    ("offer", "Offer"),
]

ATS_DOMAINS = {
    "greenhouse.io": "Greenhouse ATS",
    "greenhouse-mail.io": "Greenhouse ATS",
    "lever.co": "Lever ATS",
    "ashbyhq.com": "Ashby ATS",
    "myworkday.com": "Workday Portal",
    "myworkdayjobs.com": "Workday Portal",
    "icims.com": "iCIMS Portal",
    "smartrecruiters.com": "SmartRecruiters",
    "jobvite.com": "Jobvite ATS",
    "rippling.com": "Rippling ATS",
    "gem.com": "Gem Recruiting",
    "workablemail.com": "Workable ATS",
    "bamboohr.com": "BambooHR",
    "jazz.co": "JazzHR",
    "linkedin.com": "LinkedIn Easy Apply",
    "indeed.com": "Indeed",
    "indeedemail.com": "Indeed",
    "wellfound.com": "Wellfound",
    "angel.co": "Wellfound",
    "dice.com": "Dice",
}

COMPANY_DOMAIN_MAP = {
    "google": "Google", "youtube": "YouTube", "tiktok": "TikTok", "bytedance": "TikTok",
    "openai": "OpenAI", "anthropic": "Anthropic", "stripe": "Stripe", "meta": "Meta",
    "fb": "Meta", "facebook": "Meta", "airbnb": "Airbnb", "spotify": "Spotify",
    "discord": "Discord", "figma": "Figma", "notion": "Notion", "linear": "Linear",
    "vercel": "Vercel", "cloudflare": "Cloudflare", "databricks": "Databricks",
    "snowflake": "Snowflake", "coinbase": "Coinbase", "block": "Block", "squareup": "Block",
    "netflix": "Netflix", "amazon": "Amazon", "apple": "Apple", "microsoft": "Microsoft",
    "roblox": "Roblox", "snap": "Snap", "reddit": "Reddit", "pinterest": "Pinterest",
    "duolingo": "Duolingo", "coursera": "Coursera", "doordash": "DoorDash",
    "instacart": "Instacart", "uber": "Uber", "lyft": "Lyft", "nyt": "New York Times",
    "nytimes": "New York Times", "scale": "Scale AI", "scaleai": "Scale AI",
    "doubleverify": "DoubleVerify", "opensea": "OpenSea", "teacherspayteachers": "Teachers Pay Teachers",
    "chateauschool": "Chateau School", "elcshanghai": "ELC Shanghai", "rebelbase": "RebelBase",
}

REJECTION_PHRASES = [
    ("careful consideration", "After careful consideration"),
    ("not moving forward", "Not moving forward at this time"),
    ("more closely aligned", "Candidates more closely aligned"),
    ("high volume", "High volume of qualified applicants"),
    ("other candidates", "Decided to pursue other candidates"),
    ("keep your resume", "Keep your resume on file"),
    ("not to proceed", "Decided not to proceed"),
    ("filled the position", "Position has been filled"),
    ("best of luck", "Wish you the best of luck"),
]

PERSONAL_REJECT_HINTS = (
    "happy to stay in touch", "keep in touch", "feedback", "really enjoyed speaking",
    "thank you for taking the time to speak", "great chatting", "loved meeting",
    "reach out directly", "other openings on my team",
)

ROLE_SYNTHESIS_PATTERNS = [
    (r"\b(trust\s*&?\s*safety|policy enforcement|content policy|moderation|integrity)\b", "Trust & Safety / Policy", "Trust & Safety / Policy"),
    (r"\b(ai safety|red team|alignment|responsible ai|model policy|governance|trustworthy ai)\b", "AI Safety & Governance", "Trust & Safety / Policy"),
    (r"\b(product manager|product lead|program manager|tpm|technical program)\b", "Product & Program Mgmt", "Product Management"),
    (r"\b(security|cybersecurity|risk|compliance|privacy|fraud|abuse)\b", "Security & Risk", "Engineering / Security"),
    (r"\b(operations|ops manager|strategy & ops|bizops|analytics|chief of staff)\b", "Operations & Strategy", "Operations & Programs"),
    (r"\b(content|editorial|writer|strategist|communications|researcher)\b", "Editorial & Research", "Editorial / Education"),
    (r"\b(teacher|instructor|curriculum|education|fellow)\b", "Education & Instruction", "Editorial / Education"),
    (r"\b(software|engineer|developer|ml|data scientist|architect)\b", "Engineering & Data", "Engineering / Security"),
]


def scrub_pii(text: str) -> str:
    """Strip personal identifiers, emails, phone numbers, URLs, and greetings."""
    if not text:
        return ""
    s = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[redacted]", s := str(text))
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\+?\d[\d\-\.\(\)\s]{7,}\d", "[phone]", s)
    s = re.sub(r"\b(Hi|Hello|Dear)\s+[A-Z][a-z]+", r"\1 [Candidate]", s)
    return " ".join(s.split())


def company_hash_hex(name: str, length: int = 12) -> str:
    """Deterministic SHA-256 display cipher prefix for privacy mode."""
    digest = hashlib.sha256(str(name).strip().lower().encode("utf-8")).hexdigest()
    return "0x" + digest[:length].upper()


def slug_id(company: str, applied_date: str, index: int = 0) -> str:
    seed = f"{company}|{applied_date}|{index}".encode("utf-8")
    return "app_" + hashlib.sha256(seed).hexdigest()[:10]


def _safe_decode(payload: bytes, enc: str | None) -> str:
    try:
        return payload.decode(enc or "utf-8", errors="ignore")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="ignore")


def decode_header_val(val: str | None) -> str:
    if not val:
        return ""
    parts = []
    for chunk, enc in email.header.decode_header(str(val)):
        if isinstance(chunk, bytes):
            parts.append(_safe_decode(chunk, enc))
        else:
            parts.append(chunk)
    return "".join(parts)


def extract_domain(addr: str) -> str:
    m = re.search(r"@([A-Za-z0-9.\-]+)", str(addr or ""))
    return m.group(1).lower() if m else ""


def normalize_company(raw_name: str, domain: str = "") -> str:
    dom = (domain or "").lower()
    dom = re.sub(r"^(mail|jobs|careers|recruiting|talent|no-?reply|notifications?|updates?)\.", "", dom)
    root = dom.split(".")[0] if dom else ""
    if root in COMPANY_DOMAIN_MAP:
        return COMPANY_DOMAIN_MAP[root]
    cleaned = re.sub(r"['\"<>\[\]]", "", str(raw_name or "")).strip()
    cleaned = re.sub(r"\b(recruiting|careers|talent acquisition|talent|jobs|team|hr|via linkedin|via greenhouse)\b", "", cleaned, flags=re.I).strip(" -|:,")
    if cleaned and 2 <= len(cleaned) <= 44 and "@" not in cleaned:
        return cleaned
    if root and root not in {"gmail", "yahoo", "outlook", "icloud", "me", "proton", "greenhouse", "lever", "ashbyhq", "myworkday", "workablemail", "linkedin"}:
        return root.capitalize()
    return cleaned[:40] or "Unknown Organization"


def parse_dt(val: str) -> datetime | None:
    if not val:
        return None
    s = str(val).strip()
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


parse_date = parse_dt


def _clean_org_cand(s: str) -> str:
    s = re.sub(r"&nbsp;|&amp;", " ", str(s or ""))
    s = re.split(r"\.\s+(?:unfortunately|at this time|thank you|we |our )|(?:\s+and\s+(?:wish|for applying|thank))|(?:\s+-\s+an update)|'s$", s, maxsplit=1, flags=re.I)[0]
    s = re.sub(r"\b(?:inc\.?|llc|corp\.?|ltd\.?|careers|recruiting|hiring|people|talent acquisition|human resources|hr team|jobs|team)\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" .,-:|!\"'")


def company_from(hdr: str, subject: str = "", body: str = "") -> str:
    """Resolve organization name cleanly from corporate domain, ATS header, or body context."""
    dom = extract_domain(hdr)
    disp = _clean_org_cand(re.sub(r"<[^>]+>", "", str(hdr or "")))
    free_or_relay = (
        not dom
        or any(a in dom for a in ATS_DOMAINS)
        or any(x in dom for x in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "craigslist.org"))
    )
    if not free_or_relay:
        # Prefer corporate domain if display name looks like a personal name ("Sarah" or "Sarah Jones")
        if not disp or re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z\-]+)?$", disp):
            return normalize_company("", dom)
        return normalize_company(disp, dom)

    # For ATS / LinkedIn / generic relays, parse "at Company" or "Thank you for applying to Company"
    txt = f"{subject}\n{body[:600]}"
    for pat in (
        r"(?i:thank you for applying (?:to|for a position at|with)|application (?:to|for your role at|at)|interview with|phone screen with|was viewed by|applied to)\s+([A-Z][A-Za-z0-9&\.\-\' ]{1,40})",
        r"\b(?i:manager|engineer|writer|specialist|analyst|lead|director|associate|coordinator|intern|developer)\s+(?i:at|for|with)\s+([A-Z][A-Za-z0-9&\.\-\' ]{1,40})",
    ):
        m = re.search(pat, txt)
        if m:
            cand = _clean_org_cand(m.group(1))
            if 2 <= len(cand) <= 38 and cand.lower() not in {"us", "our team", "the", "a"}:
                return cand
    return normalize_company(disp, "" if free_or_relay else dom)


def classify_message(subject: str, text: str, outbound: bool = False) -> str:
    """Classify a thread message into pipeline stage or noise."""
    subj_l = (subject or "").lower()
    blob = f"{subject}\n{text}".lower()
    if any(w in blob for w in ("job alert", "jobs you might like", "recommended jobs", "daily digest", "unsubscribe from alerts", "new jobs match", "job search newsletter", "jobs you may be interested in")):
        return "noise"
    if re.search(r"\b(signed offer|offer accepted|accepted your offer|welcome aboard|excited to welcome you to)\b", blob):
        return "offer_accepted"
    if re.search(r"\b(formal offer|written offer|pleased to extend|offer letter|contingent offer|compensation package|job offer)\b", blob):
        return "offer"
    if outbound and re.search(r"\b(withdraw|withdrawing my application|decline the offer|accepted another offer|pursuing another)\b", blob):
        return "withdrew"
    if re.search(r"\b(unfortunately|not moving forward|not selected|other candidates|more closely aligned|regret to inform|not to proceed|decided not to move)\b", blob):
        return "rejection"
    if re.search(r"\b(onsite|final round|panel interview|loop interview|superday)\b", blob):
        return "onsite"
    if re.search(r"\b(technical screen|technical interview|coding challenge|take-home|case study|work sample|system design|hirevue)\b", blob):
        return "technical"
    if re.search(r"\b(phone screen|introductory call|recruiter screen|invite you to interview|schedule an interview|schedule a call|availability for a call|calendly\.com|goodtime\.io|next steps in the interview)\b", blob) or ("interview" in subj_l and "tips" not in subj_l and "question" not in subj_l):
        return "screen"
    if re.search(r"\b(thank you for applying|application received|received your application|thanks for your interest|we received your resume|application was viewed)\b", blob):
        return "receipt"
    if outbound and re.search(r"\b(apply|application|resume|attached|cover letter|interested in the)\b", blob):
        return "applied"
    return "other"


def channel_for(from_header: str, subject: str = "") -> str:
    blob = f"{from_header} {subject}".lower()
    for dom, label in ATS_DOMAINS.items():
        if dom in blob:
            return label
    if any(k in blob for k in ("referral", "referred", "introduced me", "mutual")):
        return "Warm Referral"
    if any(k in blob for k in ("reaching out", "saw your profile", "inmail", "sourcing")):
        return "Recruiter Outreach"
    return "Direct Email / Company Domain"


def extract_role(subject: str, text: str) -> str:
    blob = f"{subject}\n{text[:900]}"
    m = re.search(r"(?:application for|role of|position[:\s]+|for the)\s+([A-Z][A-Za-z0-9 &/\-,]{3,56})(?:\s+(?:at|with|role|position|\-|\n)|$)", blob)
    if m:
        cand = m.group(1).strip(" .,-:|")
        if 3 <= len(cand) <= 56:
            return cand
    for pat, label, _ in ROLE_SYNTHESIS_PATTERNS:
        if re.search(pat, blob.lower()):
            return label
    return "Product / Operations"


def infer_role_meta(role: str, sector: str = "") -> tuple[str, str, str]:
    """Return (roleLevel, department, track) from role title and sector."""
    r = f"{role} {sector}".lower()
    if any(k in r for k in ("director", "vp", "vice president", "head of", "chief", "executive")):
        level = "Executive / Director"
    elif any(k in r for k in ("principal", "staff", "lead")):
        level = "Lead / Staff"
    elif any(k in r for k in ("manager", "supervisor", "mgr")):
        level = "Manager"
    elif any(k in r for k in ("senior", "sr.", "sr ", "iii", "iv")):
        level = "Senior IC"
    elif any(k in r for k in ("teacher", "fellow", "associate", "coordinator", "intern")):
        level = "Fellow / Specialist"
    else:
        level = "Mid / Specialist"

    track = "General Ops & Strategy"
    dept = sector or "Operations & Programs"
    for pat, trk, dep in ROLE_SYNTHESIS_PATTERNS:
        if re.search(pat, r):
            track = trk
            dept = sector or dep
            break
    return level, dept, track


def detect_resume_variant(raw_hint: str = "", attachments: list[str] | None = None, role: str = "", ts: str = "") -> str:
    """Detect resumeVariant ID from CSV tag, attachment filename, or return untagged."""
    blob = " ".join([str(raw_hint or ""), " ".join(attachments or [])]).strip().lower()
    if not blob:
        return "untagged"
    if blob in RESUME_VARIANTS:
        return blob
    norm = re.sub(r"[^a-z0-9]+", "_", blob).strip("_")
    if "v1" in norm or "exec" in norm or "leadership" in norm or "director" in norm or "pm_rot" in norm:
        return "v1_executive_summary"
    if "v2" in norm or "tech" in norm or "cyber" in norm or "security" in norm or "engineering" in norm or "deepdive" in norm:
        return "v2_technical_deepdive"
    if "v3" in norm or "min" in norm or "clean" in norm or "onepage" in norm or "short" in norm:
        return "v3_minimalist"
    return norm[:32] if re.match(r"^[a-z0-9_]{3,32}$", norm) and norm not in {"resume", "cv", "pdf"} else "untagged"


def infer_sector(company: str, role: str, blob: str = "") -> str:
    t = f"{company} {role} {blob}".lower()
    if any(k in t for k in ("ai", "anthropic", "openai", "youtube", "google", "meta", "tiktok", "scale", "cloudflare", "databricks", "linear", "vercel", "tech", "software")):
        return "Tech & AI"
    if any(k in t for k in ("fintech", "stripe", "coinbase", "block", "bank", "opensea", "web3", "doubleverify")):
        return "Fintech & Risk"
    if any(k in t for k in ("school", "teachers", "coursera", "duolingo", "education", "edtech", "chateau", "elc", "omsi", "ybm")):
        return "Education & EdTech"
    if any(k in t for k in ("nytimes", "media", "editorial", "content", "writing", "publishing")):
        return "Media & Editorial"
    if any(k in t for k in ("policy", "civic", "assembly", "government", "health", "nonprofit", "safety")):
        return "Civic & Policy"
    return "Tech & Operations"


def map_to_status(stage_depth: int, outcome: str, days_since_touch: int) -> str:
    """Map pipeline depth + outcome + inactivity into strict 7-state ApplicationRecord enum."""
    if outcome in ("built", "i_passed"):
        return "offer"
    if outcome == "they_passed":
        return "rejected"
    if outcome == "silence" or days_since_touch > GHOST_THRESHOLD_DAYS:
        return "ghosted"
    if stage_depth >= 3:
        return "onsite"
    if stage_depth == 2:
        return "technical_round"
    if stage_depth == 1:
        return "recruiter_screen"
    return "applied"


def assemble_record(
    company: str,
    msgs: list[dict[str, Any]],
    outcome_override: str | None = None,
    role_override: str = "",
    sector: str = "",
    resume_variant: str = "",
    ref_dt: datetime | None = None,
    idx: int = 0,
) -> dict[str, Any]:
    """Assemble one deduplicated company ApplicationRecord from classified thread messages."""
    sorted_msgs = sorted(msgs, key=lambda m: m.get("dt") or datetime.min.replace(tzinfo=timezone.utc))
    dts = [m["dt"] for m in sorted_msgs if m.get("dt")]
    applied_dt = dts[0] if dts else None
    last_dt = dts[-1] if dts else applied_dt
    now_ref = ref_dt or datetime.now(timezone.utc)

    first_anchor_dt = None
    first_anchor_idx = None
    first_resp_dt = None
    first_resp_idx = None
    first_rej_dt = None
    first_rej_idx = None
    rej_msg = None
    depth = 0
    stages_seen = set()
    phrases = []
    attachments = []
    outbound_text = []

    for msg_i, m in enumerate(sorted_msgs):
        kind = m.get("kind", "other")
        if kind in ("applied", "receipt") and first_anchor_dt is None and m.get("dt"):
            first_anchor_dt = m["dt"]
            first_anchor_idx = msg_i
        if kind in ("screen", "technical", "onsite", "offer", "offer_accepted", "rejection") and not m.get("outbound"):
            if first_resp_dt is None and m.get("dt"):
                first_resp_dt = m["dt"]
                first_resp_idx = msg_i
        if kind == "screen":
            depth = max(depth, 1)
            stages_seen.add("screen")
        elif kind == "technical":
            depth = max(depth, 2)
            stages_seen.add("technical")
        elif kind == "onsite":
            depth = max(depth, 3)
            stages_seen.add("onsite")
        elif kind in ("offer", "offer_accepted"):
            depth = max(depth, 4)
            stages_seen.add("offer")
        elif kind == "withdrew":
            stages_seen.add("withdrew")
        elif kind == "rejection":
            if first_rej_dt is None and m.get("dt"):
                first_rej_dt = m["dt"]
                first_rej_idx = msg_i
                rej_msg = m
        txt_l = f"{m.get('subject', '')} {m.get('text', '')}".lower()
        for pat, label in REJECTION_PHRASES:
            if pat in txt_l and label not in phrases:
                phrases.append(label)
        if m.get("attachments"):
            attachments.extend(m["attachments"])
        if m.get("outbound"):
            outbound_text.append(m.get("text", ""))

    # Determine outcome disposition (keeping Accepted vs I Passed cleanly distinct)
    outcome = outcome_override
    if not outcome:
        if any(m.get("kind") == "offer_accepted" for m in sorted_msgs):
            outcome = "built"
        elif "withdrew" in stages_seen:
            outcome = "i_passed"
        elif "offer" in stages_seen:
            outcome = "i_passed"
        elif first_rej_dt is not None:
            outcome = "they_passed"
        elif depth >= 1:
            outcome = "interviewed"
        else:
            outcome = "silence"

    label_map = {
        "built": "Accepted Offer",
        "i_passed": "Offer Declined",
        "interviewed": "Screened / Interviewed",
        "they_passed": "Not Selected",
        "silence": "No Response (30d+)",
    }

    role = role_override or extract_role(
        " ".join(m.get("subject", "") for m in sorted_msgs[:3]),
        " ".join(m.get("text", "")[:300] for m in sorted_msgs[:3]),
    )
    resolved_sector = sector or infer_sector(company, role, " ".join(m.get("subject", "") for m in sorted_msgs[:2]))
    role_level, department, track = infer_role_meta(role, resolved_sector)

    channel = next((channel_for(m.get("from", ""), m.get("subject", "")) for m in sorted_msgs if not m.get("outbound")), "Direct Email / Company Domain")
    # Measure latency only when an earlier submission/receipt anchor exists strictly before the decision message
    has_resp_anchor = first_anchor_dt is not None and first_resp_dt is not None and first_resp_idx is not None and first_anchor_idx is not None and first_resp_idx > first_anchor_idx
    has_rej_anchor = first_anchor_dt is not None and first_rej_dt is not None and first_rej_idx is not None and first_anchor_idx is not None and first_rej_idx > first_anchor_idx
    days_resp = max(0, (first_resp_dt - first_anchor_dt).days) if has_resp_anchor else None
    days_rej = max(0, (first_rej_dt - first_anchor_dt).days) if has_rej_anchor else None
    days_since_touch = max(0, (now_ref - last_dt).days) if last_dt else 999

    status = map_to_status(depth, outcome, days_since_touch)
    rejected_after_interview = outcome == "they_passed" and depth >= 1
    ghosted_after_interview = status == "ghosted" and depth >= 1
    rej_has_clock = bool(rej_msg and rej_msg.get("has_clock", True) and first_rej_dt)
    rej_hour = first_rej_dt.hour if rej_has_clock else None
    rej_wd = first_rej_dt.weekday() if first_rej_dt else None
    rej_personal = bool(rej_msg and any(h in rej_msg.get("text", "").lower() for h in PERSONAL_REJECT_HINTS))

    variant = detect_resume_variant(resume_variant or " ".join(outbound_text), attachments, role)
    applied_iso = applied_dt.strftime("%Y-%m-%d") if applied_dt else ""
    last_iso = last_dt.strftime("%Y-%m-%d") if last_dt else applied_iso

    return {
        "id": slug_id(company, applied_iso, idx),
        "company": company,
        "company_hash": company_hash_hex(company),
        "accepted": outcome == "built",
        "role": role,
        "roleLevel": role_level,
        "department": department,
        "track": track,
        "sector": resolved_sector,
        "channel": channel,
        "date": applied_iso,
        "appliedDate": applied_iso,
        "lastTouchDate": last_iso,
        "ts": applied_iso[:7] if applied_iso else "",
        "status": status,
        "outcome": outcome,
        "outcome_label": label_map.get(outcome, "Silence"),
        "resumeVariant": variant,
        "stage_depth": depth,
        "daysToResponse": days_resp if days_resp is not None else -1,
        "days_to_response": days_resp,
        "days_to_rejection": days_rej,
        "days_elapsed": days_resp if days_resp is not None else days_rej,
        "rejection_hour": rej_hour,
        "rejection_weekday": rej_wd,
        "rejected_after_interview": rejected_after_interview,
        "rejection_personal": rej_personal,
        "rejection_phrases": phrases,
        "ghosted_after_interview": ghosted_after_interview,
        "messages": len(sorted_msgs),
    }


def _safe_pct(num: int, den: int) -> float:
    return round((num / den) * 100, 1) if den > 0 else 0.0


def analyze(records: list[dict[str, Any]], hash_names: bool = False) -> dict[str, Any]:
    """Compute complete career telemetry, survival curve, Sankey, Resume A/B analytics, and coach findings."""
    n = len(records)
    by_outcome = Counter(r["outcome"] for r in records)
    by_status = Counter(r["status"] for r in records)

    reply_days = [r["days_to_response"] for r in records if r.get("days_to_response") is not None and r["days_to_response"] >= 0]
    rej_days = [r["days_to_rejection"] for r in records if r.get("days_to_rejection") is not None and r["days_to_rejection"] >= 0]
    overnight = sum(1 for r in records if r.get("rejection_hour") is not None and (r["rejection_hour"] < 7 or r["rejection_hour"] >= 20))
    rej_hours = [r["rejection_hour"] for r in records if r.get("rejection_hour") is not None]

    # Cumulative stage survival funnel
    screen_plus = sum(1 for r in records if r["stage_depth"] >= 1 or r["outcome"] in ("built", "i_passed", "interviewed"))
    tech_plus = sum(1 for r in records if r["stage_depth"] >= 2 or r["outcome"] in ("built", "i_passed"))
    onsite_plus = sum(1 for r in records if r["stage_depth"] >= 3 or r["outcome"] in ("built", "i_passed"))
    offers = sum(1 for r in records if r["outcome"] in ("built", "i_passed") or r["status"] == "offer")

    funnel = [
        {"stage": "Applied", "count": n, "pct": 100.0},
        {"stage": "Screened", "count": screen_plus, "pct": _safe_pct(screen_plus, n)},
        {"stage": "Technical / Case", "count": tech_plus, "pct": _safe_pct(tech_plus, n)},
        {"stage": "Final Round", "count": onsite_plus, "pct": _safe_pct(onsite_plus, n)},
        {"stage": "Offer", "count": offers, "pct": _safe_pct(offers, n)},
    ]

    # Response time curve across days 0..60
    survival_curve = []
    for day in (0, 1, 3, 5, 7, 10, 14, 21, 30, 45, 60):
        responded = sum(1 for d in reply_days if d <= day)
        remaining = n - responded
        survival_curve.append({
            "day": day,
            "responded_cum": responded,
            "remaining_silent": remaining,
            "waiting_pct": _safe_pct(remaining, n),
            "ghost_threshold": day == GHOST_THRESHOLD_DAYS,
        })

    # Response buckets
    buckets = Counter()
    for r in records:
        d = r.get("days_to_response")
        if r["status"] == "ghosted" and d is None:
            buckets["No Response (30d+)"] += 1
        elif d is None:
            buckets["Open / Pending"] += 1
        elif d == 0:
            buckets["Same Day (0d)"] += 1
        elif d <= 3:
            buckets["Fast (1-3d)"] += 1
        elif d <= 7:
            buckets["Week 1 (4-7d)"] += 1
        elif d <= 14:
            buckets["Weeks 2 (8-14d)"] += 1
        elif d <= 30:
            buckets["Slow (15-30d)"] += 1
        else:
            buckets["Delayed (31+d)"] += 1
    latency_order = ["Same Day (0d)", "Fast (1-3d)", "Week 1 (4-7d)", "Weeks 2 (8-14d)", "Slow (15-30d)", "Delayed (31+d)", "No Response (30d+)", "Open / Pending"]
    latency_buckets = [{"bucket": k, "count": buckets[k]} for k in latency_order if buckets[k] > 0]

    # Rejection phrase frequency & circadian hour map
    phrase_counts = Counter(p for r in records for p in r.get("rejection_phrases", []))
    circadian = Counter(rej_hours)
    circadian_24h = [{"hour": h, "count": circadian[h]} for h in range(24)]

    # Channel & track performance
    chan_stats = defaultdict(lambda: {"total": 0, "interviews": 0, "ghosted": 0, "offers": 0})
    track_stats = defaultdict(lambda: {"total": 0, "interviews": 0, "offers": 0, "accepted": 0, "days": []})
    for r in records:
        c = chan_stats[r["channel"]]
        c["total"] += 1
        if r["stage_depth"] >= 1 or r["outcome"] in ("built", "i_passed", "interviewed"):
            c["interviews"] += 1
        if r["status"] == "ghosted":
            c["ghosted"] += 1
        if r["outcome"] in ("built", "i_passed"):
            c["offers"] += 1

        t = track_stats[r["track"]]
        t["total"] += 1
        if r["stage_depth"] >= 1 or r["outcome"] in ("built", "i_passed", "interviewed"):
            t["interviews"] += 1
        if r["outcome"] in ("built", "i_passed"):
            t["offers"] += 1
        if r["outcome"] == "built":
            t["accepted"] += 1
        if r.get("days_to_response") is not None and r["days_to_response"] >= 0:
            t["days"].append(r["days_to_response"])

    channels = sorted([
        {
            "channel": k,
            "total": v["total"],
            "interview_rate": _safe_pct(v["interviews"], v["total"]),
            "ghost_rate": _safe_pct(v["ghosted"], v["total"]),
            "offer_rate": _safe_pct(v["offers"], v["total"]),
        }
        for k, v in chan_stats.items()
    ], key=lambda x: -x["total"])

    tracks = sorted([
        {
            "track": k,
            "total": v["total"],
            "interviews": v["interviews"],
            "offers": v["offers"],
            "accepted": v["accepted"],
            "interview_rate": _safe_pct(v["interviews"], v["total"]),
            "median_days": round(median(v["days"]), 1) if v["days"] else None,
        }
        for k, v in track_stats.items()
    ], key=lambda x: -x["total"])

    # Resume A/B Comparative Analytics
    ab_groups = defaultdict(list)
    for r in records:
        ab_groups[r.get("resumeVariant", "untagged")].append(r)

    resume_ab = []
    for var, items in sorted(ab_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        cnt = len(items)
        scr = sum(1 for r in items if r["stage_depth"] >= 1 or r["outcome"] in ("built", "i_passed", "interviewed"))
        tech = sum(1 for r in items if r["stage_depth"] >= 2 or r["outcome"] in ("built", "i_passed"))
        off = sum(1 for r in items if r["outcome"] in ("built", "i_passed") or r["status"] == "offer")
        gho = sum(1 for r in items if r["status"] == "ghosted")
        d_list = [r["days_to_response"] for r in items if r.get("days_to_response") is not None and r["days_to_response"] >= 0]
        fast = sum(1 for d in d_list if d <= 7)
        resume_ab.append({
            "variant": var,
            "applications": cnt,
            "share_pct": _safe_pct(cnt, n),
            "screen_rate": _safe_pct(scr, cnt),
            "technical_rate": _safe_pct(tech, cnt),
            "interview_rate": _safe_pct(scr, cnt),
            "offer_rate": _safe_pct(off, cnt),
            "ghost_rate": _safe_pct(gho, cnt),
            "median_days": round(median(d_list), 1) if d_list else None,
            "fast_reply_pct": _safe_pct(fast, cnt),
        })

    # Sankey 3-stage flows (Channel -> Department -> Disposition)
    sankey_links = Counter()
    for r in records:
        ch = r["channel"]
        dep = r["department"]
        out = r["outcome_label"]
        sankey_links[("channel:" + ch, "dept:" + dep)] += 1
        sankey_links[("dept:" + dep, "outcome:" + out)] += 1
    sankey = [{"source": s, "target": t, "value": v} for (s, t), v in sankey_links.most_common(48)]

    # Monthly timeline
    monthly = defaultdict(lambda: {"applied": 0, "interviews": 0, "offers": 0, "rejected": 0})
    for r in records:
        ym = r["ts"] or "Unknown"
        monthly[ym]["applied"] += 1
        if r["stage_depth"] >= 1:
            monthly[ym]["interviews"] += 1
        if r["outcome"] in ("built", "i_passed"):
            monthly[ym]["offers"] += 1
        elif r["outcome"] == "they_passed":
            monthly[ym]["rejected"] += 1
    timeline = [{"month": k, **v} for k, v in sorted(monthly.items()) if k != "Unknown"]

    # Sectors summary
    sec_stats = defaultdict(lambda: {"total": 0, "accepted": 0, "i_passed": 0, "interviewed": 0, "they_passed": 0, "silence": 0})
    for r in records:
        st = sec_stats[r["sector"]]
        st["total"] += 1
        st[r["outcome"]] = st.get(r["outcome"], 0) + 1
    sectors = sorted([{"sector": k, **v} for k, v in sec_stats.items()], key=lambda x: -x["total"])

    summary = {
        "total_companies": n,
        "accepted": by_outcome["built"],
        "i_passed": by_outcome["i_passed"],
        "interviewed": by_outcome["interviewed"],
        "they_passed": by_outcome["they_passed"],
        "silence": by_outcome["silence"],
        "ghosted": by_status["ghosted"],
        "offers_total": offers,
        "human_reply_rate": _safe_pct(n - by_status["ghosted"], n),
        "interview_conversion": _safe_pct(screen_plus, n),
        "offer_conversion": _safe_pct(offers, n),
        "ghosted_rate": _safe_pct(by_status["ghosted"], n),
        "median_reply_days": round(median(reply_days), 1) if reply_days else None,
        "median_rejection_days": round(median(rej_days), 1) if rej_days else None,
        "overnight_auto_pct": _safe_pct(overnight, len(rej_hours)) if rej_hours else None,
        "after_interview_rejections": sum(1 for r in records if r["rejected_after_interview"]),
        "ghosted_after_interview": sum(1 for r in records if r["ghosted_after_interview"]),
    }

    findings = coach_notes(summary, channels, tracks, resume_ab, records)

    # Optionally mask non-accepted companies for public publication
    export_records = []
    for r in records:
        row = dict(r)
        if hash_names and not row["accepted"]:
            row["company"] = row["company_hash"]
            row["company_masked"] = True
        else:
            row["company_masked"] = False
        export_records.append(row)

    export_records.sort(key=lambda r: ({"built": 0, "i_passed": 1, "interviewed": 2, "they_passed": 3, "silence": 4}.get(r["outcome"], 5), -(r["stage_depth"]), r["appliedDate"]))

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ghost_threshold_days": GHOST_THRESHOLD_DAYS,
        "summary": summary,
        "funnel": funnel,
        "survival_curve": survival_curve,
        "latency_buckets": latency_buckets,
        "rejection_phrases": [{"phrase": k, "count": v} for k, v in phrase_counts.most_common(10)],
        "circadian_24h": circadian_24h,
        "channels": channels,
        "tracks": tracks,
        "sectors": sectors,
        "resume_ab": resume_ab,
        "sankey": sankey,
        "timeline": timeline,
        "findings": findings,
        "companies": export_records,
        "records": export_records,
    }


def coach_notes(
    summary: dict[str, Any],
    channels: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
    resume_ab: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Generate actionable headhunter / career coach diagnostics answering 'What Was Missing?'."""
    notes = []
    valid_tracks = [t for t in tracks if t["total"] >= 5] or tracks
    if valid_tracks:
        best = max(valid_tracks, key=lambda t: (t["interview_rate"], t["total"]))
        worst = min(valid_tracks, key=lambda t: (t["interview_rate"], -t["total"]))
        notes.append({
            "title": "Role Focus",
            "metric": f"{best['interview_rate']}% vs {worst['interview_rate']}%",
            "finding": f"{best['track']} roles led to interviews {best['interview_rate']}% of the time ({best['total']} apps), vs. {worst['interview_rate']}% for {worst['track']} ({worst['total']} apps).",
            "missing": "Lead with matching domain experience at the top of page one before branching into secondary lanes.",
        })
    valid_ch = [c for c in channels if c["total"] >= 5] or channels
    if valid_ch:
        top_ch = max(valid_ch, key=lambda c: c["interview_rate"])
        high_gho = max(valid_ch, key=lambda c: c["ghost_rate"])
        notes.append({
            "title": "How You Applied",
            "metric": f"{top_ch['interview_rate']}% vs {high_gho['ghost_rate']}% no reply",
            "finding": f"{top_ch['channel']} converted to screens at {top_ch['interview_rate']}% ({top_ch['total']} apps), whereas {high_gho['channel']} had no response after {GHOST_THRESHOLD_DAYS} days on {high_gho['ghost_rate']}% of {high_gho['total']} submissions.",
            "missing": "Direct referrals or short notes to the hiring manager alongside cold job-board submissions.",
        })
    notes.append({
        "title": "Follow-Up Drop-Off (30d+)",
        "metric": f"{summary['ghosted_rate']}% no reply",
        "finding": f"{summary['ghosted']} of {summary['total_companies']} applications received no closing reply past 30 days ({summary['ghosted_after_interview']} occurred after an interview).",
        "missing": "A brief 7-day post-interview check-in and confirming decision timelines on the first call.",
    })
    tagged_ab = [v for v in resume_ab if v["variant"] != "untagged"]
    if len(tagged_ab) >= 2:
        leader = max(tagged_ab, key=lambda v: v["interview_rate"])
        trailer = min(tagged_ab, key=lambda v: v["interview_rate"])
        is_era = leader["variant"].startswith("era_")
        notes.append({
            "title": "Resume Versions",
            "metric": f"{leader['variant']} (+{round(leader['interview_rate'] - trailer['interview_rate'], 1)} pts)",
            "finding": f"{leader['variant']} reached recruiter screens {leader['interview_rate']}% of the time ({leader['offer_rate']}% offer rate) vs. {trailer['interview_rate']}% for {trailer['variant']}.",
            "missing": (
                "Tighten technical and safety metrics clearly when framing experience for senior AI Product roles."
                if is_era
                else "Make the stronger resume version your default and retire underperforming headlines."
            ),
        })
    elif tagged_ab:
        notes.append({
            "title": "Resume Tracking",
            "metric": f"{tagged_ab[0]['share_pct']}% tagged",
            "finding": "Only one tagged resume version appears here; the rest are untagged.",
            "missing": "Label applications with v1_executive_summary, v2_technical_deepdive, or v3_minimalist to compare screen rates.",
        })
    auto_txt = f"{summary['overnight_auto_pct']}% of automated declines arrived outside business hours." if summary.get("overnight_auto_pct") is not None else "Use an email .mbox export to measure overnight vs. human business-hour reply timing."
    notes.append({
        "title": "Response Speed & Offers",
        "metric": f"{summary['offers_total']} offers / {summary['interview_conversion']}% screened",
        "finding": f"Median first response arrived in {summary['median_reply_days'] or '—'} days; {auto_txt}",
        "missing": "Concrete scale metrics (weekly volume, team scope, turnaround improvements) in the top bullet block.",
    })
    return notes


def llm_brief(data: dict[str, Any]) -> str:
    s = data["summary"]
    med_days = f"{s['median_reply_days']} days" if s.get("median_reply_days") is not None else "—"
    auto_pct = f"{s['overnight_auto_pct']}%" if s.get("overnight_auto_pct") is not None else "N/A (CSV input)"
    lines = [
        "# Career Pipeline & Resume Summary (Privacy-Scrubbed)",
        "",
        f"- **Applications**: {s['total_companies']}",
        f"- **Accepted Offer**: {s['accepted']} · **Offer Declined**: {s['i_passed']} · **Total Offers**: {s['offers_total']}",
        f"- **Screen Rate**: {s['interview_conversion']}% · **No Response (30d+)**: {s['ghosted_rate']}%",
        f"- **Median Reply Time**: {med_days} · **Off-Hours Automated Replies**: {auto_pct}",
        "",
        "## Pipeline Funnel",
    ]
    for f in data["funnel"]:
        lines.append(f"- {f['stage']}: {f['count']} ({f['pct']}%)")
    lines.extend(["", "## Resume Versions Compared"])
    for v in data["resume_ab"]:
        v_med = f"{v['median_days']}d" if v.get("median_days") is not None else "—"
        lines.append(f"- `{v['variant']}`: {v['applications']} apps · Screen {v['screen_rate']}% · Offer {v['offer_rate']}% · No Reply {v['ghost_rate']}% · Median {v_med}")
    lines.extend(["", "## What Made the Difference"])
    for n in data["findings"]:
        lines.append(f"- **{n['title']}** ({n['metric']}): {n['finding']} *What helped:* {n['missing']}")
    return "\n".join(lines) + "\n"


def _strip_html(raw: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    return " ".join(cleaned.split())


def _extract_mime_parts(msg: email.message.Message) -> tuple[str, list[str]]:
    texts = []
    html_fallback = []
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            fname = part.get_filename()
            if fname:
                attachments.append(decode_header_val(fname))
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    texts.append(_safe_decode(payload, part.get_content_charset()))
            elif ctype == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html_fallback.append(_strip_html(_safe_decode(payload, part.get_content_charset())))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            raw = _safe_decode(payload, msg.get_content_charset())
            texts.append(_strip_html(raw) if msg.get_content_type() == "text/html" else raw)
    combined = "\n".join(texts) if texts else "\n".join(html_fallback)
    return combined[:3000], attachments


def records_from_mbox(path: str, my_email: str = "") -> list[dict[str, Any]]:
    """Parse a Google Takeout .mbox archive cleanly on-device."""
    threads: dict[str, dict[str, Any]] = {}
    me_l = my_email.strip().lower()
    mbox = mailbox.mbox(path)
    max_dt = None

    for msg in mbox:
        frm = decode_header_val(msg.get("From", ""))
        to = decode_header_val(msg.get("To", ""))
        subj = decode_header_val(msg.get("Subject", ""))
        dt = parse_dt(msg.get("Date", ""))
        if dt and (max_dt is None or dt > max_dt):
            max_dt = dt
        body, attachments = _extract_mime_parts(msg)
        outbound = bool(me_l and me_l in frm.lower())
        kind = classify_message(subj, body, outbound=outbound)
        if kind == "noise" or kind == "other":
            continue
        other_hdr = to if outbound else frm
        comp = company_from(other_hdr, subj, body)
        if comp == "Unknown Organization":
            continue
        base_key = re.sub(r"\s+(ai|labs|inc|corp|co|hq|io)$", "", comp.lower()).strip() or comp.lower()
        threads.setdefault(base_key, {"names": Counter(), "msgs": []})
        threads[base_key]["names"][comp] += 1
        threads[base_key]["msgs"].append({
            "dt": dt,
            "subject": subj,
            "text": scrub_pii(body[:1200]),
            "from": frm,
            "outbound": outbound,
            "kind": kind,
            "attachments": attachments,
        })

    ref_dt = max_dt or datetime.now(timezone.utc)
    return [
        assemble_record(
            max(bucket["names"].keys(), key=lambda k: (len(k), bucket["names"][k])),
            bucket["msgs"],
            ref_dt=ref_dt,
            idx=i,
        )
        for i, (_, bucket) in enumerate(sorted(threads.items()))
    ]


def records_from_csv(path: str) -> list[dict[str, Any]]:
    """Load applications from simple or Telemetry CSV files."""
    out = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    dates = [parse_dt(r.get("appliedDate") or r.get("date") or "") for r in rows]
    valid_dts = [d for d in dates if d is not None]
    ref_dt = max(valid_dts) if valid_dts else datetime.now(timezone.utc)

    status_to_outcome = {
        "offer": "i_passed",
        "rejected": "they_passed",
        "ghosted": "silence",
        "applied": "silence",
        "recruiter_screen": "interviewed",
        "technical_round": "interviewed",
        "onsite": "interviewed",
    }
    outcome_label_norm = {
        "accepted": "built", "accepted offer": "built", "built": "built", "built here": "built",
        "i passed": "i_passed", "i_passed": "i_passed", "declined": "i_passed", "offer declined": "i_passed", "declined offer": "i_passed", "withdrew": "i_passed",
        "interviewed": "interviewed", "screened": "interviewed", "screened / interviewed": "interviewed",
        "they passed": "they_passed", "they_passed": "they_passed", "rejected": "they_passed", "not selected": "they_passed",
        "silence": "silence", "ghosted": "silence", "pending": "silence", "no response": "silence", "no response (30d+)": "silence",
    }

    for i, r in enumerate(rows):
        company = (r.get("company") or "").strip()
        if not company:
            continue
        role = (r.get("role") or r.get("title") or "Product / Operations").strip()
        sector = (r.get("sector") or r.get("department") or "").strip()
        applied_str = (r.get("appliedDate") or r.get("date") or "").strip()
        last_str = (r.get("lastTouchDate") or r.get("last_touch") or applied_str).strip()
        dt_app = parse_dt(applied_str)
        dt_last = parse_dt(last_str) or dt_app
        notes = scrub_pii(r.get("notes") or "")

        raw_status = (r.get("status") or "").strip().lower()
        raw_out = (r.get("outcome") or r.get("outcome_label") or "").strip().lower()
        outcome = outcome_label_norm.get(raw_out) or status_to_outcome.get(raw_status) or "silence"

        depth = 0
        if raw_status == "recruiter_screen" or outcome == "interviewed":
            depth = 1
        elif raw_status == "technical_round":
            depth = 2
        elif raw_status == "onsite" or "final" in notes.lower() or "onsite" in notes.lower():
            depth = 3
        elif raw_status == "offer" or outcome in ("built", "i_passed"):
            depth = 4

        days_raw = (r.get("daysToResponse") or r.get("days_to_response") or r.get("days_elapsed") or "").strip()
        msgs = [{
            "dt": dt_app,
            "subject": f"Application for {role}",
            "text": notes,
            "outbound": True,
            "kind": "applied",
        }]
        if outcome in ("built", "i_passed", "interviewed", "they_passed") or depth > 0:
            resp_dt = dt_last
            if days_raw and str(days_raw).lstrip("-").isdigit() and int(days_raw) >= 0 and dt_app:
                from datetime import timedelta
                resp_dt = dt_app + timedelta(days=int(days_raw))
            kind_map = {
                "built": "offer_accepted",
                "i_passed": "offer",
                "interviewed": {1: "screen", 2: "technical", 3: "onsite"}.get(depth, "screen"),
                "they_passed": "rejection",
            }
            msgs.append({
                "dt": resp_dt,
                "subject": role,
                "text": notes,
                "outbound": False,
                "kind": kind_map[outcome],
                "has_clock": ("T" in last_str or ":" in last_str),
            })
        elif dt_last and dt_app and dt_last > dt_app:
            msgs.append({"dt": dt_last, "subject": role, "text": notes, "outbound": False, "kind": "receipt"})

        rec = assemble_record(
            company=company,
            msgs=msgs,
            outcome_override=outcome,
            role_override=role,
            sector=sector,
            resume_variant=(r.get("resumeVariant") or r.get("resume_variant") or r.get("resume_id") or "").strip(),
            ref_dt=ref_dt,
            idx=i,
        )
        if r.get("roleLevel"):
            rec["roleLevel"] = r["roleLevel"].strip()
        if r.get("department"):
            rec["department"] = r["department"].strip()
        out.append(rec)

    return out


def records_from_json(path: str) -> list[dict[str, Any]]:
    """Load structured ApplicationRecord JSON array or {applications/records: [...]} logs."""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload if isinstance(payload, list) else (payload.get("applications") or payload.get("records") or [])
    tmp_rows = []
    for item in items:
        tmp_rows.append(item)
    # Reuse CSV normalization seamlessly via in-memory dict normalization
    if not tmp_rows:
        return []
    import tempfile
    keys = sorted({k for r in tmp_rows for k in r.keys()})
    with tempfile.NamedTemporaryFile("w", suffix=".csv", encoding="utf-8", newline="", delete=False) as tf:
        w = csv.DictWriter(tf, fieldnames=keys)
        w.writeheader()
        for r in tmp_rows:
            w.writerow({k: r.get(k, "") for k in keys})
        tmp_path = tf.name
    try:
        return records_from_csv(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def write_outputs(records: list[dict[str, Any]], out_dir: str, hash_names: bool = False) -> dict[str, Any]:
    """Write data.json, audit_table.csv, and llm-brief.md cleanly into out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    data = analyze(records, hash_names=hash_names)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    stage_display = {0: "Applied", 1: "Screened", 2: "Technical / Case", 3: "Final Round", 4: "Offer"}
    with open(os.path.join(out_dir, "audit_table.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "id", "company", "role", "roleLevel", "department", "sector",
            "appliedDate", "lastTouchDate", "status", "stage", "outcome",
            "resumeVariant", "daysToResponse",
        ])
        for r in data["companies"]:
            w.writerow([
                r["id"], r["company"], r["role"], r["roleLevel"], r["department"], r["sector"],
                r["appliedDate"], r["lastTouchDate"], r["status"],
                stage_display.get(r["stage_depth"], "Applied"), r["outcome_label"],
                r["resumeVariant"], "" if r["daysToResponse"] < 0 else r["daysToResponse"],
            ])

    with open(os.path.join(out_dir, "llm-brief.md"), "w", encoding="utf-8") as f:
        f.write(llm_brief(data))
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="Local Career Telemetry & Resume A/B Engine")
    ap.add_argument("--mbox", help="Path to Google Takeout .mbox archive")
    ap.add_argument("--csv", help="Path to CSV job tracker")
    ap.add_argument("--json", help="Path to JSON ApplicationRecord / API log")
    ap.add_argument("--email", default="", help="Your email address for inbound/outbound detection")
    ap.add_argument("--out", default=".", help="Output directory")
    ap.add_argument("--hash-names", action="store_true", help="SHA-256 mask non-accepted company names in output")
    args = ap.parse_args()

    if args.mbox:
        recs = records_from_mbox(args.mbox, my_email=args.email)
    elif args.csv:
        recs = records_from_csv(args.csv)
    elif args.json:
        recs = records_from_json(args.json)
    else:
        ap.error("Provide --mbox, --csv, or --json")
        return

    data = write_outputs(recs, args.out, hash_names=args.hash_names)
    s = data["summary"]
    print(
        f"Parsed {s['total_companies']} applications "
        f"(Offers: {s['offers_total']} · No Reply 30d+: {s['ghosted']} · Variants: {len(data['resume_ab'])}) "
        f"-> {os.path.join(args.out, 'data.json')}"
    )


if __name__ == "__main__":
    main()
