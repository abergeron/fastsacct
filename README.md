# fastsacct

A fast, narrow replacement for one specific `sacct` invocation shape:

```
sacct -A <accounts> -S <start> -E <end> -D -X -a --json
```

It calls `slurmdb_jobs_get()` directly via `cffi` (the same libslurmdb.so
API `sacct` itself uses) and skips Slurm's `data_parser` plugin, which is
the actual source of `sacct --json`'s slowness (a generic, reflection-driven
OpenAPI serializer that walks every schema field for every job) — not the
RPC itself.

**Status as of 2026-08-05**: working and validated against a real cluster
(mila, Slurm 25.05.2). `--full` mode's output was diffed field-by-field
against real `sacct --json` output for the exact same query (2003 real
jobs) — see "Validation" below. Both output modes are usable; `--full` is
new and less road-tested than flat, but the diff gives real confidence.

## Quick start

This is a [uv](https://docs.astral.sh/uv/) project; `uv run` resolves and
installs dependencies (just `cffi`) into a local venv on first use.

```
uv run python fastsacct.py -A <accounts> -S <start> -E <end> -D -X -a --json \
    --library /path/to/libslurm.so
```

`-D`/`-X`/`-a`/`--json` are all **required** markers, not optional flags —
see the module docstring in `fastsacct.py` for why (fastsacct always
behaves as if they were passed; requiring them at the CLI makes any
sacct invocation that would behave differently fail loudly instead of
silently mismatching). `-S`/`-E` only accept `YYYY-MM-DD[THH:MM[:SS]]` or
`now`, not sacct's full relative-time grammar.

Useful flags for diagnosis, in rough order of how often you'll want them:

- `--debug` — prints the resolved `job_cond` fields, the raw
  `slurmdb_jobs_get()` result count (before any Python-side processing),
  and which `AccountingStorageHost`/port/type `slurm_conf` resolved to
  after `slurm_init()`. Always reach for this first when output looks
  wrong — it tells you whether the problem is upstream of fastsacct
  (connection/query) or in fastsacct's own formatting.
- `--full` — emit `sacct --json`'s exact nested schema instead of the flat
  one. See "Two output modes" below.
- `--abi {24.11,25.05,25.11}` — override auto-detection (see `detect_abi()`).
- `--library` — path to `libslurm.so`/`libslurmdb.so`. Defaults to
  `libslurmdb.so` resolved via the normal linker search path, but most
  clusters only ship `libslurm.so` (see the RTLD_GLOBAL gotcha below) —
  pass the real path explicitly if in doubt.

## Two output modes

**Flat (default)**: one flat JSON object per job, `json_key: raw_value`,
using mostly-literal C struct field names as keys (`abi/*.py`'s
`JOB_FIELDS`). Originally "no Python processing at all" for maximum speed;
now "light Python processing" instead — a few fields get cheap, no-extra-
RPC-per-job treatment where leaving them raw would just push the same
parsing work onto every downstream consumer:

- `qos` is resolved to a name (matching `--full`'s `qos` field) via the
  same one-time `slurmdb_qos_get()` id->name map both modes now fetch --
  no RPC per job. Replaces `qosid` rather than sitting alongside it.
- `state` is a list of decoded state name strings (e.g. `["COMPLETED"]`,
  or `["COMPLETED", "COMPLETING"]` while cleanup is still in flight),
  matching `--full`'s `state/current` field -- same `full_format.job_state()`
  base-state + flag-bit decode `--full` uses, no RPC, so it's cheap
  enough to keep even in flat mode.
- `flags` is a list of decoded flag name strings (e.g. `["STARTED_ON_SUBMIT"]`,
  or `["NONE"]` when unset), matching `--full`'s `flags` field -- same
  `full_format.job_flags()` bit-table decode `--full` uses, no RPC, so
  it's cheap enough to keep even in flat mode.
- `group` is resolved to a name (matching `--full`'s `group` field) via the
  same local `getgrgid` NSS lookup `--full` uses -- no RPC. `gid` is still
  emitted alongside it as the raw id.
- `array_task_id` is `null`, not the raw `NO_VAL`/`INFINITE` sentinel
  (`4294967294`/`4294967295`), for a non-array job -- same sentinel check
  `--full`'s `no_val()` does for this field, just collapsed to a plain
  `null` instead of full's `{"set","infinite","number"}` wrapping.
- The epoch-timestamp/duration fields all carry a `time_` prefix --
  `time_eligible`, `time_end`, `time_elapsed`, `time_start`, `time_submit`,
  `time_suspended`, `time_timelimit` -- values are still the same raw
  seconds/minutes, just renamed so they're easy to pick out from the rest
  of the flat fields. The CPU-usage counters (`sys_cpu_sec`, `tot_cpu_sec`,
  `user_cpu_sec`, and their `_usec` siblings) are left as-is -- they're
  already qualified by `sys_`/`tot_`/`user_`.
- `tres_alloc_str`/`tres_req_str` (e.g. `"1=4,2=17179869184,1001=2"`) are
  still emitted raw, but also expanded into `allocated_<type>[_<name>]`/
  `requested_<type>[_<name>]` fields (e.g. `allocated_cpu`, `allocated_mem`,
  `allocated_gres_gpu`) using the one-time `slurmdb_tres_get()` id->name map
  both modes now fetch. Sentinel counts that round-trip through the wire's
  unsigned format (e.g. `18446744073709551614`, which is really `-2` --
  see `full_format.parse_tres`) are reinterpreted back to a real negative
  number, same as `--full` does. A GRES name's optional model/type suffix
  (e.g. `"gpu:a100"`) is kept out of the key -- always `allocated_gres_gpu`,
  never `allocated_gres_gpu_a100` -- and surfaced instead as a separate
  `allocated_gres_gpu_type: "a100"` field when present.
- `exitcode` (a Unix wait-status int) is still emitted raw, but also
  expanded into `exitcode_return_code`/`exitcode_signal` -- the plain
  number (or `null` if not applicable), reusing `full_format.
  process_exit_code()`'s WIFEXITED/WIFSIGNALED decoding for the
  WEXITSTATUS/WTERMSIG bit-twiddling but without full's
  `{"set","infinite","number"}` wrapping or `status`/signal-name lookup.
  `derived_ec` is left as raw only, for now.

Good enough if your downstream consumer is your own code and you're fine
post-processing the remaining raw fields yourself.

**`--full`**: byte-for-byte the same nested JSON shape real `sacct --json`
produces (state/flags/reason decoded to strings, NO_VAL-aware
`{"set","infinite","number"}` wrapping, uid/gid/qos resolved to names,
TRES strings parsed into structured lists, stdio `%`-expansion, nested
paths like `time/elapsed`, `array/task_id`). Costs two extra one-time RPCs
(`slurmdb_tres_get`, `slurmdb_qos_get`, plus `slurmdb_associations_get` on
24.11 only) before the job loop, then pure-Python dict-building per job —
no RPC per job. Not benchmarked head-to-head against real `sacct --json`
on a large query yet; the working assumption (from where the slowness
actually comes from, per the module docstring) is a modest constant-factor
cost, not the order-of-magnitude gap that makes real `sacct --json` slow.

## Architecture

```
fastsacct.py       CLI, argument parsing, cffi/RPC plumbing, Slurmdb class,
                   flat-mode formatter (_job_to_dict), full-mode formatter
                   (_job_to_full_dict)
abi/v25_11.py      Slurm 25.11 ABI: struct layouts (CDEF), JOBCOND_FLAG_*
                   values, flat-mode JOB_FIELDS, and the ONE piece of
                   full-mode rendering that's genuinely version-specific
                   (job_assoc/fetch_assoc_list/stdio_node — see below)
abi/v25_05.py      Same, for Slurm 25.05
abi/v24_11.py      Same, for Slurm 24.11
full_format.py     Everything else full-mode needs, shared across ABI
                   versions because it was confirmed byte-for-byte
                   identical between 24.11, 25.05, and 25.11 by diffing
                   all three releases' data_parser plugin source (plus two
                   purely additive 25.11 exceptions folded in directly —
                   a new SLURMDB_JOB_FLAGS bit and a new JOB_STATE flag
                   bit, both harmless no-ops on older releases)
mock/              A fake libslurmdb.so, compiled against vendored copies
                   of the real slurm/slurm.h + slurmdb.h (mock/include/),
                   for local testing without a real cluster
```

Adding a new Slurm release's ABI module: see the docstring at the top of
`abi/v25_05.py` — across 22.05→26.05, `slurmdb_job_rec_t`/
`slurmdb_job_cond_t` have only ever grown by appending fields, so it's a
diff-and-copy job, not a rewrite. Bump `SLURM_ABI_VERSION`/
`SLURM_API_MAJOR`, add fields to `CDEF`, and if you want them in flat
output, to `JOB_FIELDS`.

### Why `abi/*.py` and `full_format.py` are split the way they are

Every full-mode table/algorithm was checked against Slurm 24.11
(`data_parser` v0.0.42), 25.05 (v0.0.43), and 25.11 (v0.0.44) source by
direct diff before being written down — not assumed identical. One piece
turned out to actually differ across releases in a way that needs real
per-version code: `JOB_ASSOC_ID` (the `"association"` field). On
25.05/25.11 it's synthesized locally with no RPC (`job_cond.associd`+
`cluster`, nothing else — yes, real `sacct --json` on those releases also
never populates `account`/`partition`/`user` there, that's not a shortcut
we're taking). On 24.11 it needs a real `slurmdb_associations_get()` call
plus a **24.11-specific** `slurmdb_assoc_rec_t` layout (two extra
`uint32_t` fields, `lft`/`rgt`, that 25.05 removed — confirmed by diffing
against `origin/slurm-24.11:slurm/slurmdb.h` directly, not guessed).
That's why `job_assoc()`/`fetch_assoc_list()`/`stdio_node()` live in
`abi/*.py` instead of `full_format.py`.

25.11 did add two small, purely *additive* pieces to the shared tables in
`full_format.py` itself rather than a per-version module: a new
`SLURMDB_JOB_FLAGS` bit (`JOB_ALTERED`, bit 5) and a new `JOB_STATE` flag
bit (`EXPEDITING`, bit 24), plus filling in `state/reason` code 17
(`NvidiaImexChannels`) — previously a reserved-but-unused placeholder
(`DEFUNCT_WAIT_17`) in 24.11/25.05, not a renumbering of any other reason
code. All three are safe to keep unconditional across every ABI version
since older `libslurmdb.so` builds simply never set that bit/code on a
real job.

## Validation

On 2026-08-05, ran the exact same real query on the mila cluster (Slurm
25.05.2, 2003 jobs) through both real `sacct --json` (→ `sample.json`) and
`fastsacct.py --full` (→ `test.json`), then diffed every leaf value across
every job. Before fixing one bug (below), the only differences were:

- `tres/allocated`/`tres/requested` `count` for TRES entries with a
  negative sentinel value (e.g. `energy` showing `-2` when unavailable):
  real sacct showed `-2`, fastsacct showed `18446744073709551614` (the
  same bit pattern read as unsigned instead of signed int64). **Fixed**
  in `full_format.parse_tres()` — `tres_alloc_str`/`tres_req_str` are
  written with an unsigned format specifier
  (`assoc_mgr_make_tres_str_from_array()`: `"%u=%"PRIu64`) but the
  data_parser dumps a TRES entry's `count` as `INT64`, so a value ≥ 2⁶³
  needs reinterpreting as its two's-complement signed equivalent.
- `time/planned` and `time/elapsed` for still-pending/still-running jobs:
  differed by a **uniform 33 seconds** across every affected job — not a
  bug, just wall-clock skew between when the two captures were taken
  (both fields are computed live from `time(NULL)` for jobs without a
  fixed start/end yet).

After the TRES fix, **zero remaining differences** across all 2003 jobs
(excluding the two inherently time-skewed fields). `sample.json`/
`test.json` are still sitting in this directory as the artifacts of that
run — they contain **real job data from a real cluster** (usernames,
working directories, submit lines, account names). Don't commit them or
share this directory as-is; regenerate or delete them once you're done
referring back to this validation run. (This whole directory is currently
untracked by git, so nothing's actually been committed — just don't `git
add -A` here without checking first.)

## Testing locally (no cluster needed)

`mock/mock_libslurmdb.c` is a fake `libslurmdb.so` compiled against
vendored copies of the real `slurm/slurm.h`+`slurmdb.h`+`slurm_errno.h`
(under `mock/include/slurm/`, pulled from a slurm-25.05 checkout), so it
shares the exact struct layout fastsacct's cffi `cdef` is meant to
describe. It exercises both directions: fastsacct writes a `job_cond` →
mock prints what it received (confirms Python wrote at the right
offsets); mock fabricates job records → fastsacct reads them back
(confirms Python reads from the right offsets). It also fakes
`slurmdb_tres_get()`/`slurmdb_qos_get()` with real id/name tables matching
what the fabricated jobs use, so `--full`'s id-resolution path has
something real to resolve.

It does **not** prove the vendored struct in `abi/v25_05.py` matches
whatever real `libslurmdb.so` is on a given cluster node — only that
fastsacct's own harness (arg parsing, list walking, JSON emission,
formatting logic) is correct. `slurmdb_associations_get()` (the 24.11-only
RPC) is a stub that returns an empty list on purpose — this mock is always
compiled against the vendored 25.05 `slurmdb.h`, which is NOT 24.11's
`slurmdb_assoc_rec_t` layout, so exercising it with real data here would
risk a false pass/fail on the wrong struct. The 24.11 struct was instead
verified independently against `origin/slurm-24.11`.

`mock/include/slurm/slurm_version.h` is hand-written, not vendored — it's
normally generated by `configure` and isn't present in a bare Slurm
checkout, so this is a stub with a hardcoded version number instead.

```bash
clang -shared -fPIC -Imock/include -o /tmp/mock_libslurmdb.so \
    mock/mock_libslurmdb.c

uv run python fastsacct.py -S 2026-01-01 -E 2026-01-02 -D -X -a \
    --json --library /tmp/mock_libslurmdb.so --debug --full
```

Pass `-DMOCK_API_MAJOR=42` to `clang` to make the mock report itself as
24.11 instead of 25.05 for auto-detection testing — but see the caveat
above about the mock always being 25.05-shaped underneath regardless of
what it claims to be. `-DMOCK_API_MAJOR=44` (25.11) doesn't have that
caveat: `slurmdb_job_rec_t`/`slurmdb_job_cond_t`/`slurmdb_assoc_rec_t` are
byte-for-byte identical between 25.05 and 25.11, so the mock's
25.05-shaped structs are equally valid for exercising `abi/v25_11.py`
end-to-end (confirmed — this is how `abi/v25_11.py` was validated).

## Debugging history / gotchas worth knowing before touching this again

Roughly chronological; each of these cost real time to track down and is
easy to reintroduce if this code gets refactored without re-reading the
comment that's already next to the fix:

1. **`undefined symbol: slurm_conf` when loading `auth_munge.so`.**
   `cffi.FFI.dlopen()` defaults to `RTLD_LOCAL`, so the library's symbols
   (including the global `slurm_conf` that internal plugins reference)
   never join the process's global symbol table — real Slurm binaries get
   this for free because their `DT_NEEDED` deps load with global scope by
   default. Fix: `dlopen(path, ffi.RTLD_NOW | ffi.RTLD_GLOBAL)`. See the
   comment in `Slurmdb.__init__`.

2. **`job_cond->db_flags` left at 0 → every query returns zero jobs,
   silently, no error.** `0` is not a wildcard for this field — it's
   `SLURMDB_JOB_FLAG_NONE`, and the mysql query builder turns any
   non-`NOTSET` value into a literal `t1.flags = <value>` filter. Since
   almost every real job has `SLURMDB_JOB_FLAG_START_R` set the moment its
   start RPC lands, `t1.flags = 0` matches almost nothing. `sacct` always
   sets `db_flags = SLURMDB_JOB_FLAG_NOTSET` in `_init_params()` for
   exactly this reason. This was the single most confusing bug so far —
   the RPC succeeds, returns a valid empty list, no error anywhere, and
   the window/account/user filters are all red herrings since the bug is
   completely independent of all of them.

3. **`-A` account names need trimming + lowercasing**, matching
   `slurm_addto_char_list()`'s behavior (`src/common/
   slurm_protocol_defs.c`) — the mysql filter is a case-sensitive
   `t1.account='%s'`, so a mixed-case or space-padded `-A` value silently
   matches zero rows under fastsacct's naive `.split(",")` while real
   `sacct` normalizes and matches fine.

4. **`--allusers` needed no code change**, just a required CLI flag —
   `job_cond.userid_list` was already left `NULL`, which is exactly what
   `-a` means to `slurmdb_jobs_get()` (`sacct` only narrows to the caller's
   uid when `userid_list` is empty AND `-a` wasn't passed).

5. **The `"comment"` field is `derived_es` in the C struct, not
   `admin_comment`/`system_comment`'s sibling you'd expect from the name.**
   `sacct`'s own "Comment" column reads `job->derived_es`
   (`src/sacct/print.c` `PRINT_COMMENT`). Flat mode's `JOB_FIELDS` uses
   `("comment", "derived_es", "str")` — json key ≠ C field name, on
   purpose, so it's discoverable.

## Known limitations / open items

- `--full`'s performance hasn't been benchmarked against real `sacct
  --json` on a large query — only correctness has been validated so far.
  Worth doing before leaning on the "should be much faster" assumption for
  anything latency-sensitive.
- The hostlist "first node" parser (`full_format.hostlist_first()`, used
  for `%N` in stdio expansion on 25.05) covers the common
  single-bracket-range syntax, not Slurm's full hostlist grammar
  (multi-dimensional/nested ranges). Untested against anything exotic.
- Signal names in `exit_code.signal.name` use Python's stdlib `signal`
  module with the `SIG` prefix stripped, as a stand-in for Slurm's own
  `sig_num2name()` table (`src/common/proc_args.c`) — functionally
  equivalent for standard POSIX signals, not verified to match exactly for
  anything unusual.
- 24.11's `--full` association RPC path (`abi/v24_11.py`'s
  `fetch_assoc_list`/`job_assoc`) is verified by careful independent
  struct-layout cross-checking against `origin/slurm-24.11`, not by an
  end-to-end mock run (see "Testing locally" above for why) or a real
  24.11 cluster. If you have access to one, that's the next thing worth
  doing.
- Only `-A`/`-S`/`-E` are implemented as real filters; everything else
  about a `sacct` invocation this doesn't recognize is a hard error by
  design (see the module docstring) — this is a narrow drop-in for one
  invocation shape, not a general `sacct` replacement.
