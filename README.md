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

**Status as of 2026-08-12**: working and validated against a real cluster
(mila, Slurm 25.05.2). Emits one flat JSON schema per job (see "Output
schema" below) — there used to also be a `--full` mode that replicated
`sacct --json`'s exact nested schema, but it's been removed in favor of
resolving the handful of fields worth resolving (state, flags, qos, group,
TRES, exit code) directly in the flat schema instead.

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
- `--abi {24.11,25.05,25.11}` — override auto-detection (see `detect_abi()`).
- `--library` — path to `libslurm.so`/`libslurmdb.so`. Defaults to
  `libslurmdb.so` resolved via the normal linker search path, but most
  clusters only ship `libslurm.so` (see the RTLD_GLOBAL gotcha below) —
  pass the real path explicitly if in doubt.

## Output schema

One flat JSON object per job, `json_key: raw_value`, using mostly-literal
C struct field names as keys (`abi/*.py`'s `JOB_FIELDS`). Most fields are
raw — `state_reason_prev`/`qos_req`/etc. are still bare ints/strings
straight off the wire — but a handful get cheap, no-extra-RPC-per-job
decoding where leaving them raw would just push the same parsing work onto
every downstream consumer:

- `qos` is resolved to a name via a one-time `slurmdb_qos_get()` id->name
  map fetched at startup — no RPC per job. Replaces `qosid` rather than
  sitting alongside it.
- `state` is a list of decoded state name strings (e.g. `["COMPLETED"]`,
  or `["COMPLETED", "COMPLETING"]` while cleanup is still in flight) —
  `job_state()`'s base-state + flag-bit decode, pure bit twiddling against
  a fixed table, no RPC.
- `flags` is a list of decoded flag name strings (e.g. `["STARTED_ON_SUBMIT"]`,
  or `["NONE"]` when unset) — `job_flags()`'s bit-table decode, no RPC.
- `group` is resolved to a name via a local `getgrgid` NSS lookup — no RPC.
  `gid` is still emitted alongside it as the raw id.
- `array_task_id` is `null`, not the raw `NO_VAL`/`INFINITE` sentinel
  (`4294967294`/`4294967295`), for a non-array job.
- The epoch-timestamp/duration fields all carry a `time_` prefix —
  `time_eligible`, `time_end`, `time_elapsed`, `time_start`, `time_submit`,
  `time_suspended`, `time_timelimit` — values are still the same raw
  seconds/minutes, just renamed so they're easy to pick out from the rest
  of the flat fields. The CPU-usage counters (`sys_cpu_sec`, `tot_cpu_sec`,
  `user_cpu_sec`, and their `_usec` siblings) are left as-is — they're
  already qualified by `sys_`/`tot_`/`user_`.
- `tres_alloc_str`/`tres_req_str` (e.g. `"1=4,2=17179869184,1001=2"`) are
  still emitted raw, but also expanded into `allocated_<type>[_<name>]`/
  `requested_<type>[_<name>]` fields (e.g. `allocated_cpu`, `allocated_mem`,
  `allocated_gres_gpu`) using a one-time `slurmdb_tres_get()` id->name map.
  Sentinel counts that round-trip through the wire's unsigned format (e.g.
  `18446744073709551614`, which is really `-2`) are reinterpreted back to
  a real negative number — see `parse_tres()`. A GRES name's
  optional model/type suffix (e.g. `"gpu:a100"`) is kept out of the key —
  always `allocated_gres_gpu`, never `allocated_gres_gpu_a100` — and
  surfaced instead as a separate `allocated_gres_gpu_type: "a100"` field
  when present.
- `exitcode` (a Unix wait-status int) is still emitted raw, but also
  expanded into `exitcode_return_code`/`exitcode_signal` — the plain
  number (or `null` if not applicable) via `process_exit_code()`'s
  WIFEXITED/WIFSIGNALED decoding. `derived_ec` is left as raw only, for
  now.

Good enough if your downstream consumer is your own code and you're fine
post-processing the remaining raw fields yourself.

## Architecture

```
fastsacct.py       CLI, argument parsing, cffi/RPC plumbing, Slurmdb class,
                   the flat-mode formatter (_job_to_dict), and the shared
                   decode helpers it uses (job_state, job_flags, qos_name,
                   group_name, parse_tres, process_exit_code) — the
                   helpers are version-independent (unlike abi/*.py)
                   because they were confirmed byte-for-byte identical
                   between 24.11, 25.05, and 25.11 by diffing all three
                   releases' data_parser plugin source (plus a couple of
                   purely additive 25.11 exceptions folded in directly —
                   a new SLURMDB_JOB_FLAGS bit and a new JOB_STATE flag
                   bit, both harmless no-ops on older releases)
abi/v25_11.py      Slurm 25.11 ABI: struct layouts (CDEF), JOBCOND_FLAG_*
                   values, flat-mode JOB_FIELDS
abi/v25_05.py      Same, for Slurm 25.05
abi/v24_11.py      Same, for Slurm 24.11
mock/              A fake libslurmdb.so, compiled against vendored copies
                   of the real slurm/slurm.h + slurmdb.h (mock/include/),
                   for local testing without a real cluster
```

Adding a new Slurm release's ABI module: see the docstring at the top of
`abi/v25_05.py` — across 22.05→26.05, `slurmdb_job_rec_t`/
`slurmdb_job_cond_t` have only ever grown by appending fields, so it's a
diff-and-copy job, not a rewrite. Bump `SLURM_ABI_VERSION`/
`SLURM_API_MAJOR`, add fields to `CDEF`, and if you want them in the
output, to `JOB_FIELDS`.

## Validation

fastsacct.py's decode helpers (`job_state`, `job_flags`, `qos_name`,
`group_name`, `parse_tres`, `process_exit_code`) were originally built for
a `--full` mode (since removed) that replicated `sacct --json`'s exact
nested schema. On
2026-08-05, that mode's output was diffed leaf-by-leaf against real `sacct
--json` output for the same real query on the mila cluster (Slurm 25.05.2,
2003 real jobs), which caught one real bug that still matters now that the
same decode logic feeds the flat schema: `parse_tres()`'s TRES `count` for
entries with a negative sentinel value (e.g. `energy` showing `-2` when
unavailable) came out as `18446744073709551614` — the same bit pattern
read as unsigned instead of signed int64 — instead of `-2`. Fixed by
reinterpreting any count ≥ 2⁶³ as its two's-complement signed equivalent
(`tres_alloc_str`/`tres_req_str` are written with an unsigned format
specifier, but the value is logically an `INT64`). After that fix, the
diffed output matched real `sacct --json` exactly across all 2003 jobs.

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
what the fabricated jobs use, so the flat schema's `qos`/`allocated_*`/
`requested_*` fields have something real to resolve.

It does **not** prove the vendored struct in `abi/v25_05.py` matches
whatever real `libslurmdb.so` is on a given cluster node — only that
fastsacct's own harness (arg parsing, list walking, JSON emission,
formatting logic) is correct.

`mock/include/slurm/slurm_version.h` is hand-written, not vendored — it's
normally generated by `configure` and isn't present in a bare Slurm
checkout, so this is a stub with a hardcoded version number instead.

```bash
clang -shared -fPIC -Imock/include -o /tmp/mock_libslurmdb.so \
    mock/mock_libslurmdb.c

uv run python fastsacct.py -S 2026-01-01 -E 2026-01-02 -D -X -a \
    --json --library /tmp/mock_libslurmdb.so --debug
```

Pass `-DMOCK_API_MAJOR=42` to `clang` to make the mock report itself as
24.11 instead of 25.05 for auto-detection testing, or `-DMOCK_API_MAJOR=44`
for 25.11 — `slurmdb_job_rec_t`/`slurmdb_job_cond_t` are byte-for-byte
identical across all three releases, so the mock's 25.05-shaped structs
are equally valid for exercising any of the three `abi/*.py` modules
end-to-end.

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

- Only `-A`/`-S`/`-E` are implemented as real filters; everything else
  about a `sacct` invocation this doesn't recognize is a hard error by
  design (see the module docstring) — this is a narrow drop-in for one
  invocation shape, not a general `sacct` replacement.
