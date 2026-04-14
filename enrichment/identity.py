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

    def __post_init__(self):
        if self.alternatives is None:
            self.alternatives = []
        if self.log is None:
            self.log = []


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


def _follow_email_evidence(results: list[dict], name: str, log: list[str]) -> list[dict]:
    """Follow email evidence: fetch pages where the email appeared, extract LinkedIn URLs.

    This is the key step the full pipeline does — broker pages and org websites
    often contain the person's LinkedIn URL in their HTML even when it's not
    in the search snippet.
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

        # Check snippet for LinkedIn URL first (no fetch needed)
        li_match = re.search(r'https?://(?:\w+\.)?linkedin\.com/in/[\w-]+/?', desc)
        if li_match:
            li_url = li_match.group(0).rstrip("/")
            if li_url not in seen_li:
                seen_li.add(li_url)
                log.append(f"    LinkedIn from snippet: {li_url}")
                found.append({"title": title, "url": li_url, "description": desc, "_email_evidence": True})
            continue

        # Fetch the page — the email was on it, so it's about this person
        is_broker = any(d in url_lower for d in DATA_BROKER_DOMAINS)
        page_type = "broker" if is_broker else "page"
        log.append(f"    Fetching {page_type}: {url[:80]}")

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
                log.append(f"    LinkedIn from {page_type} HTML: {li_url}")
                found.append({"title": f"via {url_lower.split('/')[2]}", "url": li_url, "description": title, "_email_evidence": True})

    return found


# ── Search functions ──────────────────────────────────────

def _brave_search(query: str, api_key: str) -> list[dict]:
    if not api_key:
        return []
    try:
        resp = requests.get(
            BRAVE_SEARCH_URL,
            headers={"X-Subscription-Token": api_key},
            params={"q": query, "count": 5},
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
            for r in resp.json().get("web", {}).get("results", [])
        ]
    except requests.Timeout:
        print(f"  [TIMEOUT] Brave search timed out for: {query[:60]}")
        return []
    except Exception:
        return []


def _serper_search(query: str, api_key: str) -> list[dict]:
    if not api_key:
        return []
    try:
        resp = requests.post(
            SERPER_SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=30,
        )
        if resp.status_code != 200:
            return []
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

        def search(query: str, label: str):
            if query in queries_run:
                return []
            queries_run.add(query)
            _search_count[0] += 1
            log.append(f"  Search ({label}): {query}")
            results = _web_search(query, self.brave_key, self.serper_key)
            li_results = [r for r in results if _is_linkedin_profile_url(r["url"])]
            log.append(f"    → {len(results)} results, {len(li_results)} LinkedIn profiles")
            return li_results

        MAX_SEARCH_TIME = 10  # seconds per individual search query timeout

        # ── Step 1: Email evidence (ground truth) ──
        # Search for literal email — pages containing it are ground truth about this person.
        # Broker pages (ContactOut, RocketReach) often have LinkedIn URL in their snippet.
        email_verified_company = ""
        if ctx.get("email"):
            email = ctx["email"]
            email_results = search(f'"{email}"', "email-exact")

            # Also search with email username on LinkedIn/social sites
            email_local = email.split("@")[0]
            broad_email = search(f'"{email_local}" linkedin OR researchgate', "email-username")

            for r in email_results + broad_email:
                # Check if snippet contains a LinkedIn URL (broker pages often do)
                li_match = re.search(r'https?://(\w+\.)?linkedin\.com/in/[\w-]+/?', r.get("description", ""))
                if li_match:
                    li_url = li_match.group(0).rstrip("/")
                    log.append(f"    LinkedIn URL found in snippet: {li_url}")
                    all_candidates.append({
                        "title": r["title"], "url": li_url,
                        "description": r["description"], "_email_evidence": True,
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
                    # Clean out broker site names
                    for noise in ["ContactOut", "RocketReach", "SignalHire", "Lusha",
                                  "Apollo", "Hunter", "ZoomInfo", "LeadIQ"]:
                        company_part = company_part.replace(noise, "").strip(" |,-.")
                    if company_part and len(company_part) > 2:
                        email_verified_company = company_part
                        log.append(f"    Email-verified company from broker: {email_verified_company}")

                # LinkedIn results from email search are high-trust
                if _is_linkedin_profile_url(r.get("url", "")):
                    r["_email_evidence"] = True
                    all_candidates.append(r)

            # Follow email evidence: fetch pages where the email appeared,
            # extract LinkedIn URLs from HTML (broker pages, org websites, etc.)
            if email_results:
                log.append(f"  Following email evidence ({len(email_results)} pages)...")
                evidence_candidates = _follow_email_evidence(email_results, name, log)
                all_candidates.extend(evidence_candidates)

                # SHORT-CIRCUIT: if email evidence directly yielded LinkedIn URLs,
                # those are near-ground-truth. Score them immediately.
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
        # Org websites often list team members with LinkedIn links
        if ctx.get("email_domain") and first and last:
            org_domain = ctx["email"].split("@")[-1] if ctx.get("email") else ""
            if org_domain and org_domain not in PERSONAL_DOMAINS:
                org_site_results = search(f'site:{org_domain} "{first} {last}"', "org-website")
                if org_site_results:
                    non_li = [r for r in org_site_results if "linkedin.com" not in r.get("url", "").lower()]
                    if non_li:
                        log.append(f"  Scraping org website ({org_domain}) for LinkedIn URLs...")
                        org_evidence = _follow_email_evidence(non_li, name, log)
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
            # Extract LinkedIn URLs from snippets
            for r in broad_results:
                li_match = re.search(r'https?://(\w+\.)?linkedin\.com/in/[\w-]+/?', r.get("description", ""))
                if li_match and not _is_linkedin_profile_url(r.get("url", "")):
                    li_url = li_match.group(0).rstrip("/")
                    log.append(f"    LinkedIn from snippet: {li_url}")
                    all_candidates.append({"title": r["title"], "url": li_url, "description": r["description"]})
            all_candidates.extend(broad_results)

            # Follow non-LinkedIn results — fetch pages, extract LinkedIn URLs from HTML
            non_li_results = [r for r in broad_results if not _is_linkedin_profile_url(r.get("url", ""))]
            if non_li_results:
                log.append(f"  Following {len(non_li_results)} broad results for LinkedIn URLs...")
                page_evidence = _follow_email_evidence(non_li_results, name, log)
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
            log.append("  No LinkedIn profiles found across all searches")
            return ResolveResult(error="No LinkedIn profile found", log=log)

        log.append(f"  {len(unique)} unique LinkedIn candidates found")

        # ── Score candidates ──
        result = self._score_candidates(unique, ctx, email_verified_company, log)
        return result

    def _score_candidates(self, candidates: list[dict], ctx: dict,
                          email_verified_company: str, log: list[str]) -> ResolveResult:
        """Score all candidates against context and pick best."""
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

            # Email evidence (from step 1) — highest trust
            if c.get("_email_evidence"):
                score += 20
                reasons.append("email-evidence(+20)")

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
        # we can't tell who's the right person — reject rather than guess wrong
        tied = [s for s in scored if s[0] == best_score]
        if len(tied) > 1:
            # Check if the best has a distinguishing signal the others don't
            # (e.g., org match, email evidence, email-slug match)
            distinguishing = {"org-exact", "org-words", "email-evidence", "slug=email",
                              "title-exact", "loc-city", "loc-country"}
            best_has_unique = any(r.split("(")[0] in distinguishing for r in best_reasons)
            second_reasons = tied[1][2]
            second_has_same = any(r.split("(")[0] in distinguishing for r in second_reasons)

            if not best_has_unique or second_has_same:
                log.append(f"  REJECTED: {len(tied)} candidates tied at score {best_score} — ambiguous, refusing to guess")
                return ResolveResult(
                    error=f"Ambiguous: {len(tied)} candidates tied at score {best_score}",
                    alternatives=[url for _, url, _, _ in tied[:4]],
                    log=log,
                )
            else:
                log.append(f"  {len(tied)} tied but best has distinguishing signal: {best_reasons}")

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

        return stats
