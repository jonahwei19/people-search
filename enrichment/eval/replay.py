"""Offline replay of the enrichment verifier against stored enrichment_log
data. No API calls are made — we reconstruct the verifier's scoring inputs
from the per-profile log and re-apply a parameterised acceptance rule.

Intended use: "what would happen if we changed the verifier's thresholds or
scoring weights?" — answered in seconds against the current dataset.

Design
------

`_verify_match` in enrichers.py already logs every signal it considered
for each attempted LinkedIn URL:

    Verify name: MATCH ({'andrew', 'gerard'}, strength=strong)
    Verify org: MATCH ('ifp' found in experience)
    Verify org: MISMATCH (...)
    Verify location: MATCH ('san francisco')
    Verify location: MISMATCH (...)
    Verify content relevance: MATCH (N shared terms: ...)
    Verify content relevance: WEAK (zero overlap ...)
    Verify name: WEAK-MATCH REJECTED (...)
    Verify result: ACCEPTED (score=X, checks=Y, name=Z, positives=P, penalties=S)
    Verify result: REJECTED (score=X, checks=Y — likely wrong person)

We parse those lines into structured `VerifyAttempt` records (one per
LinkedIn URL tried) and then re-score them under a `ReplayConfig`. The
config exposes the same levers that have been the subject of discussion in
plans/diagnosis_hitrate.md:

    name_strong_score      — points awarded for a "strength=strong" name match
    name_normal_score      — points awarded for "strength=normal"
    name_weak_score        — points awarded for "strength=weak"
    org_match_score        — points awarded for Verify org: MATCH
    org_mismatch_penalty   — positive number, subtracted on Verify org: MISMATCH
    location_match_score   — points awarded for Verify location: MATCH
    location_mismatch_penalty
    content_match_score    — points awarded for Verify content relevance: MATCH
    content_weak_penalty   — positive number, subtracted on content relevance WEAK
    slug_anchor_score      — points awarded when the URL slug contains both
                             profile first AND last name (P4 anchor)
    require_anchors        — 0  → current default (soft penalty without
                                  positive → reject; weak-name without positive
                                  → reject; score >= 2)
                             1  → require ≥1 positive non-name signal
                                  to accept regardless of penalties
                             2  → require ≥2 positive non-name signals
    baseline_threshold     — minimum total score to accept (default 2)

`replay_verify(profile, enriched, config)` reruns the current logic with
the given config for a single (profile, enriched) pair, by re-parsing
the log it produced.

`replay_dataset(profiles, config, default_config=None)` walks every
profile, replays its decisions, and reports flips between the stored
outcome and the counterfactual.

Validation
----------
Replaying with `ReplayConfig()` (current defaults) must reproduce the
stored decisions for ≥95% of profiles. The `_validate_roundtrip` helper
returns the reproducibility rate and a list of mismatches so log-parse
gaps can be patched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from enrichment.models import Dataset, EnrichmentStatus, Profile  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────


@dataclass
class ReplayConfig:
    """Tunable parameters for the offline verifier replay.

    Defaults mirror enrichers.py:_verify_match as of enrichment v1.
    """
    name_strong_score: int = 3
    name_normal_score: int = 2
    name_weak_score: int = 1
    org_match_score: int = 3
    org_mismatch_penalty: int = 1   # stored as positive; subtracted on mismatch
    location_match_score: int = 2
    location_mismatch_penalty: int = 1
    content_match_score: int = 2
    content_weak_penalty: int = 1
    slug_anchor_score: int = 0      # P4 — 0 preserves current behaviour

    baseline_threshold: int = 2
    require_anchors: int = 0        # 0=current rule, 1=≥1 positive, 2=≥2 positives

    # Behavioural flags
    reject_on_weak_name_no_positive: bool = True
    reject_on_penalty_no_positive: bool = True
    # If True, a "WEAK-MATCH REJECTED" line in the log is treated as a hard
    # reject regardless of other parameters. Mirrors current behaviour.
    honor_weak_match_hard_reject: bool = True
    # If True, a "Verify name: MISMATCH" line is a hard reject. Mirrors
    # current behaviour.
    honor_name_mismatch_hard_reject: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────
# Log parsing
# ────────────────────────────────────────────────────────────────────


_TRYING_RE = re.compile(r"Trying LinkedIn:\s*(\S+)")
_NAME_MATCH_RE = re.compile(r"Verify name: MATCH.*?strength=(\w+)")
_NAME_MISMATCH_RE = re.compile(r"Verify name: MISMATCH")
_NAME_WEAK_REJECT_RE = re.compile(r"Verify name: WEAK-MATCH REJECTED")
_ORG_MATCH_RE = re.compile(r"Verify org: MATCH")
_ORG_MISMATCH_RE = re.compile(r"Verify org: MISMATCH")
_LOC_MATCH_RE = re.compile(r"Verify location: MATCH")
_LOC_MISMATCH_RE = re.compile(r"Verify location: MISMATCH")
_CONTENT_MATCH_RE = re.compile(r"Verify content relevance: MATCH")
_CONTENT_WEAK_RE = re.compile(r"Verify content relevance: WEAK")
_ACCEPTED_RE = re.compile(r"Verify result: ACCEPTED")
_REJECTED_RE = re.compile(r"Verify result: REJECTED")
_ENRICHLAYER_FAIL_RE = re.compile(r"LinkedIn enrichment failed")
_API_NO_DATA_RE = re.compile(r"API returned no data")


@dataclass
class VerifyAttempt:
    """Structured record of one `_verify_match` attempt extracted from a log.

    Each attempt corresponds to one Trying-LinkedIn block in the log. If
    the block terminates before a Verify result is emitted (e.g. the API
    call returned None), `stored_decision` is "no_data" and the attempt
    is skipped during replay.
    """
    url: str = ""
    name_strength: str = "none"      # "strong", "normal", "weak", "none"
    name_mismatch: bool = False
    name_weak_rejected: bool = False
    org_match: bool = False
    org_mismatch: bool = False
    location_match: bool = False
    location_mismatch: bool = False
    content_match: bool = False
    content_weak: bool = False
    slug_anchor: bool = False        # derived outside verifier (for P4)
    stored_decision: str = "unknown" # "accepted", "rejected", "no_data", "unknown"
    raw_score: int = 0               # score reported in ACCEPTED/REJECTED line
    raw_checks: int = 0              # checks reported
    # True when the stored ACCEPTED/REJECTED line carries the pre-FM2 format
    # ("score=X, checks=Y" with no name=/positives=/penalties=). Those rows
    # were produced by an older `_verify_match` whose only rule was
    # `score >= 2`, so current-code replay disagrees on ~20% of historical
    # cases (strong-name + org-mismatch with no positive, which the current
    # code rejects). Surfaced here so `validate_roundtrip` can restrict its
    # comparison to rows whose log format matches the current pipeline.
    legacy_log_format: bool = False


def _compute_slug_anchor(url: str, profile: Profile) -> bool:
    """Cheap heuristic for slug-anchor presence — the verifier used to not
    score this, but P4 proposes it as a positive signal. Returns True iff
    both the profile's first and last name appear as substrings of the
    URL slug (after lowercasing and stripping separators).
    """
    if not url or not profile.name:
        return False
    parts = profile.name.split()
    if len(parts) < 2:
        return False
    first = parts[0].lower()
    last = parts[-1].lower()
    # Slug is the segment after /in/, minus trailing digits.
    m = re.search(r"/in/([^/?#]+)", url.lower())
    if not m:
        return False
    slug = m.group(1)
    slug_clean = re.sub(r"[-_\.]", "", slug)
    return first in slug_clean and last in slug_clean


def parse_attempts(profile: Profile) -> list[VerifyAttempt]:
    """Walk a profile's enrichment_log and emit one VerifyAttempt per
    Trying-LinkedIn block. Attempts are in log order.
    """
    attempts: list[VerifyAttempt] = []
    current: VerifyAttempt | None = None

    for raw in profile.enrichment_log or []:
        line = str(raw)

        m = _TRYING_RE.search(line)
        if m:
            # Close previous attempt if it never saw a decision
            if current is not None:
                attempts.append(current)
            current = VerifyAttempt(url=m.group(1).rstrip("/"))
            current.slug_anchor = _compute_slug_anchor(current.url, profile)
            continue

        if current is None:
            continue

        # Hard rejects that short-circuit the verifier
        if _NAME_WEAK_REJECT_RE.search(line):
            current.name_weak_rejected = True
            current.stored_decision = "rejected"
            continue
        if _NAME_MISMATCH_RE.search(line):
            current.name_mismatch = True
            current.stored_decision = "rejected"
            continue

        m = _NAME_MATCH_RE.search(line)
        if m:
            current.name_strength = m.group(1).lower()
            continue

        if _ORG_MATCH_RE.search(line):
            current.org_match = True
        elif _ORG_MISMATCH_RE.search(line):
            current.org_mismatch = True

        if _LOC_MATCH_RE.search(line):
            current.location_match = True
        elif _LOC_MISMATCH_RE.search(line):
            current.location_mismatch = True

        if _CONTENT_MATCH_RE.search(line):
            current.content_match = True
        elif _CONTENT_WEAK_RE.search(line):
            current.content_weak = True

        if _ACCEPTED_RE.search(line):
            current.stored_decision = "accepted"
            sc = re.search(r"score=(-?\d+)", line)
            ch = re.search(r"checks=(\d+)", line)
            if sc:
                current.raw_score = int(sc.group(1))
            if ch:
                current.raw_checks = int(ch.group(1))
            # Current verifier writes "name=Z, positives=P, penalties=S".
            # Legacy verifier wrote only "score=X, checks=Y".
            current.legacy_log_format = "name=" not in line and "positives=" not in line
            attempts.append(current)
            current = None
            continue

        if _REJECTED_RE.search(line):
            current.stored_decision = "rejected"
            sc = re.search(r"score=(-?\d+)", line)
            ch = re.search(r"checks=(\d+)", line)
            if sc:
                current.raw_score = int(sc.group(1))
            if ch:
                current.raw_checks = int(ch.group(1))
            # Same legacy detection logic: current REJECTED lines include
            # "penalties=" or a descriptive reason; legacy ones stop at
            # "checks=Y — likely wrong person".
            current.legacy_log_format = (
                "penalties=" not in line
                and "corroborating" not in line
                and "weak name match" not in line
            )
            attempts.append(current)
            current = None
            continue

        if _API_NO_DATA_RE.search(line):
            # API returned None — verifier never ran for this URL
            current.stored_decision = "no_data"
            attempts.append(current)
            current = None
            continue

    if current is not None:
        # Dangling block (no clean close) — record as-is so replay is a no-op
        if current.stored_decision == "unknown":
            current.stored_decision = "no_data"
        attempts.append(current)

    return attempts


# ────────────────────────────────────────────────────────────────────
# Replay core
# ────────────────────────────────────────────────────────────────────


def _replay_attempt(a: VerifyAttempt, config: ReplayConfig) -> tuple[bool, dict]:
    """Apply the parameterised acceptance rule to a single attempt.

    Returns (would_accept, details). `details` holds the replay score
    breakdown so callers can audit flips.
    """
    # Hard rejects — mirror the verifier's early returns
    if config.honor_name_mismatch_hard_reject and a.name_mismatch:
        return False, {"reason": "name-mismatch"}
    if config.honor_weak_match_hard_reject and a.name_weak_rejected:
        return False, {"reason": "weak-match-reject"}
    if a.name_strength == "none":
        # No usable name signal. The verifier would have hit
        # "no name/enriched_name" and fallen through to scoring, but in
        # practice this happens only on malformed log lines; treat as a
        # conservative reject.
        return False, {"reason": "no-name-signal"}

    score = 0
    positives = 0
    penalties = 0
    breakdown: dict[str, int] = {}

    # Name
    if a.name_strength == "strong":
        score += config.name_strong_score
        breakdown["name_strong"] = config.name_strong_score
    elif a.name_strength == "normal":
        score += config.name_normal_score
        breakdown["name_normal"] = config.name_normal_score
    elif a.name_strength == "weak":
        score += config.name_weak_score
        breakdown["name_weak"] = config.name_weak_score

    # Org
    if a.org_match:
        score += config.org_match_score
        positives += 1
        breakdown["org_match"] = config.org_match_score
    elif a.org_mismatch:
        score -= config.org_mismatch_penalty
        penalties += 1
        breakdown["org_mismatch"] = -config.org_mismatch_penalty

    # Location
    if a.location_match:
        score += config.location_match_score
        positives += 1
        breakdown["location_match"] = config.location_match_score
    elif a.location_mismatch:
        score -= config.location_mismatch_penalty
        penalties += 1
        breakdown["location_mismatch"] = -config.location_mismatch_penalty

    # Content
    if a.content_match:
        score += config.content_match_score
        positives += 1
        breakdown["content_match"] = config.content_match_score
    elif a.content_weak:
        score -= config.content_weak_penalty
        penalties += 1
        breakdown["content_weak"] = -config.content_weak_penalty

    # Slug anchor (P4)
    if a.slug_anchor and config.slug_anchor_score:
        score += config.slug_anchor_score
        positives += 1
        breakdown["slug_anchor"] = config.slug_anchor_score

    details = {
        "score": score,
        "positives": positives,
        "penalties": penalties,
        "breakdown": breakdown,
        "name_strength": a.name_strength,
    }

    # Decision
    if score < config.baseline_threshold:
        return False, {**details, "reason": f"score<{config.baseline_threshold}"}

    if config.require_anchors > 0 and positives < config.require_anchors:
        return False, {**details, "reason": f"<{config.require_anchors}-positive-anchors"}

    if (
        config.reject_on_weak_name_no_positive
        and a.name_strength == "weak"
        and positives == 0
    ):
        return False, {**details, "reason": "weak-name-no-positive"}

    if (
        config.reject_on_penalty_no_positive
        and penalties > 0
        and positives == 0
    ):
        return False, {**details, "reason": "penalty-no-positive"}

    return True, {**details, "reason": "accepted"}


def _profile_stored_decision(profile: Profile, attempts: list[VerifyAttempt]) -> str:
    """Infer the profile-level decision from status + log.

    Returns one of: "accepted", "rejected", "skipped", "no_attempt".
    """
    status = profile.enrichment_status
    if not isinstance(status, EnrichmentStatus):
        try:
            status = EnrichmentStatus(status)
        except ValueError:
            status = EnrichmentStatus.PENDING

    if status == EnrichmentStatus.ENRICHED:
        if any(a.stored_decision == "accepted" for a in attempts):
            return "accepted"
        # Pre-enriched (LinkedIn text imported directly, no verify run)
        return "accepted"
    if status == EnrichmentStatus.FAILED:
        return "rejected" if attempts else "no_attempt"
    if status == EnrichmentStatus.SKIPPED:
        return "skipped"
    return "no_attempt"


def _profile_replay_decision(attempts: list[VerifyAttempt], config: ReplayConfig) -> tuple[str, dict | None]:
    """Replay every attempt under `config`. First accept wins. If none
    accept, decision is "rejected" if any attempt ran (rejected by
    replay), else "no_attempt".

    Returns (decision, winning_attempt_details_or_reason).
    """
    if not attempts:
        return "no_attempt", None

    first_failure: dict | None = None
    ran_any = False
    for a in attempts:
        if a.stored_decision == "no_data":
            continue
        ran_any = True
        accepted, details = _replay_attempt(a, config)
        if accepted:
            return "accepted", {"url": a.url, **details}
        if first_failure is None:
            first_failure = {"url": a.url, **details}

    if ran_any:
        return "rejected", first_failure
    return "no_attempt", None


@dataclass
class ProfileReplayResult:
    profile_id: str
    name: str
    email: str
    stored_decision: str
    replay_decision: str
    stored_url: str = ""
    replay_details: dict | None = None
    attempts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────
# Dataset-level replay
# ────────────────────────────────────────────────────────────────────


def replay_verify(
    profile: Profile,
    enriched: dict | None,
    config: ReplayConfig | None = None,
) -> dict:
    """Convenience single-profile replay.

    `enriched` is accepted for API symmetry with the real verifier but is
    not used — replay works entirely off the stored log. Returns a dict
    with the replay decision and attempt breakdown.
    """
    cfg = config or ReplayConfig()
    attempts = parse_attempts(profile)
    stored = _profile_stored_decision(profile, attempts)
    replay, details = _profile_replay_decision(attempts, cfg)
    return {
        "profile_id": profile.id,
        "stored_decision": stored,
        "replay_decision": replay,
        "attempts": [asdict(a) for a in attempts],
        "replay_details": details,
    }


def replay_dataset(
    profiles: Iterable[Profile],
    config: ReplayConfig | None = None,
) -> dict[str, Any]:
    """Replay every profile in the dataset. Returns a summary dict.

    Shape:
        {
            "config": {...},
            "total": N,
            "compared": N - (no_attempt or pending),
            "would_accept": int,
            "would_reject": int,
            "flips": [{profile_id, name, stored, replay, ...}, ...],
            "confusion": {"stored→replay": count, ...},
            "integrity": {...},
        }
    """
    cfg = config or ReplayConfig()
    profiles = list(profiles)
    total = len(profiles)
    would_accept = 0
    would_reject = 0
    skipped = 0
    results: list[ProfileReplayResult] = []
    confusion: Counter = Counter()
    flips: list[dict] = []

    for p in profiles:
        attempts = parse_attempts(p)
        stored = _profile_stored_decision(p, attempts)
        replay, details = _profile_replay_decision(attempts, cfg)

        if replay == "accepted":
            would_accept += 1
        elif replay == "rejected":
            would_reject += 1
        else:
            skipped += 1

        confusion[f"{stored}→{replay}"] += 1

        stored_url = ""
        for a in attempts:
            if a.stored_decision == "accepted":
                stored_url = a.url
                break

        res = ProfileReplayResult(
            profile_id=p.id,
            name=p.name,
            email=p.email,
            stored_decision=stored,
            replay_decision=replay,
            stored_url=stored_url,
            replay_details=details,
            attempts=len(attempts),
        )
        results.append(res)

        if stored != replay and {stored, replay} <= {"accepted", "rejected"}:
            flips.append(res.to_dict())

    compared = would_accept + would_reject
    integrity = {
        "total": total,
        "compared": compared,
        "would_accept": would_accept,
        "would_reject": would_reject,
        "uncompared_no_attempt": skipped,
        "sum_check_ok": (would_accept + would_reject + skipped) == total,
    }

    return {
        "config": cfg.to_dict(),
        "total": total,
        "compared": compared,
        "would_accept": would_accept,
        "would_reject": would_reject,
        "flips": flips,
        "confusion": dict(confusion),
        "integrity": integrity,
    }


def validate_roundtrip(profiles: Iterable[Profile]) -> dict[str, Any]:
    """Replay with defaults and check reproducibility.

    Only profiles where stored decision is accepted/rejected participate
    in the comparison. The reproducibility rate is reported two ways:

    - `reproducibility_rate`: matches / compared across ALL profiles
    - `reproducibility_rate_current_format`: same, restricted to profiles
      whose log was produced by the current pipeline (v1-or-later format,
      carrying `name=/positives=/penalties=` fields in ACCEPTED/REJECTED
      lines). Legacy-format logs were produced by a pre-FM2 verifier whose
      rule was `score >= 2` — they're expected to disagree with current
      code on strong-name + org-mismatch + no-positive cases and should
      not be counted against the replay's fidelity.

    Plus a breakdown of mismatch types to drive log-parse debugging.
    """
    profiles = list(profiles)
    result = replay_dataset(profiles, ReplayConfig())
    flips = result["flips"]
    compared = result["compared"]
    matches = compared - len(flips)
    rate = matches / compared if compared else 1.0

    # Per-profile legacy detection: a profile is "legacy format" if any
    # attempt carries the pre-FM2 log shape. We classify flips by whether
    # the accepted attempt (for accepted→rejected flips) used the legacy
    # shape. For stored-rejected cases, any rejected attempt's format
    # counts.
    def _is_legacy(profile: Profile) -> bool:
        for a in parse_attempts(profile):
            if a.legacy_log_format:
                return True
        return False

    profiles_by_id = {p.id: p for p in profiles}

    current_compared = 0
    current_matches = 0
    legacy_flips = 0
    current_flips: list[dict] = []
    for p in profiles:
        attempts = parse_attempts(p)
        stored = _profile_stored_decision(p, attempts)
        if stored not in ("accepted", "rejected"):
            continue
        replay, _ = _profile_replay_decision(attempts, ReplayConfig())
        if replay not in ("accepted", "rejected"):
            continue
        if _is_legacy(p):
            if stored != replay:
                legacy_flips += 1
            continue
        current_compared += 1
        if stored == replay:
            current_matches += 1
        else:
            # Find the flip record to include
            for f in flips:
                if f["profile_id"] == p.id:
                    current_flips.append(f)
                    break

    current_rate = (
        current_matches / current_compared if current_compared else 1.0
    )

    mismatch_by_type: Counter = Counter()
    for f in flips:
        mismatch_by_type[f"{f['stored_decision']}→{f['replay_decision']}"] += 1

    return {
        "compared": compared,
        "matches": matches,
        "mismatches": len(flips),
        "reproducibility_rate": round(rate, 4),
        "compared_current_format": current_compared,
        "matches_current_format": current_matches,
        "mismatches_current_format": len(current_flips),
        "legacy_flips_ignored": legacy_flips,
        "reproducibility_rate_current_format": round(current_rate, 4),
        "mismatch_by_type": dict(mismatch_by_type),
        "examples": flips[:10],
        "examples_current_format": current_flips[:10],
    }


# ────────────────────────────────────────────────────────────────────
# Loaders + CLI (mirror coverage_report.py conventions)
# ────────────────────────────────────────────────────────────────────


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


def _parse_config_overrides(pairs: list[str]) -> dict:
    out: dict = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"Bad --set value: {p!r}; expected key=value")
        k, v = p.split("=", 1)
        k = k.strip()
        v = v.strip()
        # Coerce
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enrichment.eval.replay",
        description="Offline replay of the enrichment verifier.",
    )
    parser.add_argument("--account-id", help="Supabase account_id (cloud mode)")
    parser.add_argument("--dataset-id", help="Dataset id (cloud or local filename stem)")
    parser.add_argument("--local", help="Path to a local Dataset JSON")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        help="Override a ReplayConfig field: --set name_strong_score=4",
    )
    parser.add_argument("--validate", action="store_true",
                        help="Run defaults replay and report reproducibility.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-flips", type=int, default=20)
    parser.add_argument("--out", help="Write text report here (in addition to stdout)")
    args = parser.parse_args(argv)

    if args.local:
        profiles, label = _load_local(args.local)
    elif args.account_id and args.dataset_id:
        profiles, label = _load_cloud(args.account_id, args.dataset_id)
    else:
        parser.error("Provide --local or --account-id + --dataset-id")

    overrides = _parse_config_overrides(args.overrides)
    cfg = ReplayConfig(**overrides) if overrides else ReplayConfig()

    if args.validate:
        out = validate_roundtrip(profiles)
    else:
        out = replay_dataset(profiles, cfg)

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    bar = "=" * 72
    lines = [bar, f"REPLAY — {label}", bar]
    if args.validate:
        lines.append(
            f"\nReproducibility (all logs): "
            f"{out['matches']}/{out['compared']} "
            f"({out['reproducibility_rate']*100:.2f}%)"
        )
        lines.append(
            f"Reproducibility (current-format logs only): "
            f"{out['matches_current_format']}/{out['compared_current_format']} "
            f"({out['reproducibility_rate_current_format']*100:.2f}%)"
        )
        lines.append(
            f"Legacy-format flips ignored: {out['legacy_flips_ignored']} "
            f"(pre-FM2 verifier; score>=2 was the only rule)"
        )
        lines.append(f"Mismatches by type (all): {out['mismatch_by_type']}")
        if out["examples_current_format"]:
            lines.append("\nCurrent-format mismatch examples:")
            for ex in out["examples_current_format"]:
                lines.append(f"  {ex['stored_decision']}→{ex['replay_decision']}  "
                             f"{ex['name']!r} ({ex['email']})")
                lines.append(f"    url={ex['stored_url']}  details={ex['replay_details']}")
        elif out["examples"]:
            lines.append("\nMismatch examples (all legacy):")
            for ex in out["examples"]:
                lines.append(f"  {ex['stored_decision']}→{ex['replay_decision']}  "
                             f"{ex['name']!r} ({ex['email']})")
    else:
        lines.append(f"\nConfig: {out['config']}")
        lines.append(
            f"\nwould_accept={out['would_accept']}  "
            f"would_reject={out['would_reject']}  "
            f"compared={out['compared']}  total={out['total']}"
        )
        lines.append(f"Confusion: {out['confusion']}")
        if out["flips"]:
            lines.append(f"\nFlips ({len(out['flips'])}): showing first {args.max_flips}")
            for f in out["flips"][:args.max_flips]:
                lines.append(
                    f"  {f['stored_decision']}→{f['replay_decision']}  "
                    f"{f['name']!r} {f['email']}  {f['stored_url']}"
                )

    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
