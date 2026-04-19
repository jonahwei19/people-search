"""Fetch TLS applicants → CSV ready to upload to People Search.

Pulls name, email, LinkedIn, pitch content, and category so the enrichment
pipeline can identify each person and the LLM judge has rich text to score.
"""
from __future__ import annotations

import csv
import os
import sys
import time

import requests

API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
if not API_KEY:
    raise SystemExit(
        "Set AIRTABLE_API_KEY in the environment before running this script."
    )
BASE_ID = "app6scuWYbynmQfMP"
TABLE = "tblkPNL8BooqXgRsY"

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
URL = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE}"


def fetch_all() -> list[dict]:
    """Fetch every record, every field."""
    records = []
    offset = None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(URL, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(0.2)
    return records


def flat(f: dict, key: str) -> str:
    """Read a field, coerce list → '; '-separated string, strip control chars."""
    v = f.get(key, "")
    if isinstance(v, list):
        s = "; ".join(str(x) for x in v if x)
    else:
        s = str(v) if v is not None else ""
    # Strip null bytes and other control chars that Postgres TEXT/JSONB reject.
    # Keep tab/newline/CR — only remove truly invalid chars.
    return "".join(c for c in s if c == "\t" or c == "\n" or c == "\r" or (c >= " " and c != "\x7f"))


def normalize_twitter(s: str) -> str:
    """@handle → https://twitter.com/handle. www.x.com/u → https. Leave else alone."""
    s = s.strip()
    if not s or s.upper() in ("NA", "N/A", "NONE"):
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("@") and " " not in s:
        return "https://twitter.com/" + s[1:]
    if s.startswith("www.") and ("twitter.com" in s or "x.com" in s):
        return "https://" + s
    return s  # leave mixed/ambiguous values alone — parser will validate


def normalize_linkedin(s: str) -> str:
    """www.linkedin.com/... → https://www.linkedin.com/... Leave else alone."""
    s = s.strip()
    if not s or s.upper() in ("NA", "N/A", "NONE"):
        return ""
    if s.lower().startswith("www.linkedin.com"):
        return "https://" + s
    if s.lower().startswith("linkedin.com"):
        return "https://www." + s
    return s


def split_merged_urls(s: str) -> list[str]:
    """Break a merged URL field into individual URLs.

    The Airtable `LinkedIn/X/CV (merged)` field often contains multiple
    URLs concatenated with no separator, a newline, or a space. Many are
    schemeless (`linkedin.com/in/foo`, `www.x.com/bar`). We detect URL-like
    tokens and prepend `https://` as needed.
      "https://www.linkedin.com/in/ashravikumar/https://x.com/onceageek/"
      "linkedin.com/in/jonathon-moore \nhttps://vettic.ai/"
      "www.linkedin.com/in/foo\nx.com/bar"
    """
    import re
    s = s.strip()
    if not s:
        return []
    # Insert a newline before every URL boundary (scheme or bare known domain).
    # This lets us split even when URLs are jammed together with no separator.
    boundary_pattern = re.compile(
        r'(?<!^)(?<![\s,;|])(https?://|'
        r'www\.|'
        r'linkedin\.com/|'
        r'twitter\.com/|'
        r'x\.com/|'
        r'github\.com/|'
        r'substack\.com/|'
        r'medium\.com/)',
        re.IGNORECASE,
    )
    normalized = boundary_pattern.sub(lambda m: '\n' + m.group(1), s)
    parts = re.split(r'[\s,;|]+', normalized)

    urls = []
    for p in parts:
        p = p.strip().rstrip('/').rstrip(',.')
        if not p:
            continue
        # Already has scheme?
        if p.lower().startswith(('http://', 'https://')):
            urls.append(p)
            continue
        # Bare www./domain.com cases: prepend https:// if it matches a known
        # host or looks like a domain.
        if p.lower().startswith('www.'):
            urls.append('https://' + p)
            continue
        if re.match(r'^(linkedin|twitter|x|github|substack|medium)\.com/', p, re.IGNORECASE):
            urls.append('https://' + p)
            continue
        # Otherwise skip — not confident it's a URL.
    return urls


def pick_from_merged(merged: str, want: str) -> str:
    """Return first URL matching `want` (\"linkedin\" or \"twitter\") from a merged field."""
    want = want.lower()
    for url in split_merged_urls(merged):
        ul = url.lower()
        if want == "linkedin" and "linkedin.com/" in ul:
            return url
        if want == "twitter" and ("twitter.com/" in ul or "://x.com/" in ul or "x.com/" in ul.split("://", 1)[-1]):
            return url
    return ""


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tls_applicants_for_people_search.csv"
    records = fetch_all()
    print(f"Fetched {len(records)} TLS applicants", file=sys.stderr)

    if not records:
        print("No records", file=sys.stderr)
        return 1

    # Inspect available field names so the user can see what's in Airtable
    all_fields: set[str] = set()
    for rec in records:
        all_fields.update((rec.get("fields") or {}).keys())
    print(f"Available Airtable fields: {sorted(all_fields)}", file=sys.stderr)

    # Useful fields for People Search. Column header → list of Airtable keys
    # to try (first non-empty wins). Keeps output schema stable even when
    # Airtable has multiple legacy field names.
    # Note: LinkedIn is pulled from the MERGED field — may contain LinkedIn
    # AND Twitter AND personal site concatenated. Split below.
    columns = [
        ("Full Name", ["Full Name", "Name", "Applicant Name"]),
        ("Email", ["Email", "Email address", "Applicant email"]),
        ("LinkedIn", ["LinkedIn/X/CV (merged)", "LinkedIn or CV", "LinkedIn URL", "LinkedIn"]),
        ("Twitter", ["Twitter/X", "Twitter", "X"]),
        ("Category", ["Category"]),
        ("Pitch Type", ["Pitch Type"]),
        ("Pitch", ["Pitch in one sentence (merged)", "Pitch in one sentence", "Pitch"]),
        ("Problem", ["Problem or Opportunity (merged)", "Problem or Opportunity", "Problem"]),
        ("Motivation", ["Motivation_science", "Motivation"]),
        ("Solution", ["Solution"]),
        ("Uncertainties", ["Uncertainties"]),
        ("Other details", ["Other details you'd like to add?", "Other details"]),
        ("Scraped LinkedIn Profile", ["Scraped LinkedIn Profile"]),
    ]

    # Extra output column: Personal Site (split out from the merged field
    # when we find a non-LinkedIn, non-Twitter URL).
    header_names = [h for h, _ in columns] + ["Personal Site"]

    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header_names)
        for rec in records:
            rf = rec.get("fields") or {}
            merged_linkedin_raw = ""
            row_by_header: dict[str, str] = {}
            for header, keys in columns:
                val = ""
                for k in keys:
                    val = flat(rf, k)
                    if val:
                        break
                if header == "LinkedIn":
                    merged_linkedin_raw = val
                row_by_header[header] = val

            # Split the merged LinkedIn field into individual URLs and route.
            merged_urls = split_merged_urls(merged_linkedin_raw) if merged_linkedin_raw else []
            linkedin_url = ""
            twitter_from_merged = ""
            personal_site = ""
            for url in merged_urls:
                ul = url.lower()
                if "linkedin.com/" in ul and not linkedin_url:
                    linkedin_url = url
                elif ("twitter.com/" in ul or "x.com/" in ul) and not twitter_from_merged:
                    twitter_from_merged = url
                elif not personal_site and url.lower().startswith(("http://", "https://")):
                    # First non-LinkedIn, non-Twitter URL in the merged field is
                    # likely their personal site / portfolio.
                    personal_site = url
            if not linkedin_url and merged_linkedin_raw:
                # No LinkedIn URL was in the merged field — fall back to the
                # normalized raw value (handles bare www.linkedin.com/... cases).
                linkedin_url = normalize_linkedin(merged_linkedin_raw)
                # Safety: if normalization still didn't produce a LinkedIn URL,
                # drop to empty rather than putting garbage in the LinkedIn column.
                if "linkedin.com" not in linkedin_url.lower():
                    linkedin_url = ""
            row_by_header["LinkedIn"] = linkedin_url

            # If Twitter column is empty but the merged field had one, use it.
            if not row_by_header.get("Twitter") and twitter_from_merged:
                row_by_header["Twitter"] = twitter_from_merged
            row_by_header["Twitter"] = normalize_twitter(row_by_header.get("Twitter", ""))

            row = [row_by_header.get(h, "") for h, _ in columns] + [personal_site]
            w.writerow(row)

    print(f"Wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
