"""Identity resolution: find LinkedIn profiles from name + context.

Full pipeline adapted from github.com/jonahwei19/email-to-linkedin.
Uses multi-query search strategy:
1. Email evidence search (search for literal email — ground truth)
2. Name + org + content keywords on LinkedIn
3. Name + location on LinkedIn
4. Broad name search
5. Slug-based search

Every step is logged per-profile so you can see exactly what happened.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .models import Profile, EnrichmentStatus


BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_SEARCH_URL = "https://google.serper.dev/search"

PERSONAL_DOMAINS = {
    "gmail.com", "hotmail.com", "yahoo.com", "outlook.com", "me.com",
    "icloud.com", "aol.com", "protonmail.com", "proton.me", "msn.com",
    "live.com", "mail.com", "ymail.com", "zoho.com", "hey.com", "pm.me",
}

RATE_LIMIT_DELAY = 0.1


@dataclass
class ResolveResult:
    linkedin_url: str = ""
    confidence: str = ""   # "high", "medium", "low", "none"
    method: str = ""
    alternatives: list[str] = None
    log: list[str] = None  # per-profile log of what happened
    error: str = ""
    # Non-LinkedIn evidence gathered during search (personal sites, GitHub,
    # Substack, academic pages, etc.). Preserved even when LinkedIn lookup fails
    # so the profile can still be scored against them.
    evidence_urls: list[dict] = None  # [{"url", "title", "description", "source"}]

    def __post_init__(self):
        if self.alternatives is None:
            self.alternatives = []
        if self.log is None:
            self.log = []
        if self.evidence_urls is None:
            self.evidence_urls = []


def _extract_context(profile: Profile) -> dict:
    """Pull all usable context from a profile, regardless of schema."""
    ctx = {}

    if profile.name:
        ctx["name"] = profile.name
        parts = profile.name.split()
        if len(parts) >= 2:
            ctx["first"] = parts[0]
            ctx["last"] = parts[-1]
        elif len(parts) == 1:
            ctx["first"] = parts[0]
            ctx["last"] = ""
    if profile.email:
        ctx["email"] = profile.email
    if profile.organization:
        ctx["org"] = profile.organization
    if profile.title:
        ctx["title"] = profile.title

    # Mine metadata for useful signals
    for key, val in profile.metadata.items():
        if not val or not str(val).strip():
            continue
        val = str(val).strip()
        key_lower = key.lower()

        if ("title" in key_lower or "role" in key_lower) and "title" not in ctx:
            ctx["title"] = val
        if ("org" in key_lower or "company" in key_lower) and "org" not in ctx:
            ctx["org"] = val
        if "country" in key_lower or "region" in key_lower:
            ctx["country"] = val
        if "city" in key_lower or "nearest" in key_lower:
            ctx["city"] = val
        if "linkedin" in key_lower and val.lower() in ("no", "false", "0"):
            ctx["has_linkedin"] = False
        if "linkedin" in key_lower and val.lower() in ("yes", "true", "1"):
            ctx["has_linkedin"] = True

    # Extract keywords from content fields (pitches, bios, notes)
    # Also extract 2-word phrases which are much more distinctive than single words
    content_keywords = set()
    for field_name, text in profile.content_fields.items():
        if text and len(text) > 20:
            text_lower = text.lower()
            from collections import Counter

            # Extract 2-word phrases (much more distinctive: "animal welfare" > "animal")
            words_list = re.findall(r'\b[a-zA-Z]{3,}\b', text_lower)
            bigrams = [f"{words_list[i]} {words_list[i+1]}" for i in range(len(words_list)-1)]
            bigram_freq = Counter(bigrams)
            # Filter out bigrams that are all stopwords
            stopwords = {"this", "that", "with", "from", "have", "been", "they", "their",
                         "will", "would", "could", "should", "about", "which", "there",
                         "these", "those", "into", "also", "more", "most", "some", "such",
                         "than", "then", "when", "what", "your", "very", "just", "like",
                         "make", "made", "know", "need", "want", "work", "well", "back",
                         "only", "come", "each", "over", "other", "after", "before", "being",
                         "between", "through", "during", "without", "within", "across",
                         "hope", "learn", "various", "based", "skills", "people", "world",
                         "improving", "plan", "event", "looking", "interested", "help",
                         "working", "using", "order", "believe", "having", "focus",
                         "currently", "completed", "online", "training", "program"}
            good_bigrams = [(bg, c) for bg, c in bigram_freq.most_common(10)
                           if not all(w in stopwords for w in bg.split())]
            content_keywords.update(bg for bg, _ in good_bigrams[:3])

            # Also extract distinctive single words as fallback
            words = re.findall(r'\b[a-zA-Z]{5,}\b', text_lower)
            freq = Counter(words)
            distinctive = [(w, c) for w, c in freq.most_common(20)
                          if w not in stopwords and c >= 1]
            content_keywords.update(w for w, _ in distinctive[:3])

    if content_keywords:
        ctx["content_keywords"] = list(content_keywords)[:6]

    # Corporate email domain — always extract, even if we already have org
    if "email" in ctx:
        domain = ctx["email"].split("@")[-1] if "@" in ctx["email"] else ""
        if domain and domain not in PERSONAL_DOMAINS:
            ctx["email_domain"] = domain.split(".")[0]
            ctx["email_company"] = _domain_to_company(domain)

    return ctx


def _domain_to_company(domain: str) -> str:
    """Convert email domain to likely company name."""
    parts = domain.split(".")
    name = parts[0]
    # Remove common TLD-like substrings
    name = re.sub(r"(mail|email|info|contact|admin|support)", "", name)
    return name.strip() if name.strip() else parts[0]


def _is_linkedin_profile_url(url: str) -> bool:
    """Check if URL is a LinkedIn individual profile (handles country subdomains)."""
    return bool(re.match(r'https?://(\w+\.)?linkedin\.com/in/', url))


def _extract_linkedin_urls(html: str) -> list[str]:
    """Extract LinkedIn profile URLs from HTML content."""
    return list(set(re.findall(r'https?://(?:\w+\.)?linkedin\.com/in/[\w-]+/?', html)))


def _fetch_page(url: str, timeout: int = 10) -> str:
    """Fetch a page and return HTML. Plain HTTP only (no headless browser)."""
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            allow_redirects=True,
        )
        if resp.status_code == 200 and "just a moment" not in resp.text[:500].lower():
            return resp.text
    except Exception:
        pass
    return ""


DATA_BROKER_DOMAINS = {
    "contactout.com", "rocketreach.com", "signalhire.com", "lusha.com",
    "apollo.io", "hunter.io", "snov.io", "skrapp.io", "uplead.com",
    "zoominfo.com", "leadiq.com", "seamless.ai", "nymeria.io",
}


def _verify_evidence(evidence: dict, profile: Profile) -> str:
    """Return confidence level that this URL is actually about the profile's person.

    Returns "strong", "medium", or "none".

    Strong signals (accept as primary):
    - Search source was "email-exact" (page contained the literal email)
    - URL domain matches the profile's email domain (e.g., stripe.com/team/x)
    - URL path contains first+last name slug (dylan-matthews, dylanmatthews)

    Medium signals (retain as fetched_content only, don't claim as primary):
    - Full name appears in snippet title+description
    - Source was "name+domain-broad" (name + org domain constraint)

    None: reject — we can't tell if it's the right person. Common names in
    "broad" search (name only) are too likely to be different people.
    """
    url = (evidence.get("url") or "").lower()
    source = (evidence.get("source") or "").lower()
    snippet = f"{evidence.get('title','')} {evidence.get('description','')}".lower()

    first = (profile.name.split()[0] if profile.name else "").lower()
    last_parts = profile.name.split() if profile.name else []
    last = last_parts[-1].lower() if len(last_parts) >= 2 else ""

    email = (profile.email or "").lower()
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    # Strip subdomain prefix for a looser match
    email_base = ".".join(email_domain.split(".")[-2:]) if email_domain else ""

    # ── Strong signals ───────────────────────────────────────
    if source == "email-exact":
        return "strong"

    if email_base and email_base in url and email_base not in {
        "gmail.com", "hotmail.com", "yahoo.com", "outlook.com", "me.com",
        "icloud.com", "aol.com", "proton.me", "protonmail.com", "msn.com",
        "live.com", "hey.com", "pm.me",
    }:
        return "strong"

    # Name slug in URL path: requires BOTH first + last (or one long one)
    if first and last:
        slug_candidates = [
            f"{first}-{last}",
            f"{first}_{last}",
            f"{first}.{last}",
            f"{first}{last}",
            f"{last}-{first}",
            f"{last}{first}",
        ]
        # Only trust slugs ≥7 chars (avoids matching "joli" or "liyu" by accident)
        if any(len(s) >= 7 and s in url for s in slug_candidates):
            return "strong"

    # ── Medium signals ──────────────────────────────────────
    if source in {"name+domain-broad", "name+domain"}:
        if first and last and first in snippet and last in snippet:
            return "medium"

    # Full name (both first and last) appears in snippet text
    if first and last and first in snippet and last in snippet:
        # Require they're reasonably close together to avoid random co-occurrence
        return "medium"

    return "none"


def _save_evidence_urls(profile: Profile, evidence: list[dict]) -> int:
    """Persist non-LinkedIn evidence URLs onto the profile, with verification.

    Each URL is graded for how confidently it belongs to this person:
    - strong: goes into primary fields (twitter_url, website_url) if empty,
              plus other_links + fetched_content snippet
    - medium: goes into fetched_content only (scoring judge can use the text
              but we don't claim it as the person's primary website/Twitter)
    - none:   dropped entirely — too likely to be a different person with the
              same name

    Returns the number of URLs retained (strong + medium).
    """
    if not evidence:
        return 0

    existing_links = set(l.lower().rstrip("/") for l in (profile.other_links or []))
    retained = 0
    # Cap at 10 evidence URLs to avoid ballooning the profile
    for e in evidence[:20]:
        url = (e.get("url") or "").strip()
        if not url:
            continue

        confidence = _verify_evidence(e, profile)
        if confidence == "none":
            continue

        ul = url.lower()
        key = ul.rstrip("/")
        retained += 1

        is_github = "github.com" in ul
        is_twitter = "twitter.com" in ul or ul.startswith("https://x.com/") or "://x.com/" in ul
        is_aux = ("substack.com" in ul or ".edu/" in ul
                  or "medium.com" in ul or "scholar.google" in ul)

        # Only set PRIMARY profile fields (twitter_url, website_url) from
        # strong-confidence evidence — medium evidence lives in fetched_content.
        if confidence == "strong":
            if is_github:
                if key not in existing_links:
                    profile.other_links.append(url)
                    existing_links.add(key)
            elif is_twitter:
                if not profile.twitter_url:
                    profile.twitter_url = url
                elif key not in existing_links:
                    profile.other_links.append(url)
                    existing_links.add(key)
            elif is_aux:
                if key not in existing_links:
                    profile.other_links.append(url)
                    existing_links.add(key)
            else:
                if not profile.website_url:
                    profile.website_url = url
                elif key not in existing_links:
                    profile.other_links.append(url)
                    existing_links.add(key)

        # Snippet goes to fetched_content regardless of confidence tier, but
        # labeled so the judge knows how much weight to give it.
        snippet = f"{e.get('title','')}\n{e.get('description','')}".strip()
        if snippet:
            slot = f"search_evidence({confidence}):{url[:80]}"
            profile.fetched_content[slot] = snippet

    return retained


# Loud/unreliable broker domains we never fetch. Snippets we already see
# (cheap) can still be mined; this set only gates the expensive HTML fetch
# and the attribution inference that comes with "the email was on the page,
# so any LinkedIn in the HTML belongs to this person".
BROKER_FETCH_SKIP = {
    "spokeo.com", "beenverified.com", "whitepages.com", "radaris.com",
    "truepeoplesearch.com", "peekyou.com", "rocketreach.com", "clearbit.com",
    "zoominfo.com", "apollo.io", "contactout.com", "signalhire.com",
    "lusha.com", "hunter.io", "snov.io", "skrapp.io", "uplead.com",
    "leadiq.com", "seamless.ai", "nymeria.io",
}

# Academic / government / nonprofit TLDs that are safe to fetch (org-site
# team pages, faculty pages, etc).
_SAFE_TLDS = (".edu", ".edu/", ".gov", ".gov/", ".ac.uk", ".ac.uk/")


def _is_safe_followup_domain(url: str, email_domain: str | None) -> bool:
    """Decide whether we should fetch this page to extract LinkedIn URLs.

    Allowed:
    - pages on the profile's own email domain (the person's org site)
    - .edu / .gov / .ac.uk pages
    - .org pages that aren't loud broker domains

    Denied:
    - known broker domains (spokeo, beenverified, rocketreach, etc.)
    """
    url_lower = (url or "").lower()
    if not url_lower:
        return False

    # Never fetch known broker domains — noisy and cost-inflating.
    if any(d in url_lower for d in BROKER_FETCH_SKIP):
        return False

    # Person's own email domain is always fair game.
    if email_domain and email_domain not in PERSONAL_DOMAINS:
        if email_domain in url_lower:
            return True

    # Academic / government pages.
    if any(t in url_lower for t in _SAFE_TLDS):
        return True

    # Generic .org pages (nonprofit / org team pages) — safe-ish, still
    # subject to the broker-skip above.
    if ".org/" in url_lower or url_lower.endswith(".org"):
        return True

    return False


def _follow_email_evidence(
    results: list[dict],
    name: str,
    log: list[str],
    email_domain: str | None = None,
) -> list[dict]:
    """Follow email evidence: fetch pages where the email appeared, extract LinkedIn URLs.

    This is the key step the full pipeline does — org websites and academic
    pages often contain the person's LinkedIn URL in their HTML even when it's
    not in the search snippet. We deliberately skip loud broker domains to
    avoid cost blow-ups and inflated false-positive attribution.
    """
    found = []
    seen_li = set()

    for r in results:
        url = r.get("url", "")
        url_lower = url.lower()
        desc = r.get("description", "")
        title = r.get("title", "")

        # Skip LinkedIn results themselves
        if "linkedin.com" in url_lower:
            continue

        # Check snippet for LinkedIn URL first (no fetch needed). Snippets are
        # free to consume — the broker-skip only applies to HTML fetches.
        li_match = re.search(r'https?://(?:\w+\.)?linkedin\.com/in/[\w-]+/?', desc)
        if li_match:
            li_url = li_match.group(0).rstrip("/")
            if li_url not in seen_li:
                seen_li.add(li_url)
                log.append(f"    LinkedIn from snippet: {li_url}")
                # Snippet-based LinkedIn from the email-exact search is
                # ground-truth-ish, so tag as "exact".
                found.append({
                    "title": title,
                    "url": li_url,
                    "description": desc,
                    "_email_evidence": True,
                    "_email_evidence_type": "exact",
                })
            continue

        # Only fetch pages we trust enough to infer identity from.
        if not _is_safe_followup_domain(url, email_domain):
            log.append(f"    Skipping non-safe domain: {url[:80]}")
            continue

        log.append(f"    Fetching page: {url[:80]}")
        html = _fetch_page(url, timeout=15)
        if not html:
            log.append(f"    → fetch failed")
            continue

        # Extract LinkedIn URLs from page HTML
        li_urls = _extract_linkedin_urls(html)
        for li_url in li_urls:
            li_url = li_url.rstrip("/")
            if li_url not in seen_li:
                seen_li.add(li_url)
                log.append(f"    LinkedIn from page HTML: {li_url}")
                found.append({
                    "title": f"via {url_lower.split('/')[2]}",
                    "url": li_url,
                    "description": title,
                    "_email_evidence": True,
                    "_email_evidence_type": "exact",
                })

    return found


# ── Search functions ──────────────────────────────────────

def _brave_search(query: str, api_key: str) -> list[dict]:
    if not api_key:
        return []
    from ._retry import retry_request
    resp = retry_request(
        lambda: requests.get(
            BRAVE_SEARCH_URL,
            headers={"X-Subscription-Token": api_key},
            params={"q": query, "count": 5},
            timeout=30,
        ),
        max_attempts=3, base_delay=1.5, label=f"Brave {query[:40]}",
    )
    if resp is None or resp.status_code != 200:
        return []
    try:
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
            for r in resp.json().get("web", {}).get("results", [])
        ]
    except Exception:
        return []


def _serper_search(query: str, api_key: str) -> list[dict]:
    if not api_key:
        return []
    from ._retry import retry_request
    resp = retry_request(
        lambda: requests.post(
            SERPER_SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=30,
        ),
        max_attempts=3, base_delay=1.5, label=f"Serper {query[:40]}",
    )
    if resp is None or resp.status_code != 200:
        return []
    try:
        return [
            {"title": r.get("title", ""), "url": r.get("link", ""), "description": r.get("snippet", "")}
            for r in resp.json().get("organic", [])
        ]
    except Exception:
        return []


def _web_search(query: str, brave_key: str, serper_key: str) -> list[dict]:
    """Search using all available engines, merge and deduplicate."""
    results = _brave_search(query, brave_key)
    serper_results = _serper_search(query, serper_key)
    seen = {r["url"] for r in results}
    for r in serper_results:
        if r["url"] not in seen:
            results.append(r)
            seen.add(r["url"])
    time.sleep(RATE_LIMIT_DELAY)
    return results


# ── Main resolver ─────────────────────────────────────────

class IdentityResolver:
    """Full identity resolution pipeline: name + context → LinkedIn URL."""

    def __init__(
        self,
        brave_api_key: str | None = None,
        serper_api_key: str | None = None,
        rate_limit: float = 0.5,
    ):
        self.brave_key = brave_api_key or os.environ.get("BRAVE_API_KEY", "")
        self.serper_key = serper_api_key or os.environ.get("SERPER_API_KEY", "")
        self.rate_limit = rate_limit

    def resolve_profile(self, profile: Profile) -> ResolveResult:
        """Run full resolution pipeline for a single profile."""
        log = []
        ctx = _extract_context(profile)

        if "name" not in ctx:
            return ResolveResult(error="No name", log=["SKIP: no name to search for"])

        # Note: "LinkedIn: No" in metadata means they didn't provide it,
        # not that they don't have one. Still try to find it.

        name = ctx["name"]
        first = ctx.get("first", "")
        last = ctx.get("last", "")
        log.append(f"Resolving: {name} (org={ctx.get('org','?')}, email={ctx.get('email','?')})")

        all_candidates = []
        queries_run = set()
        _search_count = [0]

        # Accumulate non-LinkedIn URLs we encounter. Even when LinkedIn lookup
        # fails, these are valuable signals (personal sites, GitHub, Substack,
        # academic pages) that the scoring judge can use.
        evidence_urls: list[dict] = []
        evidence_seen: set = set()
        # Non-LinkedIn results bucketed by search label. Used by the email-exact
        # and org-site paths to run `_follow_email_evidence` against the
        # actual pages the search returned — the feature was dead before
        # because `search()` returned LinkedIn-only.
        non_li_by_label: dict[str, list[dict]] = {}

        def _record_evidence(results: list[dict], source: str) -> None:
            for r in results or []:
                url = (r.get("url") or "").strip()
                if not url or "linkedin.com" in url.lower():
                    continue
                # Skip obvious data brokers / spam
                ul = url.lower()
                if any(d in ul for d in ("spokeo.com", "beenverified", "whitepages", "radaris",
                                           "truepeoplesearch", "peekyou", "rocketreach", "clearbit",
                                           "zoominfo", "apollo.io")):
                    continue
                key = ul.rstrip("/")
                if key in evidence_seen:
                    continue
                evidence_seen.add(key)
                evidence_urls.append({
                    "url": url,
                    "title": r.get("title", "")[:200],
                    "description": (r.get("description") or "")[:400],
                    "source": source,
                })

        def search(query: str, label: str):
            """Run a query and return just the LinkedIn-profile results.

            Non-LinkedIn results are captured as fallback evidence via
            `_record_evidence` AND stashed into `non_li_by_label[label]`
            so that callers who want to follow them (org-site HTML,
            broker snippets, etc.) can retrieve them from that dict
            rather than re-running the query. This is what revives
            `_follow_email_evidence` (which used to be dead because
            `search()` only returned LinkedIn).
            """
            if query in queries_run:
                return []
            queries_run.add(query)
            _search_count[0] += 1
            log.append(f"  Search ({label}): {query}")
            results = _web_search(query, self.brave_key, self.serper_key)
            li_results = [r for r in results if _is_linkedin_profile_url(r["url"])]
            non_li_results = [r for r in results if not _is_linkedin_profile_url(r["url"])]
            # Capture non-LinkedIn URLs as fallback evidence AND store for
            # targeted follow-up from callers (see email-exact / org-site).
            _record_evidence(non_li_results, label)
            non_li_by_label.setdefault(label, []).extend(non_li_results)
            log.append(f"    → {len(results)} results, {len(li_results)} LinkedIn profiles")
            return li_results

        MAX_SEARCH_TIME = 10  # seconds per individual search query timeout

        # ── Step 1: Email evidence (ground truth) ──
        # Two queries that used to be conflated, now carefully separated:
        #   email-exact:     literal "{email}" — pages that actually contain the
        #                    email. LinkedIn results here are ground-truth-ish
        #                    and deserve the +20 email-evidence bonus.
        #   email-username:  "{email_local}" linkedin OR researchgate — a broad
        #                    sweep that returns 5–10 unrelated LinkedIns for
        #                    common first-name locals (dan, ari, john, milica).
        #                    These are NOT ground truth; they get ordinary
        #                    name/slug/org scoring only.
        email_verified_company = ""
        email_domain_for_follow = ""
        if ctx.get("email"):
            email = ctx["email"]
            email_domain_for_follow = email.split("@", 1)[1].lower() if "@" in email else ""
            email_results = search(f'"{email}"', "email-exact")

            # Also search with email username on LinkedIn/social sites.
            # NOTE: this query's LinkedIn results are NOT email evidence —
            # a "dan" query returns every Dan on the Internet.
            email_local = email.split("@")[0]
            broad_email = search(f'"{email_local}" linkedin OR researchgate', "email-username")

            # ── email-exact results: legitimate email evidence ──
            for r in email_results:
                # LinkedIn URL extracted from the snippet of a page that
                # contained the literal email address. Tag as exact evidence.
                li_match = re.search(r'https?://(\w+\.)?linkedin\.com/in/[\w-]+/?', r.get("description", ""))
                if li_match:
                    li_url = li_match.group(0).rstrip("/")
                    log.append(f"    LinkedIn URL found in email-exact snippet: {li_url}")
                    all_candidates.append({
                        "title": r["title"], "url": li_url,
                        "description": r["description"],
                        "_email_evidence": True,
                        "_email_evidence_type": "exact",
                    })

                # Extract company from broker page titles
                # Format: "Kevin Jurczyk Email & Phone Number | Deloitte & Touche LLP"
                title = r.get("title", "")
                url_lower = r.get("url", "").lower()
                is_broker = any(d in url_lower for d in [
                    "contactout.com", "rocketreach.com", "signalhire.com", "lusha.com",
                    "apollo.io", "hunter.io", "zoominfo.com", "leadiq.com",
                ])
                if is_broker and " | " in title:
                    company_part = title.split(" | ")[1].strip()
                    for noise in ["ContactOut", "RocketReach", "SignalHire", "Lusha",
                                  "Apollo", "Hunter", "ZoomInfo", "LeadIQ"]:
                        company_part = company_part.replace(noise, "").strip(" |,-.")
                    if company_part and len(company_part) > 2:
                        email_verified_company = company_part
                        log.append(f"    Email-verified company from broker: {email_verified_company}")

                # Direct LinkedIn hit from the email-exact query is ground-truth.
                if _is_linkedin_profile_url(r.get("url", "")):
                    r["_email_evidence"] = True
                    r["_email_evidence_type"] = "exact"
                    all_candidates.append(r)

            # ── email-username results: treat as ordinary broad hits ──
            # No +20 bonus. Let name/slug/org signals decide.
            for r in broad_email:
                # Snippet may still contain a LinkedIn URL — harvest it as a
                # plain candidate (no evidence flag).
                li_match = re.search(r'https?://(\w+\.)?linkedin\.com/in/[\w-]+/?', r.get("description", ""))
                if li_match:
                    li_url = li_match.group(0).rstrip("/")
                    log.append(f"    LinkedIn URL found in email-username snippet: {li_url}")
                    all_candidates.append({
                        "title": r["title"], "url": li_url,
                        "description": r["description"],
                        # no _email_evidence flag — this is a broad hit, not ground truth
                    })

                if _is_linkedin_profile_url(r.get("url", "")):
                    # Intentionally NO _email_evidence flag. This was the bug.
                    all_candidates.append(r)

            # Follow email evidence: fetch pages where the email literally
            # appeared to extract LinkedIn URLs from HTML (org websites,
            # academic pages). `search()` now stores non-LinkedIn results
            # keyed by label so we have actual pages to follow.
            non_li_pages = non_li_by_label.get("email-exact", [])
            if non_li_pages:
                log.append(f"  Following email evidence ({len(non_li_pages)} non-LinkedIn pages)...")
                evidence_candidates = _follow_email_evidence(
                    non_li_pages, name, log,
                    email_domain=email_domain_for_follow,
                )
                all_candidates.extend(evidence_candidates)

                if evidence_candidates:
                    log.append(f"  Email evidence found {len(evidence_candidates)} LinkedIn URLs — high confidence")

        # ── Step 2: Name + org on LinkedIn ──
        if ctx.get("org"):
            org = ctx["org"]
            all_candidates.extend(search(
                f'"{first} {last}" "{org}" site:linkedin.com/in', "name+org"))
            org_simple = org.split()[0] if org else ""
            if org_simple and len(org_simple) > 3 and org_simple.lower() != org.lower():
                all_candidates.extend(search(
                    f'"{first} {last}" "{org_simple}" site:linkedin.com/in', "name+org-simple"))

        # ── Step 3: Name + content keywords on LinkedIn ──
        if ctx.get("content_keywords"):
            kw_str = " ".join(ctx["content_keywords"][:3])
            all_candidates.extend(search(
                f'"{first} {last}" {kw_str} site:linkedin.com/in', "name+keywords"))

        # ── Step 4: Name + title on LinkedIn ──
        if ctx.get("title"):
            all_candidates.extend(search(
                f'"{first} {last}" "{ctx["title"]}" site:linkedin.com/in', "name+title"))

        # ── Step 5: Name + location on LinkedIn ──
        loc = ctx.get("city") or ctx.get("country", "")
        if loc:
            all_candidates.extend(search(
                f'"{first} {last}" {loc} site:linkedin.com/in', "name+location"))

        # ── Step 6: Name + email domain company ──
        if ctx.get("email_company"):
            all_candidates.extend(search(
                f'"{first} {last}" "{ctx["email_company"]}" site:linkedin.com/in', "name+email-domain"))

        # ── Step 7: Name + email domain on LinkedIn ──
        if ctx.get("email_domain"):
            all_candidates.extend(search(
                f'"{first} {last}" "{ctx["email_domain"]}" site:linkedin.com/in', "name+email-domain"))

        # ── Step 7b: Scrape org website for LinkedIn URLs ──
        # Org websites often list team members with LinkedIn links.
        # `search()` already split out LinkedIn vs non-LinkedIn results, so
        # we pull the non-LinkedIn pages from non_li_by_label["org-website"].
        if ctx.get("email_domain") and first and last:
            org_domain = ctx["email"].split("@")[-1].lower() if ctx.get("email") else ""
            if org_domain and org_domain not in PERSONAL_DOMAINS:
                _ = search(f'site:{org_domain} "{first} {last}"', "org-website")
                org_non_li = non_li_by_label.get("org-website", [])
                if org_non_li:
                    log.append(f"  Scraping org website ({org_domain}) for LinkedIn URLs...")
                    org_evidence = _follow_email_evidence(
                        org_non_li, name, log,
                        email_domain=email_domain_for_follow or org_domain,
                    )
                    all_candidates.extend(org_evidence)

        # ── Step 8: Name + org/keywords WITHOUT site:linkedin restriction ──
        # This catches pages that mention the person and link to their LinkedIn
        # (e.g., org websites, conference pages, articles)
        # Then follows those pages to extract LinkedIn URLs from HTML.
        broad_results = []
        if ctx.get("org"):
            org_key = ctx["org"].split()[0] if len(ctx["org"].split()) > 3 else ctx["org"]
            broad_results = search(f'"{first} {last}" "{org_key}" linkedin', "name+org-broad")
        elif ctx.get("content_keywords"):
            kw = ctx["content_keywords"][0]
            broad_results = search(f'"{first} {last}" {kw} linkedin', "name+kw-broad")
        elif ctx.get("email_domain"):
            broad_results = search(f'"{first} {last}" "{ctx["email_domain"]}" linkedin', "name+domain-broad")

        if broad_results:
            # Extract LinkedIn URLs from snippets (no email-evidence bonus —
            # this is a broad query, not a literal-email match).
            for r in broad_results:
                li_match = re.search(r'https?://(\w+\.)?linkedin\.com/in/[\w-]+/?', r.get("description", ""))
                if li_match and not _is_linkedin_profile_url(r.get("url", "")):
                    li_url = li_match.group(0).rstrip("/")
                    log.append(f"    LinkedIn from snippet: {li_url}")
                    all_candidates.append({"title": r["title"], "url": li_url, "description": r["description"]})
            all_candidates.extend(broad_results)

        # Follow non-LinkedIn results for each broad label — pull from
        # non_li_by_label and subject to the safe-domain filter inside
        # _follow_email_evidence (skips brokers, only fetches own-domain
        # or academic/org pages).
        for label in ("name+org-broad", "name+kw-broad", "name+domain-broad"):
            non_li = non_li_by_label.get(label, [])
            if non_li:
                log.append(f"  Following {len(non_li)} {label} pages for LinkedIn URLs...")
                page_evidence = _follow_email_evidence(
                    non_li, name, log,
                    email_domain=email_domain_for_follow,
                )
                all_candidates.extend(page_evidence)

        # ── Step 9: Broad name search (fallback) ──
        if not all_candidates and first and last:
            all_candidates.extend(search(
                f'"{first} {last}" linkedin', "broad"))

        # ── Step 10: Slug-based search ──
        if ctx.get("email") and first and last:
            email_local = ctx["email"].split("@")[0].lower()
            # Skip generic email prefixes like "info", "admin", "contact"
            if email_local not in ("info", "admin", "contact", "hello", "office", "support", "mail"):
                all_candidates.extend(search(
                    f"site:linkedin.com/in/{email_local}", "slug-email"))

        # ── Deduplicate ──
        seen = set()
        unique = []
        for c in all_candidates:
            url = c["url"].rstrip("/").lower()
            if url not in seen:
                seen.add(url)
                unique.append(c)

        if not unique:
            log.append(f"  No LinkedIn profiles found across all searches ({len(evidence_urls)} non-LinkedIn evidence URLs retained)")
            return ResolveResult(error="No LinkedIn profile found", log=log, evidence_urls=evidence_urls)

        log.append(f"  {len(unique)} unique LinkedIn candidates found")

        # ── Score candidates ──
        result = self._score_candidates(
            unique, ctx, email_verified_company, log, profile=profile
        )
        # Attach evidence to successful results too — useful signal when LinkedIn
        # scoring rejects all candidates.
        if not result.evidence_urls:
            result.evidence_urls = evidence_urls
        return result

    def _score_candidates(
        self,
        candidates: list[dict],
        ctx: dict,
        email_verified_company: str,
        log: list[str],
        *,
        profile: Profile | None = None,
    ) -> ResolveResult:
        """Score all candidates against context and pick best.

        `profile` is optional — when provided, the Gemini arbiter is consulted
        for genuinely ambiguous cases (top candidates tied within 1 point OR
        the top candidate accepting exactly at threshold). The arbiter is
        capped at 1 call per invocation for cost control.
        """
        first = ctx.get("first", "").lower()
        last = ctx.get("last", "").lower()
        name_parts = [p for p in [first, last] if p]

        scored = []
        for c in candidates:
            score = 0
            reasons = []
            title = c.get("title", "").lower()
            desc = c.get("description", "").lower()
            url = c.get("url", "")
            combined = title + " " + desc

            # Email evidence (from step 1). Two tiers:
            #   "exact"     — page contained the literal email. +20 (ground truth).
            #   "proximity" — weaker signal (reserved). +5.
            # Legacy _email_evidence=True defaults to "exact" so callers that
            # predate this change aren't silently downgraded.
            et = c.get("_email_evidence_type")
            if et is None and c.get("_email_evidence"):
                et = "exact"
            if et == "exact":
                score += 20
                reasons.append("email-evidence(+20)")
            elif et == "proximity":
                score += 5
                reasons.append("email-proximity(+5)")

            # Name match in title
            if first and first in title:
                score += 2
                reasons.append(f"first-in-title(+2)")
            if last and last in title:
                score += 3
                reasons.append(f"last-in-title(+3)")

            # Org match — email-verified company gets massive weight (ground truth)
            if email_verified_company:
                ev_lower = email_verified_company.lower()
                ev_words = [w for w in re.split(r'[\s&,]+', ev_lower) if len(w) > 2]
                if ev_lower in combined or (ev_words and all(w in combined for w in ev_words)):
                    score += 10
                    reasons.append("email-verified-org(+10)")
                else:
                    score -= 3
                    reasons.append("no-verified-org(-3)")
            elif ctx.get("org"):
                org_lower = ctx["org"].lower()
                org_words = [w for w in re.split(r'[\s&,]+', org_lower) if len(w) > 3]
                if org_lower in combined:
                    score += 6
                    reasons.append("org-exact(+6)")
                elif org_words and sum(1 for w in org_words if w in combined) >= len(org_words) * 0.6:
                    score += 4
                    reasons.append("org-words(+4)")

            # Title match
            if ctx.get("title"):
                title_lower = ctx["title"].lower()
                if title_lower in combined:
                    score += 4
                    reasons.append("title-exact(+4)")
                elif any(w in combined for w in title_lower.split() if len(w) > 4):
                    score += 2
                    reasons.append("title-partial(+2)")

            # Location match
            for loc_key in ("city", "country"):
                loc = ctx.get(loc_key, "").lower()
                if loc and loc in combined:
                    score += 2
                    reasons.append(f"loc-{loc_key}(+2)")
                    break

            # Content keyword match — catches domain-specific terms
            if ctx.get("content_keywords"):
                kw_hits = sum(1 for kw in ctx["content_keywords"] if kw in combined)
                if kw_hits >= 2:
                    score += 3
                    reasons.append(f"keywords({kw_hits})(+3)")
                elif kw_hits == 1:
                    score += 1
                    reasons.append(f"keywords(1)(+1)")

            # Email domain match
            if ctx.get("email_domain"):
                if ctx["email_domain"].lower() in combined:
                    score += 3
                    reasons.append("email-domain(+3)")

            # LinkedIn slug matches name or email
            slug = url.rstrip("/").split("/")[-1].lower()
            slug_clean = slug.replace("-", "").replace("_", "")
            if ctx.get("email"):
                email_local = ctx["email"].split("@")[0].lower().replace(".", "").replace("-", "")
                if slug_clean == email_local:
                    score += 8
                    reasons.append("slug=email(+8)")
            # Check slug both as split parts AND as substring
            slug_parts = set(re.split(r"[-_]", slug))
            name_slug_hits = sum(1 for p in name_parts if p in slug_parts)
            # Also check if name parts appear as substrings (handles "joshsiegle")
            if name_slug_hits == 0:
                name_slug_hits = sum(1 for p in name_parts if len(p) >= 4 and p in slug_clean)
            # Handle initial-based slugs: "nathan-l" for "Nathan Leonard"
            if name_slug_hits <= 1 and first and last:
                # Check if slug matches first + last_initial pattern
                initial_slug = f"{first}{last[0]}" if last else ""
                if initial_slug and initial_slug in slug_clean:
                    name_slug_hits = 2
                # Or first_initial + last pattern
                initial_slug2 = f"{first[0]}{last}" if first else ""
                if initial_slug2 and initial_slug2 in slug_clean:
                    name_slug_hits = 2
            if name_slug_hits:
                score += name_slug_hits * 2
                reasons.append(f"slug-name({name_slug_hits})(+{name_slug_hits*2})")
            # Penalize if slug doesn't contain the last name OR initial-based match
            if last and len(last) >= 4 and last not in slug_clean and f"{first[0] if first else ''}{last[0]}" not in slug:
                score -= 1
                reasons.append("slug-no-last(-1)")

            scored.append((score, url, reasons, c.get("title", "")))

        scored.sort(key=lambda x: -x[0])

        # Log top candidates
        for score, url, reasons, title in scored[:5]:
            log.append(f"  [{score:3d}] {url}  ({', '.join(reasons)})")

        best_score, best_url, best_reasons, best_title = scored[0]

        # Minimum threshold — adaptive but not punishing.
        # More context means we CAN check more signals, but shouldn't REQUIRE them all.
        # A strong name + slug match should be enough even with rich context.
        context_richness = sum(1 for k in ("org", "title", "city", "country", "email_domain", "content_keywords") if ctx.get(k))
        min_score = 3 + min(context_richness, 3)  # Cap at 6, was unbounded up to 9
        log.append(f"  Threshold: {min_score} (richness={context_richness}), best={best_score}")

        if best_score < min_score:
            log.append(f"  REJECTED: best score {best_score} < threshold {min_score}")
            return ResolveResult(
                error=f"Best match too weak (score={best_score}, need={min_score})",
                log=log,
            )

        # Ambiguity check: if multiple candidates have the same top score,
        # try to break the tie before rejecting.
        tied = [s for s in scored if s[0] == best_score]

        # Arbiter gating. We call Gemini when heuristic scoring is genuinely
        # a coin flip:
        #   (a) top 2+ candidates are within 1 point of the best score
        #       (tied OR near-tied, which includes tied), AND there is
        #       substantive differentiation to ask about, OR
        #   (b) the top candidate is accepting exactly at threshold — that
        #       regime has been the source of wrong-person matches in the
        #       past because a single tiebreaker flips the decision.
        # Cap: at most ONE arbiter call per profile resolution.
        near_tied = [s for s in scored if s[0] >= best_score - 1]
        at_threshold = best_score == min_score
        should_arbitrate = profile is not None and (
            (len(near_tied) >= 2) or at_threshold
        )

        arbiter_decision: dict | None = None
        if should_arbitrate:
            # Build candidate dicts from the scored tuples + original `unique`
            # list. We match by URL, which is the common key.
            pool = near_tied if len(near_tied) >= 2 else scored[:2]
            candidate_pool = pool[:5]
            url_to_raw = {c.get("url", "").rstrip("/").lower(): c for c in candidates}
            arbiter_candidates = []
            for idx, (s, u, reasons, t) in enumerate(candidate_pool):
                raw = url_to_raw.get(u.rstrip("/").lower(), {})
                arbiter_candidates.append({
                    "index": idx,
                    "url": u,
                    "title": raw.get("title", t),
                    "description": raw.get("description", ""),
                    "score": s,
                    "reasons": list(reasons),
                })

            log.append(
                f"  Arbiter: consulting Gemini — near_tied={len(near_tied)}, "
                f"at_threshold={at_threshold}, candidates={len(arbiter_candidates)}"
            )
            try:
                from .arbiter import arbitrate_identity
                arbiter_decision = arbitrate_identity(profile, arbiter_candidates)
            except Exception as e:
                log.append(f"  Arbiter: error — {e}")
                arbiter_decision = {
                    "winner_index": None,
                    "confidence": "low",
                    "reason": f"arbiter_error: {e}",
                    "model": "",
                    "arbiter_called": True,
                    "error": True,
                }

            log.append(
                f"  Arbiter: winner_index={arbiter_decision.get('winner_index')}, "
                f"confidence={arbiter_decision.get('confidence')}, "
                f"reason={arbiter_decision.get('reason')}"
            )

            # Record as a verification_decisions entry so the eval harness can
            # slice arbiter behavior alongside heuristic decisions.
            self._record_arbiter_decision(profile, arbiter_candidates, arbiter_decision)

            winner_idx = arbiter_decision.get("winner_index")
            if winner_idx is not None:
                try:
                    winner_idx = int(winner_idx)
                except (TypeError, ValueError):
                    winner_idx = None
            if winner_idx is not None and 0 <= winner_idx < len(arbiter_candidates):
                chosen_url = arbiter_candidates[winner_idx]["url"]
                # Map back to the scored tuple so we use the heuristic score /
                # reasons for the confidence/method strings.
                for s_tuple in scored:
                    if s_tuple[1] == chosen_url:
                        best_score, best_url, best_reasons, best_title = s_tuple
                        break
                log.append(f"  Arbiter: picked {best_url} (overrides heuristic)")
            else:
                # Arbiter abstained → treat as ambiguous, skip rather than
                # accepting a likely-wrong top candidate.
                log.append(
                    f"  REJECTED: arbiter abstained on {len(tied)}-way tie "
                    f"(score={best_score})"
                )
                return ResolveResult(
                    error=(
                        f"Ambiguous: arbiter abstained on "
                        f"{len(tied)} tied at score {best_score}"
                    ),
                    alternatives=[url for _, url, _, _ in tied[:4]],
                    log=log,
                )
        elif len(tied) > 1:
            # No arbiter available (profile==None) — fall back to the pre-arbiter
            # heuristic tie-break chain. Preserved verbatim so behavior is
            # unchanged when `profile` is not supplied (e.g., unit tests).
            distinguishing = {"org-exact", "org-words", "email-evidence", "slug=email",
                              "title-exact", "loc-city", "loc-country"}
            best_has_unique = any(r.split("(")[0] in distinguishing for r in best_reasons)
            second_reasons = tied[1][2]
            second_has_same = any(r.split("(")[0] in distinguishing for r in second_reasons)

            if best_has_unique and not second_has_same:
                log.append(f"  {len(tied)} tied but best has distinguishing signal: {best_reasons}")
            else:
                def count_signals(reasons):
                    return sum(1 for r in reasons if r.split("(")[0] in distinguishing)
                signal_counts = [(count_signals(t[2]), t) for t in tied]
                signal_counts.sort(key=lambda x: -x[0])
                if signal_counts[0][0] > signal_counts[1][0]:
                    best_score, best_url, best_reasons, best_title = signal_counts[0][1]
                    log.append(f"  {len(tied)} tied but best has more signals ({signal_counts[0][0]} vs {signal_counts[1][0]})")
                elif len(tied) <= 3:
                    log.append(f"  {len(tied)} candidates tied at score {best_score} — accepting top with low confidence")
                else:
                    log.append(f"  REJECTED: {len(tied)} candidates tied at score {best_score} — ambiguous, refusing to guess")
                    return ResolveResult(
                        error=f"Ambiguous: {len(tied)} candidates tied at score {best_score}",
                        alternatives=[url for _, url, _, _ in tied[:4]],
                        log=log,
                    )

        confidence = "high" if best_score >= 12 else "medium" if best_score >= 7 else "low"
        alts = [url for _, url, _, _ in scored[1:4]]
        log.append(f"  SELECTED: {best_url} ({confidence}, score={best_score})")

        return ResolveResult(
            linkedin_url=best_url,
            confidence=confidence,
            method=f"score={best_score} ({', '.join(best_reasons)})",
            alternatives=alts,
            log=log,
        )

    @staticmethod
    def _record_arbiter_decision(
        profile: Profile | None,
        arbiter_candidates: list[dict],
        arbiter_decision: dict,
    ) -> None:
        """Append the arbiter's output as a verification_decisions entry on
        the profile, so eval harnesses can slice arbiter activity alongside
        heuristic decisions.

        Defensive: older Profile instances may not have the
        `verification_decisions` attribute; we initialise it if missing, and
        swallow any assignment errors (e.g., frozen dataclasses).
        """
        if profile is None:
            return
        from datetime import datetime, timezone

        decisions = getattr(profile, "verification_decisions", None)
        if decisions is None:
            decisions = []
            try:
                profile.verification_decisions = decisions
            except Exception:
                return

        winner_idx = arbiter_decision.get("winner_index")
        winner_url = ""
        if winner_idx is not None:
            try:
                widx = int(winner_idx)
                if 0 <= widx < len(arbiter_candidates):
                    winner_url = arbiter_candidates[widx].get("url", "")
            except (TypeError, ValueError):
                pass

        if winner_idx is None:
            decision_label = "ambiguous"
            reason_prefix = "arbiter abstained"
        elif arbiter_decision.get("error"):
            decision_label = "ambiguous"
            reason_prefix = "arbiter error"
        else:
            decision_label = "accept"
            reason_prefix = f"arbiter selected index={winner_idx}"

        decisions.append(
            {
                "linkedin_url": winner_url,
                "enriched_name": "",
                "score": 0,
                "anchors_positive": ["arbiter_called"],
                "anchors_negative": [],
                "decision": decision_label,
                "reason": (
                    f"{reason_prefix} (confidence={arbiter_decision.get('confidence')}, "
                    f"reason={arbiter_decision.get('reason')}, "
                    f"candidates={len(arbiter_candidates)})"
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def resolve_batch(
        self,
        profiles: list[Profile],
        on_progress: Callable[[int, int, str], None] | None = None,
        max_workers: int = 10,
    ) -> dict:
        """Resolve LinkedIn URLs for profiles in parallel."""
        import concurrent.futures

        to_resolve = [
            p for p in profiles
            if not p.linkedin_url
            and p.name
            and p.enrichment_status == EnrichmentStatus.PENDING
        ]

        stats = {"resolved": 0, "failed": 0, "skipped": 0, "total": len(to_resolve)}
        completed = [0]

        def do_resolve(profile: Profile) -> tuple[Profile, ResolveResult]:
            result = self.resolve_profile(profile)
            return profile, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(do_resolve, p): p for p in to_resolve}

            for future in concurrent.futures.as_completed(futures):
                profile, result = future.result()
                completed[0] += 1

                if on_progress:
                    on_progress(completed[0], len(to_resolve), f"Resolved {profile.display_name()}")

                profile.enrichment_log.extend(result.log)

                if result.linkedin_url:
                    profile.linkedin_url = result.linkedin_url
                    profile.enrichment_log.append(f"→ LinkedIn: {result.linkedin_url} ({result.confidence})")
                    stats["resolved"] += 1
                else:
                    profile.enrichment_log.append(f"→ Not found: {result.error}")
                    stats["failed"] += 1

                # Save non-LinkedIn evidence URLs regardless of LinkedIn outcome.
                # Each URL is verified (strong/medium/none) against the person's
                # name, email domain, and URL slug — rejects common-name collisions.
                if result.evidence_urls:
                    retained = _save_evidence_urls(profile, result.evidence_urls)
                    profile.enrichment_log.append(
                        f"→ Evidence URLs: {len(result.evidence_urls)} seen, {retained} verified as this person"
                    )

        return stats
