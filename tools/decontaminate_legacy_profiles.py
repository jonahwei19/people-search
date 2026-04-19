"""Decontaminate v0-legacy profiles whose organization/title were overwritten
by the buggy backfill in the legacy enrichment pipeline.

Background
----------
Before the FM5 fix (see plans/diagnosis_correctness.md), the enricher silently
overwrote `profiles.organization` and `profiles.title` with whatever LinkedIn
returned — even when the LinkedIn match was the wrong person. The fix in
`enrichers.py` now writes the LinkedIn values to `enriched_organization` /
`enriched_title` instead, but legacy rows (`enrichment_version = 'v0-legacy'`)
already have contaminated fields in the DB. This script cleans them up.

Behavior per enriched v0-legacy row
-----------------------------------
1. Classify the row:
     - "wrong-person" if it appears in the wrong_person_audit suspect list
       (different last name etc.)
     - "clean" otherwise
2. Decide the fix for organization / title:
     - If the source CSV is locally available and we can match by email/name,
       restore the user-provided value.
     - Else if `organization` equals `linkedin_enriched.current_company`
       (case-insensitive, trimmed), this is *backfill evidence* — the field
       was overwritten. If the row is a wrong-person suspect, BLANK it.
       If the row is not a suspect, leave it (the LinkedIn company is the
       same as the user's upload, so the value is fine — and we'll still
       populate the shadow field from enrichment below).
     - Else the field appears to be user-provided (differs from LinkedIn):
       LEAVE IT alone.
3. Populate the shadow fields `enriched_organization` / `enriched_title`
   from `linkedin_enriched.current_company` / `current_title` when empty,
   so downstream code has them available retroactively.

All decisions are logged to stderr and the script is idempotent — re-running
after a partial run only touches rows that still need work.

Usage
-----
    # Dry run on Allan's dataset (recommended first step):
    python -m tools.decontaminate_legacy_profiles \
        --account-id <allan-uuid> --dataset-id c773996b --dry-run

    # Apply for real:
    python -m tools.decontaminate_legacy_profiles \
        --account-id <allan-uuid> --dataset-id c773996b

    # Limit to a small sample:
    python -m tools.decontaminate_legacy_profiles \
        --account-id <allan-uuid> --limit 20 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

from enrichment.eval.wrong_person_audit import audit_profile  # noqa: E402
from enrichment.models import EnrichmentStatus, Profile  # noqa: E402


# ─────────────────────────── helpers ───────────────────────────


def _norm(s: str) -> str:
    """Normalize for case/diacritic/whitespace-insensitive comparison."""
    if not s:
        return ""
    n = unicodedata.normalize("NFKD", s)
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", n).strip().lower()


def _norm_email(s: str) -> str:
    return _norm(s)


def _same(a: str, b: str) -> bool:
    """Case/whitespace/diacritic-insensitive equality after trimming."""
    a = _norm(a)
    b = _norm(b)
    return bool(a) and a == b


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ─────────────────────── source-file loader ────────────────────


@dataclass
class SourceRow:
    """User-provided row from the original upload, keyed by email/name."""

    email: str = ""
    name: str = ""
    organization: str = ""
    title: str = ""


def _load_source_rows(csv_path: Path) -> list[SourceRow]:
    """Read a source CSV and extract email/name/org/title columns."""
    rows: list[SourceRow] = []
    try:
        with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            # Heuristically detect the relevant columns. We check every header
            # and pick the first that matches a known pattern.
            fieldnames = [h.strip() for h in (reader.fieldnames or [])]
            email_col = _pick(fieldnames, ("email", "e-mail", "emailaddress"))
            name_col = _pick(fieldnames, ("full name", "name", "fullname"))
            first_col = _pick(fieldnames, ("first name", "firstname"))
            last_col = _pick(fieldnames, ("last name", "lastname", "surname"))
            org_col = _pick(
                fieldnames,
                (
                    "organization",
                    "organisation",
                    "org",
                    "company",
                    "employer",
                    "current company",
                    "current organization",
                ),
            )
            title_col = _pick(
                fieldnames,
                (
                    "title",
                    "job title",
                    "position",
                    "role",
                    "current title",
                    "current role",
                ),
            )

            for r in reader:
                email = (r.get(email_col, "") or "").strip() if email_col else ""
                name = (r.get(name_col, "") or "").strip() if name_col else ""
                if not name and first_col and last_col:
                    name = f"{(r.get(first_col) or '').strip()} {(r.get(last_col) or '').strip()}".strip()
                org = (r.get(org_col, "") or "").strip() if org_col else ""
                title = (r.get(title_col, "") or "").strip() if title_col else ""
                if not (email or name):
                    continue
                rows.append(SourceRow(email=email, name=name, organization=org, title=title))
    except FileNotFoundError:
        return []
    except Exception as e:  # pragma: no cover — best-effort loader
        _log(f"[source-load] Failed to parse {csv_path}: {e}")
        return []
    return rows


def _pick(headers: list[str], patterns: tuple[str, ...]) -> str | None:
    """Return the original header that best matches any of the lowercase patterns."""
    for h in headers:
        hl = h.strip().lower()
        for p in patterns:
            if hl == p or p in hl:
                return h
    return None


def _index_source_rows(rows: list[SourceRow]) -> tuple[dict[str, SourceRow], dict[str, SourceRow]]:
    """Build lookup indexes: by email (lowercase) and by normalized name."""
    by_email: dict[str, SourceRow] = {}
    by_name: dict[str, SourceRow] = {}
    for r in rows:
        e = _norm_email(r.email)
        if e and e not in by_email:
            by_email[e] = r
        n = _norm(r.name)
        if n and n not in by_name:
            by_name[n] = r
    return by_email, by_name


def _candidate_source_paths(dataset_id: str, source_file: str) -> list[Path]:
    """Return filesystem candidates where the original upload may live.

    `source_file` may be:
      - empty
      - an absolute path (may or may not still exist)
      - a bare basename
      - a path relative to the project root
    We also look in `uploads/` for any file whose name starts with the dataset id.
    """
    candidates: list[Path] = []
    if source_file:
        p = Path(source_file)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(_root / p)
            candidates.append(Path.cwd() / p)
            candidates.append(_root / "uploads" / p.name)
    uploads = _root / "uploads"
    if uploads.is_dir():
        # Uploads are typically named "<dataset-prefix>_<original>.csv".
        prefix = dataset_id[:8]
        for entry in sorted(uploads.iterdir()):
            if entry.name.startswith(prefix + "_"):
                candidates.append(entry)
    # Deduplicate while preserving order
    seen: set[Path] = set()
    unique: list[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except Exception:
            resolved = c
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(c)
    return unique


# ───────────────────────── decontamination core ─────────────────


@dataclass
class Counts:
    scanned: int = 0
    wrong_person_fixed: int = 0
    wrong_person_restored_from_source: int = 0
    wrong_person_blanked: int = 0
    shadow_field_populated: int = 0
    backfill_cleared_nonsuspect: int = 0
    skipped_uncertain: int = 0
    already_clean: int = 0

    def to_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _decide_fix_for_field(
    *,
    current_value: str,
    enriched_value: str,
    is_suspect: bool,
    source_value: str | None,
) -> tuple[str | None, str]:
    """Decide what to do with one field (organization or title).

    Returns `(new_value, reason)` where `new_value` is:
      - None    -> leave as-is
      - "<x>"   -> set the field to x (may be the empty string to blank it)
    """
    cv = current_value or ""
    ev = enriched_value or ""

    # If we have a source CSV value, prefer it whenever it differs from current.
    if source_value is not None:
        sv = source_value.strip()
        if sv and not _same(sv, cv):
            return sv, "restored-from-source"
        if sv and _same(sv, cv):
            return None, "matches-source-already"
        if not sv:
            # Source said the field was blank. If current equals the LinkedIn
            # value, it was backfilled; restore blank. Otherwise leave.
            if cv and _same(cv, ev):
                return "" if is_suspect else None, "source-blank-current-matches-enriched"
            return None, "source-blank-current-differs"

    # No source available. Use backfill heuristic.
    if cv and ev and _same(cv, ev):
        # Current value matches LinkedIn verbatim — almost certainly written
        # by the buggy backfill. If the row is a suspect, blank it.
        if is_suspect:
            return "", "blanked-suspect-matches-enriched"
        # Not a suspect: the LinkedIn match looks correct, so even though the
        # field was copied from enrichment it's not "wrong" data. Leave it.
        return None, "nonsuspect-matches-enriched-keep"

    if not cv and ev and is_suspect:
        # Already empty, nothing to blank. No-op.
        return None, "already-empty-suspect"

    # cv differs from ev: the user's original upload value likely survived
    # (backfill only ran when the field was empty OR was triggered before
    # our conservative branch). Don't touch.
    return None, "unchanged-differs-from-enriched"


def decontaminate_profile(
    profile: Profile,
    *,
    is_suspect: bool,
    source_row: SourceRow | None,
    counts: Counts,
) -> dict[str, Any] | None:
    """Decide what changes to apply to a single profile.

    Returns a dict of column -> new value to update, or None if nothing changes.
    Mutates `counts` in place for bookkeeping.
    """
    counts.scanned += 1

    enriched = profile.linkedin_enriched or {}
    enriched_org = (enriched.get("current_company") or "").strip()
    enriched_title = (enriched.get("current_title") or "").strip()

    updates: dict[str, Any] = {}

    # Shadow-field population (always safe; idempotent because we only fill
    # when the shadow column is currently empty).
    if enriched_org and not (profile.enriched_organization or "").strip():
        updates["enriched_organization"] = enriched_org
    if enriched_title and not (profile.enriched_title or "").strip():
        updates["enriched_title"] = enriched_title
    shadow_filled = bool(updates)

    # Field-restoration/blanking decisions.
    src_org = source_row.organization if source_row else None
    src_title = source_row.title if source_row else None

    new_org, org_reason = _decide_fix_for_field(
        current_value=profile.organization or "",
        enriched_value=enriched_org,
        is_suspect=is_suspect,
        source_value=src_org,
    )
    new_title, title_reason = _decide_fix_for_field(
        current_value=profile.title or "",
        enriched_value=enriched_title,
        is_suspect=is_suspect,
        source_value=src_title,
    )

    touched_primary = False
    if new_org is not None and new_org != (profile.organization or ""):
        updates["organization"] = new_org
        touched_primary = True
    if new_title is not None and new_title != (profile.title or ""):
        updates["title"] = new_title
        touched_primary = True

    # Count buckets.
    tag = "suspect" if is_suspect else "nonsuspect"
    _log(
        f"[decide] {profile.id} {tag} "
        f"org:{org_reason!r} title:{title_reason!r} "
        f"shadow={'yes' if shadow_filled else 'no'} "
        f"touched={'yes' if touched_primary else 'no'}"
    )

    if is_suspect:
        if touched_primary:
            counts.wrong_person_fixed += 1
            if any(r.startswith("restored-from-source") for r in (org_reason, title_reason)):
                counts.wrong_person_restored_from_source += 1
            else:
                counts.wrong_person_blanked += 1
        else:
            counts.skipped_uncertain += 1
    else:
        if touched_primary:
            # Non-suspect primary touch can happen when the source CSV disagrees
            # with the current value (real rename or user-provided update).
            counts.backfill_cleared_nonsuspect += 1
        else:
            counts.already_clean += 1

    if shadow_filled:
        counts.shadow_field_populated += 1

    if not updates:
        return None
    return updates


# ─────────────────────────── driver ────────────────────────────


def _load_env() -> None:
    if load_dotenv is None:
        return
    for p in (_root / ".env", Path.cwd() / ".env"):
        if p.exists():
            load_dotenv(str(p))
            break


def _supabase_client():
    _load_env()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY must be set")
    from supabase import create_client

    return create_client(url, key)


def _fetch_legacy_profiles(
    client, *, account_id: str, dataset_id: str | None, limit: int | None
) -> list[Profile]:
    """Pull v0-legacy enriched profiles for the account (optionally one dataset)."""
    from cloud.storage.supabase import SupabaseStorage

    storage = SupabaseStorage(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"], account_id
    )

    profiles: list[Profile] = []
    offset = 0
    page = 1000
    # Three cohorts to decontaminate:
    #   (v0-legacy, enriched)  — classic contaminated backfills
    #   (v0-legacy, failed/skipped) — unclear if these have user-provided org
    #   (v1, failed)           — re-enriched under fixed code; LinkedIn was
    #                            rejected, but the PRIOR bad run already wrote
    #                            org/title from a wrong-person match
    # Anything with a non-empty linkedin_enriched hints the old backfill ran.
    while True:
        q = (
            client.table("profiles")
            .select("*")
            .eq("account_id", account_id)
            .in_("enrichment_status", ["enriched", "failed", "skipped"])
            .range(offset, offset + page - 1)
        )
        if dataset_id:
            q = q.eq("dataset_id", dataset_id)
        response = q.execute()
        for row in response.data:
            ver = row.get("enrichment_version") or ""
            status = row.get("enrichment_status") or ""
            # In-scope:
            #   v0-legacy + enriched (classic)
            #   any version + failed WITH a linkedin_enriched that wrote org
            if ver == "v0-legacy" and status == "enriched":
                pass  # include
            elif status in ("failed", "skipped") and (row.get("organization") or row.get("title")):
                # Only include if there's evidence the org/title came from LinkedIn backfill
                le = row.get("linkedin_enriched") or {}
                if isinstance(le, str):
                    try:
                        import json as _json
                        le = _json.loads(le)
                    except Exception:
                        le = {}
                lcomp = (le.get("current_company") or "").strip()
                lttl = (le.get("current_title") or "").strip()
                org = (row.get("organization") or "").strip()
                ttl = (row.get("title") or "").strip()
                # Only include if DB org/title matches what LinkedIn said
                # (i.e. was almost certainly contaminated by backfill).
                if not ((lcomp and lcomp == org) or (lttl and lttl == ttl)):
                    continue
            else:
                continue
            profiles.append(storage._row_to_profile(row))
        if len(response.data) < page:
            break
        offset += page
        if limit and len(profiles) >= limit:
            break
    if limit:
        profiles = profiles[:limit]
    return profiles


def _dataset_source_paths_by_id(client, account_id: str) -> dict[str, str]:
    """Map dataset_id -> source_file for the account."""
    resp = (
        client.table("datasets")
        .select("id, source_file")
        .eq("account_id", account_id)
        .execute()
    )
    return {r["id"]: (r.get("source_file") or "") for r in resp.data}


def _profile_dataset_ids(client, account_id: str, profile_ids: list[str]) -> dict[str, str]:
    """Map profile_id -> dataset_id, in chunks to keep the IN filter small."""
    mapping: dict[str, str] = {}
    for i in range(0, len(profile_ids), 500):
        chunk = profile_ids[i : i + 500]
        resp = (
            client.table("profiles")
            .select("id, dataset_id")
            .eq("account_id", account_id)
            .in_("id", chunk)
            .execute()
        )
        for r in resp.data:
            mapping[r["id"]] = r.get("dataset_id") or ""
    return mapping


def _resolve_source_for_dataset(
    dataset_id: str, source_file: str
) -> tuple[dict[str, SourceRow], dict[str, SourceRow], Path | None]:
    """Try every candidate path; return (by_email, by_name, path_used)."""
    for path in _candidate_source_paths(dataset_id, source_file):
        if path.exists() and path.is_file():
            rows = _load_source_rows(path)
            if rows:
                by_email, by_name = _index_source_rows(rows)
                _log(f"[source] dataset={dataset_id} using {path} ({len(rows)} rows)")
                return by_email, by_name, path
    _log(f"[source] dataset={dataset_id} no source file available (source_file={source_file!r})")
    return {}, {}, None


def _lookup_source_row(
    profile: Profile,
    *,
    by_email: dict[str, SourceRow],
    by_name: dict[str, SourceRow],
) -> SourceRow | None:
    e = _norm_email(profile.email)
    if e and e in by_email:
        return by_email[e]
    n = _norm(profile.name)
    if n and n in by_name:
        return by_name[n]
    return None


def run(
    *,
    account_id: str | None,
    dataset_id: str | None,
    dry_run: bool,
    limit: int | None,
    local_path: str | None = None,
) -> dict[str, Any]:
    _load_env()
    if local_path:
        # Offline mode: load a Dataset JSON, apply the same decisions, and
        # print them. Never writes anywhere. Useful for testing the policy
        # on exported data without touching Supabase.
        return _run_local(local_path=local_path, limit=limit)

    if not account_id:
        raise RuntimeError("--account-id is required when --local is not provided")

    client = _supabase_client()

    profiles = _fetch_legacy_profiles(
        client, account_id=account_id, dataset_id=dataset_id, limit=limit
    )
    _log(
        f"[scan] account={account_id} dataset={dataset_id or '(all)'} "
        f"v0-legacy enriched rows: {len(profiles)}"
    )

    # Build the suspect set from the wrong-person audit (conservative — only
    # flag "strong" suspects, i.e. different last name with full uploaded name,
    # mirroring what the audit surfaces for human review).
    suspect_ids: set[str] = set()
    for p in profiles:
        flag = audit_profile(p)
        if flag and "single_token_uploaded_name" not in (flag.get("flags") or []):
            suspect_ids.add(p.id)
    _log(f"[audit] strong wrong-person suspects in pool: {len(suspect_ids)}")

    # Load source CSVs (one per dataset).
    dataset_ids = sorted({pp.id: pp for pp in profiles}.keys())  # unique profile ids
    prof_ds = _profile_dataset_ids(client, account_id, dataset_ids)
    ds_sources = _dataset_source_paths_by_id(client, account_id)

    per_dataset_source_cache: dict[str, tuple[dict[str, SourceRow], dict[str, SourceRow], Path | None]] = {}

    counts = Counts()
    sample_diff: list[dict[str, Any]] = []

    for profile in profiles:
        ds_id = prof_ds.get(profile.id, "")
        if ds_id not in per_dataset_source_cache:
            per_dataset_source_cache[ds_id] = _resolve_source_for_dataset(
                ds_id, ds_sources.get(ds_id, "")
            )
        by_email, by_name, _path = per_dataset_source_cache[ds_id]
        source_row = _lookup_source_row(profile, by_email=by_email, by_name=by_name)

        is_suspect = profile.id in suspect_ids
        updates = decontaminate_profile(
            profile, is_suspect=is_suspect, source_row=source_row, counts=counts
        )

        if updates is None:
            continue

        before_after = {
            "profile_id": profile.id,
            "email": profile.email,
            "name": profile.name,
            "is_suspect": is_suspect,
            "before": {
                "organization": profile.organization,
                "title": profile.title,
                "enriched_organization": profile.enriched_organization,
                "enriched_title": profile.enriched_title,
            },
            "after": {
                **{k: v for k, v in updates.items() if k in {"organization", "title", "enriched_organization", "enriched_title"}},
            },
        }
        if len(sample_diff) < 25:
            sample_diff.append(before_after)

        if dry_run:
            continue

        (
            client.table("profiles")
            .update(updates)
            .eq("id", profile.id)
            .eq("account_id", account_id)
            .execute()
        )

    return {
        "account_id": account_id,
        "dataset_id": dataset_id,
        "dry_run": dry_run,
        "counts": counts.to_dict(),
        "sample_diff": sample_diff,
    }


def _run_local(*, local_path: str, limit: int | None) -> dict[str, Any]:
    """Offline dry run against a local Dataset JSON (never writes)."""
    from enrichment.models import Dataset

    ds = Dataset.load(Path(local_path))
    profiles = [
        p
        for p in ds.profiles
        if p.enrichment_status == EnrichmentStatus.ENRICHED
        and (p.enrichment_version == "v0-legacy" or not p.enrichment_version)
    ]
    if limit:
        profiles = profiles[:limit]
    _log(f"[scan] local {local_path} v0-legacy enriched rows: {len(profiles)}")

    suspect_ids: set[str] = set()
    for p in profiles:
        flag = audit_profile(p)
        if flag and "single_token_uploaded_name" not in (flag.get("flags") or []):
            suspect_ids.add(p.id)
    _log(f"[audit] strong wrong-person suspects in pool: {len(suspect_ids)}")

    by_email, by_name, _path = _resolve_source_for_dataset(
        ds.id, ds.source_file or ""
    )

    counts = Counts()
    sample_diff: list[dict[str, Any]] = []

    for profile in profiles:
        source_row = _lookup_source_row(profile, by_email=by_email, by_name=by_name)
        is_suspect = profile.id in suspect_ids
        updates = decontaminate_profile(
            profile, is_suspect=is_suspect, source_row=source_row, counts=counts
        )
        if updates is None:
            continue
        before_after = {
            "profile_id": profile.id,
            "email": profile.email,
            "name": profile.name,
            "is_suspect": is_suspect,
            "before": {
                "organization": profile.organization,
                "title": profile.title,
                "enriched_organization": profile.enriched_organization,
                "enriched_title": profile.enriched_title,
            },
            "after": {
                k: v
                for k, v in updates.items()
                if k in {"organization", "title", "enriched_organization", "enriched_title"}
            },
        }
        if len(sample_diff) < 25:
            sample_diff.append(before_after)

    return {
        "local_path": local_path,
        "dry_run": True,
        "counts": counts.to_dict(),
        "sample_diff": sample_diff,
    }


# ─────────────────────────── CLI ───────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.decontaminate_legacy_profiles",
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument("--account-id", help="Supabase account UUID (required unless --local is passed)")
    parser.add_argument("--dataset-id", help="Restrict to one dataset (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions but do not update the DB")
    parser.add_argument("--limit", type=int, help="Process at most N profiles (debug)")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    parser.add_argument(
        "--local",
        help="Offline dry-run against a local Dataset JSON export (never writes)",
    )
    args = parser.parse_args(argv)

    if not args.local and not args.account_id:
        parser.error("provide --account-id (live) or --local <path> (offline)")

    report = run(
        account_id=args.account_id,
        dataset_id=args.dataset_id,
        dry_run=args.dry_run or bool(args.local),
        limit=args.limit,
        local_path=args.local,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print("=" * 72)
        print(f"decontaminate_legacy_profiles — account={args.account_id} dataset={args.dataset_id or '(all)'}")
        print(f"dry-run: {args.dry_run}")
        print("=" * 72)
        for k, v in report["counts"].items():
            print(f"  {k:42s} {v}")
        print()
        print(f"Sample diffs ({len(report['sample_diff'])} of up to 25 shown):")
        for d in report["sample_diff"]:
            star = "*" if d["is_suspect"] else " "
            print(f"  [{star}] {d['profile_id']}  {d['name']!r}  <{d['email']}>")
            for field in ("organization", "title"):
                if field in d["after"]:
                    before = d["before"].get(field, "")
                    after = d["after"][field]
                    print(f"       {field}: {before!r} -> {after!r}")
            for field in ("enriched_organization", "enriched_title"):
                if field in d["after"]:
                    before = d["before"].get(field, "")
                    after = d["after"][field]
                    print(f"       {field}: {before!r} -> {after!r}  (shadow)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
