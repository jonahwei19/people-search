#!/usr/bin/env python3
"""People Search — Data Upload & Management.

Upload CSV/JSON files of people, review schema mappings, enrich via LinkedIn,
and manage datasets for search.

Storage: JSON files in datasets/ directory, embeddings as .npz.
Sufficient for local use with hundreds to low thousands of profiles.
Switch to SQLite when: multi-user, >10K profiles, or need concurrent writes.

Usage:
    python3 upload_web.py              # http://localhost:5556
    python3 upload_web.py --port 5557
"""

import argparse
import json
import os
import sys
import threading
import uuid
from pathlib import Path

# Add parent directory to path so we can import enrichment/ and search/
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, request, jsonify, render_template_string

from enrichment import EnrichmentPipeline, FieldType
from enrichment.schema import FieldMapping
from enrichment.enrichers import is_valid_linkedin_url
from local.search_blueprint import search_bp, init_search, SEARCH_NAV, SEARCH_PAGE, SEARCH_JS

app = Flask(__name__)
app.register_blueprint(search_bp)

UPLOAD_DIR = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

PIPELINE: EnrichmentPipeline = None
JOBS: dict[str, dict] = {}

# ── HTML ─────────────────────────────────────────────────────

def _load_html():
    html_path = Path(__file__).parent.parent / "shared" / "ui.html"
    html = html_path.read_text(encoding="utf-8")
    # Set local mode
    html = html.replace("'{{APP_MODE}}'", "'local'")
    return html


# ── API Routes ───────────────────────────────────────────────

ENV_PATH = Path(__file__).parent.parent / ".env"

MANAGED_KEYS = ["GOOGLE_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY", "ENRICHLAYER_API_KEY", "ANTHROPIC_API_KEY"]


def _load_env() -> dict:
    """Load API keys from .env file."""
    keys = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k in MANAGED_KEYS:
                keys[k] = v
    return keys


def _save_env(keys: dict):
    """Save API keys to .env file and apply to environment."""
    lines = ["# People Search — API keys (auto-generated, do not commit)\n"]
    for k in MANAGED_KEYS:
        v = keys.get(k, "")
        if v:
            lines.append(f"{k}={v}")
            os.environ[k] = v
    ENV_PATH.write_text("\n".join(lines) + "\n")


def _apply_env():
    """Load .env and set environment variables on startup."""
    keys = _load_env()
    for k, v in keys.items():
        if v and not os.environ.get(k):
            os.environ[k] = v


@app.route("/")
def index():
    return _load_html()


@app.route("/api/keys")
def api_get_keys():
    """Return which keys are set (masked values)."""
    env_keys = _load_env()
    result = {}
    for k in MANAGED_KEYS:
        val = os.environ.get(k, "") or env_keys.get(k, "")
        if val:
            # Return masked version so UI shows "Connected" without exposing the key
            result[k] = val[:4] + "..." + val[-4:] if len(val) > 12 else "***"
        else:
            result[k] = ""
    return jsonify(result)


@app.route("/api/keys", methods=["POST"])
def api_save_keys():
    """Save API keys to .env and apply to environment."""
    data = request.json or {}
    # Merge with existing (don't overwrite keys not in this request)
    current = _load_env()
    for k in MANAGED_KEYS:
        if k in data and data[k]:
            # Don't save the masked placeholder back
            if "..." not in data[k] and "***" not in data[k]:
                current[k] = data[k]
    _save_env(current)
    # Reinitialize pipeline with new keys
    global PIPELINE
    PIPELINE = EnrichmentPipeline(
        data_dir=str(PIPELINE.data_dir),
        enrichlayer_api_key=os.environ.get("ENRICHLAYER_API_KEY", ""),
    )
    return jsonify({"ok": True})


@app.route("/api/detect_schema", methods=["POST"])
def api_detect_schema():
    # Support both JSON body (large files) and multipart FormData
    if request.is_json:
        data = request.json
        content = data.get("content", "")
        filename = data.get("filename", "upload.csv")
        if not content:
            return jsonify({"error": "No file content"}), 400
        suffix = ".json" if filename.endswith(".json") else ".csv"
        filepath = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{filename}"
        filepath.write_text(content, encoding="utf-8")
    else:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "Empty filename"}), 400
        filepath = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        f.save(str(filepath))

    try:
        ms = PIPELINE.detect_schema(filepath)
        return jsonify({
            "mappings": [m.to_dict() for m in ms],
            "filename": filepath.name,
            "rows": _count_rows(filepath),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    # Support both JSON body (large files) and multipart FormData
    if request.is_json:
        data = request.json
        content = data.get("content", "")
        filename = data.get("filename", "upload.csv")
        name = data.get("name", "")
        raw_mappings = data.get("mappings", [])
        if not content:
            return jsonify({"error": "No file content"}), 400
        filepath = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{filename}"
        filepath.write_text(content, encoding="utf-8")
    else:
        f = request.files["file"]
        name = request.form.get("name", "")
        raw_mappings = json.loads(request.form.get("mappings", "[]"))
        filepath = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        f.save(str(filepath))

    try:
        fmaps = [FieldMapping(
            source_column=m["source_column"],
            field_type=FieldType(m["field_type"]),
            target_name=m.get("target_name", m["source_column"]),
            sample_values=m.get("sample_values", []),
            confidence=m.get("confidence", 0.5),
        ) for m in raw_mappings]

        # Support appending to existing dataset (for chunked uploads)
        # Lightweight: just parse rows into profiles, skip dedup/cost
        append_to = data.get("append_to") if request.is_json else None
        if append_to:
            existing = PIPELINE.load(append_to)
            rows = PIPELINE._load_rows(filepath)
            new_profiles = []
            for i, row in enumerate(rows):
                p = PIPELINE._row_to_profile(row, fmaps, len(existing.profiles) + i)
                has_identity = p.name or p.email or p.linkedin_url
                has_content = any(v.strip() for v in p.content_fields.values()) if p.content_fields else False
                if has_identity or has_content:
                    new_profiles.append(p)
            existing.profiles.extend(new_profiles)
            existing.total_rows += len(rows)
            PIPELINE.save(existing)
            return jsonify({"dataset_id": append_to, "appended": len(new_profiles)})

        dataset, cost = PIPELINE.prepare(filepath, fmaps, name=name)
        PIPELINE.save(dataset)

        have_li = sum(1 for p in dataset.profiles if is_valid_linkedin_url(p.linkedin_url))
        email_only = sum(1 for p in dataset.profiles if p.email and not is_valid_linkedin_url(p.linkedin_url))
        cfields = set()
        for p in dataset.profiles:
            cfields.update(p.content_fields.keys())

        return jsonify({
            "dataset_id": dataset.id,
            "stats": {
                "total": len(dataset.profiles),
                "have_linkedin": have_li,
                "email_only": email_only,
                "content_fields": len(cfields),
                "content_field_names": sorted(cfields),
                "skipped_rows": dataset.enrichment_stats.get("skipped_rows", 0),
                "duplicates": len(dataset.enrichment_stats.get("duplicates", [])),
            },
            "cost_summary": cost.summary(),
            "cost": cost.to_dict(),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 400


@app.route("/api/enrich", methods=["POST"])
def api_enrich():
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "running", "current": 0, "total": 0, "message": "Starting...", "stats": {}}

    def run():
        def cancelled():
            return JOBS[job_id].get("cancel")

        try:
            ds = PIPELINE.load(ds_id)
            JOBS[job_id]["total"] = len(ds.profiles)

            def on_prog(cur, tot, msg):
                JOBS[job_id].update(current=cur, total=tot, message=msg)

            stats = PIPELINE.run_enrichment(ds, on_progress=on_prog)
            PIPELINE.save(ds)
            if cancelled(): return

            JOBS[job_id].update(status="running", message="Fetching link content...")
            link_stats = PIPELINE.fetch_links(ds, on_progress=on_prog)
            stats.update({f"links_{k}": v for k, v in link_stats.items()})
            PIPELINE.save(ds)
            if cancelled(): return

            JOBS[job_id].update(status="embedding", message="Building profile cards...")
            PIPELINE.build_profile_cards(ds)
            PIPELINE.save(ds)

            JOBS[job_id].update(status="done", stats=stats)
        except Exception as e:
            if not cancelled():
                JOBS[job_id].update(status="error", message=str(e))
            import traceback; traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/embed_only", methods=["POST"])
def api_embed_only():
    """Skip enrichment, just generate embeddings from uploaded content."""
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "embedding", "current": 0, "total": 0, "message": "Building profile cards...", "stats": {}}

    def run():
        try:
            ds = PIPELINE.load(ds_id)
            # Mark all profiles as skipped (no enrichment attempted)
            from enrichment.models import EnrichmentStatus
            for p in ds.profiles:
                if p.enrichment_status == EnrichmentStatus.PENDING:
                    p.enrichment_status = EnrichmentStatus.SKIPPED

            # Still fetch links if available
            link_stats = PIPELINE.fetch_links(ds)

            # Build profile cards from whatever content we have
            PIPELINE.build_profile_cards(ds)
            PIPELINE.save(ds)
            JOBS[job_id].update(status="done", stats={"enriched": 0, "skipped": len(ds.profiles), "failed": 0, "total_cost": 0})
        except Exception as e:
            JOBS[job_id].update(status="error", message=str(e))
            import traceback; traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({k: v for k, v in job.items() if k != "cancel"})


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancel"] = True
    job["status"] = "cancelled"
    job["message"] = "Cancelled by user"
    return jsonify({"ok": True})


@app.route("/api/datasets")
def api_datasets():
    return jsonify(PIPELINE.list_datasets())


@app.route("/api/dataset/<ds_id>")
def api_dataset_detail(ds_id):
    try:
        ds = PIPELINE.load(ds_id)
        return jsonify({
            "id": ds.id,
            "name": ds.name,
            "created_at": ds.created_at,
            "source_file": ds.source_file,
            "total_rows": ds.total_rows,
            "searchable_fields": ds.searchable_fields,
            "enrichment_stats": ds.enrichment_stats,
            "profiles": [p.to_dict() for p in ds.profiles],
        })
    except FileNotFoundError:
        return jsonify({"error": "Dataset not found"}), 404


@app.route("/api/profile/<profile_id>")
def api_profile(profile_id):
    """Get a single profile by ID (searches across all datasets)."""
    for ds_info in PIPELINE.list_datasets():
        ds = PIPELINE.load(ds_info["id"])
        for p in ds.profiles:
            if p.id == profile_id:
                d = p.to_dict()
                d["_dataset_name"] = ds_info["name"]
                d["_dataset_id"] = ds_info["id"]
                return jsonify(d)
    return jsonify({"error": "Profile not found"}), 404


@app.route("/api/profile/<profile_id>/linkedin", methods=["POST"])
def api_profile_linkedin(profile_id):
    """Update a profile's LinkedIn URL. Optionally re-enriches."""
    from enrichment.models import EnrichmentStatus
    from enrichment.enrichers import normalize_linkedin_url

    data = request.json or {}
    new_url = data.get("linkedin_url", "").strip()

    # Find the profile and its dataset
    for ds_info in PIPELINE.list_datasets():
        ds = PIPELINE.load(ds_info["id"])
        for p in ds.profiles:
            if p.id == profile_id:
                if not new_url:
                    # Clear LinkedIn
                    p.linkedin_url = ""
                    p.linkedin_enriched = {}
                    p.enrichment_status = EnrichmentStatus.FAILED
                    p.enrichment_log.append("LinkedIn manually cleared by user")
                    p.profile_card = ""
                    p.build_raw_text()
                    PIPELINE.save(ds)
                    return jsonify({"status": "cleared"})

                # Set new URL and re-enrich
                p.linkedin_url = new_url
                p.enrichment_log.append(f"LinkedIn manually set to: {new_url}")

                # Enrich via EnrichLayer (skip verification — user provided this URL)
                url = normalize_linkedin_url(new_url)
                api_data, _reason = PIPELINE.enricher._call_api(url)

                if api_data and api_data != "OUT_OF_CREDITS":
                    parsed = PIPELINE.enricher._parse_response(api_data)
                    p.linkedin_enriched = parsed
                    p.enrichment_status = EnrichmentStatus.ENRICHED
                    exp_count = len(parsed.get("experience", []))
                    p.enrichment_log.append(f"LinkedIn enriched (manual): {exp_count} experiences, {parsed.get('headline', '')}")

                    # Backfill identity fields
                    if not p.name and parsed.get("full_name"):
                        p.name = parsed["full_name"]
                    if not p.organization and parsed.get("current_company"):
                        p.organization = parsed["current_company"]
                    if not p.title and parsed.get("current_title"):
                        p.title = parsed["current_title"]
                else:
                    p.enrichment_log.append("LinkedIn URL set but EnrichLayer returned no data")
                    p.enrichment_status = EnrichmentStatus.ENRICHED  # URL is correct, just no data

                # Rebuild profile card
                p.build_raw_text()
                PIPELINE.save(ds)
                return jsonify({"status": "enriched", "profile": p.to_dict()})

    return jsonify({"error": "Profile not found"}), 404


@app.route("/api/reenrich_estimate", methods=["POST"])
def api_reenrich_estimate():
    """Estimate costs for re-enriching an existing dataset."""
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400
    try:
        ds = PIPELINE.load(ds_id)
        from enrichment.costs import CostBreakdown
        # All profiles will be re-resolved and re-enriched
        have_li = sum(1 for p in ds.profiles if is_valid_linkedin_url(p.linkedin_url))
        have_email = sum(1 for p in ds.profiles if p.email and not is_valid_linkedin_url(p.linkedin_url))
        no_li_no_email = sum(1 for p in ds.profiles if not p.email and not is_valid_linkedin_url(p.linkedin_url))

        cost = CostBreakdown(
            linkedin_enrichments=len(ds.profiles),  # all will be re-enriched after resolution
            identity_lookups=len(ds.profiles),  # all will be re-resolved
            embedding_profiles=len(ds.profiles),
        )
        cfields = set()
        for p in ds.profiles:
            cfields.update(p.content_fields.keys())
        return jsonify({
            "stats": {
                "total": len(ds.profiles),
                "have_linkedin": have_li,
                "email_only": have_email,
                "no_signal": no_li_no_email,
                "content_fields": len(cfields),
                "content_field_names": sorted(cfields),
            },
            "cost": cost.to_dict(),
            "cost_summary": cost.summary(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/reenrich", methods=["POST"])
def api_reenrich():
    """Re-enrich an existing dataset: reset enrichment status and re-run."""
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "running", "current": 0, "total": 0, "message": "Starting re-enrichment...", "stats": {}}

    def run():
        try:
            ds = PIPELINE.load(ds_id)
            # Reset all enrichment statuses to pending
            from enrichment.models import EnrichmentStatus
            for p in ds.profiles:
                p.enrichment_status = EnrichmentStatus.PENDING
                p.linkedin_enriched = {}
                p.profile_card = ""
                p.field_summaries = {}
                p.fetched_content = {}
            PIPELINE.save(ds)

            JOBS[job_id]["total"] = len(ds.profiles)

            def on_prog(cur, tot, msg):
                JOBS[job_id].update(current=cur, total=tot, message=msg)

            # Run full enrichment pipeline
            stats = PIPELINE.run_enrichment(ds, on_progress=on_prog)
            PIPELINE.save(ds)

            # Fetch links
            JOBS[job_id].update(message="Fetching link content...")
            link_stats = PIPELINE.fetch_links(ds, on_progress=on_prog)
            stats.update({f"links_{k}": v for k, v in link_stats.items()})
            PIPELINE.save(ds)

            # Build profile cards
            JOBS[job_id].update(status="embedding", message="Building profile cards...")
            PIPELINE.build_profile_cards(ds)
            PIPELINE.save(ds)

            JOBS[job_id].update(status="done", stats=stats)
        except Exception as e:
            JOBS[job_id].update(status="error", message=str(e))
            import traceback; traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/dataset/<ds_id>", methods=["DELETE"])
def api_delete_dataset(ds_id):
    try:
        path = PIPELINE.data_dir / f"{ds_id}.json"
        emb_path = PIPELINE.data_dir / f"{ds_id}_embeddings.npz"
        if path.exists():
            path.unlink()
        if emb_path.exists():
            emb_path.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _count_rows(filepath: Path) -> int:
    try:
        if filepath.suffix.lower() == ".json":
            with open(filepath) as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 1
        else:
            with open(filepath) as f:
                return sum(1 for _ in f) - 1
    except Exception:
        return 0


# ── Main ─────────────────────────────────────────────────────

def main():
    global PIPELINE

    parser = argparse.ArgumentParser(description="People Search — Upload & Manage")
    parser.add_argument("--port", type=int, default=5556)
    parser.add_argument("--data-dir", type=str, default=str(Path(__file__).parent.parent / "datasets"))
    args = parser.parse_args()

    # Load saved API keys from .env (won't override env vars already set)
    _apply_env()

    PIPELINE = EnrichmentPipeline(
        data_dir=args.data_dir,
        enrichlayer_api_key=os.environ.get("ENRICHLAYER_API_KEY", ""),
    )

    # Initialize search
    init_search(PIPELINE)

    print(f"People Search: http://localhost:{args.port}")
    print(f"Data dir: {args.data_dir}")
    app.run(host="0.0.0.0", port=args.port, debug=True)


if __name__ == "__main__":
    main()
