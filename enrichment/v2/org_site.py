"""Stage 2: Org-site crawl.

For profiles with a non-personal email (corp/edu/gov/org cohort), fetch a set
of conventional "who works here" paths on the email domain and look for a
link whose anchor text or href path matches the profile's name.

A hit here is a STRONG anchor: both the email domain AND the name verified on
a page the organization controls. That's two independent anchors right out
of the gate:
    email_match  (because we only visited the email's own domain)
    name_match   (name tokens present in the anchor text) OR
    slug_match   (first-last slug present in the href)

The module returns an Evidence list. It does NOT mutate the profile.
"""

from __future__ import annotations

import concurrent.futures
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests

from .cohort import CohortSignals
from .evidence import Evidence


# Paths commonly used for team/about/people listings. Fetched in parallel.
DEFAULT_PATHS = [
    "/team", "/people", "/staff", "/about", "/about-us", "/about/team",
    "/authors", "/fellows", "/research", "/researchers", "/leadership",
    "/company/team", "/our-team", "/bio", "/bios", "/members",
    "/contributors",
]

# Outbound-link domains we care about extracting when they appear on the
# org bio page (these become sub-evidence).
_SOCIAL_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "github.com", "substack.com",
    "medium.com", "scholar.google.com", "orcid.org", "mastodon.social",
}


@dataclass
class OrgSiteResult:
    hits: list[Evidence]
    pages_fetched: int
    log: list[str]


class _AnchorParser(HTMLParser):
    """Minimal HTML parser — collect (href, text) for every <a>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            d = dict(attrs)
            self._current_href = (d.get("href") or "").strip()
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href is not None:
            text = " ".join("".join(self._buf).split())
            self.anchors.append((self._current_href, text))
            self._current_href = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._buf.append(data)


def _fetch(url: str, timeout: int = 8) -> tuple[int, str]:
    """Fetch a URL. Returns (status, body). Body is empty on failure.

    Handles redirects, returns ('', '') on exception.
    """
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (research-bot)"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
            # Cap body at 1 MB to avoid pathological pages.
            return resp.status_code, resp.text[:1_000_000]
        return resp.status_code, ""
    except Exception:
        return 0, ""


def _extract_bio_near(text: str, anchor_text: str, window: int = 400) -> str:
    """Given full page text and a matched anchor text, try to extract the
    surrounding bio. Returns the nearest sentence-ish snippet up to `window`
    chars, centered on the anchor match.
    """
    if not text or not anchor_text:
        return ""
    idx = text.lower().find(anchor_text.lower())
    if idx < 0:
        return ""
    start = max(0, idx - window // 2)
    end = min(len(text), idx + window // 2)
    # Trim to sentence boundaries if possible
    snippet = text[start:end]
    return re.sub(r"\s+", " ", snippet).strip()


def _html_to_text(html: str) -> str:
    """Strip tags, return plain text (compact)."""
    # Drop script/style/nav
    html = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>",
                  " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&#\d+;", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _name_in_anchor_text(first: str, last: str, anchor_text: str) -> bool:
    """Return True if both first AND last appear in the anchor text.

    Case-insensitive, requires both tokens. Falls back to single-token match
    only if last is empty (single-token uploaded name).
    """
    if not anchor_text:
        return False
    t = anchor_text.lower()
    if first and last:
        return first in t and last in t
    if first and not last:
        return first in t and len(first) >= 5
    return False


def crawl_org_site(
    signals: CohortSignals,
    paths: Optional[Iterable[str]] = None,
    max_workers: int = 6,
    schemes: tuple[str, ...] = ("https://",),
) -> OrgSiteResult:
    """Crawl the profile's email domain for name hits.

    Args:
        signals: Stage-1 output. Requires non-empty org_domain + name tokens.
        paths: Paths to try (default: DEFAULT_PATHS)
        max_workers: Parallelism for page fetches
        schemes: URL scheme(s) to try. Default is https only.

    Returns:
        OrgSiteResult — list of Evidence + stats.
    """
    log: list[str] = []
    if not signals.org_domain:
        return OrgSiteResult(hits=[], pages_fetched=0, log=["skip: no org domain"])
    if not signals.first or not (signals.last or len(signals.first) >= 5):
        return OrgSiteResult(hits=[], pages_fetched=0, log=["skip: name too sparse"])

    paths_list = list(paths) if paths is not None else list(DEFAULT_PATHS)
    urls = [f"{sch}{signals.org_domain}{p}" for sch in schemes for p in paths_list]
    # Also try the homepage as a last resort (some orgs put people on /).
    urls.append(f"{schemes[0]}{signals.org_domain}/")

    pages: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch, u): u for u in urls}
        for fut in concurrent.futures.as_completed(futures):
            u = futures[fut]
            try:
                status, body = fut.result()
            except Exception:
                continue
            if body:
                pages[u] = body
                log.append(f"fetched {u} ({len(body)} bytes)")

    if not pages:
        return OrgSiteResult(hits=[], pages_fetched=0, log=log + ["no pages returned HTML"])

    hits: list[Evidence] = []
    seen_targets: set[str] = set()
    first, last = signals.first, signals.last
    slugs = list(signals.name_slugs)

    for page_url, html in pages.items():
        parser = _AnchorParser()
        try:
            parser.feed(html)
        except Exception:
            continue
        page_text = _html_to_text(html)

        for href, text in parser.anchors:
            if not href:
                continue
            absolute = urljoin(page_url, href)
            parsed = urlparse(absolute)
            if not parsed.netloc:
                continue

            href_lower = absolute.lower()
            text_lower = text.lower() if text else ""

            # Check for name hit: anchor text mentions both names,
            # OR the href path contains a name slug.
            slug_in_href = next((s for s in slugs if s in parsed.path.lower()), "")
            name_in_text = _name_in_anchor_text(first, last, text_lower)

            if not (slug_in_href or name_in_text):
                continue

            canon = absolute.lower().split("#", 1)[0].split("?", 1)[0].rstrip("/")
            if canon in seen_targets:
                continue
            seen_targets.add(canon)

            anchors = set()
            # Email domain is anchored: we only fetched pages on signals.org_domain,
            # so *this page* is on the email domain. That's email_match.
            anchors.add("email_match")
            if name_in_text:
                anchors.add("name_match")
            if slug_in_href:
                anchors.add("slug_match")

            # A bio snippet near the anchor becomes bio_match if we can find one.
            bio = _extract_bio_near(page_text, text or slug_in_href, window=400)
            if bio and (first in bio.lower() or last in bio.lower()):
                anchors.add("bio_match")

            # Determine kind based on destination host
            host = parsed.netloc.lower()
            kind = "listing"
            if "linkedin.com" in host:
                kind = "linkedin"
            elif "twitter.com" in host or host == "x.com" or host.endswith(".x.com"):
                kind = "twitter"
            elif "github.com" in host:
                kind = "github"
            elif "substack.com" in host:
                kind = "substack"
            elif signals.org_domain in host:
                kind = "bio"   # internal bio page

            hits.append(Evidence(
                url=absolute,
                source="org_site",
                kind=kind,
                anchors=anchors,
                snippet=bio,
                title=text,
                raw={"via_page": page_url},
            ))
            log.append(
                f"hit: {absolute[:80]} anchors={sorted(anchors)}"
            )

    return OrgSiteResult(hits=hits, pages_fetched=len(pages), log=log)
