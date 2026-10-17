#!/usr/bin/env bash
# Run a repository's GitHub Actions workflows locally with nektos/act
# (https://github.com/nektos/act; install with `gh extension install nektos/gh-act`,
# your package manager, or the release binary).
#
#   scripts/run-actions.sh REPO [EVENT] [JOB]
#   scripts/run-actions.sh claude                    list the jobs a pull request runs
#   scripts/run-actions.sh claude pull_request lint  run one job
#   DRY_RUN=1 scripts/run-actions.sh grok pull_request   plan every job, run nothing
#
# Safe for the service repositories: act copies the repository into its job containers
# (we never pass --bind), so workflow steps cannot modify the files or the git history.
# The default event is pull_request, under which no workflow here pushes images or
# deploys. Steps that need cloud credentials (the AWS deploy) are skipped or fail early.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${1:-.}"
EVENT="${2:-}"
JOB="${3:-}"
DIR="$ROOT/$REPO"

command -v act >/dev/null || { echo "act is not installed; see the header of $0" >&2; exit 1; }
[ -d "$DIR/.github/workflows" ] || { echo "$REPO has no .github/workflows" >&2; exit 1; }

# Runner images that match GitHub's ubuntu runners closely enough for these workflows,
# passed explicitly so act never prompts to create ~/.actrc.
platforms=(
  -P ubuntu-latest=catthehacker/ubuntu:act-24.04
  -P ubuntu-24.04=catthehacker/ubuntu:act-24.04
)
args=("${EVENT:-pull_request}" --directory "$DIR" "${platforms[@]}"
      --container-architecture linux/amd64 --artifact-server-path /tmp/act-artifacts)

if [ -z "$EVENT" ] && [ -z "$JOB" ]; then
  exec act "${args[@]}" --list
fi
[ -n "$JOB" ] && args+=(--job "$JOB")
[ -n "${DRY_RUN:-}" ] && args+=(--dryrun)
exec act "${args[@]}"
