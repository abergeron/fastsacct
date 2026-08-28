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
schema" below), resolving the handful of fields worth resolving (state,
flags, qos, group, TRES, exit code) directly in that flat schema rather
than replicating `sacct --json`'s nested one.

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
- `--abi {24.11,25.05,25.11,26.05}` — override auto-detection (see `detect_abi()`).
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
- `time_timelimit` is `0`, not the raw `INFINITE` sentinel
  (`4294967295`/`0xFFFFFFFF`), for a job with no time limit.
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

## Testing locally (no cluster needed)

```bash
uv run pytest
```

That's the whole test suite: for each supported ABI, it compiles
`mock/mock_libslurmdb.c` — a fake `libslurmdb.so` — against
`mock/include/<version>/slurm/{slurm.h,slurmdb.h,slurm_errno.h}` (real
headers vendored from that release's own `slurm-<version>-*` tag, plus a
hand-written `slurm_version.h` stub — that one file is normally generated
by `configure` and isn't present in a bare checkout), points
`fastsacct.py` at the resulting `.so`, and checks the flat JSON output
field-by-field against what `mock_libslurmdb.c`'s `make_job()` actually
wrote — including the fields that only exist on some releases (`lft` on
24.11 only; `resv_req`/`segment_size` on 25.05+ only; `exclusive`/
`oversubscribe`/`sluid` on 26.05+ only, see `abi/v24_11.py`'s and
`abi/v26_05.py`'s docstrings). Requires a C compiler (`cc`/`clang`/`gcc`) on
`PATH`; the whole suite is skipped if none is found.

Using a _different_ release's headers per version (rather than one
struct reused everywhere) matters here: `slurmdb_job_rec_t` is not
actually byte-for-byte identical across 24.11/25.05/25.11/26.05 — 24.11 has
an extra field and is missing two that 25.05+ added, and 26.05 adds three
more on top of that (see `abi/v24_11.py`'s and `abi/v26_05.py`'s
docstrings) — so building 24.11's mock against a 25.05-shaped struct would
silently validate the wrong offsets. Building each mock against its own
release's real header is what makes a passing test mean anything.

The mock exercises both directions of the ABI: fastsacct writes a
`job_cond` → mock prints what it received (confirms Python wrote at the
right offsets); mock fabricates job records → fastsacct reads them back
(confirms Python reads from the right offsets). It also fakes
`slurmdb_tres_get()`/`slurmdb_qos_get()` with real id/name tables matching
what the fabricated jobs use, so the flat schema's `qos`/`allocated_*`/
`requested_*` fields have something real to resolve.

It does **not** prove any `abi/vXX_YY.py` matches whatever real
`libslurmdb.so` is on a given cluster node — the vendored headers are
real, but only fastsacct's own harness (arg parsing, list walking, JSON
emission, formatting logic) against them is under test.

For a one-off manual run against a single ABI without pytest:

```bash
clang -shared -fPIC -Imock/include/25.05 -DMOCK_API_MAJOR=43 \
    -o /tmp/mock_libslurmdb.so mock/mock_libslurmdb.c

TZ=UTC uv run python fastsacct.py -S 2026-01-01 -E 2026-01-02 -D -X -a \
    --json --library /tmp/mock_libslurmdb.so --debug
```

Swap `-Imock/include/25.05 -DMOCK_API_MAJOR=43` for
`-Imock/include/24.11 -DMOCK_API_MAJOR=42`,
`-Imock/include/25.11 -DMOCK_API_MAJOR=44`, or
`-Imock/include/26.05 -DMOCK_API_MAJOR=45` to target the other ABIs —
`MOCK_API_MAJOR` must match the header set (it picks the mock's
`slurm_api_version()` return value _and_, via `#if MOCK_API_MAJOR == 42`
in `mock_libslurmdb.c`, which of the version-specific job fields to set).

## Known limitations / open items

- Only `-A`/`-S`/`-E` are implemented as real filters; everything else
  about a `sacct` invocation this doesn't recognize is a hard error by
  design (see the module docstring) — this is a narrow drop-in for one
  invocation shape, not a general `sacct` replacement.
