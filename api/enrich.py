"""POST /api/enrich — Chunked enrichment.

Each invocation processes a batch of profiles and returns progress.
The frontend loops, calling this endpoint until status is "done".
Progress is persisted in Supabase so enrichment survives function restarts.

Phases (tracked in jobs.stats.phase so they resume across invocations):

  1. enriching — resolve identities + LinkedIn enrich pending profiles
                 (CHUNK_SIZE profiles per call, parallel within chunk)
  2. fetching  — fetch_links() for every profile with a twitter/website/
                 resume/other link but no fetched_content yet
                 (FETCH_CHUNK_SIZE profiles per call, parallel within chunk)
  3. carding   — build_profile_cards once across the whole dataset, then done

The frontend polling loop (`pollJob` in shared/ui.html) re-triggers
`/api/enrich` whenever status is "running", so these phases unfold
transparently as the user watches the progress bar.

Fixes bug #1 from plans/diagnosis_architecture.md section 4: the cloud
path previously skipped fetch_links() entirely, so no cloud-enriched
profile ever populated website_url / twitter_url / other_links / fetched_content.

Request:  {"dataset_id": "...", "job_id": "..." (optional, omit for first call)}
Response: {"job_id": "...", "status": "running"|"done"|"error", "current": N, "total": N}
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler

from api._helpers import (
    require_auth, json_response, read_json_body,
    get_pipeline, get_storage, check_enrichment_keys,
)
from enrichment.fetchers import fetch_all_links
from enrichment.models import EnrichmentStatus

CHUNK_SIZE = 50        # profiles per invocation during enrichment (parallel within chunk)
FETCH_CHUNK_SIZE = 20  # profiles per invocation during link-fetching (parallel within chunk)
FETCH_WORKERS = 10     # threads per fetch chunk — link fetchers are I/O-bound


def _profile_has_links(p) -> bool:
    """Does this profile have any link worth fetching?"""
    return bool(p.twitter_url or p.website_url or p.resume_url or p.other_links)


def _fetch_one_profile(profile) -> dict:
    """Fetch every link on a profile, mutating profile.fetched_content in place.

    Returns counters {"fetched": N, "failed": N, "skipped": N}.
    """
    counts = {"fetched": 0, "failed": 0, "skipped": 0}
    results = fetch_all_links(
        twitter_url=profile.twitter_url,
        website_url=profile.website_url,
        resume_url=profile.resume_url,
        other_links=profile.other_links,
    )
    for name, result in results.items():
        if result.success:
            profile.fetched_content[name] = result.text
            counts["fetched"] += 1
        elif "not yet implemented" in (result.error or ""):
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
    # Always mark a fetch attempt with a sentinel so we don't re-process this
    # profile on the next invocation. If every URL failed we'd otherwise loop
    # forever because fetched_content would still be {}.
    if not profile.fetched_content:
        profile.fetched_content["_fetch_attempted"] = ""
    return counts


def _fetch_chunk_parallel(profiles, max_workers: int = FETCH_WORKERS) -> dict:
    """Fetch links for a chunk of profiles in parallel."""
    totals = {"fetched": 0, "failed": 0, "skipped": 0}
    if not profiles:
        return totals
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_fetch_one_profile, p) for p in profiles]
        for fut in as_completed(futures):
            try:
                counts = fut.result()
            except Exception:
                counts = {"fetched": 0, "failed": 1, "skipped": 0}
            for k in totals:
                totals[k] += counts.get(k, 0)
    return totals


def _ids_needing_fetch(storage, dataset_id: str, account_id: str) -> list[str]:
    """List profile IDs with at least one link and no fetched_content yet.

    PostgREST `eq` on JSONB is finicky, so we pull a narrow column set
    for every profile in the dataset and filter in Python. Each row is
    a few hundred bytes; this is cheap even for datasets of several
    thousand profiles and it keeps the query contract predictable.
    """
    resp = (
        storage.client.table("profiles")
        .select("id,twitter_url,website_url,resume_url,other_links,fetched_content")
        .eq("dataset_id", dataset_id)
        .eq("account_id", account_id)
        .execute()
    )
    pj = storage._parse_json
    ids = []
    for r in (resp.data or []):
        # Profile has at least one link?
        has_link = bool(
            r.get("twitter_url") or r.get("website_url")
            or r.get("resume_url") or r.get("other_links")
        )
        if not has_link:
            continue
        # Has nothing been fetched yet?
        fc = pj(r.get("fetched_content"), {})
        if not fc:
            ids.append(r["id"])
    return ids


def _load_profiles_needing_fetch(storage, dataset_id: str, account_id: str,
                                 limit: int) -> list:
    """Load up to `limit` profiles that have links but no fetched_content yet."""
    ids = _ids_needing_fetch(storage, dataset_id, account_id)[:limit]
    if not ids:
        return []
    resp = (
        storage.client.table("profiles")
        .select("*")
        .eq("dataset_id", dataset_id)
        .eq("account_id", account_id)
        .in_("id", ids)
        .execute()
    )
    return [storage._row_to_profile(r) for r in resp.data]


def _count_profiles_needing_fetch(storage, dataset_id: str, account_id: str) -> int:
    """Count remaining profiles that still need link fetching."""
    return len(_ids_needing_fetch(storage, dataset_id, account_id))


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        dataset_id = body.get("dataset_id")
        job_id = body.get("job_id")

        if not dataset_id:
            json_response(self, 400, {"error": "dataset_id required"})
            return

        aid = account["account_id"]
        missing = check_enrichment_keys(aid)
        if missing:
            json_response(self, 400, {"error": f"Missing API keys: {', '.join(missing)}. Add them in Settings."})
            return
        storage = get_storage(aid)
        pipeline = get_pipeline(aid)

        # Lightweight total count (never load all profiles)
        try:
            all_count = storage.client.table("profiles").select("id", count="exact").eq("dataset_id", dataset_id).eq("account_id", aid).execute()
            total = all_count.count or 0
        except Exception as e:
            json_response(self, 404, {"error": f"Dataset not found: {e}"})
            return

        # Create job on first call. Track which phase we're in via stats.phase.
        if not job_id:
            job_id = storage.create_job(dataset_id, total)
            storage.update_job(job_id, stats={"phase": "enriching"})
            phase = "enriching"
        else:
            job = storage.get_job(job_id) or {}
            phase = (job.get("stats") or {}).get("phase") or "enriching"

        try:
            if phase == "enriching":
                self._handle_enriching(storage, pipeline, dataset_id, aid, job_id, total)
            elif phase == "fetching":
                self._handle_fetching(storage, dataset_id, aid, job_id, total)
            elif phase == "carding":
                self._handle_carding(storage, pipeline, dataset_id, job_id, total)
            else:
                # Unknown phase — treat as done.
                storage.update_job(job_id, status="done", current_count=total,
                                   message="Done")
                json_response(self, 200, {
                    "job_id": job_id, "status": "done",
                    "current": total, "total": total,
                })
        except Exception as e:
            storage.update_job(job_id, status="error", message=str(e))
            json_response(self, 200, {
                "job_id": job_id, "status": "error",
                "current": 0, "total": total, "message": str(e),
            })

    @staticmethod
    def _merge_stats(storage, job_id, **delta):
        """Read job.stats, add delta values, write back. Preserves keys we
        don't touch (e.g. `phase`)."""
        cur = (storage.get_job(job_id) or {}).get("stats") or {}
        if not isinstance(cur, dict):
            cur = {}
        for k, v in delta.items():
            if isinstance(v, (int, float)):
                cur[k] = (cur.get(k) or 0) + v
            else:
                cur[k] = v
        storage.update_job(job_id, stats=cur)
        return cur

    # ── Phase 1: enrichment ──────────────────────────────────

    def _handle_enriching(self, storage, pipeline, dataset_id, aid, job_id, total):
        # Load only the next chunk of pending profiles.
        pending_resp = (
            storage.client.table("profiles")
            .select("*")
            .eq("dataset_id", dataset_id)
            .eq("account_id", aid)
            .eq("enrichment_status", "pending")
            .limit(CHUNK_SIZE)
            .execute()
        )
        pending = [storage._row_to_profile(r) for r in pending_resp.data]

        if not pending:
            # Enrichment complete — advance to fetching phase.
            self._merge_stats(storage, job_id, phase="fetching")
            storage.update_job(
                job_id,
                current_count=total,
                message="Fetching links...",
            )
            json_response(self, 200, {
                "job_id": job_id, "status": "running",
                "current": total, "total": total,
            })
            return

        chunk = pending

        # Capture stats from each batch so the final "Done" screen can show
        # actual numbers instead of zeros. Return values are dicts:
        #   resolve_stats: {"resolved": N, "failed": N, ...}
        #   enrich_stats:  {"enriched": N, "skipped": N, "failed": N, "total_cost": $}
        resolve_stats = pipeline.resolver.resolve_batch(chunk, max_workers=20) or {}
        enrich_stats = pipeline.enricher.enrich_batch(chunk, max_workers=20) or {}

        # Trim enrichment logs before saving (reduces I/O)
        for p in chunk:
            if len(p.enrichment_log) > 20:
                p.enrichment_log = p.enrichment_log[:5] + ["... truncated ..."] + p.enrichment_log[-5:]

        # Save only the chunk we just processed
        storage.save_profiles(dataset_id, chunk)

        # Count done via lightweight query
        still_pending = storage.client.table("profiles").select("id", count="exact").eq("dataset_id", dataset_id).eq("enrichment_status", "pending").execute()
        done_count = total - (still_pending.count or 0)

        self._merge_stats(
            storage,
            job_id,
            phase="enriching",
            resolved=resolve_stats.get("resolved", 0),
            resolve_failed=resolve_stats.get("failed", 0),
            enriched=enrich_stats.get("enriched", 0),
            skipped=enrich_stats.get("skipped", 0),
            failed=enrich_stats.get("failed", 0),
            total_cost=enrich_stats.get("total_cost", 0.0),
        )
        storage.update_job(
            job_id,
            current_count=done_count,
            message=f"Enriched {done_count}/{total}",
        )

        json_response(self, 200, {
            "job_id": job_id,
            "status": "running",
            "current": done_count,
            "total": total,
        })

    # ── Phase 2: link fetching ───────────────────────────────

    def _handle_fetching(self, storage, dataset_id, aid, job_id, total):
        # Grab the next chunk of profiles that have links and no fetched_content.
        chunk = _load_profiles_needing_fetch(
            storage, dataset_id, aid, FETCH_CHUNK_SIZE,
        )

        if not chunk:
            # Nothing left to fetch — advance to card-building phase.
            self._merge_stats(storage, job_id, phase="carding")
            storage.update_job(
                job_id,
                current_count=total,
                message="Building profile cards...",
            )
            json_response(self, 200, {
                "job_id": job_id, "status": "running",
                "current": total, "total": total,
            })
            return

        _fetch_chunk_parallel(chunk)
        storage.save_profiles(dataset_id, chunk)

        # Rough progress: we don't know total-with-links cheaply, so report
        # remaining as a message and keep the bar pinned at 100% on total.
        remaining = _count_profiles_needing_fetch(storage, dataset_id, aid)
        msg = f"Fetching links... {remaining} profiles remaining"
        storage.update_job(
            job_id,
            current_count=total,
            message=msg,
        )

        json_response(self, 200, {
            "job_id": job_id,
            "status": "running",
            "current": total,
            "total": total,
            "message": msg,
        })

    # ── Phase 3: build profile cards, finish ─────────────────

    def _handle_carding(self, storage, pipeline, dataset_id, job_id, total):
        # This loads the full dataset once at the end. Acceptable — it runs
        # exactly once per enrichment run and build_profile_cards is CPU-only.
        dataset = storage.load_dataset(dataset_id)
        pipeline.build_profile_cards(dataset)
        storage.save_profiles(dataset_id, dataset.profiles)

        # Reconcile final counts from the database — chunked accumulation can
        # under-count if a chunk's stats write raced or was retried. Cost stays
        # as-accumulated since per-profile cost isn't persisted.
        cur = (storage.get_job(job_id) or {}).get("stats") or {}
        if not isinstance(cur, dict):
            cur = {}
        from enrichment.models import EnrichmentStatus
        enriched_count = sum(1 for p in dataset.profiles if p.enrichment_status == EnrichmentStatus.ENRICHED)
        skipped_count = sum(1 for p in dataset.profiles if p.enrichment_status == EnrichmentStatus.SKIPPED)
        failed_count = sum(1 for p in dataset.profiles if p.enrichment_status == EnrichmentStatus.FAILED)
        cur["enriched"] = enriched_count
        cur["skipped"] = skipped_count
        cur["failed"] = failed_count
        cur["phase"] = "done"

        storage.update_job(
            job_id,
            status="done",
            current_count=total,
            message="Done",
            stats=cur,
        )
        json_response(self, 200, {
            "job_id": job_id, "status": "done",
            "current": total, "total": total,
        })

    def log_message(self, format, *args):
        pass
