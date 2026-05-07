#!/bin/bash
# Deploy people-search to the agents EC2 box.
#
#   bash tools/aws/deploy.sh        # full deploy (build + rsync + restart)
#   bash tools/aws/deploy.sh quick  # rsync only (no venv update, no service restart)
#
# Reads from the project root. Requires `ssh agents` to work.

set -euo pipefail

cd "$(dirname "$0")/../.."

MODE="${1:-full}"
HOST="agents"
REMOTE="$HOST:~/agents/people-search/"

# Build with the EC2 path prefix baked into the HTML.
APP_BASE=/people-search bash build.sh

rsync -az --delete \
  --exclude='.git' \
  --exclude='.git/**' \
  --exclude='__pycache__' \
  --exclude='**/__pycache__' \
  --exclude='*.pyc' \
  --exclude='.venv' \
  --exclude='node_modules' \
  --exclude='archive' \
  --exclude='datasets' \
  --exclude='uploads' \
  --exclude='.claude' \
  --exclude='.env' \
  --exclude='plans' \
  --exclude='tests' \
  --exclude='enrichment/eval' \
  --exclude='enrichment/test_fixtures' \
  ./ "$REMOTE"

if [ "$MODE" = "quick" ]; then
  echo "Quick deploy done (no venv update, no restart)."
  exit 0
fi

# Refresh deps if requirements.txt changed since last deploy.
ssh "$HOST" 'cd ~/agents/people-search && .venv/bin/pip install --quiet -r requirements.txt'
ssh "$HOST" 'sudo systemctl restart people-search && sleep 1 && sudo systemctl status people-search --no-pager | head -5'

# Restore the local build to the Vercel default so a stray git push doesn't
# ship the EC2-prefixed HTML.
bash build.sh > /dev/null

echo "Deployed."
