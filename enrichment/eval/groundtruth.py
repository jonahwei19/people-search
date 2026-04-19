"""Ground-truth loader + metrics for enrichment evaluation.

Usage:
    >>> gt = load_groundtruth("plans/groundtruth_c773996b.csv")
    >>> metrics = score_against(profiles, gt)
    >>> print(metrics["linkedin_precision"], metrics["linkedin_recall"])

The ground-truth CSV is produced by `tools/export_groundtruth_sample.py`
and hand-filled. Each row pins what the enrichment *should* have produced
for a given profile_id.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GroundTruthEntry:
    profile_id: str
    uploaded_name: str
    email: str
    true_linkedin_url: str = ""
    true_website_url: str = ""
    true_twitter_url: str = ""
    true_is_hidden: bool = False
    notes: str = ""


def _norm_url(u: str) -> str:
    u = (u or "").strip().lower().rstrip("/")
    # Strip common LinkedIn URL noise
    if "linkedin.com" in u:
        u = u.split("?")[0]
    return u


def load_groundtruth(path: str | Path) -> list[GroundTruthEntry]:
    """Parse a hand-labeled ground-truth CSV."""
    entries: list[GroundTruthEntry] = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = (row.get("profile_id") or "").strip()
            if not pid:
                continue
            entries.append(
                GroundTruthEntry(
                    profile_id=pid,
                    uploaded_name=row.get("uploaded_name", ""),
                    email=row.get("email", ""),
                    true_linkedin_url=_norm_url(row.get("true_linkedin_url", "")),
                    true_website_url=_norm_url(row.get("true_website_url", "")),
                    true_twitter_url=_norm_url(row.get("true_twitter_url", "")),
                    true_is_hidden=(row.get("true_is_hidden", "").strip().lower() in ("true", "1", "yes")),
                    notes=row.get("notes", ""),
                )
            )
    return entries


def score_against(profiles: list, gt: list[GroundTruthEntry]) -> dict:
    """Compute precision/recall/F1 for LinkedIn/website/twitter vs ground truth.

    Only scores profiles that appear in both sides (matched by profile_id).
    A TP = we produced the same URL the ground-truth says. FP = we produced
    a URL but it differs. FN = ground-truth has a URL but we produced none.
    TN = both agree "no URL".
    """
    gt_by_id = {e.profile_id: e for e in gt}
    prof_by_id = {p.id: p for p in profiles}

    common_ids = set(gt_by_id) & set(prof_by_id)
    metrics: dict = {"matched_profiles": len(common_ids)}

    for field, pred_attr, true_attr in [
        ("linkedin", "linkedin_url", "true_linkedin_url"),
        ("website", "website_url", "true_website_url"),
        ("twitter", "twitter_url", "true_twitter_url"),
    ]:
        tp = fp = fn = tn = 0
        correct = wrong = 0
        for pid in common_ids:
            p = prof_by_id[pid]
            g = gt_by_id[pid]
            pred = _norm_url(getattr(p, pred_attr, "") or "")
            truth = _norm_url(getattr(g, true_attr, "") or "")
            if truth and pred:
                if truth == pred:
                    tp += 1
                    correct += 1
                else:
                    fp += 1
                    wrong += 1
                    fn += 1
            elif truth and not pred:
                fn += 1
            elif pred and not truth:
                fp += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        metrics[f"{field}_tp"] = tp
        metrics[f"{field}_fp"] = fp
        metrics[f"{field}_fn"] = fn
        metrics[f"{field}_tn"] = tn
        metrics[f"{field}_precision"] = round(precision, 3)
        metrics[f"{field}_recall"] = round(recall, 3)
        metrics[f"{field}_f1"] = round(f1, 3)
        metrics[f"{field}_wrong_person"] = wrong

    # Hidden-label accuracy
    hidden_correct = hidden_total = 0
    for pid in common_ids:
        p = prof_by_id[pid]
        g = gt_by_id[pid]
        hidden_total += 1
        pred_hidden = (
            not getattr(p, "linkedin_url", "")
            and not getattr(p, "website_url", "")
            and not getattr(p, "twitter_url", "")
        )
        if pred_hidden == g.true_is_hidden:
            hidden_correct += 1
    metrics["hidden_accuracy"] = round(hidden_correct / hidden_total, 3) if hidden_total else 0.0

    return metrics


def format_metrics(m: dict) -> str:
    out = [f"Matched profiles: {m['matched_profiles']}"]
    for field in ("linkedin", "website", "twitter"):
        out.append(
            f"  {field:10s} P={m[f'{field}_precision']:.2f} "
            f"R={m[f'{field}_recall']:.2f} F1={m[f'{field}_f1']:.2f} "
            f"TP={m[f'{field}_tp']} FP={m[f'{field}_fp']} FN={m[f'{field}_fn']} "
            f"wrong_person={m[f'{field}_wrong_person']}"
        )
    out.append(f"  hidden-label accuracy: {m['hidden_accuracy']:.2f}")
    return "\n".join(out)
