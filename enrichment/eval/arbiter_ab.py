"""A/B harness for the Gemini identity arbiter.

For each profile in a given dataset that was previously marked SKIPPED due
to an ambiguous candidate tie (see `_score_candidates` in
`enrichment/identity.py`), this script:

  1. Parses the stored `enrichment_log` to reconstruct the candidate pool
     (URL + score + reasons) that the heuristic saw.
  2. Records what the heuristic chose (usually: skip — "ambiguous, refusing
     to guess").
  3. Invokes the Gemini arbiter on the tied candidates.
  4. Reports what the arbiter chose vs. what the heuristic chose.

Usage:
    python -m enrichment.eval.arbiter_ab \\
        --account-id 4cff802c-ac7d-4836-8d50-5d5c1e31962e \\
        --dataset-id c773996b --limit 10

Requires GOOGLE_API_KEY + SUPABASE_* env vars (or a .env at project root).

Cost: < $0.001 per arbiter call × 10 profiles = pennies.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if p.exists():
            load_dotenv(str(p))
            break
except ImportError:
    pass

from cloud.storage.supabase import SupabaseStorage  # noqa: E402
from enrichment.arbiter import arbitrate_identity  # noqa: E402
from enrichment.models import Profile  # noqa: E402


# Match lines like "  [ 23] https://.../xyz  (first-in-title(+2), ...)"
_CAND_LINE = re.compile(
    r"\s*\[\s*(?P<score>-?\d+)\]\s+(?P<url>https?://\S+)\s+\((?P<reasons>.*)\)\s*$"
)
_TIE_SUMMARY = re.compile(r"(\d+)\s+candidates tied at score\s+(-?\d+)")


def _extract_candidates(profile: Profile) -> list[dict]:
    """Pull the scored candidate list out of a profile's enrichment_log."""
    candidates: list[dict] = []
    for line in profile.enrichment_log or []:
        m = _CAND_LINE.match(str(line))
        if not m:
            continue
        candidates.append({
            "score": int(m.group("score")),
            "url": m.group("url").rstrip("/"),
            "reasons": [r.strip() for r in m.group("reasons").split(",")],
        })
    # Sort by score descending
    candidates.sort(key=lambda c: -c["score"])
    return candidates


def _tie_signature(profile: Profile) -> tuple[int, int] | None:
    """Return (tie_count, tie_score) if this profile was skipped for tie."""
    joined = "\n".join(str(l) for l in (profile.enrichment_log or []))
    m = _TIE_SUMMARY.search(joined)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _run_one(profile: Profile) -> dict:
    """Reconstruct the arbiter input for this profile and call Gemini."""
    cands = _extract_candidates(profile)
    tie = _tie_signature(profile)

    out = {
        "profile_id": profile.id,
        "name": profile.name,
        "email": profile.email,
        "org": profile.organization,
        "heuristic_pick": None,  # None = heuristic rejected/ambiguous
        "heuristic_reason": "heuristic ambiguous tie",
        "tie_count": tie[0] if tie else None,
        "tie_score": tie[1] if tie else None,
        "arbiter_pick_index": None,
        "arbiter_pick_url": None,
        "arbiter_confidence": None,
        "arbiter_reason": None,
        "arbiter_error": None,
    }

    if len(cands) < 2:
        out["arbiter_reason"] = f"skipped: only {len(cands)} candidates parseable"
        return out

    # Pass top 5 candidates — the arbiter handles the cap too, but we trim
    # here so the logged A/B row is clear about what we showed it.
    top = cands[:5]
    arbiter_input = [
        {
            "index": i,
            "url": c["url"],
            "title": "",  # Title/description weren't persisted to the log
            "description": "",
            "score": c["score"],
            "reasons": c["reasons"],
        }
        for i, c in enumerate(top)
    ]

    decision = arbitrate_identity(profile, arbiter_input)

    out["arbiter_pick_index"] = decision.get("winner_index")
    out["arbiter_confidence"] = decision.get("confidence")
    out["arbiter_reason"] = decision.get("reason")
    if decision.get("error"):
        out["arbiter_error"] = decision.get("reason")
    idx = decision.get("winner_index")
    if idx is not None:
        try:
            i = int(idx)
            if 0 <= i < len(top):
                out["arbiter_pick_url"] = top[i]["url"]
        except (TypeError, ValueError):
            pass
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m enrichment.eval.arbiter_ab")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY", file=sys.stderr)
        return 1

    storage = SupabaseStorage(url, key, args.account_id)
    ds = storage.load_dataset(args.dataset_id)

    # Pick profiles that (a) have a tie signature in the log AND
    # (b) ended up SKIPPED. Profiles where the heuristic got tied but
    # eventually resolved another URL on retry are not interesting here.
    ambiguous = [
        p for p in ds.profiles
        if _tie_signature(p) is not None
        and getattr(p.enrichment_status, "value", "") == "skipped"
    ]
    print(f"Dataset {args.dataset_id}: {len(ds.profiles)} profiles, {len(ambiguous)} ambiguous-ties (skipped)")
    ambiguous = ambiguous[: args.limit]

    rows: list[dict] = []
    for p in ambiguous:
        print(f"  → arbiter on {p.id} {p.name!r} ({p.email})")
        row = _run_one(p)
        rows.append(row)

    if args.json:
        import json
        print(json.dumps(rows, indent=2))
    else:
        # Pretty print
        print("\n=== A/B RESULTS ===")
        for r in rows:
            hd = "HEURISTIC" if r["heuristic_pick"] else "heuristic: (skipped)"
            ar = (
                f"arbiter → {r['arbiter_pick_url']} ({r['arbiter_confidence']})"
                if r["arbiter_pick_url"]
                else f"arbiter abstained ({r['arbiter_confidence']})"
            )
            print(f"\n{r['profile_id']} {r['name']!r} / {r['email']}")
            print(f"  tie: {r['tie_count']} candidates @ score {r['tie_score']}")
            print(f"  {hd} vs {ar}")
            if r["arbiter_reason"]:
                print(f"  arbiter reason: {r['arbiter_reason']}")

        # Summary
        n_picked = sum(1 for r in rows if r["arbiter_pick_url"])
        n_abstain = sum(1 for r in rows if r["arbiter_pick_index"] is None and not r["arbiter_error"])
        n_error = sum(1 for r in rows if r["arbiter_error"])
        print("\n=== SUMMARY ===")
        print(f"  profiles tested:       {len(rows)}")
        print(f"  arbiter picked winner: {n_picked}")
        print(f"  arbiter abstained:     {n_abstain}")
        print(f"  arbiter errored:       {n_error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
