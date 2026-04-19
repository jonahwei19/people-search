"""Cost simulator for proposed enrichment pipelines.

Given a pipeline spec — a list of stages, each with a firing probability
and per-stage unit cost — produces a projected cost distribution across
a population of N profiles. Intended for pricing A/B experiments before
running them.

The simulator supports three input forms:

1. `PipelineSpec.from_current()` — the current v1 pipeline with search
   query count + LinkedIn call rate sampled from `coverage_report` on a
   real dataset. The simulator never calls the APIs itself.

2. `PipelineSpec.from_stages([...])` — an explicit list of `Stage`
   records. Each stage has a label, probability of firing, and a fixed
   per-firing unit cost (plus an optional count multiplier — e.g.
   "runs on average 1.5 queries when it fires").

3. Manual construction for exploratory what-ifs in notebooks.

Output is a JSON-able dict with per-stage expected cost, total expected
cost per profile, total expected cost for N profiles, and (if a
population cohort breakdown is passed) per-cohort totals.

Typical use
-----------

    from enrichment.eval.cost_simulator import (
        PipelineSpec, Stage, simulate,
    )

    v2 = PipelineSpec(
        name="v2 — aggressive search + LLM arbiter",
        stages=[
            Stage("email-exact",      prob=1.00, unit_cost=0.006, count=1),
            Stage("email-username",   prob=1.00, unit_cost=0.006, count=1),
            Stage("name+org",         prob=0.80, unit_cost=0.006, count=2),
            Stage("name+keywords",    prob=0.50, unit_cost=0.006, count=1),
            Stage("slug-email",       prob=0.40, unit_cost=0.006, count=1),
            Stage("linkedin-verify",  prob=0.72, unit_cost=0.0168, count=1.17),
            Stage("llm-arbiter",      prob=0.10, unit_cost=0.020, count=1),
        ],
    )
    result = simulate(v2, n_profiles=6000)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# ────────────────────────────────────────────────────────────────────
# Spec primitives
# ────────────────────────────────────────────────────────────────────


@dataclass
class Stage:
    """One stage of the enrichment pipeline.

    Fields
    ------
    label:      Human-readable identifier. Shown in the breakdown.
    prob:       Probability this stage fires at all for a given profile
                (e.g. 0.3 = fires on 30% of profiles).
    unit_cost:  Per-call USD cost (Brave+Serper combined search ≈ $0.006,
                EnrichLayer profile call = $0.0168).
    count:      Expected number of calls when the stage fires. Allows
                expressing "this stage runs ~1.5 queries on average".
    retry_multiplier:
                Multiplier on `count` to account for retries on
                transient failures. 1.0 = no retries, 1.17 mirrors the
                observed verify-failure retry rate on the v1 pipeline.
    """
    label: str
    prob: float
    unit_cost: float
    count: float = 1.0
    retry_multiplier: float = 1.0

    def expected_cost_per_profile(self) -> float:
        return self.prob * self.count * self.retry_multiplier * self.unit_cost

    def expected_calls_per_profile(self) -> float:
        return self.prob * self.count * self.retry_multiplier


@dataclass
class PipelineSpec:
    """A set of `Stage`s plus a label."""
    name: str
    stages: list[Stage] = field(default_factory=list)

    def total_cost_per_profile(self) -> float:
        return sum(s.expected_cost_per_profile() for s in self.stages)

    # ── Presets ─────────────────────────────────────────────

    @classmethod
    def from_current(cls) -> PipelineSpec:
        """Current v1 pipeline (as observed in the Allan/BlueDot runs).

        Probabilities and counts are drawn from plans/diagnosis_hitrate.md:
        - Search runs across all search-needed profiles (~30% of pool).
        - EnrichLayer runs on ~89% of profiles (preset or resolved URL).
        - Retries: 1.17x average on verify failures.
        """
        return cls(
            name="v1 — current",
            stages=[
                # Avg search queries per search-needed profile ≈ 7
                # (email-exact, email-username, name+org, name+title,
                # name+location, name+org-broad, slug-email)
                Stage("search-queries",    prob=0.30, unit_cost=0.006,
                      count=7.0),
                Stage("linkedin-enrich",   prob=0.89, unit_cost=0.0168,
                      count=1.0, retry_multiplier=1.17),
            ],
        )

    @classmethod
    def from_stages(cls, name: str, stages: list[dict]) -> PipelineSpec:
        return cls(name=name, stages=[Stage(**s) for s in stages])


# ────────────────────────────────────────────────────────────────────
# Simulation
# ────────────────────────────────────────────────────────────────────


def simulate(
    spec: PipelineSpec,
    n_profiles: int,
    *,
    cohort_shares: dict[str, float] | None = None,
    cohort_cost_multipliers: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compute expected costs for a population.

    Parameters
    ----------
    spec:           The pipeline spec to price.
    n_profiles:     Population size (e.g. 6000 pending profiles).
    cohort_shares:  Optional {cohort: share} mapping (shares should sum to
                    ~1.0). Lets you break the population into buckets
                    (e.g. {"corp": 0.5, "personal": 0.3, "edu": 0.1,
                    "gov": 0.1}) and multiply per-cohort cost.
    cohort_cost_multipliers:
                    Per-cohort multiplier on total pipeline cost. Lets you
                    express "personal-email cohort runs 2x more queries
                    because name+org fails first". Defaults to 1.0.

    Returns
    -------
    Dict with per-stage expected cost, total per profile, total for
    population, and per-cohort breakdown if cohort_shares is given.
    """
    per_profile = spec.total_cost_per_profile()
    total = per_profile * n_profiles

    per_stage = []
    for s in spec.stages:
        per_stage.append({
            "label": s.label,
            "prob": s.prob,
            "count": s.count,
            "retry_multiplier": s.retry_multiplier,
            "unit_cost": s.unit_cost,
            "expected_cost_per_profile": round(s.expected_cost_per_profile(), 6),
            "expected_calls_per_profile": round(s.expected_calls_per_profile(), 4),
            "total_cost_for_population": round(
                s.expected_cost_per_profile() * n_profiles, 2
            ),
        })

    cohort_breakdown: list[dict] = []
    if cohort_shares:
        mult = cohort_cost_multipliers or {}
        share_sum = sum(cohort_shares.values())
        if abs(share_sum - 1.0) > 0.01:
            # Warn but proceed; the user may have meant relative weights.
            pass
        for cohort, share in cohort_shares.items():
            m = mult.get(cohort, 1.0)
            n_cohort = n_profiles * share
            cost_cohort = per_profile * m * n_cohort
            cohort_breakdown.append({
                "cohort": cohort,
                "share": share,
                "n_profiles": round(n_cohort, 1),
                "cost_multiplier": m,
                "total_cost": round(cost_cohort, 2),
                "per_profile_cost": round(per_profile * m, 4),
            })

    # Confidence band: assume ±20% on each probability. This isn't a
    # rigorous MC simulation — it's a "how wide is the uncertainty"
    # heuristic. Caller can override with `bands`.
    low = sum(
        s.prob * 0.8 * s.count * s.retry_multiplier * s.unit_cost
        for s in spec.stages
    ) * n_profiles
    high = sum(
        s.prob * 1.2 * s.count * s.retry_multiplier * s.unit_cost
        for s in spec.stages
    ) * n_profiles

    return {
        "spec": {
            "name": spec.name,
            "stages": [asdict(s) for s in spec.stages],
        },
        "n_profiles": n_profiles,
        "per_profile_cost_usd": round(per_profile, 6),
        "total_cost_usd": round(total, 2),
        "confidence_band_usd": {
            "p20_low": round(low, 2),
            "p80_high": round(high, 2),
            "method": "±20% perturbation on each stage's firing probability",
        },
        "per_stage_breakdown": per_stage,
        "cohort_breakdown": cohort_breakdown,
    }


def compare(specs: list[PipelineSpec], n_profiles: int) -> dict[str, Any]:
    """Price multiple specs side-by-side against the same population."""
    rows = [simulate(s, n_profiles) for s in specs]
    return {
        "n_profiles": n_profiles,
        "comparison": [
            {
                "name": r["spec"]["name"],
                "per_profile_cost_usd": r["per_profile_cost_usd"],
                "total_cost_usd": r["total_cost_usd"],
                "band_low": r["confidence_band_usd"]["p20_low"],
                "band_high": r["confidence_band_usd"]["p80_high"],
            }
            for r in rows
        ],
        "details": rows,
    }


# ────────────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────────────


def format_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    bar = "=" * 72
    lines.append(bar)
    lines.append(f"COST SIMULATOR — {result['spec']['name']}")
    lines.append(bar)
    lines.append(f"\nPopulation: {result['n_profiles']} profiles")
    lines.append(f"Per-profile cost: ${result['per_profile_cost_usd']:.4f}")
    lines.append(f"Total: ${result['total_cost_usd']:.2f}")
    band = result["confidence_band_usd"]
    lines.append(f"Confidence band: ${band['p20_low']:.2f} – ${band['p80_high']:.2f}")
    lines.append(f"  ({band['method']})")

    lines.append("\nPer-stage breakdown:")
    lines.append(f"  {'label':<24} {'prob':>6} {'count':>6} {'retry':>6} "
                 f"{'unit$':>8} {'$/profile':>10} {'total':>10}")
    for s in result["per_stage_breakdown"]:
        lines.append(
            f"  {s['label']:<24} {s['prob']:>6.2f} {s['count']:>6.2f} "
            f"{s['retry_multiplier']:>6.2f} ${s['unit_cost']:>6.4f} "
            f"${s['expected_cost_per_profile']:>9.5f} "
            f"${s['total_cost_for_population']:>9.2f}"
        )

    if result["cohort_breakdown"]:
        lines.append("\nCohort breakdown:")
        lines.append(
            f"  {'cohort':<24} {'share':>6} {'N':>6} "
            f"{'mult':>6} {'$/profile':>10} {'total':>10}"
        )
        for c in result["cohort_breakdown"]:
            lines.append(
                f"  {c['cohort']:<24} {c['share']:>6.2f} {c['n_profiles']:>6.0f} "
                f"{c['cost_multiplier']:>6.2f} ${c['per_profile_cost']:>9.4f} "
                f"${c['total_cost']:>9.2f}"
            )

    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enrichment.eval.cost_simulator",
        description="Price a proposed pipeline spec for N profiles.",
    )
    parser.add_argument(
        "--spec",
        help="Path to a JSON spec: {name: str, stages: [{label, prob, "
             "unit_cost, count, retry_multiplier}, ...]}. If omitted, uses "
             "`PipelineSpec.from_current()`.",
    )
    parser.add_argument("--n", type=int, default=1000, help="Population size")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    if args.spec:
        with open(args.spec) as f:
            spec_data = json.load(f)
        spec = PipelineSpec.from_stages(
            spec_data.get("name", "custom"), spec_data.get("stages", [])
        )
    else:
        spec = PipelineSpec.from_current()

    result = simulate(spec, args.n)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        text = format_text(result)
        print(text)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
