"""GET /api/dataset/:id/facebook — Render the dataset as a face-book gallery.

Returns a self-contained HTML page (Airtable-style photo grid). Confidence
buckets:
  - confirmed: linkedin_url_source == 'user' (uploaded as ground truth)
  - review:    linkedin_url_source == 'resolved' (matched by identity search)
  - missing:   no linkedin_url at all

Profiles without a cached photo show initials. The build_photos endpoint
populates photo_path; this endpoint just reads.
"""

from __future__ import annotations

import html as _html
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from api._helpers import require_auth, path_param, get_storage
from enrichment.photos import public_url


def _initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _employer(p) -> str:
    return (
        getattr(p, "enriched_organization", "")
        or getattr(p, "organization", "")
        or (p.linkedin_enriched or {}).get("current_company", "")
        or ""
    ).strip()


def _title_of(p) -> str:
    return (
        getattr(p, "enriched_title", "")
        or getattr(p, "title", "")
        or (p.linkedin_enriched or {}).get("current_title", "")
        or ""
    ).strip()


def _location_of(p) -> str:
    return ((p.linkedin_enriched or {}).get("location", "") or "").strip()


def _card(p, supabase_url: str) -> str:
    name = _html.escape(p.name or "Unnamed")
    photo_path = getattr(p, "photo_path", "") or ""
    if photo_path:
        photo_html = f'<img src="{_html.escape(public_url(supabase_url, photo_path))}" alt="{name}" loading="lazy" />'
    else:
        photo_html = f'<div class="placeholder">{_html.escape(_initials(p.name or ""))}</div>'

    rows = []
    employer = _employer(p)
    title = _title_of(p)
    location = _location_of(p)
    if employer:
        rows.append(f'<div class="row"><span class="label">Employer</span><span class="value">{_html.escape(employer)}</span></div>')
    if title:
        rows.append(f'<div class="row"><span class="label">Title</span><span class="value">{_html.escape(title)}</span></div>')
    if location:
        rows.append(f'<div class="row"><span class="label">Location</span><span class="value">{_html.escape(location)}</span></div>')
    if p.linkedin_url:
        rows.append(f'<div class="row"><a class="li" href="{_html.escape(p.linkedin_url)}" target="_blank" rel="noopener">View LinkedIn ↗</a></div>')

    return f"""
    <div class="card">
      <div class="photo-wrap">{photo_html}</div>
      <div class="info">
        <div class="name">{name}</div>
        {''.join(rows)}
      </div>
    </div>"""


_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; background: #f7f7f5; color: #1d1d1f; }
header { background: #2a6ee0; color: white; padding: 1.25rem 2rem; display: flex; align-items: baseline; gap: 1rem; }
header h1 { margin: 0; font-size: 1.4rem; font-weight: 600; }
header .meta { font-size: 0.9rem; opacity: 0.85; }
main { padding: 2rem; max-width: 1400px; margin: 0 auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1.25rem; }
.card { background: white; border-radius: 12px; overflow: hidden; border: 1px solid #e5e5e7; display: flex; flex-direction: column; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
.photo-wrap { aspect-ratio: 1 / 1; background: #f0f0f2; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.photo-wrap img { width: 100%; height: 100%; object-fit: cover; }
.placeholder { font-size: 3rem; font-weight: 600; color: #c7c7cc; }
.info { padding: 0.9rem 1rem 1rem; }
.name { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.5rem; }
.row { font-size: 0.85rem; margin-top: 0.4rem; line-height: 1.35; }
.row .label { display: block; color: #86868b; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em; }
.row .value { color: #1d1d1f; }
.row a.li { display: inline-block; margin-top: 0.5rem; background: #2a6ee0; color: white; padding: 0.45rem 0.7rem; border-radius: 6px; text-decoration: none; font-size: 0.8rem; }
.row a.li:hover { background: #1f59c0; }
section.review, section.failed { margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid #d7d7da; }
section h2 { font-size: 1.1rem; font-weight: 600; margin: 0 0 0.4rem; }
section .count { color: #86868b; font-weight: 400; }
section .hint { color: #86868b; font-size: 0.9rem; margin: 0 0 1rem; }
section.failed ul { padding: 0; list-style: none; }
section.failed li { padding: 0.4rem 0; font-size: 0.9rem; }
section.failed .org { color: #86868b; }
"""


def _render(profiles, dataset_name, supabase_url):
    confirmed, review, missing = [], [], []
    for p in profiles:
        if not p.linkedin_url:
            missing.append(p)
            continue
        src = getattr(p, "linkedin_url_source", "")
        if src == "user":
            confirmed.append(p)
        else:
            review.append(p)

    confirmed.sort(key=lambda p: (p.name or "").lower())
    review.sort(key=lambda p: (p.name or "").lower())
    missing.sort(key=lambda p: (p.name or "").lower())

    cards = "\n".join(_card(p, supabase_url) for p in confirmed)

    review_html = ""
    if review:
        review_cards = "\n".join(_card(p, supabase_url) for p in review)
        review_html = f"""
<section class="review">
  <h2>Needs review <span class="count">({len(review)})</span></h2>
  <p class="hint">LinkedIn URL was matched by identity search rather than uploaded — verify before relying on it.</p>
  <div class="grid">{review_cards}</div>
</section>"""

    missing_html = ""
    if missing:
        rows = "\n".join(
            f'<li><strong>{_html.escape(p.name or "")}</strong> '
            f'<span class="org">{_html.escape(_employer(p))}</span></li>'
            for p in missing
        )
        missing_html = f"""
<section class="failed">
  <h2>No LinkedIn <span class="count">({len(missing)})</span></h2>
  <ul>{rows}</ul>
</section>"""

    title = _html.escape(f"{dataset_name} — Face Book")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <style>{_CSS}</style>
</head>
<body>
  <header>
    <h1>{_html.escape(dataset_name)}</h1>
    <span class="meta">{len(confirmed)} confirmed · {len(review)} needs review · {len(missing)} no LinkedIn</span>
  </header>
  <main>
    <div class="grid">{cards}</div>
    {review_html}
    {missing_html}
  </main>
</body>
</html>
"""


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return

        ds_id = path_param(self, -2)
        storage = get_storage(account["account_id"])

        try:
            ds = storage.load_dataset(ds_id)
        except Exception:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Dataset not found")
            return

        html_body = _render(ds.profiles, ds.name, os.environ.get("SUPABASE_URL", ""))
        body = html_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass
