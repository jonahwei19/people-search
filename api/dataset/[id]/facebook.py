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


# Editorial / yearbook aesthetic: cream paper, serif masthead, mono labels.
# Self-contained; one Google Fonts request; CSS-only motion. Print-ready.
_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT@9..144,300..800,0..100&family=Sora:wght@200..700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --paper:       #f3ecdc;
  --paper-mat:   #ece1c5;
  --paper-deep:  #e1d3b1;
  --ink:         #1d1611;
  --ink-soft:    #5a4a3b;
  --ink-faint:   #8a7a68;
  --rule:        #c4b89c;
  --rule-soft:   #d8cdb0;
  --accent:      #7a1d1d;
  --accent-warm: #b04a2f;
  --serif: "Fraunces", "EB Garamond", "Iowan Old Style", "Hoefler Text", Georgia, serif;
  --sans:  "Sora", system-ui, -apple-system, sans-serif;
  --mono:  "JetBrains Mono", "SFMono-Regular", ui-monospace, Menlo, monospace;
}

* { box-sizing: border-box; }

html, body { margin: 0; padding: 0; }

body {
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.55;
  color: var(--ink);
  background-color: var(--paper);
  /* Subtle paper grain via inline SVG noise. */
  background-image:
    radial-gradient(1200px 800px at 10% -10%, rgba(176,74,47,0.05), transparent 60%),
    radial-gradient(900px 700px at 110% 110%, rgba(122,29,29,0.05), transparent 55%),
    url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0.11  0 0 0 0 0.09  0 0 0 0 0.06  0 0 0 0.07 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
  background-attachment: fixed;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

main.book {
  max-width: 1240px;
  margin: 0 auto;
  padding: 4.5rem 3rem 6rem;
}

/* ── Masthead ───────────────────────────────────────── */
.masthead { padding-bottom: 2.5rem; margin-bottom: 3rem; }
.overline {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--ink-soft);
  margin: 0 0 1.5rem;
}
.overline .vol { color: var(--accent); }
.title {
  font-family: var(--serif);
  font-variation-settings: "opsz" 144, "SOFT" 100, "wght" 600;
  font-size: clamp(72px, 12vw, 168px);
  line-height: 0.88;
  letter-spacing: -0.04em;
  margin: 0;
  color: var(--ink);
  text-rendering: geometricPrecision;
}
.title .amp {
  font-style: italic;
  font-variation-settings: "opsz" 144, "SOFT" 0, "wght" 300;
  color: var(--accent);
}

.tally {
  display: grid;
  grid-template-columns: 1fr auto 1fr auto 1fr;
  align-items: end;
  gap: 1.5rem;
  margin-top: 2.5rem;
  padding-top: 1.25rem;
  border-top: 1px solid var(--ink);
}
.tally .cell { display: flex; flex-direction: column; gap: 0.35rem; }
.tally .cell.right { text-align: right; }
.tally .cell.center { text-align: center; }
.tally .label {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--ink-faint);
}
.tally .num {
  font-family: var(--serif);
  font-variation-settings: "opsz" 96, "SOFT" 60, "wght" 500;
  font-size: 32px;
  line-height: 1;
  color: var(--ink);
}
.tally .div {
  width: 1px;
  background: var(--rule);
  align-self: stretch;
}

/* ── Grid + cards ──────────────────────────────────── */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 2.4rem 1.6rem;
}

.card {
  display: flex;
  flex-direction: column;
  break-inside: avoid;
  opacity: 0;
  transform: translateY(8px);
  animation: rise 700ms cubic-bezier(0.22, 1, 0.36, 1) forwards;
}
.card:nth-child(1)  { animation-delay: 0.02s; }
.card:nth-child(2)  { animation-delay: 0.06s; }
.card:nth-child(3)  { animation-delay: 0.10s; }
.card:nth-child(4)  { animation-delay: 0.14s; }
.card:nth-child(5)  { animation-delay: 0.18s; }
.card:nth-child(6)  { animation-delay: 0.22s; }
.card:nth-child(7)  { animation-delay: 0.26s; }
.card:nth-child(8)  { animation-delay: 0.30s; }
.card:nth-child(9)  { animation-delay: 0.34s; }
.card:nth-child(10) { animation-delay: 0.38s; }
.card:nth-child(n+11) { animation-delay: 0.42s; }

@keyframes rise {
  to { opacity: 1; transform: translateY(0); }
}

.portrait {
  margin: 0 0 0.9rem;
  aspect-ratio: 1 / 1;
  background: var(--paper-mat);
  padding: 6px;
  position: relative;
  box-shadow: 0 0.5px 0 var(--rule), 0 1px 0 var(--rule-soft) inset;
}
.portrait::before {
  content: "";
  position: absolute;
  inset: 0;
  border: 1px solid var(--ink);
  pointer-events: none;
  mix-blend-mode: multiply;
  opacity: 0.55;
}
.portrait .mat {
  width: 100%; height: 100%;
  background: var(--paper-deep);
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
  filter: contrast(1.02) saturate(0.92);
}
.portrait img {
  width: 100%; height: 100%; object-fit: cover;
  display: block;
  /* faint warm-tone wash so photos sit on the paper */
  filter: contrast(1.02) saturate(0.94) sepia(0.04);
}
.portrait .initials {
  font-family: var(--serif);
  font-variation-settings: "opsz" 144, "SOFT" 100, "wght" 400;
  font-size: 56px;
  letter-spacing: -0.02em;
  color: var(--ink-faint);
}

.meta { padding: 0 2px; }

.name {
  font-family: var(--serif);
  font-variation-settings: "opsz" 36, "SOFT" 80, "wght" 500;
  font-size: 22px;
  line-height: 1.1;
  letter-spacing: -0.015em;
  margin: 0 0 0.35rem;
  color: var(--ink);
}

.role {
  font-family: var(--sans);
  font-size: 12.5px;
  font-style: italic;
  font-weight: 350;
  line-height: 1.4;
  color: var(--ink-soft);
  margin: 0 0 0.55rem;
}
.role .sep { color: var(--ink-faint); margin: 0 0.25em; font-style: normal; }

dl.kv { margin: 0 0 0.55rem; display: flex; gap: 0.45rem; align-items: baseline; }
dl.kv dt {
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-faint);
  margin: 0;
}
dl.kv dd {
  font-family: var(--sans);
  font-size: 12px;
  font-weight: 400;
  color: var(--ink-soft);
  margin: 0;
}

a.li {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px solid var(--accent);
  padding-bottom: 1px;
  display: inline-block;
  transition: color 160ms ease, border-color 160ms ease, transform 160ms ease;
}
a.li:hover { color: var(--accent-warm); border-color: var(--accent-warm); transform: translateY(-1px); }

/* ── Section dividers + supplementary sections ─────── */
.ornament {
  display: flex;
  align-items: center;
  gap: 1.5rem;
  margin: 5rem 0 3rem;
  color: var(--rule);
}
.ornament::before, .ornament::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--rule);
}
.ornament span {
  font-family: var(--serif);
  font-size: 22px;
  color: var(--ink-faint);
  letter-spacing: 0.4em;
}

section.section h2 {
  font-family: var(--serif);
  font-variation-settings: "opsz" 96, "SOFT" 80, "wght" 400;
  font-style: italic;
  font-size: 30px;
  letter-spacing: -0.01em;
  margin: 0 0 0.4rem;
  color: var(--ink);
}
section.section h2 .count {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  font-style: normal;
  color: var(--ink-faint);
  margin-left: 0.5rem;
  vertical-align: 4px;
}
section.section .hint {
  font-family: var(--sans);
  font-size: 13px;
  font-style: italic;
  color: var(--ink-soft);
  max-width: 64ch;
  margin: 0 0 2rem;
}

.absent {
  list-style: none;
  padding: 0;
  margin: 0;
  border-top: 1px solid var(--rule);
}
.absent li {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: baseline;
  gap: 1.5rem;
  padding: 0.85rem 0;
  border-bottom: 1px solid var(--rule-soft);
  font-size: 13.5px;
}
.absent .who {
  font-family: var(--serif);
  font-variation-settings: "opsz" 24, "SOFT" 50, "wght" 500;
  font-size: 17px;
  color: var(--ink);
}
.absent .org {
  font-family: var(--sans);
  font-style: italic;
  color: var(--ink-soft);
}
.absent .tag {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--ink-faint);
}

.empty {
  padding: 3rem 0;
  text-align: center;
  font-family: var(--serif);
  font-style: italic;
  color: var(--ink-faint);
  font-size: 22px;
}

/* ── Print ─────────────────────────────────────────── */
@page { size: A4; margin: 18mm 14mm; }
@media print {
  body { background: var(--paper); background-image: none; color: var(--ink); }
  main.book { padding: 0; max-width: none; }
  .card { animation: none; opacity: 1; transform: none; break-inside: avoid; }
  .grid { gap: 1.6rem 1.2rem; grid-template-columns: repeat(4, 1fr); }
  .ornament { margin: 2rem 0 1.5rem; }
  a.li { color: var(--ink); border-color: var(--ink); }
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
