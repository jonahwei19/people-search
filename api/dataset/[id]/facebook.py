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
    # Per-card "Wrong / Fix" controls. Hits POST /api/profile/<id>/linkedin
    # which clears or replaces the URL and re-caches the photo.
    has_li = bool(p.linkedin_url)
    # Fix controls are HIDDEN until the card itself is clicked (search-modal
    # pattern: click the box → tools appear). Then the small link triggers
    # show; then clicking a trigger reveals its input.
    fix_html = (
        f'<div class="fix" data-pid="{_html.escape(p.id)}">'
        f'<div class="fix-default">'
        f'  <button class="fix-link fix-link-li" type="button"{" hidden" if not has_li else ""}>Wrong LinkedIn</button>'
        f'  <button class="fix-link fix-link-photo" type="button">Replace photo</button>'
        f'</div>'
        f'<div class="fix-edit fix-edit-li" hidden>'
        f'  <input type="text" class="fix-li-input" placeholder="https://www.linkedin.com/in/…" />'
        f'  <button class="fix-li-save" type="button">Set</button>'
        f'  <button class="fix-cancel-li" type="button" aria-label="Cancel">×</button>'
        f'</div>'
        f'<div class="fix-edit fix-edit-photo" hidden>'
        f'  <input type="text" class="fix-photo-input" placeholder="https://…/photo.jpg" />'
        f'  <button class="fix-photo-save" type="button">Set</button>'
        f'  <button class="fix-cancel-photo" type="button" aria-label="Cancel">×</button>'
        f'</div>'
        f'<div class="fix-status" aria-live="polite"></div>'
        f'</div>'
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
        {fix_html}
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

.masthead { margin-bottom: 32px; }
.title {
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.02em;
  margin: 0 0 4px;
  color: var(--text);
}
.tally-mono {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-3);
  margin: 0;
}

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

.missing { margin-top: 48px; }
.missing-label {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
  margin: 0 0 8px;
}
.missing-label .count { margin-left: 4px; }

.absent { list-style: none; padding: 0; margin: 0; border-top: 1px solid var(--border); }
.absent li {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(140px, 1fr) auto;
  gap: 16px;
  padding: 9px 0;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  align-items: baseline;
}
.absent .who { font-weight: 500; color: var(--text); }
.absent .org { color: var(--text-2); }
.absent .email { font-family: var(--mono); font-size: 12px; color: var(--text-3);
                 text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.empty {
  padding: 32px 0;
  text-align: center;
  color: var(--text-3);
  font-size: 14px;
}

/* Per-card correction controls. Mirrors the search-modal pattern:
   the entire .fix block is hidden until the card is clicked. Inside it,
   the inline trigger links show; clicking a trigger reveals its input. */
.card { cursor: pointer; }
.fix { margin-top: 0; }
.fix-default { display: none; gap: 10px; margin-top: 8px; }
.card.expanded { cursor: default; }
.card.expanded .fix-default { display: flex; }
.fix-link {
  background: none; border: none; padding: 0;
  font-family: var(--mono); font-size: 11px; cursor: pointer;
}
.fix-link-li    { color: #b00020; }
.fix-link-li:hover { text-decoration: underline; }
.fix-link-photo { color: var(--text-3); }
.fix-link-photo:hover { color: var(--text-2); text-decoration: underline; }

.fix-edit[hidden] { display: none !important; }
.fix-edit {
  display: flex; gap: 4px; margin-top: 6px; align-items: center;
}
.fix-edit input {
  font: inherit; font-size: 12px;
  padding: 5px 8px; border: 1px solid var(--border-2);
  border-radius: 4px; outline: none;
  flex: 1; min-width: 0;
}
.fix-edit input:focus { border-color: var(--accent); }
.fix-edit button {
  font: inherit; font-size: 11px; cursor: pointer;
  padding: 5px 10px; border-radius: 4px; white-space: nowrap;
  border: 1px solid var(--text); background: var(--text); color: white;
}
.fix-edit button:hover { background: var(--accent); border-color: var(--accent); }
.fix-edit button[class*="cancel"] {
  background: var(--surface); color: var(--text-3);
  border-color: var(--border-2);
  padding: 5px 8px; line-height: 1;
}
.fix-edit button[class*="cancel"]:hover { color: var(--text); background: var(--bg); }
.fix-edit button:disabled { opacity: 0.5; cursor: wait; }
.fix-status {
  font-family: var(--mono); font-size: 11px; color: var(--text-3);
  min-height: 0; margin-top: 4px;
}
.fix-status:empty { display: none; }
.fix-status.error { color: #b00020; }
.fix-status.success { color: #0c8047; }

@media print {
  body { background: white; }
  main.book { padding: 0; }
  .grid { gap: 14px; grid-template-columns: repeat(4, 1fr); }
  a.li { color: var(--text); }
  .fix, .fix-toggle, .fix-panel { display: none !important; }
}
"""


def _render(profiles, dataset_name, supabase_url):
    has_link, missing = [], []
    for p in profiles:
        if p.linkedin_url:
            has_link.append(p)
        else:
            missing.append(p)
    has_link.sort(key=lambda p: (p.name or "").lower())
    missing.sort(key=lambda p: (p.name or "").lower())

    grid_html = (
        f'<div class="grid">{chr(10).join(_card(p, supabase_url) for p in has_link)}</div>'
        if has_link else ""
    )

    missing_html = ""
    if missing:
        rows = "\n".join(
            f'<li>'
            f'<span class="who">{_html.escape(p.name or "")}</span>'
            f'<span class="org">{_html.escape(_employer(p))}</span>'
            f'<span class="email">{_html.escape(getattr(p, "email", "") or "")}</span>'
            f'</li>'
            for p in missing
        )
        missing_html = f"""
<section class="missing">
  <p class="missing-label">No LinkedIn <span class="count">{len(missing)}</span></p>
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
      <h1 class="title">{name_html}</h1>
      <p class="tally-mono">{len(has_link)} {"profile" if len(has_link) == 1 else "profiles"}{f" · {len(missing)} no LinkedIn" if missing else ""}</p>
    </header>

    {grid_html}
    {missing_html}
  </main>
  <script>
  // Per-card correction controls. Mirrors the search-modal pattern:
  //   - "Wrong LinkedIn" → clears the URL immediately, reveals input for new URL.
  //   - "Replace photo"  → reveals just the photo URL input (no destructive default).
  // On any save: reload with a cache-buster so the new photo paints.
  (function() {{
    const apiBase = new URL('../../', location.href).pathname.replace(/\\/$/, '');

    // Card-click reveals the .fix block. Clicks on the LinkedIn link,
    // the trigger buttons, and any input/button inside .fix don't toggle
    // the card so editing flows aren't interrupted.
    document.querySelectorAll('.card').forEach(function(card) {{
      const fix = card.querySelector('.fix');
      if (!fix) return;
      card.addEventListener('click', function(ev) {{
        // Don't toggle when clicking the external LinkedIn link.
        if (ev.target.closest('a.li')) return;
        card.classList.toggle('expanded');
      }});
      // Clicks inside the fix UI don't bubble — keeps inputs/buttons working
      // without collapsing the card the moment you interact with them.
      fix.addEventListener('click', function(ev) {{ ev.stopPropagation(); }});
    }});

    document.querySelectorAll('.fix').forEach(function(box) {{
      const pid       = box.getAttribute('data-pid');
      const linkLi    = box.querySelector('.fix-link-li');
      const linkPhoto = box.querySelector('.fix-link-photo');
      const editLi    = box.querySelector('.fix-edit-li');
      const editPhoto = box.querySelector('.fix-edit-photo');
      const liInput   = box.querySelector('.fix-li-input');
      const liSave    = box.querySelector('.fix-li-save');
      const liCancel  = box.querySelector('.fix-cancel-li');
      const photoInput= box.querySelector('.fix-photo-input');
      const photoSave = box.querySelector('.fix-photo-save');
      const photoCancel = box.querySelector('.fix-cancel-photo');
      const status    = box.querySelector('.fix-status');

      function setStatus(text, kind) {{
        status.className = 'fix-status' + (kind ? ' ' + kind : '');
        status.textContent = text || '';
      }}
      function setBusy(b, group) {{
        const els = group === 'li' ? [liInput, liSave, liCancel]
                  : group === 'photo' ? [photoInput, photoSave, photoCancel]
                  : [liInput, liSave, liCancel, photoInput, photoSave, photoCancel];
        els.forEach(function(el) {{ if (el) el.disabled = !!b; }});
      }}
      function reloadSoon() {{
        setTimeout(function() {{
          const u = new URL(location.href); u.searchParams.set('_', Date.now()); location.href = u.toString();
        }}, 500);
      }}

      async function call(endpoint, body) {{
        const resp = await fetch(apiBase + '/profile/' + pid + '/' + endpoint, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          credentials: 'same-origin',
          body: JSON.stringify(body),
        }});
        const text = await resp.text();
        if (!resp.ok) {{
          let msg = 'HTTP ' + resp.status;
          try {{ const j = JSON.parse(text); if (j.error) msg = j.error; }} catch(_) {{}}
          throw new Error(msg);
        }}
      }}

      // Click "Wrong LinkedIn" → clear the URL immediately (search-modal
      // behavior), hide the trigger, reveal the input for a corrected URL.
      linkLi.addEventListener('click', async function() {{
        if (!confirm('Mark LinkedIn as wrong? The URL and cached photo will be cleared while you type the correct one.')) return;
        setBusy(true, 'li'); setStatus('Clearing…');
        try {{
          await call('linkedin', {{ linkedin_url: '' }});
          linkLi.hidden = true;
          editLi.hidden = false;
          setStatus('Cleared. Paste the correct URL below.', 'success');
          liInput.value = ''; liInput.focus();
          setBusy(false, 'li');
        }} catch (err) {{
          setStatus('Failed: ' + err.message, 'error');
          setBusy(false, 'li');
        }}
      }});

      // Save the corrected LinkedIn URL → re-enrich + recache + reload.
      liSave.addEventListener('click', async function() {{
        const url = liInput.value.trim();
        if (!url || url.indexOf('linkedin.com/in/') < 0) {{
          setStatus('Paste a full https://www.linkedin.com/in/… URL.', 'error'); return;
        }}
        setBusy(true, 'li'); setStatus('Re-enriching…');
        try {{
          await call('linkedin', {{ linkedin_url: url }});
          setStatus('Saved. Refreshing…', 'success');
          reloadSoon();
        }} catch (err) {{
          setStatus('Failed: ' + err.message, 'error');
          setBusy(false, 'li');
        }}
      }});
      liCancel.addEventListener('click', function() {{ editLi.hidden = true; setStatus(''); }});
      liInput.addEventListener('keydown', function(e) {{ if (e.key === 'Enter') liSave.click(); }});

      // "Replace photo" → reveal just the photo input. Submit caches it.
      linkPhoto.addEventListener('click', function() {{
        editPhoto.hidden = false; setStatus(''); photoInput.value = ''; photoInput.focus();
      }});
      photoSave.addEventListener('click', async function() {{
        const url = photoInput.value.trim();
        if (!/^https?:\\/\\//i.test(url)) {{
          setStatus('Paste a full http(s) image URL.', 'error'); return;
        }}
        setBusy(true, 'photo'); setStatus('Caching photo…');
        try {{
          await call('photo', {{ photo_url: url }});
          setStatus('Saved. Refreshing…', 'success');
          reloadSoon();
        }} catch (err) {{
          setStatus('Failed: ' + err.message, 'error');
          setBusy(false, 'photo');
        }}
      }});
      photoCancel.addEventListener('click', function() {{ editPhoto.hidden = true; setStatus(''); }});
      photoInput.addEventListener('keydown', function(e) {{ if (e.key === 'Enter') photoSave.click(); }});
    }});
  }})();
  </script>
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
