"""Stage 3c: Substack vertical.

Substack does not have an official public search API. We use the undocumented
`substack.com/api/v1/reader/search` endpoint (accessible without auth) to
find publications by name. Hits get anchors:

    - name_match:    publication title or author name contains profile name
    - slug_match:    publication subdomain / URL contains a name slug
    - bio_match:     publication description mentions first+last tokens
    - platform_match:always added for live Substack publications

If the undocumented endpoint is unreachable, we fall back silently (return
empty) — Substack is a nice-to-have, not a required stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import requests

from .cohort import CohortSignals, slug_matches_url
from .evidence import Evidence


SUBSTACK_SEARCH = "https://substack.com/api/v1/reader/search"


@dataclass
class SubstackResult:
    hits: list[Evidence]
    queries: int
    log: list[str]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", _norm(s)))


def query_substack(
    signals: CohortSignals,
    max_results: int = 5,
    timeout: int = 8,
) -> SubstackResult:
    log: list[str] = []
    if not (signals.first and signals.last):
        return SubstackResult(hits=[], queries=0, log=["skip: need first+last"])

    q = f"{signals.first} {signals.last}"
    try:
        r = requests.get(
            SUBSTACK_SEARCH,
            params={"query": q, "type": "publication", "limit": max_results},
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (research)"},
        )
        if r.status_code != 200:
            log.append(f"substack {r.status_code}")
            return SubstackResult(hits=[], queries=1, log=log)
        payload = r.json()
    except Exception as e:
        log.append(f"substack exception: {e}")
        return SubstackResult(hits=[], queries=1, log=log)

    hits: list[Evidence] = []
    for pub in _iter_results(payload):
        ev = _evidence_from_pub(pub, signals)
        if ev is not None:
            hits.append(ev)
            log.append(f"substack hit: {ev.url} anchors={sorted(ev.anchors)}")

    return SubstackResult(hits=hits, queries=1, log=log)


def _iter_results(payload) -> list[dict]:
    """Normalize different response shapes Substack has returned over time."""
    if not isinstance(payload, dict):
        return []
    # Current shape: {"results": [...]}; older: {"publications": [...]}
    for key in ("results", "publications", "hits", "items"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return []


def _evidence_from_pub(pub: dict, signals: CohortSignals) -> Optional[Evidence]:
    if not isinstance(pub, dict):
        return None

    url = pub.get("base_url") or pub.get("url") or pub.get("custom_domain") or ""
    if url and not url.startswith("http"):
        url = f"https://{url.lstrip('/')}"

    # Try publication + author signals
    title = pub.get("name") or pub.get("title") or ""
    description = pub.get("description") or pub.get("subtitle") or ""
    author_name = pub.get("author_name") or ""

    name_toks = _tokens(f"{title} {author_name}")
    bio_toks = _tokens(description)

    anchors: set[str] = set()
    if signals.first and signals.last:
        if signals.first in name_toks and signals.last in name_toks:
            anchors.add("name_match")
        if signals.first in bio_toks and signals.last in bio_toks:
            anchors.add("bio_match")
    elif signals.first and signals.first in name_toks:
        anchors.add("name_match")

    if url and slug_matches_url(signals.name_slugs, url):
        anchors.add("slug_match")

    if anchors:
        anchors.add("platform_match")

    if "name_match" not in anchors and "slug_match" not in anchors:
        return None

    return Evidence(
        url=url or f"https://substack.com/@{author_name}",
        source="substack",
        kind="substack",
        anchors=anchors,
        snippet=description or title,
        title=title,
        raw={"author_name": author_name},
    )
