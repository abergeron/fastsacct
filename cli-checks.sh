#!/usr/bin/env bash
# CLI error-path checks (manual, mirrors the old argparse behavior).
cd "$(dirname "$0")"
BIN=./target/debug/fastsacct
M=(-S 2026-01-01 -E 2026-01-02 -D -X -a --json --library /tmp/fa-mocks/lib_25.05.so)

check() { # desc, expected_rc, env-assignments..., args...
  desc="$1"; want="$2"; shift 2
  out=$(env TZ=UTC "$@" 2>&1 >/dev/null); rc=$?
  status=$([ "$rc" = "$want" ] && echo PASS || echo "FAIL(want $want)")
  echo "== $status rc=$rc | $desc"
  echo "$out" | grep -v "^usage:" | tail -2 | sed 's/^/   /'
}

check "missing markers" 2 TZ=UTC $BIN -S 2026-01-01 -E 2026-01-02
check "start after end" 2 TZ=UTC $BIN -S 2026-01-02 -E 2026-01-01 -D -X -a --json
check "bad time format" 2 TZ=UTC $BIN -S now+1h -E 2026-01-02 -D -X -a --json
check "bad --abi" 2 TZ=UTC $BIN --abi 99.99 -S 2026-01-01 -E 2026-01-02 -D -X -a --json
check "unknown flag" 2 TZ=UTC $BIN -P "${M[@]}"
check "non-UTC TZ rejected" 1 TZ=America/New_York $BIN "${M[@]}"
check "missing library" 1 TZ=UTC $BIN -S 2026-01-01 -E 2026-01-02 -D -X -a --json --library /nonexistent.so
check "combined -DXa shorts + ok run" 0 TZ=UTC $BIN -DXa --json -S 2026-01-01 -E 2026-01-02 --library /tmp/fa-mocks/lib_25.05.so
