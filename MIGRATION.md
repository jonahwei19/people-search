# Vercel → EC2 migration spec

Status: in progress. Owner: Jonah. Target: people-search runs on the
agents EC2 box (`ssh agents`, Tailscale 100.102.107.13) behind Tailscale
Funnel, fully replacing the Vercel deploy.

## Why

- Vercel's 300s function ceiling forces chunked enrichment / build_photos /
  scoring with frontend-driven polling. EC2 with a long-running Python
  process collapses all of that.
- Cold starts re-import the enrichment package on every request. EC2
  keeps imports warm, connection pools to Supabase, and only starts up
  on `systemctl restart`.
- Deploy on EC2 is `git pull && bash build.sh && systemctl restart` —
  ~5 seconds vs Vercel's queue + build + propagation.
- Voice logger / notion-meeting-transfer / oj-tooling are already on the
  same box. People-search joins the same operational pattern.

## Non-goals (this migration)

- Custom domain or DNS work. Public URL stays a Tailscale Funnel one.
- Rewriting handlers to FastAPI/Flask. Existing per-file
  `BaseHTTPRequestHandler` modules are kept; a tiny router dispatches
  to them by path. Refactor is a follow-up if/when warranted.
- Removing the chunked enrichment endpoints. They'll keep working; we
  can simplify later once a persistent worker pattern proves out.

## URL layout

EC2 box already has 3 Funnel slots in use (only 3 exist):

  443    → 8787  (notion-meeting-transfer, gunicorn)
  8443   → 5173  (oj-tooling, node)
  10000  → 8788  (voice-logger)

We don't take a slot. Instead a small reverse proxy listens at a fresh
local port and gets pointed at by Funnel:443; the proxy multiplexes
existing services and the new one:

  Funnel:443  →  127.0.0.1:8786 (caddy multiplexer)
                 ├── /people-search/*  →  127.0.0.1:8789 (new)  [strip prefix]
                 └── /*                →  127.0.0.1:8787 (notion-meeting-transfer)

Public URL becomes `https://agents.tail83bd73.ts.net/people-search/`.
Notion meeting transfer's URL is unchanged.

## Process model

- One systemd service: `people-search.service` (User=ec2-user).
- ExecStart: `python3 server.py` from `~/agents/people-search/`.
- `server.py` is a `ThreadingHTTPServer` bound to `127.0.0.1:8789`. It
  contains the dispatch logic (URL → `api/<path>.py` module) and serves
  `cloud/public/index.html` for the root.
- Working dir holds the full repo checkout (no separate build step
  beyond `bash build.sh` to stamp APP_VERSION + APP_MODE).
- Logs to journald. `journalctl -u people-search -f` for tail.

## Routing details

The router resolves `/api/<segments>` against `api/<segments>.py`,
preferring literal paths and falling back to `[id]` directories/files
for dynamic segments — same convention Vercel uses, so no per-handler
changes are needed.

Each per-file handler is a `class handler(BaseHTTPRequestHandler)` that
only references inherited request fields (`self.path`, `self.headers`,
`self.rfile`, `self.wfile`, `self.send_*`). That means the router can
call `module.handler.do_GET(self)` with `self` being the router's own
`BaseHTTPRequestHandler` instance — no instance translation, no copy of
the request socket.

Path-prefix handling: Caddy strips `/people-search` before forwarding,
so the Python server sees the same paths it does on Vercel today
(`/api/dataset/abc/facebook`). The frontend prepends `window.APP_BASE`
(set by `build.sh` to `/people-search` for EC2, `''` for Vercel) to API
URLs in `apiFetch`.

## Secrets

`.env` lives at `~/agents/people-search/.env` (mode 600, owner
ec2-user). Same keys as the local file: `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`, `ENRICHLAYER_API_KEY`, `OPENAI_API_KEY`,
`BRAVE_API_KEY`, `SERPER_API_KEY`. systemd service uses
`EnvironmentFile=` to load them.

## Migration phases

Each phase ends in a verifiable checkpoint. We don't move to the next
until the previous one passes.

### Phase 0 — local dry run

1. Add `server.py` with router + ThreadingHTTPServer.
2. Add `APP_BASE` placeholder in `shared/ui.html` and `build.sh`; update
   `apiFetch` to prepend it.
3. Run locally: `python3 server.py` on port 8789. Hit `/people-search/`
   in browser (with a local Caddy or curl with stripped prefix). Verify
   that GET /api/datasets returns the same shape as Vercel.

### Phase 1 — deploy to EC2

4. `rsync` the repo to `~/agents/people-search/` on the box (excluding
   .git, node_modules, etc.).
5. Install deps in a venv: `python3 -m venv .venv && .venv/bin/pip
   install -r requirements.txt`.
6. Drop `.env` (mode 600).
7. Install systemd unit `~/.config/systemd/user/people-search.service`
   (or system unit at `/etc/systemd/system/`). Enable + start.
8. Verify service is up: `systemctl status people-search` and
   `curl http://127.0.0.1:8789/api/datasets` (should 401 without auth).

### Phase 2 — proxy

9. Install Caddy. Configure `Caddyfile` to listen on 127.0.0.1:8786 and
   route as documented above. Run as a systemd service.
10. Repoint Funnel: `tailscale funnel --bg --set-path / 127.0.0.1:8786`
    (replacing the current 8787 mapping). Confirm both
    `https://agents.tail83bd73.ts.net/` (meeting transfer) and
    `https://agents.tail83bd73.ts.net/people-search/` (new) work.

### Phase 3 — frontend cutover

11. Test the full app at the new URL. Login, datasets, search, face-book.
12. Watch logs for an hour. Real usage.
13. Remove the Vercel deployment OR leave it dormant for a week as a
    rollback target.

### Phase 4 — cleanup

14. Document deploy procedure: `tools/aws/DEPLOYED.md` gets a section
    for people-search. Same scp-via-tmp pattern as voice logger if
    the rsync flow has problems.
15. Update CLAUDE.md to point at the new URL.

## Rollback

- Vercel deployment can be reactivated instantly by reverting the DNS /
  link (Vercel build is zero-cost when idle).
- Caddy mapping can be reverted with one `tailscale funnel` command —
  Funnel:443 goes back to `→ 8787` directly, people-search goes
  offline but no other service is affected.
- systemd can stop people-search without affecting other services.

## Known limitations under EC2's persistent process

These don't affect the single-account user (Jonah) today, but should be
fixed before adding a second account / multi-tenant access:

- **`os.environ` is process-wide.** `api/_helpers.get_storage` and
  `get_pipeline` write per-account API keys into the environment so
  downstream modules (`enrichment.identity`, `search.gemini_helpers`,
  `enrichment.summarizer`, etc.) can find them. On Vercel each request
  was its own process; on EC2 these writes are visible to every other
  request. The bulk write is locked (`_keys_lock` in `_helpers.py`) so
  partial-state reads can't happen, but a concurrent request from a
  *different* account would still see the wrong key mid-flight. Proper
  fix: thread keys through call chains explicitly (constructors already
  accept them in `LinkedInEnricher`, `IdentityResolver`) or use
  `contextvars` + `copy_context()` for ThreadPoolExecutor workers.
- **`api/search/chat.py` `CONVERSATIONS`** is an in-process dict.
  Lock-protected as of the EC2 cutover; survives process restarts is
  *not* guaranteed (active question chains lose state on restart).
  Acceptable because the chain is short (~3 turns during search
  creation). Move to a Supabase-backed table if it becomes load-bearing.
- **Photo cache eager hook still pending.** Backfill on first open
  works; documented in MIGRATION.md follow-ups.

## Open questions

- Long-running enrichment: keep chunked-and-polled for now (works
  identically on EC2). Open question is whether we replace it with a
  single in-process job + websocket/SSE progress later.
- Photo cache eager hook: doesn't depend on EC2 — but moves from
  "blocked by Vercel chunking" to "trivial in a persistent process"
  after migration. Worth a dedicated follow-up once the rest is stable.
- Cron: `vercel.json` has no crons; we have nothing to migrate. If/when
  scheduled jobs are added, use plain `crond` on the box (already
  running).
