#!/usr/bin/env bash
# Manual smoke run against the four mock ABIs (not part of cargo test).
set -euo pipefail
cd "$(dirname "$0")"
BIN=${BIN:-./target/debug/fastsacct}
export TZ=UTC

for v in 24.11 25.05 25.11 26.05; do
  out=$($BIN -S 2026-01-01 -E 2026-01-02 -D -X -a --json \
        --library /tmp/fa-mocks/lib_$v.so --abi $v 2>/dev/null)
  echo "$out" | LIBV=$v python3 -c '
import json, sys, os
d = json.load(sys.stdin)
j = d["jobs"][0]
v = os.environ["LIBV"]
extras = {
    "24.11": ("lft" in j, "resv_req" not in j, "exclusive" not in j),
    "25.05": ("lft" not in j, "resv_req" in j, "exclusive" not in j),
    "25.11": ("lft" not in j, "resv_req" in j, "exclusive" not in j),
    "26.05": ("lft" not in j, "resv_req" in j, "exclusive" in j and j["sluid"] == 0x123456789),
}[v]
print(v, "count:", d["meta"]["job_count"], "state:", j["state"], "qos:", j["qos"],
      "alloc_node:", j["allocated_node"], "ttlim:", j["time_timelimit"], "extras ok:", all(extras))
'
done

echo "--- jsonl (26.05) ---"
$BIN -S 2026-01-01 -E 2026-01-02 -D -X -a --json --jsonl \
     --library /tmp/fa-mocks/lib_26.05.so --abi 26.05 2>/dev/null | python3 -c '
import json, sys
lines = sys.stdin.read().splitlines()
print("lines:", len(lines), "(1 meta + %d jobs)" % (len(lines) - 1))
print("meta:", json.loads(lines[0])["meta"])
names = [json.loads(l)["name"] for l in lines[1:]]
print("weird name roundtrip:", repr(names[-1]))
'

echo "--- debug (25.05) ---"
$BIN -S 2026-01-01 -E now -D -X -a --json --library /tmp/fa-mocks/lib_25.05.so \
     --abi 25.05 --debug 2>&1 >/dev/null | grep -E "slurm_conf|job_cond before|returned|fetched" || true
