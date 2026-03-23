"""Link content fetchers.

Pull useful text from URLs found in profile data.

Setup requirements per fetcher:
- GitHub:       None (public API, no auth needed)
- Website:      None (plain HTTP)
- Twitter/X:    BRAVE_API_KEY env var (uses Brave Search to extract bio)
- Google Drive:  gws CLI installed + configured (see SETUP.md)

See enrichment/SETUP.md for detailed setup instructions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class FetchResult:
    """Result of fetching content from a URL."""
    success: bool
    text: str = ""
    source: str = ""
    error: str = ""


# ── GitHub ──────────────────────────────────────────────────

class GitHubFetcher:
    """Fetch profile info from GitHub public API. No auth needed."""

    def fetch(self, url: str) -> FetchResult:
        username = self._extract_username(url)
        if not username:
            return FetchResult(success=False, error="Could not parse GitHub username")

        try:
            resp = requests.get(
                f"https://api.github.com/users/{username}",
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return FetchResult(success=False, error=f"GitHub API {resp.status_code}")

            user = resp.json()
            parts = [f"GitHub: {user.get('name', username)}"]
            if user.get("bio"):
                parts.append(f"Bio: {user['bio']}")
            if user.get("company"):
                parts.append(f"Company: {user['company']}")
            if user.get("location"):
                parts.append(f"Location: {user['location']}")

            repos_resp = requests.get(
                f"https://api.github.com/users/{username}/repos",
                params={"sort": "stargazers_count", "per_page": 5},
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=10,
            )
            if repos_resp.status_code == 200:
                repos = repos_resp.json()
                if repos:
                    repo_lines = []
                    for r in repos[:5]:
                        desc = r.get("description", "")
                        stars = r.get("stargazers_count", 0)
                        lang = r.get("language", "")
                        line = f"  {r['name']}"
                        if desc:
                            line += f" — {desc[:80]}"
                        if stars > 0:
                            line += f" ({stars} stars)"
                        if lang:
                            line += f" [{lang}]"
                        repo_lines.append(line)
                    parts.append("Repos:\n" + "\n".join(repo_lines))

            return FetchResult(success=True, text="\n".join(parts), source="github_api")
        except Exception as e:
            return FetchResult(success=False, error=str(e))

    def _extract_username(self, url: str) -> Optional[str]:
        match = re.search(r"github\.com/([a-zA-Z0-9_-]+)", url)
        return match.group(1) if match else None


# ── Website ─────────────────────────────────────────────────

class WebsiteFetcher:
    """Scrape bio/about text from a personal or org website."""

    def fetch(self, url: str) -> FetchResult:
        try:
            resp = requests.get(
                url, timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (research tool)"},
                allow_redirects=True,
            )
            if resp.status_code != 200:
                return FetchResult(success=False, error=f"HTTP {resp.status_code}")

            text = self._extract_text(resp.text)
            if not text or len(text) < 50:
                return FetchResult(success=False, error="No usable text found")

            return FetchResult(success=True, text=text[:2000], source="website_scrape")
        except Exception as e:
            return FetchResult(success=False, error=str(e))

    def _extract_text(self, html: str) -> str:
        html = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&nbsp;", " ").replace("&quot;", '"')
        text = re.sub(r"\s+", " ", text).strip()
        sentences = text.split(". ")
        bio_parts = []
        for s in sentences:
            s = s.strip()
            if 20 < len(s) < 500 and not s.startswith(("Cookie", "Privacy", "Terms", "Copyright", "©")):
                bio_parts.append(s)
        if bio_parts:
            return ". ".join(bio_parts[:10]) + "."
        return text[:2000]


# ── Twitter/X ───────────────────────────────────────────────

class TwitterFetcher:
    """Fetch X/Twitter profile bio via Brave Search.

    The X API free tier is unusable (1 req/15 min). Instead, we search
    Brave for the profile page and extract the bio from the search snippet.
    This gives us the bio, display name, and sometimes follower count.

    Requires: BRAVE_API_KEY environment variable.
    """

    def __init__(self, brave_api_key: str | None = None):
        self.api_key = brave_api_key or os.environ.get("BRAVE_API_KEY", "")

    def fetch(self, url: str) -> FetchResult:
        if not self.api_key:
            return FetchResult(
                success=False,
                error="BRAVE_API_KEY not set. See enrichment/SETUP.md for setup instructions.",
            )

        handle = self._extract_handle(url)
        if not handle:
            return FetchResult(success=False, error="Could not parse X handle from URL")

        try:
            # Search Brave for this X profile
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": self.api_key},
                params={"q": f"site:x.com/{handle} OR site:twitter.com/{handle}", "count": 5},
                timeout=10,
            )
            if resp.status_code != 200:
                return FetchResult(success=False, error=f"Brave API {resp.status_code}")

            results = resp.json().get("web", {}).get("results", [])
            if not results:
                return FetchResult(success=False, error="No search results found for this X profile")

            # Find the profile page result (URL is x.com/handle or twitter.com/handle, no /status/)
            parts = []
            profile_result = None
            for r in results:
                result_url = r.get("url", "").lower().rstrip("/")
                # Match the profile page, not individual tweets
                if (result_url.endswith(f"/{handle.lower()}")
                        and ("x.com" in result_url or "twitter.com" in result_url)):
                    profile_result = r
                    break

            if not profile_result:
                # Fall back to first result that mentions the handle
                for r in results:
                    if handle.lower() in r.get("url", "").lower():
                        profile_result = r
                        break

            if profile_result:
                title = profile_result.get("title", "")
                desc = profile_result.get("description", "")
                # Clean up title: "Elon Musk (@elonmusk) / X" → "Elon Musk (@elonmusk)"
                if title:
                    title = re.sub(r"\s*/\s*X\s*$", "", title).strip()
                    title = re.sub(r"\s*/\s*Twitter\s*$", "", title).strip()
                    parts.append(f"X/Twitter: {title}")
                if desc:
                    # Filter out JS-disabled messages
                    if "javascript is not available" not in desc.lower():
                        # Clean HTML tags from description
                        clean_desc = re.sub(r"<[^>]+>", "", desc).strip()
                        parts.append(f"Bio: {clean_desc}")

            if not parts:
                return FetchResult(success=False, error="Could not extract bio from search results")

            return FetchResult(success=True, text="\n".join(parts), source="brave_search")

        except Exception as e:
            return FetchResult(success=False, error=str(e))

    def _extract_handle(self, url: str) -> Optional[str]:
        # Handle both x.com and twitter.com URLs
        match = re.search(r"(?:x\.com|twitter\.com)/([a-zA-Z0-9_]+)", url)
        if match:
            handle = match.group(1)
            # Filter out non-profile paths
            if handle.lower() in ("search", "explore", "home", "settings", "i", "intent"):
                return None
            return handle
        return None


# ── Google Drive ────────────────────────────────────────────

class GoogleDriveFetcher:
    """Fetch document content from Google Drive via gws CLI.

    Supports:
    - Google Docs → exported as plain text
    - PDFs → downloaded, text extracted via python
    - Other files → downloaded, best-effort text extraction

    Requires: gws CLI installed and configured.
    See enrichment/SETUP.md for setup instructions.

    Uses gws-personal config by default. Override with GWS_CONFIG_DIR env var.
    """

    def __init__(self, config_dir: str | None = None):
        self.config_dir = config_dir or os.environ.get(
            "GWS_CONFIG_DIR",
            os.path.expanduser("~/.config/gws-personal"),
        )

    def fetch(self, url: str) -> FetchResult:
        file_id = self._extract_file_id(url)
        if not file_id:
            return FetchResult(success=False, error="Could not parse Google Drive file ID from URL")

        # Check if gws is available
        if not self._gws_available():
            return FetchResult(
                success=False,
                error="gws CLI not found. See enrichment/SETUP.md for setup instructions.",
            )

        try:
            # Get file metadata to determine type
            meta = self._get_metadata(file_id)
            if not meta:
                return FetchResult(
                    success=False,
                    error="Could not access file. It may be private or the link may be invalid.",
                )

            mime = meta.get("mimeType", "")
            name = meta.get("name", "unknown")

            # Google Docs → export as text
            if mime == "application/vnd.google-apps.document":
                text = self._export_as_text(file_id)
                if text:
                    return FetchResult(success=True, text=text[:5000], source="gdrive_export")
                return FetchResult(success=False, error="Failed to export Google Doc as text")

            # Google Sheets → export as CSV (limited use for profiles, but capture it)
            if mime == "application/vnd.google-apps.spreadsheet":
                return FetchResult(success=False, error="Google Sheets not supported for text extraction")

            # PDFs and other downloadable files
            if mime == "application/pdf":
                text = self._download_and_extract_pdf(file_id, name)
                if text:
                    return FetchResult(success=True, text=text[:5000], source="gdrive_pdf")
                return FetchResult(success=False, error="Failed to extract text from PDF")

            # Other files → try downloading as text
            text = self._download_as_text(file_id, name)
            if text:
                return FetchResult(success=True, text=text[:5000], source="gdrive_download")
            return FetchResult(success=False, error=f"Unsupported file type: {mime}")

        except Exception as e:
            return FetchResult(success=False, error=str(e))

    def _extract_file_id(self, url: str) -> Optional[str]:
        """Extract file ID from various Google Drive URL formats."""
        # drive.google.com/file/d/FILE_ID/view
        match = re.search(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        # drive.google.com/open?id=FILE_ID
        match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        # docs.google.com/document/d/FILE_ID
        match = re.search(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        # docs.google.com/spreadsheets/d/FILE_ID
        match = re.search(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        return None

    def _gws_available(self) -> bool:
        try:
            result = self._run_gws("--version")
            return result is not None
        except Exception:
            return False

    def _run_gws(self, *args, output_file: str | None = None) -> Optional[str]:
        """Run a gws command via shell (sources gcloud path for auth).

        Note: gws CLI restricts -o paths to be within the current working
        directory. We cd to the output file's parent directory to handle this.
        """
        env = {**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": self.config_dir}

        gcloud_path = os.path.expanduser("~/google-cloud-sdk/path.zsh.inc")
        source_cmd = f"source '{gcloud_path}' 2>/dev/null; " if os.path.exists(gcloud_path) else ""

        # Quote args that contain JSON
        quoted_args = []
        for a in args:
            if "{" in a or " " in a:
                quoted_args.append(f"'{a}'")
            else:
                quoted_args.append(a)

        cmd = source_cmd
        if output_file:
            # cd to the file's directory so -o uses a relative path
            out_dir = os.path.dirname(os.path.abspath(output_file))
            out_name = os.path.basename(output_file)
            cmd += f"cd '{out_dir}' && gws " + " ".join(quoted_args) + f" -o '{out_name}'"
        else:
            cmd += "gws " + " ".join(quoted_args)

        try:
            result = subprocess.run(
                ["zsh", "-c", cmd],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, Exception):
            return None

    def _get_metadata(self, file_id: str) -> Optional[dict]:
        output = self._run_gws(
            "drive", "files", "get",
            "--params", json.dumps({"fileId": file_id, "fields": "id,name,mimeType,size"}),
        )
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return None

    def _export_as_text(self, file_id: str) -> Optional[str]:
        """Export a Google Doc as plain text."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            tmp_path = f.name

        try:
            self._run_gws(
                "drive", "files", "export",
                "--params", json.dumps({"fileId": file_id, "mimeType": "text/plain"}),
                output_file=tmp_path,
            )
            if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                with open(tmp_path, encoding="utf-8", errors="replace") as f:
                    return f.read()
            return None
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _download_and_extract_pdf(self, file_id: str, name: str) -> Optional[str]:
        """Download a PDF and extract text."""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp_path = f.name

        try:
            self._run_gws(
                "drive", "files", "get",
                "--params", json.dumps({"fileId": file_id, "alt": "media"}),
                output_file=tmp_path,
            )
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                return None

            # Try PyPDF2 or pdfplumber for text extraction
            try:
                import PyPDF2
                with open(tmp_path, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    text_parts = []
                    for page in reader.pages[:10]:  # first 10 pages
                        text_parts.append(page.extract_text() or "")
                    return "\n".join(text_parts).strip()
            except ImportError:
                pass

            # Fallback: try pdfplumber
            try:
                import pdfplumber
                with pdfplumber.open(tmp_path) as pdf:
                    text_parts = []
                    for page in pdf.pages[:10]:
                        text_parts.append(page.extract_text() or "")
                    return "\n".join(text_parts).strip()
            except ImportError:
                return f"[PDF: {name} — install PyPDF2 or pdfplumber for text extraction: pip install PyPDF2]"

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _download_as_text(self, file_id: str, name: str) -> Optional[str]:
        """Download a file and try to read as text."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name

        try:
            self._run_gws(
                "drive", "files", "get",
                "--params", json.dumps({"fileId": file_id, "alt": "media"}),
                output_file=tmp_path,
            )
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                return None

            with open(tmp_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            # Sanity check: if it's mostly binary, skip
            printable_ratio = sum(c.isprintable() or c.isspace() for c in text[:1000]) / max(len(text[:1000]), 1)
            if printable_ratio < 0.8:
                return None
            return text[:5000]
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


# ── Registry ────────────────────────────────────────────────

FETCHERS = {
    "github": GitHubFetcher(),
    "website": WebsiteFetcher(),
    "twitter": TwitterFetcher(),
    "gdrive": GoogleDriveFetcher(),
}


def fetch_link(url: str, link_type: str | None = None) -> FetchResult:
    """Fetch content from a URL, auto-detecting the type if not specified."""
    if not url:
        return FetchResult(success=False, error="No URL")

    url_lower = url.lower()

    if link_type:
        fetcher = FETCHERS.get(link_type)
    elif "github.com" in url_lower:
        fetcher = FETCHERS["github"]
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        fetcher = FETCHERS["twitter"]
    elif "drive.google.com" in url_lower or "docs.google.com" in url_lower:
        fetcher = FETCHERS["gdrive"]
    else:
        fetcher = FETCHERS["website"]

    if not fetcher:
        return FetchResult(success=False, error=f"Unknown link type: {link_type}")

    return fetcher.fetch(url)


def fetch_all_links(
    twitter_url: str = "",
    website_url: str = "",
    resume_url: str = "",
    other_links: list[str] | None = None,
) -> dict[str, FetchResult]:
    """Fetch content from all available links on a profile."""
    results = {}

    if twitter_url:
        results["twitter"] = fetch_link(twitter_url, "twitter")
    if website_url:
        results["website"] = fetch_link(website_url, "website")
    if resume_url:
        results["resume"] = fetch_link(resume_url)  # auto-detect (could be GDrive, Dropbox, etc.)
    for i, link in enumerate(other_links or []):
        results[f"link_{i}"] = fetch_link(link)

    return results
