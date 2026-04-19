"""Stage 3a: OpenAlex vertical.

OpenAlex is a free, no-auth scholarly metadata API covering >200M works and
>80M authors. For profiles in the .edu cohort, it's a cheap, high-signal
identity source: author name + affiliation institution + published works.

Matching strategy:
    - Query: `/authors?search=<first last>&per-page=5`
    - For each candidate, compute anchors:
        * name_match:    display_name tokens match first+last
        * email_match:   last_known_institution's display_name / ROR / URL
                         contains the profile's org_domain or the edu
                         institution nickname
        * platform_match:always added if we land a hit (OpenAlex profiles
                         are identity-verified via ORCID / publishers)

Returns Evidence objects; does NOT mutate profile.

Rate-limit: OpenAlex polite pool (adds `mailto=<user>` param) is free and
allows up to 10 req/s. We cap to 2 queries per profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import requests

from .cohort import CohortSignals
from .evidence import Evidence


OPENALEX_AUTHORS = "https://api.openalex.org/authors"


@dataclass
class OpenAlexResult:
    hits: list[Evidence]
    queries: int
    log: list[str]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", _norm(s)))


def _institution_domain_match(inst: dict, org_domain: str) -> bool:
    """Does an OpenAlex institution object contain our org domain?

    Checks homepage_url, ROR ID, display_name (institution nickname).
    """
    if not inst or not org_domain:
        return False
    dom = org_domain.lower()
    dom_root = dom
    # Strip subdomains for a lenient match: cs.mit.edu → mit.edu
    parts = dom.split(".")
    if len(parts) > 2:
        dom_root = ".".join(parts[-2:])

    for key in ("homepage_url", "ror", "id"):
        v = (inst.get(key) or "").lower()
        if dom in v or dom_root in v:
            return True

    # display_name nickname check: "Massachusetts Institute of Technology"
    # matches "mit.edu" via common-prefix of the nickname vs. dom_root.
    display = _norm(inst.get("display_name") or "")
    dom_prefix = dom_root.split(".")[0]
    if dom_prefix and dom_prefix in display:
        return True
    return False


def _evidence_from_author(
    author: dict,
    signals: CohortSignals,
    mailto: str,
) -> Optional[Evidence]:
    """Convert an OpenAlex author record into an Evidence (or None if weak)."""
    url = (author.get("id") or "").strip()
    if not url:
        return None

    display = author.get("display_name") or ""
    name_toks = _tokens(display)
    want = {t for t in (signals.first, signals.last) if t}
    if not want:
        return None

    anchors: set[str] = set()

    # name_match: both uploaded tokens present in author display_name
    if signals.first and signals.last:
        if signals.first in name_toks and signals.last in name_toks:
            anchors.add("name_match")
    elif signals.first and signals.first in name_toks:
        anchors.add("name_match")

    # Not a name match at all → reject outright.
    if "name_match" not in anchors:
        return None

    # Institution match gives us email_match (org_domain hits institution URL).
    inst = author.get("last_known_institution") or {}
    if _institution_domain_match(inst, signals.org_domain):
        anchors.add("email_match")

    # Platform match: being in OpenAlex with a specific institution is itself
    # an independent identity anchor (requires published work).
    works = author.get("works_count") or 0
    if works >= 1:
        anchors.add("platform_match")

    # Bio-like blob
    affil = inst.get("display_name", "")
    snippet = f"{display} — {affil} ({works} works)".strip(" —")

    return Evidence(
        url=url,
        source="openalex",
        kind="profile",
        anchors=anchors,
        snippet=snippet,
        title=display,
        raw={
            "works_count": works,
            "cited_by_count": author.get("cited_by_count"),
            "institution": affil,
        },
    )


def query_openalex(
    signals: CohortSignals,
    mailto: str = "research@example.com",
    max_results: int = 5,
    timeout: int = 8,
) -> OpenAlexResult:
    """Query OpenAlex for authors matching signals.first + signals.last.

    Only runs if we have both first + last names. Otherwise returns empty.
    """
    log: list[str] = []
    if not (signals.first and signals.last):
        return OpenAlexResult(hits=[], queries=0, log=["skip: need first+last"])

    query = f"{signals.first} {signals.last}"
    params = {
        "search": query,
        "per-page": max_results,
        "mailto": mailto,
    }

    try:
        resp = requests.get(OPENALEX_AUTHORS, params=params, timeout=timeout)
        if resp.status_code != 200:
            log.append(f"openalex {resp.status_code}")
            return OpenAlexResult(hits=[], queries=1, log=log)
        payload = resp.json()
    except Exception as e:
        log.append(f"openalex exception: {e}")
        return OpenAlexResult(hits=[], queries=1, log=log)

    hits: list[Evidence] = []
    for author in payload.get("results", []) or []:
        ev = _evidence_from_author(author, signals, mailto)
        if ev is not None:
            hits.append(ev)
            log.append(
                f"openalex hit: {ev.url} anchors={sorted(ev.anchors)}"
            )

    return OpenAlexResult(hits=hits, queries=1, log=log)
