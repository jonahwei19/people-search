"""ThreadingHTTPServer for the people-search EC2 deployment.

Routes URLs to the existing per-file handlers under `api/` using the
same conventions as Vercel's filesystem router (literal paths win,
`[param]` files/dirs match dynamic segments).

The per-file handlers are plain `class handler(BaseHTTPRequestHandler)`
modules. They only reference inherited request fields (self.path,
self.headers, self.rfile, self.wfile, self.send_*), so we can dispatch
to them by calling the unbound method against our own request handler
instance — no socket transfer, no per-request handler reinstantiation.

Static fall-through: any GET that isn't `/api/...` falls back to
`cloud/public/index.html` (the built UI). Path prefixes are handled by
the upstream proxy (Caddy) — this server only ever sees stripped paths
like `/api/foo`, `/`, or `/static.png`.

Run:
    python3 server.py            # binds 127.0.0.1:8789
    PORT=8000 python3 server.py  # custom port
"""

from __future__ import annotations

import importlib
import logging
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Type
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load .env if present (matches the local Flask blueprint pattern).
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


API_ROOT = ROOT / "api"
STATIC_ROOT = ROOT / "cloud" / "public"
INDEX_PATH = STATIC_ROOT / "index.html"

logger = logging.getLogger("people-search")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# ── Route resolution ──────────────────────────────────────────


_route_cache: dict[tuple[str, ...], Optional[Type[BaseHTTPRequestHandler]]] = {}


def _candidate_paths(segments: tuple[str, ...]) -> list[Path]:
    """Yield filesystem paths to try, most-specific first.

    For `/api/dataset/<id>/facebook` we'd try, in order:
        api/dataset/<id>/facebook.py            (impossible — <id> is data)
        api/dataset/[anything]/facebook.py
        api/dataset/<id>/[anything].py
        api/dataset/[anything]/[anything].py

    The recursive walk below handles this. Literal directory/file matches
    always beat bracket-param matches at the same level.
    """
    return _walk(API_ROOT, segments)


def _walk(base: Path, segments: tuple[str, ...]) -> list[Path]:
    """Return list of <handler>.py candidates, most-specific first.

    For each segment we try, in order:
      1. Descend into a literal subdirectory and resolve `rest` there.
      2. Descend into a `[param]` subdirectory.
      3. Match a literal `.py` file at this level.
      4. Match a `[param].py` file at this level.

    Rules 3+4 catch single-file handlers that route their own sub-paths
    internally — e.g. `api/airtable.py` handles `/api/airtable/connect`,
    `/api/airtable/import`, etc. via `self.path` switching.
    """
    if not segments:
        return []
    head, *rest = segments
    rest_t = tuple(rest)
    candidates: list[Path] = []

    if base.is_dir():
        literal_dir = base / head
        if literal_dir.is_dir():
            candidates.extend(_walk(literal_dir, rest_t))
        for entry in sorted(base.iterdir()):
            if (
                entry.is_dir()
                and entry.name.startswith("[")
                and entry.name.endswith("]")
            ):
                candidates.extend(_walk(entry, rest_t))

        literal_file = base / f"{head}.py"
        if literal_file.is_file():
            candidates.append(literal_file)
        for entry in sorted(base.iterdir()):
            if (
                entry.is_file()
                and entry.suffix == ".py"
                and entry.stem.startswith("[")
                and entry.stem.endswith("]")
            ):
                candidates.append(entry)

    return candidates


def _resolve_handler(url_path: str) -> Optional[Type[BaseHTTPRequestHandler]]:
    """Resolve `/api/<segments>` to a handler class. Cached forever."""
    if not url_path.startswith("/api/"):
        return None
    segments = tuple(s for s in url_path[len("/api/"):].split("/") if s)
    if not segments:
        return None

    cached = _route_cache.get(segments)
    if cached is not None or segments in _route_cache:
        return cached

    handler_cls: Optional[Type[BaseHTTPRequestHandler]] = None
    for path in _candidate_paths(segments):
        rel = path.relative_to(ROOT).with_suffix("")
        # path "api/dataset/[id]/facebook" → module "api.dataset.[id].facebook"
        # but Python module names can't contain brackets, so the bracket
        # files have to be imported by spec rather than by dotted name.
        try:
            handler_cls = _import_handler(path)
        except Exception as exc:
            logger.warning("import failed for %s: %s", rel, exc)
            continue
        if handler_cls is not None:
            break

    _route_cache[segments] = handler_cls
    return handler_cls


def _import_handler(path: Path) -> Optional[Type[BaseHTTPRequestHandler]]:
    """Import a Python file by path, return its `handler` class if any."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"_route_{path.stem}_{abs(hash(path)) & 0xFFFF:04x}",
        str(path),
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "handler", None)


# ── Static fall-through ───────────────────────────────────────


def _serve_static(handler: BaseHTTPRequestHandler, url_path: str) -> None:
    """Serve a file out of cloud/public, or index.html for the root.

    The frontend is a single-page app; unknown non-API GETs return the
    index so client-side hash routes work after a refresh.
    """
    if url_path in ("/", "", "/index.html"):
        target = INDEX_PATH
    else:
        candidate = (STATIC_ROOT / url_path.lstrip("/")).resolve()
        # Refuse traversal out of the static dir.
        try:
            candidate.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            handler.send_error(404)
            return
        if candidate.is_file():
            target = candidate
        else:
            target = INDEX_PATH

    if not target.is_file():
        handler.send_error(404)
        return

    body = target.read_bytes()
    mime, _ = mimetypes.guess_type(str(target))
    handler.send_response(200)
    handler.send_header("Content-Type", mime or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ── Request handler ───────────────────────────────────────────


class Router(BaseHTTPRequestHandler):
    server_version = "people-search/1.0"

    def do_GET(self):
        self._dispatch("do_GET")

    def do_POST(self):
        self._dispatch("do_POST")

    def do_PUT(self):
        self._dispatch("do_PUT")

    def do_DELETE(self):
        self._dispatch("do_DELETE")

    def do_OPTIONS(self):
        # Local CORS preflight stub. Origin lockdown happens at the
        # proxy layer in real use.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Account-Id")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()

    def _dispatch(self, method: str):
        url_path = urlparse(self.path).path or "/"
        # Static serving for everything that isn't /api/...
        if not url_path.startswith("/api/"):
            if method == "do_GET":
                return _serve_static(self, url_path)
            self.send_error(405)
            return

        handler_cls = _resolve_handler(url_path)
        if handler_cls is None:
            self.send_error(404, f"No route for {url_path}")
            return

        fn = getattr(handler_cls, method, None)
        if fn is None:
            self.send_error(405, f"{method[3:]} not implemented for {url_path}")
            return

        # Temporarily reclass `self` so private helper methods defined on
        # the handler class (e.g. enrich.py's _handle_enriching) resolve.
        # Both classes subclass BaseHTTPRequestHandler — instance layout
        # is compatible — so attribute access continues to work.
        original_cls = self.__class__
        self.__class__ = handler_cls
        try:
            fn(self)
        except Exception as exc:
            logger.exception("handler error for %s %s", method, url_path)
            try:
                self.send_error(500, str(exc))
            except Exception:
                pass
        finally:
            self.__class__ = original_cls

    def log_message(self, format: str, *args) -> None:  # quieter than default
        logger.info("%s - %s", self.address_string(), format % args)


# ── Entry point ───────────────────────────────────────────────


def main() -> None:
    port = int(os.environ.get("PORT", "8789"))
    bind = os.environ.get("BIND", "127.0.0.1")
    server = ThreadingHTTPServer((bind, port), Router)
    logger.info("people-search listening on %s:%d (root=%s)", bind, port, ROOT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
