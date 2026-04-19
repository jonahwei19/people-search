"""Evidence dataclass for v2 enrichment pipeline.

Each Evidence object represents one piece of provenance — a URL we think
belongs to the person, with explicit anchors describing *why* we think so.

The two-anchor verifier (verify.py) consumes these and decides whether a
profile has enough evidence to be considered "enriched". An anchor is a
distinct verified signal:

- email_match   — URL domain matches the profile's email domain
- name_match    — first + last name tokens appear on the page / URL
- slug_match    — URL path contains a name slug (first-last, firstlast, etc.)
- bio_match     — page has a bio paragraph referencing the person (org team page,
                  github bio, openalex affiliation, etc.)
- platform_match— profile on a platform that requires identity (OpenAlex,
                  official org team page, github-with-bio)
- literal_email — page contained the literal email string

Stages produce Evidence objects; they do NOT decide. verify.py decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


ANCHOR_TYPES = frozenset({
    "email_match",
    "name_match",
    "slug_match",
    "bio_match",
    "platform_match",
    "literal_email",
})


@dataclass
class Evidence:
    """One piece of verified evidence tying a URL to a profile.

    Fields:
        url:        The URL (canonical form preferred — rstrip("/") lowercase).
        source:     Which stage/module produced this ("org_site", "openalex",
                    "github", "substack", "linkedin", "open_web").
        kind:       Semantic kind — "bio", "profile", "listing", "article",
                    "twitter", "website", "github".
        anchors:    Set of anchor types (from ANCHOR_TYPES) that matched
                    this URL against the profile. Multiple anchors = stronger.
        snippet:    Short text excerpt (bio paragraph, search snippet, etc.)
                    that supports the attribution. May be empty for
                    platform_match-only hits.
        title:      Page title or display name at the destination.
        raw:        Arbitrary source-specific metadata.
    """
    url: str
    source: str
    kind: str = ""
    anchors: set[str] = field(default_factory=set)
    snippet: str = ""
    title: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def is_strong(self) -> bool:
        """Evidence with ≥2 anchors is a STRONG attribution."""
        return len(self.anchors) >= 2

    def add_anchor(self, anchor: str) -> None:
        if anchor not in ANCHOR_TYPES:
            raise ValueError(f"unknown anchor type: {anchor!r}")
        self.anchors.add(anchor)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["anchors"] = sorted(self.anchors)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        d = dict(d)
        anchors = set(d.pop("anchors", []) or [])
        ev = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        ev.anchors = anchors
        return ev


def merge_evidence(pile: list[Evidence], new: list[Evidence]) -> list[Evidence]:
    """Merge `new` into `pile` by URL, taking the UNION of anchors.

    Returns the same list (mutated in place) for chain-call convenience.
    """
    by_url: dict[str, Evidence] = {}
    for e in pile:
        by_url[_canon(e.url)] = e
    for e in new:
        key = _canon(e.url)
        if key in by_url:
            by_url[key].anchors |= e.anchors
            # Prefer the longer snippet, concatenate source tags.
            if len(e.snippet) > len(by_url[key].snippet):
                by_url[key].snippet = e.snippet
            if e.title and not by_url[key].title:
                by_url[key].title = e.title
            # Annotate multi-source merges (useful for debug).
            existing = by_url[key].raw.setdefault("_sources", [by_url[key].source])
            if e.source not in existing:
                existing.append(e.source)
        else:
            by_url[key] = e
            pile.append(e)
    return pile


def _canon(url: str) -> str:
    u = (url or "").strip().lower()
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.rstrip("/")


def strong_anchors_count(pile: list[Evidence]) -> int:
    """How many URLs in `pile` have ≥2 anchors (i.e. two-anchor verified)."""
    return sum(1 for e in pile if e.is_strong())
