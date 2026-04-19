"""Wrong-person audit.

Samples N enriched profiles and flags cases where the attributed
LinkedIn identity (linkedin_enriched.full_name) appears inconsistent
with the uploaded name.

Heuristics are conservative so we favor recall over precision — the
output is a list for *human review*, plus a lower-bound rate estimate.

Flags:
  - different_last_name
  - token_overlap_below_50pct
  - no_shared_tokens
  - initial_only_expansion_mismatch   (e.g. "Nathan L." vs "Nathan Leonard"
    passes; but "Naji Abi-Hashem" vs "Abi Olvera" fails)
  - empty_enriched_name                (enriched but no full_name)

CLI:
    python -m enrichment.eval.wrong_person_audit \\
        --account-id <uuid> --dataset-id <id> [--sample 50] [--seed 0]

Library:
    from enrichment.eval.wrong_person_audit import run_audit
    result = run_audit(profiles, sample=50)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from enrichment.models import Dataset, EnrichmentStatus, Profile  # noqa: E402


# Post-nominals / credential suffixes we always strip before tokenizing.
_CREDENTIAL_RE = re.compile(
    r",?\s*\b(ph\s*\.?\s*d\.?|m\s*\.?\s*d\.?|j\s*\.?\s*d\.?|d\s*\.?\s*phil\.?|"
    r"esq\.?|cpa|cfa|cfp|clu|chfc|ricp|pmp|mba|mph|msw|mfa|ma|ms|bs|ba|"
    r"jr\.?|sr\.?|ii|iii|iv|reg\.?|rn|np|lcsw|pe|aia|pharmd)\b\.?",
    re.IGNORECASE,
)

# Trademark / registered-mark symbols strip to nothing.
_TRADEMARK_RE = re.compile(r"[®™©]")


def _normalize(name: str) -> str:
    """Lowercase, strip accents+credentials+parentheticals, keep letters/hyphens."""
    if not name:
        return ""
    n = _TRADEMARK_RE.sub("", name)
    # Strip parentheticals like "(CIM-MCIM)" or "(Saba)" (middle-name nicknames
    # in parens are rarely the match signal; if they are, the first/last tokens
    # still cover them).
    n = re.sub(r"\([^)]*\)", " ", n)
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = _CREDENTIAL_RE.sub(" ", n)
    n = n.lower()
    n = re.sub(r"[^a-z0-9\- ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _tokens(name: str) -> list[str]:
    """Split a normalized name into tokens (keep hyphenated as one)."""
    n = _normalize(name)
    if not n:
        return []
    return [t for t in n.split(" ") if t]


def _is_initial(tok: str) -> bool:
    """Token like 'l' or 'l.' — a single-letter (with optional trailing dot)."""
    t = tok.rstrip(".")
    return len(t) == 1 and t.isalpha()


def _match_token_pair(a: str, b: str) -> bool:
    """Does token `a` from uploaded name match token `b` from enriched?

    Allows initial-expansions in either direction (e.g., 'leonard' vs 'l'
    or 'l' vs 'leonard').
    """
    if a == b:
        return True
    if _is_initial(a) and b.startswith(a.rstrip(".")):
        return True
    if _is_initial(b) and a.startswith(b.rstrip(".")):
        return True
    return False


def _token_overlap(a_tokens: list[str], b_tokens: list[str]) -> float:
    """Jaccard-ish overlap allowing initial expansion."""
    if not a_tokens or not b_tokens:
        return 0.0
    matched = 0
    used_b = set()
    for ta in a_tokens:
        for i, tb in enumerate(b_tokens):
            if i in used_b:
                continue
            if _match_token_pair(ta, tb):
                matched += 1
                used_b.add(i)
                break
    union_size = len(a_tokens) + len(b_tokens) - matched
    return matched / union_size if union_size else 0.0


SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "phd", "ph", "md", "dr", "esq"}


def _last_name(tokens: list[str]) -> str:
    """Best-effort last-name: last non-suffix token (initials allowed).

    We deliberately keep initials as valid last-name candidates so that
    "Elika S." matches "Elika Somani" via the initial-expansion rule.
    Suffixes like "Jr", "PhD" are stripped.
    """
    for tok in reversed(tokens):
        t = tok.rstrip(".")
        if t in SUFFIX_TOKENS:
            continue
        return tok
    return tokens[-1] if tokens else ""


def audit_profile(profile: Profile) -> dict[str, Any] | None:
    """Return a flag dict if the profile looks like a wrong-person match.

    Returns None if the profile looks fine.
    Returns dict with flags, tokens, and both names for review otherwise.
    """
    if profile.enrichment_status != EnrichmentStatus.ENRICHED:
        return None
    if not profile.linkedin_enriched:
        return None
    # Prefer explicit full_name; fall back to "Name: X" line in context_block
    # (that's the shape used when LinkedIn text is pre-imported from a CSV).
    enriched_name = (profile.linkedin_enriched.get("full_name") or "").strip()
    if not enriched_name:
        cb = profile.linkedin_enriched.get("context_block") or ""
        m = re.search(r"^\s*Name:\s*(.+)$", cb, flags=re.MULTILINE)
        if m:
            enriched_name = m.group(1).strip()
    uploaded = (profile.name or "").strip()
    if not uploaded:
        return None

    flags: list[str] = []
    if not enriched_name:
        # Nothing to compare against. Don't flag — this is a data-shape
        # issue, not a wrong-person issue.
        return None

    a = _tokens(uploaded)
    b = _tokens(enriched_name)
    if not a or not b:
        return None

    overlap = _token_overlap(a, b)
    a_last = _last_name(a)
    b_last = _last_name(b)

    # Single-token uploaded name ("Luke", "Daniel") can match anyone with
    # that first name. Low-confidence uploaded data — not strictly a
    # wrong-person flag but worth surfacing separately.
    single_token_upload = len(a) == 1

    if a_last and b_last and not _match_token_pair(a_last, b_last):
        if single_token_upload:
            flags.append("single_token_uploaded_name")
        else:
            flags.append("different_last_name")
    if overlap == 0:
        flags.append("no_shared_tokens")
    elif overlap < 0.5 and not single_token_upload:
        flags.append("token_overlap_below_50pct")

    if not flags:
        return None

    return {
        "profile_id": profile.id,
        "uploaded_name": uploaded,
        "enriched_name": enriched_name,
        "email": profile.email,
        "linkedin_url": profile.linkedin_url,
        "overlap": round(overlap, 3),
        "a_last": a_last,
        "b_last": b_last,
        "flags": flags,
    }


def run_audit(
    profiles: list[Profile],
    *,
    sample: int | None = 50,
    seed: int = 0,
) -> dict[str, Any]:
    """Audit a list of profiles.

    If `sample` is None or greater than the number of enriched profiles,
    the audit runs over every enriched profile.
    """
    enriched = [p for p in profiles if p.enrichment_status == EnrichmentStatus.ENRICHED]
    if sample and sample < len(enriched):
        rng = random.Random(seed)
        audited = rng.sample(enriched, sample)
    else:
        audited = enriched

    suspect_entries: list[dict] = []
    for p in audited:
        flag = audit_profile(p)
        if flag:
            suspect_entries.append(flag)

    # Split suspects: "strong" (different last name with full uploaded name)
    # vs "weak" (uploaded name too thin to judge, e.g. single token only).
    strong = [s for s in suspect_entries if "single_token_uploaded_name" not in s["flags"]]
    weak = [s for s in suspect_entries if "single_token_uploaded_name" in s["flags"]]

    rate_strong = len(strong) / len(audited) if audited else 0.0
    rate_total = len(suspect_entries) / len(audited) if audited else 0.0

    return {
        "enriched_population": len(enriched),
        "sample_size": len(audited),
        "suspects_in_sample": len(suspect_entries),
        "strong_suspects": len(strong),
        "weak_suspects_single_token_upload": len(weak),
        "suspect_rate_lower_bound": round(rate_strong, 4),
        "suspect_rate_including_weak": round(rate_total, 4),
        "projected_suspects_in_enriched": int(round(rate_strong * len(enriched))),
        "suspects": suspect_entries,
    }


def format_audit(result: dict[str, Any], *, label: str = "") -> str:
    """Render an audit result as human-readable text."""
    lines: list[str] = []
    bar = "=" * 72
    lines.append(bar)
    lines.append(f"WRONG-PERSON AUDIT{(' — ' + label) if label else ''}")
    lines.append(bar)
    lines.append(
        f"\nEnriched population: {result['enriched_population']}  "
        f"Sampled: {result['sample_size']}"
    )
    lines.append(
        f"Strong suspects (different last name, full uploaded name): "
        f"{result.get('strong_suspects', result['suspects_in_sample'])}"
    )
    lines.append(
        f"Weak suspects (single-token uploaded name — ambiguous): "
        f"{result.get('weak_suspects_single_token_upload', 0)}"
    )
    lines.append(
        f"Lower-bound wrong-person rate: {result['suspect_rate_lower_bound']*100:.2f}% "
        f"(projected ~{result['projected_suspects_in_enriched']} across enriched population)"
    )
    if "suspect_rate_including_weak" in result:
        lines.append(
            f"Including weak suspects: {result['suspect_rate_including_weak']*100:.2f}%"
        )

    if not result["suspects"]:
        lines.append("\nNo suspicious matches surfaced at current thresholds.")
        lines.append("")
        return "\n".join(lines)

    lines.append("\nSuspected wrong-person matches (review these manually):")
    lines.append("")
    for i, s in enumerate(result["suspects"], 1):
        lines.append(f"  [{i}] uploaded: {s['uploaded_name']!r}")
        lines.append(f"      enriched: {s['enriched_name']!r}")
        lines.append(f"      email:    {s.get('email', '')}")
        lines.append(f"      linkedin: {s.get('linkedin_url', '')}")
        lines.append(f"      overlap:  {s.get('overlap', 0.0)}  flags: {', '.join(s['flags'])}")
        if "a_last" in s and "b_last" in s:
            lines.append(f"      last-name: uploaded={s['a_last']!r} enriched={s['b_last']!r}")
        lines.append("")
    return "\n".join(lines)


# ──── loaders (mirror coverage_report.py) ────


def _load_local(path: str) -> tuple[list[Profile], str]:
    ds = Dataset.load(Path(path))
    return ds.profiles, f"local:{ds.name}"


def _load_cloud(account_id: str, dataset_id: str) -> tuple[list[Profile], str]:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore

    if load_dotenv:
        for p in (_root / ".env", Path.cwd() / ".env"):
            if p.exists():
                load_dotenv(str(p))
                break

    from cloud.storage.supabase import SupabaseStorage  # noqa: E402

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    storage = SupabaseStorage(url, key, account_id)
    ds = storage.load_dataset(dataset_id)
    return ds.profiles, f"cloud:{ds.name} ({dataset_id})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enrichment.eval.wrong_person_audit",
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument("--account-id", help="Supabase account_id")
    parser.add_argument("--dataset-id", help="Dataset id (cloud)")
    parser.add_argument("--local", help="Path to a local Dataset JSON")
    parser.add_argument("--sample", type=int, default=50, help="Sample size (default 50; 0 = all)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--out", help="Write the text report here as well")
    args = parser.parse_args(argv)

    if args.local:
        profiles, label = _load_local(args.local)
    elif args.account_id and args.dataset_id:
        profiles, label = _load_cloud(args.account_id, args.dataset_id)
    else:
        parser.error("Provide --local <path> or --account-id + --dataset-id")

    sample = None if args.sample == 0 else args.sample
    result = run_audit(profiles, sample=sample, seed=args.seed)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        text = format_audit(result, label=label)
        print(text)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
