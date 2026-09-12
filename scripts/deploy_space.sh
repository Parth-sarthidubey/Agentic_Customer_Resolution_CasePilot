#!/usr/bin/env bash
# Push CasePilot to a Hugging Face Space.
#
#   ./scripts/deploy_space.sh <hf-username> <hf-write-token> [space-name]
#
# Create the write token at https://huggingface.co/settings/tokens (role: write).
# The Space is created automatically if it does not exist.
set -euo pipefail

USER="${1:?usage: deploy_space.sh <hf-username> <hf-write-token> [space-name]}"
TOKEN="${2:?missing write token}"
SPACE="${3:-casepilot}"
REPO="https://huggingface.co/spaces/${USER}/${SPACE}"

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "==> Committing working changes"
  git add -A && git commit -q -m "Deploy to Hugging Face Space"
fi

echo "==> Creating the Space (ignored if it already exists)"
curl -sS -X POST https://huggingface.co/api/repos/create \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -d "{\"type\":\"space\",\"name\":\"${SPACE}\",\"sdk\":\"docker\",\"private\":false}" \
  | head -c 400; echo

echo "==> Pushing to ${REPO}"
git remote remove space 2>/dev/null || true
git remote add space "https://${USER}:${TOKEN}@huggingface.co/spaces/${USER}/${SPACE}"
git push --force space HEAD:main
git remote remove space   # do not leave the token in .git/config

echo
echo "Done. The Space is building - watch the Logs tab:"
echo "  ${REPO}"
echo "First build takes about 3-5 minutes."
echo
echo "Optional, for real model reasoning instead of the offline rules:"
echo "  Settings -> Variables and secrets -> New secret"
echo "  GEMINI_API_KEY = a free key from https://aistudio.google.com/apikey"
