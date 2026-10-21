#!/usr/bin/env bash
# First-run validation against a REAL cluster: run fastsacct and real
# sacct with the same query, then diff them with cluster_diff.py (the
# Rust port's version of the original mila validation methodology, see
# ARCHITECTURE.md). Run this on a cluster login node.
#
#   ./cluster-check.sh -L /usr/lib64/libslurm.so -A <acct> \
#       -S 2026-10-01 -E 2026-10-02 [--abi 25.05] [--debug]
#
# Expects the fastsacct binary at ./target/release/fastsacct (override
# with BIN=...) and python3 on PATH.
set -euo pipefail
cd "$(dirname "$0")"

BIN=${BIN:-./target/release/fastsacct}
LIB="" ACCOUNTS="" ABI_ARGS=() FA_EXTRA=()
START="" END=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -L|--library) LIB="$2"; shift 2;;
    -A|--accounts) ACCOUNTS="$2"; shift 2;;
    -S|--starttime) START="$2"; shift 2;;
    -E|--endtime) END="$2"; shift 2;;
    --abi) ABI_ARGS=(--abi "$2"); shift 2;;
    --debug) FA_EXTRA+=(--debug); shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$LIB" && -n "$START" && -n "$END" ]] || {
  echo "usage: $0 -L <libslurm.so> -A <accounts> -S <start> -E <end> [--abi V] [--debug]" >&2
  exit 2
}

OUT=${OUT:-/tmp/fastsacct-cluster}
mkdir -p "$OUT"

# 1. fastsacct, blob mode, with --debug diagnostics on stderr.
# (The ${arr[@]+"${arr[@]}"} guards keep empty arrays happy under `set -u`
# on the bash < 4.4 some cluster login nodes still ship.)
TZ=UTC "$BIN" ${ACCOUNTS:+-A "$ACCOUNTS"} -S "$START" -E "$END" -D -X -a --json \
    --library "$LIB" ${ABI_ARGS[@]+"${ABI_ARGS[@]}"} ${FA_EXTRA[@]+"${FA_EXTRA[@]}"} \
    > "$OUT/fastsacct.json" 2> "$OUT/fastsacct.debug"
echo "== fastsacct diagnostics:"; cat "$OUT/fastsacct.debug"
echo "   $(python3 -c 'import json;print(json.load(open("'"$OUT"'/fastsacct.json"))["meta"])')"

# 2. real sacct, same query, --json.
TZ=UTC sacct ${ACCOUNTS:+-A "$ACCOUNTS"} -S "$START" -E "$END" -D -X -a --json \
    > "$OUT/sacct.json"
# (fastsacct treats -S/-E as UTC always; sacct interprets them in the
# local zone, so both run under TZ=UTC here too. If the cluster's slurmdbd
# stores in a different zone the two are still comparable because both
# tools send/query the same epoch window.)

# 3. diff (exit code 1 on any hard-field mismatch).
echo "== diff:"
python3 cluster_diff.py "$OUT/fastsacct.json" "$OUT/sacct.json" \
  || { echo; echo "adapting key names? see: python3 cluster_diff.py $OUT/fastsacct.json $OUT/sacct.json --dump-keys"; exit 1; }
