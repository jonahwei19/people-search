"""Stage 5: Open-web fallback.

Only runs when Stages 2–4 produced < 2 strong-anchor Evidence objects.
Uses Brave/Serper search paths (already wired into v1 identity.py) with
tighter two-anchor verification:

    - Search for "<first> <last>" + (org_domain | org)
    - For each result, compute anchors: name_match (title+desc),
      email_match (org_domain in url), slug_match (slug in url)
    - Retain ONLY results with ≥2 anchors.

This is a safety net — most profiles should be resolved by earlier stages.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import requests

from .cohort import CohortSignals, slug_matches_url
from .evidence import Evidence


BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_SEARCH_URL = "https://google.serper.dev/search"


@dataclass
class OpenWebResult:
    hits: list[Evidence]
    queries: int
    log: list[str]


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (s or "").lower()))


def _brave(query: str, api_key: str, count: int = 5) -> list[dict]:
    if not api_key:
        return []
    try:
        r = requests.get(
            BRAVE_SEARCH_URL,
            headers={"X-Subscription-Token": api_key},
            params={"q": query, "count": count},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return [
            {
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "description": it.get("description", ""),
            }
            for it in r.json().get("web", {}).get("results", [])
        ]
    except Exception:
        return []


def _serper(query: str, api_key: str, num: int = 5) -> list[dict]:
    if not api_key:
        return []
    try:
        r = requests.post(
            SERPER_SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return [
            {
                "title": it.get("title", ""),
                "url": it.get("link", ""),
                "description": it.get("snippet", ""),
            }
            for it in r.json().get("organic", [])
        ]
    except Exception:
        return []


# Domains we never retain as primary evidence — notorious for wrong-person
# data and common-name collisions.
BLOCKED_DOMAINS = (
    "spokeo.com", "beenverified.com", "whitepages.com", "radaris.com",
    "truepeoplesearch.com", "peekyou.com", "rocketreach.com", "clearbit.com",
    "zoominfo.com", "apollo.io", "contactout.com", "signalhire.com",
    "lusha.com", "hunter.io", "snov.io", "skrapp.io", "uplead.com",
    "leadiq.com", "seamless.ai", "nymeria.io", "mylife.com",
)


def query_open_web(
    signals: CohortSignals,
    profile_org: str = "",
    brave_api_key: Optional[str] = None,
    serper_api_key: Optional[str] = None,
    max_queries: int = 2,
) -> OpenWebResult:
    """Tightened two-anchor open-web fallback.

    Returns only results where ≥2 of {name_match, email_match, slug_match}
    fire against the raw search snippet + URL.
    """
    brave = brave_api_key if brave_api_key is not None else os.environ.get("BRAVE_API_KEY", "")
    serper = serper_api_key if serper_api_key is not None else os.environ.get("SERPER_API_KEY", "")
    log: list[str] = []

    if not (signals.first and signals.last):
        return OpenWebResult(hits=[], queries=0, log=["skip: need first+last"])

    queries: list[str] = []
    base = f'"{signals.first} {signals.last}"'
    if signals.org_domain:
        queries.append(f"{base} {signals.org_domain}")
    if profile_org and (not signals.org_domain or profile_org.lower() not in signals.org_domain):
        queries.append(f"{base} {profile_org}")
    if not queries:
        queries.append(base)

    queries = queries[:max_queries]

    seen: set[str] = set()
    hits: list[Evidence] = []
    q_count = 0
    for q in queries:
        q_count += 1
        raw = _brave(q, brave) + _serper(q, serper)
        for r in raw:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            url_lower = url.lower()
            if any(d in url_lower for d in BLOCKED_DOMAINS):
                continue
            canon = url_lower.split("#", 1)[0].split("?", 1)[0].rstrip("/")
            if canon in seen:
                continue
            seen.add(canon)

            combined = f"{r.get('title','')} {r.get('description','')}".lower()
            anchors: set[str] = set()
            # name_match: both name tokens in title+desc
            if signals.first and signals.last:
                if signals.first in combined and signals.last in combined:
                    anchors.add("name_match")
            # email_match
            if signals.org_domain and signals.org_domain in url_lower:
                anchors.add("email_match")
            # slug_match
            if slug_matches_url(signals.name_slugs, url_lower):
                anchors.add("slug_match")

            # Two-anchor rule: require ≥2.
            if len(anchors) < 2:
                continue

            # Classify kind from URL host
            kind = "website"
            if "linkedin.com" in url_lower:
                kind = "linkedin"
            elif "twitter.com" in url_lower or "://x.com/" in url_lower or url_lower.startswith("https://x.com/"):
                kind = "twitter"
            elif "github.com" in url_lower:
                kind = "github"
            elif "substack.com" in url_lower:
                kind = "substack"

            hits.append(Evidence(
                url=url,
                source="open_web",
                kind=kind,
                anchors=anchors,
                snippet=(r.get("description") or "")[:400],
                title=r.get("title", "")[:200],
                raw={"query": q},
            ))

    log.append(f"open_web: {q_count} queries, {len(hits)} two-anchor hits")
    return OpenWebResult(hits=hits, queries=q_count, log=log)
