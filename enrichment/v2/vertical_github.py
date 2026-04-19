"""Stage 3b: GitHub vertical.

Search the GitHub user-search API (free, 10 req/min unauth, 30 req/min with
token) for the profile's name. For each candidate, fetch the user record
and score anchors:

    - name_match:    user.name contains profile first+last tokens
    - email_match:   user.email (if public) matches profile email, OR
                     user.company / user.blog contains profile org_domain
    - slug_match:    GitHub login contains a name slug
    - bio_match:     user.bio contains profile first+last tokens
    - platform_match:always added when we land a non-trivial hit (bio or
                     repos >= 1)

GitHub is free-tier. We cap to 1 search + 3 user lookups per profile.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import requests

from .cohort import CohortSignals, slug_matches_url
from .evidence import Evidence


GITHUB_API = "https://api.github.com"


@dataclass
class GitHubResult:
    hits: list[Evidence]
    queries: int
    log: list[str]


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github.v3+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"token {tok}"
    return h


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", _norm(s)))


def query_github(
    signals: CohortSignals,
    max_users: int = 3,
    timeout: int = 8,
) -> GitHubResult:
    """Search GitHub for the profile's name, return Evidence list."""
    log: list[str] = []
    if not signals.first:
        return GitHubResult(hits=[], queries=0, log=["skip: no first name"])

    # Build a search query. GitHub supports `fullname:"first last"` and
    # `in:email`/`in:name` qualifiers.
    parts = [signals.first]
    if signals.last:
        parts.append(signals.last)
    name_query = f'"{" ".join(parts)}" in:name'

    try:
        r = requests.get(
            f"{GITHUB_API}/search/users",
            params={"q": name_query, "per_page": max_users},
            headers=_headers(),
            timeout=timeout,
        )
        if r.status_code != 200:
            log.append(f"github search {r.status_code}")
            return GitHubResult(hits=[], queries=1, log=log)
        items = r.json().get("items", [])
    except Exception as e:
        log.append(f"github search exception: {e}")
        return GitHubResult(hits=[], queries=1, log=log)

    if not items:
        log.append("github: 0 candidates")
        return GitHubResult(hits=[], queries=1, log=log)

    hits: list[Evidence] = []
    queries = 1

    for item in items[:max_users]:
        login = item.get("login") or ""
        if not login:
            continue
        try:
            ur = requests.get(
                f"{GITHUB_API}/users/{login}",
                headers=_headers(),
                timeout=timeout,
            )
            queries += 1
            if ur.status_code != 200:
                log.append(f"github user {login} {ur.status_code}")
                continue
            user = ur.json()
        except Exception as e:
            log.append(f"github user {login} exception: {e}")
            continue

        ev = _evidence_from_user(user, signals, login)
        if ev is not None:
            hits.append(ev)
            log.append(f"github hit: {ev.url} anchors={sorted(ev.anchors)}")

    return GitHubResult(hits=hits, queries=queries, log=log)


def _evidence_from_user(
    user: dict,
    signals: CohortSignals,
    login: str,
) -> Optional[Evidence]:
    url = (user.get("html_url") or f"https://github.com/{login}").lower()

    anchors: set[str] = set()

    # name_match: user.name
    name_toks = _tokens(user.get("name") or "")
    if signals.first and signals.last:
        if signals.first in name_toks and signals.last in name_toks:
            anchors.add("name_match")
    elif signals.first and signals.first in name_toks:
        anchors.add("name_match")

    # slug_match: login contains a slug
    slug_hit = slug_matches_url(signals.name_slugs, login)
    if slug_hit:
        anchors.add("slug_match")

    # email_match: user.email (public) matches profile email's domain
    email = (user.get("email") or "").lower()
    company = (user.get("company") or "").lower()
    blog = (user.get("blog") or "").lower()
    if signals.org_domain:
        dom = signals.org_domain
        if email.endswith("@" + dom):
            anchors.add("email_match")
            anchors.add("literal_email")
        elif dom in company or dom in blog or dom in email:
            anchors.add("email_match")

    # bio_match
    bio = (user.get("bio") or "").lower()
    if bio and signals.first and signals.first in bio:
        if not signals.last or signals.last in bio:
            anchors.add("bio_match")

    # Drop candidate entirely if no name+(slug|bio|email) — too weak.
    if "name_match" not in anchors:
        return None
    if len(anchors) < 2:
        # name only — needs at least one more anchor or a bio to count.
        return None

    # Platform match is implicit for GitHub hits with any repos / followers
    if (user.get("public_repos") or 0) >= 1 or bio:
        anchors.add("platform_match")

    snippet = user.get("bio") or f"{user.get('name','')} @ {user.get('company','')}".strip(" @")

    return Evidence(
        url=user.get("html_url") or f"https://github.com/{login}",
        source="github",
        kind="github",
        anchors=anchors,
        snippet=snippet,
        title=user.get("name") or login,
        raw={
            "login": login,
            "public_repos": user.get("public_repos"),
            "followers": user.get("followers"),
            "company": user.get("company"),
            "blog": user.get("blog"),
        },
    )
