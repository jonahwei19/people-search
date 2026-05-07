#!/bin/bash
# Sync shared UI to cloud (sets APP_MODE to 'cloud') and stamps the build version.
# Set APP_BASE=/people-search when building for the EC2 (Caddy path-prefix) target.
set -euo pipefail

# Resolve build version. Priority: explicit env (Vercel) → git → baked VERSION file.
# We BAKE the version into ./VERSION at build time so a later run on a
# git-less box (EC2, where rsync excludes .git) can still produce the
# correct stamp instead of "unknown".
SHA=""
if [ -n "${VERCEL_GIT_COMMIT_SHA:-}" ]; then
  SHA="$VERCEL_GIT_COMMIT_SHA"
elif [ -d .git ] && SHA_TRY=$(git rev-parse HEAD 2>/dev/null); then
  # Require .git in the CURRENT directory — otherwise git walks up the tree
  # and we get the parent repo's HEAD, stamping a wrong commit.
  SHA="$SHA_TRY"
fi

if [ -n "$SHA" ]; then
  SHORT="${SHA:0:7}"
  DATE=$(git show -s --format=%cs "$SHA" 2>/dev/null || date +%Y-%m-%d)
  SUBJECT=$(git show -s --format=%s "$SHA" 2>/dev/null | tr -d '\n' | tr -c '[:alnum:][:space:]._-' '_' | cut -c1-80 || echo "")
elif [ -f VERSION ]; then
  # Fallback: read the previous build's stamp from the baked file.
  IFS= read -r BAKED < VERSION
  SHORT="${BAKED%%|*}"
  rest="${BAKED#*|}"
  DATE="${rest%%|*}"
  SUBJECT="${rest#*|}"
else
  SHORT="unknown"
  DATE=$(date +%Y-%m-%d)
  SUBJECT=""
fi

VERSION="${SHORT} · ${DATE}"
TITLE="${SHORT} · ${DATE} — ${SUBJECT}"
APP_BASE="${APP_BASE:-}"

# Bake the resolved values for next time. Pipe-delimited so subjects with
# spaces/dashes survive parsing.
if [ -n "$SHA" ]; then
  printf "%s|%s|%s\n" "$SHORT" "$DATE" "$SUBJECT" > VERSION
fi

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
