#!/usr/bin/env bash
# Commit the state/ folder and push it. Never force pushes.
#
#   bash scripts/commit_state.sh          normal mode: up to 3 push tries,
#                                         with "git pull --rebase" between tries
#   bash scripts/commit_state.sh strict   one plain push, no rebase. Fails if
#                                         the remote branch has moved. The
#                                         scanner runs this right before a
#                                         fire call, so a fire claim is in the
#                                         repo before the request is sent.
set -u
mode="${1:-normal}"
cd "$(git rev-parse --show-toplevel)" || exit 1
branch="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add state || exit 1
if ! git diff --cached --quiet; then
  git commit -q -m "scan $(date -u +%Y-%m-%dT%H:%M:%SZ)" || exit 1
fi

if [ "$mode" = "strict" ]; then
  git push -q origin "HEAD:${branch}"
  exit $?
fi

if ! git rev-parse --verify -q "refs/remotes/origin/${branch}" >/dev/null; then
  echo "origin/${branch} not found; state not pushed" >&2
  exit 1
fi

if [ -z "$(git log --oneline "origin/${branch}..HEAD")" ]; then
  echo "nothing to push"
  exit 0
fi

for try in 1 2 3; do
  if git push -q origin "HEAD:${branch}"; then
    echo "pushed on try ${try}"
    exit 0
  fi
  if ! git pull -q --rebase origin "${branch}"; then
    git rebase --abort 2>/dev/null
    echo "rebase failed; state not pushed"
    exit 1
  fi
  sleep 2
done
echo "push failed after 3 tries"
exit 1
