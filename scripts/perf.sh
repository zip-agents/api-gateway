#!/usr/bin/env bash
# Benchmark every zip service through the local gateway (perf/bench.py), and record each
# service container's peak memory while it runs. The stack must be up (make up).
#
#   scripts/perf.sh                       claude and grok, 100 MiB uploads
#   SIZE_MIB=500 SERVICES=claude scripts/perf.sh
#
# Prints a markdown table and writes it, with a CSV, to perf/results/<timestamp>.*
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ENGINE:-docker}"
cd "$ROOT"
key="$(sed -n 's/^SMOKE_API_KEY=//p' .env)"
[ -n "$key" ] || { echo "no SMOKE_API_KEY in .env (make env)" >&2; exit 1; }
services="${SERVICES:-$(sed -n 's/^SMOKE_SERVICES=//p' .env)}"
services="${services:-claude,grok,dsh}"
project="$(basename "$ROOT")"
network="${COMPOSE_NETWORK:-${project}_default}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="perf/results/$stamp"
mkdir -p perf/results

"$ENGINE" build -q -t zip-perf:local perf >/dev/null

# Sample memory every second in the background; keep the peak per container.
samples="$(mktemp)"
trap 'rm -f "$samples"; kill "$sampler" 2>/dev/null || true' EXIT
(
  while true; do
    "$ENGINE" stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null \
      | grep -E "^${project}-(gateway|${services//,/|})-" >>"$samples" || true
    sleep 1
  done
) &
sampler=$!

set +e
# shellcheck disable=SC2016 # $? and $status expand inside the container
"$ENGINE" run --rm --network "$network" \
  -e GATEWAY_URL=http://gateway:8000 -e API_KEY="$key" -e SERVICES="$services" \
  -e SIZE_MIB="${SIZE_MIB:-100}" -e PARALLEL="${PARALLEL:-4}" -e CSV=/tmp/results.csv \
  -v "$ROOT/perf/results:/results" --entrypoint sh zip-perf:local \
  -c 'python bench.py; status=$?; cp /tmp/results.csv /results/latest.csv 2>/dev/null; exit $status' \
  | tee "$out.md"
status=${PIPESTATUS[0]}
set -e
[ -f perf/results/latest.csv ] && mv perf/results/latest.csv "$out.csv"

{
  echo
  echo "Peak memory during the run (docker stats, sampled each second):"
  echo
  echo "| container | peak |"
  echo "|---|---|"
  # "name 123.4MiB / 1GiB": keep the largest first figure per container, in MiB.
  awk '{
    v=$2; u=v; gsub(/[0-9.]/,"",u); n=v; gsub(/[^0-9.]/,"",n)
    m = (u=="GiB") ? n*1024 : (u=="KiB" || u=="kB") ? n/1024 : (u=="B") ? n/1048576 : n
    if (m > peak[$1]) peak[$1] = m
  } END { for (c in peak) printf "| %s | %.0f MiB |\n", c, peak[c] }' "$samples" | sort
} | tee -a "$out.md"
echo "== saved $out.md (bench exit $status)"
exit "$status"
