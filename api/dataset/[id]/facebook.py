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
        photo_html = (
            f'<img src="{_html.escape(public_url(supabase_url, photo_path))}" '
            f'alt="{name}" loading="lazy" />'
        )
    else:
        photo_html = f'<span class="initials">{_html.escape(_initials(p.name or ""))}</span>'

    employer = _employer(p)
    title = _title_of(p)
    location = _location_of(p)

    role_bits = []
    if title:
        role_bits.append(_html.escape(title))
    if employer:
        role_bits.append(_html.escape(employer))
    role_sep = ' <span class="sep">·</span> '
    role_html = (
        f'<p class="role">{role_sep.join(role_bits)}</p>'
        if role_bits else ""
    )

    location_html = (
        f'<dl class="kv"><dt>Location</dt><dd>{_html.escape(location)}</dd></dl>'
        if location else ""
    )
    link_html = (
        f'<a class="li" href="{_html.escape(p.linkedin_url)}" '
        f'target="_blank" rel="noopener">LinkedIn <span aria-hidden="true">↗</span></a>'
        if p.linkedin_url else ""
    )

    return f"""
    <article class="card">
      <figure class="portrait">
        <div class="mat">{photo_html}</div>
      </figure>
      <div class="meta">
        <h3 class="name">{name}</h3>
        {role_html}
        {location_html}
        {link_html}
      </div>
    </article>"""


# Minimal neutral gallery: white background, near-black ink, geometric sans.
# Same token system used by the main app so the two read as one product.
_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@300..700&family=Geist+Mono:wght@400;500&display=swap');

:root {
  --bg:        #fafafa;
  --surface:   #ffffff;
  --border:    #eaeaea;
  --border-2:  #d4d4d4;
  --text:      #0a0a0a;
  --text-2:    #525252;
  --text-3:    #a3a3a3;
  --accent:    #0070f3;
  --sans: "Geist", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  --mono: "Geist Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
  color: var(--text);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  font-feature-settings: "ss01", "cv11";
}

main.book {
  max-width: 1180px;
  margin: 0 auto;
  padding: 56px 32px 80px;
}

.masthead { margin-bottom: 40px; }
.overline {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--text-3);
  margin: 0 0 6px;
}
.title {
  font-size: 32px;
  font-weight: 600;
  letter-spacing: -0.02em;
  margin: 0;
  color: var(--text);
}
.title .amp { color: var(--text-3); font-weight: 400; }
.tally {
  display: flex;
  gap: 28px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-2);
}
.tally .num { color: var(--text); font-weight: 500; margin-right: 6px; }
.tally .div { display: none; }
.tally .cell { display: inline-flex; gap: 6px; align-items: baseline; }
.tally .cell .label { color: var(--text-3); font-size: 11px; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 28px 20px;
}

.card { display: flex; flex-direction: column; }

.portrait {
  margin: 0 0 12px;
  aspect-ratio: 1 / 1;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  display: flex; align-items: center; justify-content: center;
}
.portrait .mat { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; }
.portrait img { width: 100%; height: 100%; object-fit: cover; display: block; }
.portrait .initials {
  font-size: 44px; font-weight: 500; color: var(--text-3);
  letter-spacing: -0.02em;
}

.meta { padding: 0 2px; }
.name { font-size: 14px; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 4px; color: var(--text); }
.role { font-size: 13px; color: var(--text-2); margin: 0 0 6px; line-height: 1.4; }
.role .sep { color: var(--text-3); margin: 0 4px; }

dl.kv { margin: 0 0 6px; display: flex; gap: 8px; align-items: baseline; font-size: 12px; }
dl.kv dt { font-family: var(--mono); font-size: 11px; color: var(--text-3); margin: 0; }
dl.kv dd { color: var(--text-2); margin: 0; }

a.li {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--accent);
  text-decoration: none;
}
a.li:hover { text-decoration: underline; text-underline-offset: 2px; }

section.section { margin-top: 56px; }
section.section h2 {
  font-size: 18px; font-weight: 600; letter-spacing: -0.01em;
  margin: 0 0 4px; color: var(--text);
}
section.section h2 .count {
  font-family: var(--mono); font-size: 12px; font-weight: 400;
  color: var(--text-3); margin-left: 8px;
}
section.section .hint { font-size: 13px; color: var(--text-2); max-width: 64ch; margin: 0 0 20px; }

.ornament { display: none; }

.absent { list-style: none; padding: 0; margin: 0; border-top: 1px solid var(--border); }
.absent li {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 16px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  align-items: baseline;
}
.absent .who { font-weight: 500; color: var(--text); }
.absent .org { color: var(--text-2); }
.absent .tag { font-family: var(--mono); font-size: 11px; color: var(--text-3); }

.empty {
  padding: 32px 0;
  text-align: center;
  color: var(--text-3);
  font-size: 14px;
}

@media print {
  body { background: white; }
  main.book { padding: 0; }
  .grid { gap: 14px; grid-template-columns: repeat(4, 1fr); }
  a.li { color: var(--text); }
}
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

    if confirmed:
        confirmed_grid = (
            f'<div class="grid">{chr(10).join(_card(p, supabase_url) for p in confirmed)}</div>'
        )
    else:
        confirmed_grid = '<div class="empty">No confirmed entries yet.</div>'

    review_html = ""
    if review:
        review_cards = "\n".join(_card(p, supabase_url) for p in review)
        review_html = f"""
<div class="ornament"><span>⁂</span></div>
<section class="section">
  <h2>Needs review <span class="count">{len(review):03d}</span></h2>
  <p class="hint">LinkedIn URL was matched by identity search rather than uploaded — verify before relying on it.</p>
  <div class="grid">{review_cards}</div>
</section>"""

    missing_html = ""
    if missing:
        rows = "\n".join(
            f'<li><span class="who">{_html.escape(p.name or "")}</span>'
            f'<span class="org">{_html.escape(_employer(p))}</span>'
            f'<span class="tag">No LinkedIn</span></li>'
            for p in missing
        )
        missing_html = f"""
<div class="ornament"><span>⁂</span></div>
<section class="section">
  <h2>No LinkedIn <span class="count">{len(missing):03d}</span></h2>
  <ul class="absent">{rows}</ul>
</section>"""

    page_title = _html.escape(f"{dataset_name} — Face Book")
    name_html = _html.escape(dataset_name)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{page_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <style>{_CSS}</style>
</head>
<body>
  <main class="book">
    <header class="masthead">
      <p class="overline"><span class="vol">Volume I</span> &nbsp;·&nbsp; {name_html}</p>
      <h1 class="title">Face <span class="amp">Book</span></h1>
      <div class="tally">
        <div class="cell"><span class="num">{len(confirmed):03d}</span><span class="label">Confirmed</span></div>
        <div class="div" aria-hidden="true"></div>
        <div class="cell center"><span class="num">{len(review):03d}</span><span class="label">Needs review</span></div>
        <div class="div" aria-hidden="true"></div>
        <div class="cell right"><span class="num">{len(missing):03d}</span><span class="label">No LinkedIn</span></div>
      </div>
    </header>

    <section class="roll">
      {confirmed_grid}
    </section>

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
