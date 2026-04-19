"""Stage 1: fast signals. No network, no API.

Derives:
- email cohort: personal / edu / gov / corp / org / no_email
- org_domain: the email domain (if non-personal) — used as Stage 2 anchor
- name slugs: candidate slugs for URL matching (first-last, firstlast,
  first.last, last-first, firstinitial+last)

Everything here is pure computation on Profile fields.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from ..identity import PERSONAL_DOMAINS
from ..models import Profile


COHORT_PERSONAL = "personal"
COHORT_EDU = "edu"
COHORT_GOV = "gov"
COHORT_ORG = "org"
COHORT_CORP = "corp"
COHORT_NO_EMAIL = "no_email"


@dataclass
class CohortSignals:
    """Output of Stage 1."""
    cohort: str = COHORT_NO_EMAIL
    org_domain: str = ""          # e.g. "stripe.com" — empty for personal
    email_local: str = ""         # e.g. "jdoe" — empty if no email
    first: str = ""
    last: str = ""
    name_slugs: list[str] = field(default_factory=list)  # ordered, deduped

    def to_dict(self) -> dict:
        return {
            "cohort": self.cohort,
            "org_domain": self.org_domain,
            "email_local": self.email_local,
            "first": self.first,
            "last": self.last,
            "name_slugs": list(self.name_slugs),
        }


def classify_email(email: str) -> tuple[str, str]:
    """Return (cohort, org_domain). org_domain is empty for personal cohorts."""
    if not email or "@" not in email:
        return COHORT_NO_EMAIL, ""
    dom = email.strip().split("@", 1)[1].lower()
    if not dom:
        return COHORT_NO_EMAIL, ""
    if dom in PERSONAL_DOMAINS:
        return COHORT_PERSONAL, ""
    if dom.endswith(".edu") or dom.endswith(".ac.uk") or dom.endswith(".edu.au"):
        return COHORT_EDU, dom
    if dom.endswith(".gov") or dom.endswith(".gov.uk"):
        return COHORT_GOV, dom
    if dom.endswith(".org"):
        return COHORT_ORG, dom
    return COHORT_CORP, dom


def _normalize(s: str) -> str:
    """Strip accents, lowercase, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


_CREDENTIAL_TOKENS = {
    "jr", "sr", "ii", "iii", "iv", "phd", "md", "esq",
    "mba", "mph", "dphil", "ma", "ms", "bs", "ba", "jd",
    "ph", "d",  # fragments of "Ph.D." after dot-stripping
    "m",        # fragment of "M.D."
}

# Regex to strip full credential tails BEFORE we tokenize.
_CREDENTIAL_TAIL_RE = re.compile(
    r"(?i),?\s*\b("
    r"ph\.?\s*d\.?|"
    r"m\.?\s*d\.?|"
    r"j\.?\s*d\.?|"
    r"d\.?\s*phil\.?|"
    r"esq\.?|mba|mph|ma|ms|ba|bs|jr\.?|sr\.?|iii|iv|ii"
    r")\b\.?\s*$"
)


def _split_name(name: str) -> tuple[str, str]:
    """Return (first, last) or (first, "") for single-token names.

    Strips punctuation, post-nominals, parentheticals. Credentials like
    "Ph.D." and "M.D." are stripped as whole chunks before tokenizing so
    they never leak in as the "last" token.
    """
    norm = _normalize(name)
    # Drop parentheticals: "Jane (Janie) Doe" → "Jane Doe"
    norm = re.sub(r"\([^)]*\)", " ", norm)
    # Strip credential tails in loop (handles "Ph.D., M.D." chains)
    for _ in range(4):
        stripped = _CREDENTIAL_TAIL_RE.sub("", norm).strip().strip(",").strip()
        if stripped == norm:
            break
        norm = stripped
    # Keep only letters, hyphens, spaces
    norm = re.sub(r"[^a-z\- ]", " ", norm)
    parts = [p for p in re.split(r"\s+", norm) if p]
    # Belt-and-suspenders: strip any remaining credential-shaped tokens
    while parts and parts[-1] in _CREDENTIAL_TOKENS:
        parts.pop()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def generate_name_slugs(name: str) -> list[str]:
    """Generate candidate URL-slugs for a name.

    Returns an ordered, deduplicated list — high-signal slugs first.
    Slugs shorter than 5 chars are excluded unless they're an
    initial+last-name pattern (jdoe) with a ≥4-char last name, because that
    pattern is highly distinctive in URLs.
    """
    first, last = _split_name(name)
    slugs: list[str] = []

    def _push(s: str, min_len: int = 5) -> None:
        s = s.strip("-_.")
        if len(s) >= min_len and s not in slugs:
            slugs.append(s)

    if first and last:
        _push(f"{first}-{last}")
        _push(f"{first}{last}")
        _push(f"{first}.{last}")
        _push(f"{first}_{last}")
        _push(f"{last}-{first}")
        _push(f"{last}{first}")
        # initial+last (common professional pattern: jdoe, jsmith) — allow
        # a 4-char slug here since the structure is distinctive.
        if len(last) >= 3:
            _push(f"{first[0]}{last}", min_len=4)
            _push(f"{first[0]}-{last}", min_len=5)
        # first+initial
        _push(f"{first}{last[0]}")
        _push(f"{first}-{last[0]}")
    elif first and len(first) >= 5:
        _push(first)

    return slugs


def classify_profile(profile: Profile) -> CohortSignals:
    """Compute Stage-1 signals from a profile's identity fields."""
    cohort, org_domain = classify_email(profile.email or "")

    email_local = ""
    if profile.email and "@" in profile.email:
        email_local = profile.email.strip().split("@", 1)[0].lower()

    first, last = _split_name(profile.name or "")
    slugs = generate_name_slugs(profile.name or "")

    return CohortSignals(
        cohort=cohort,
        org_domain=org_domain,
        email_local=email_local,
        first=first,
        last=last,
        name_slugs=slugs,
    )


def slug_matches_url(slugs: Iterable[str], url: str) -> str:
    """Return the first slug that appears in `url`, or "" if none do.

    Case-insensitive substring match against url path/host.
    """
    u = (url or "").lower()
    if not u:
        return ""
    for s in slugs:
        if s in u:
            return s
    return ""
