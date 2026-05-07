#!/bin/bash
# Sync shared UI to cloud (sets APP_MODE to 'cloud') and stamps the build version.
# Set APP_BASE=/people-search when building for the EC2 (Caddy path-prefix) target.
set -euo pipefail

# Vercel sets VERCEL_GIT_COMMIT_SHA on deploy; locally we fall back to git.
SHA="${VERCEL_GIT_COMMIT_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
SHORT="${SHA:0:7}"
DATE=$(git show -s --format=%cs "$SHA" 2>/dev/null || date +%Y-%m-%d)
SUBJECT=$(git show -s --format=%s "$SHA" 2>/dev/null | tr -d '\n' | tr -c '[:alnum:][:space:]._-' '_' | cut -c1-80 || echo "")
VERSION="${SHORT} · ${DATE}"
TITLE="${SHORT} · ${DATE} — ${SUBJECT}"
APP_BASE="${APP_BASE:-}"

build_one() {
  local out="$1"
  awk -v mode="cloud" -v ver="$VERSION" -v title="$TITLE" -v base="$APP_BASE" '
    {
      gsub(/'\''\{\{APP_MODE\}\}'\''/, "'\''" mode "'\''");
      gsub(/'\''\{\{APP_BASE\}\}'\''/, "'\''" base "'\''");
      gsub(/\{\{APP_VERSION_TITLE\}\}/, title);
      gsub(/\{\{APP_VERSION\}\}/, ver);
      print;
    }
  ' shared/ui.html > "$out"
}

build_one cloud/public/index.html
build_one public/index.html
echo "Synced shared/ui.html → cloud/public/index.html + public/index.html (version: ${VERSION}${APP_BASE:+, base: $APP_BASE})"
