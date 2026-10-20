# Architecture notes

Implementation rationale, validation methodology, and gotchas that are
too detailed for the README but worth keeping for whoever next touches
this code.

## Organization

```
src/main.rs        CLI (clap), required-marker validation, ABI
                   auto-detection (detect_abi + `sacct --version`
                   fallback), output-mode dispatch (blob vs --jsonl)
src/lib.rs         crate docs (the "what this tool is" contract), schema
                   version constants
src/abi/mod.rs     FieldDesc/FieldKind/Abi types, the registry + lookups,
                   the job_fields! macro (offset_of!-based), JOBCOND_FLAG_*
                   values
src/abi/v26_05.rs  Slurm 26.05 ABI: #[repr(C)] slurmdb_job_rec_t mirror +
                   JOB_FIELDS table
src/abi/v25_11.rs  Same, for Slurm 25.11 (reuses 25.05's layout verbatim)
src/abi/v25_05.rs  Same, for Slurm 25.05 (the canonical layout; carries the
                   "how to regenerate" docs)
src/abi/v24_11.rs  Same, for Slurm 24.11
src/slurmdb.rs     dlopen/dlsym binding, struct mirrors for job_cond /
                   tres+qos rec/cond / slurm_conf prefix, connection
                   lifecycle, job_cond construction, C-list -> JSON decode
src/decode.rs      version-independent decode helpers (job_state,
                   job_flags, qos_name, group_name, parse_tres,
                   process_exit_code, tres_key_part)
src/timeparse.rs   the parse_time subset grammar, require_utc_timezone,
                   epoch<->civil math, --debug ISO formatting
mock/              A fake libslurmdb.so, compiled once per supported ABI
                   against that release's real vendored slurm/slurm.h +
                   slurmdb.h (mock/include/<version>/), for local testing
                   without a real cluster
tests/mock_e2e.rs  cargo-test suite that builds the four mock variants and
                   runs the binary against each, field-for-field
smoke.sh /         manual smoke + CLI error-path scripts (expect mocks in
cli-checks.sh      /tmp/fa-mocks, binary in target/debug)
```

Adding a new Slurm release's ABI module: see the docs at the top of
`src/abi/mod.rs` and `src/abi/v25_05.rs` — across 22.05→26.05,
`slurmdb_job_rec_t`/`slurmdb_job_cond_t` have only ever grown by appending
fields, so it's a diff-and-copy job, not a rewrite. Bump
`version`/`api_major`, add fields to the `#[repr(C)]` struct at their real
positions and, if you want them in the output, to `JOB_FIELDS`, then
register the module in `ABIS`.

The decode helpers in `src/decode.rs` live outside `abi/`, unlike
everything else that varies per Slurm release, because they were confirmed
byte-for-byte identical between Slurm 24.11 (data_parser v0.0.42), 25.05
(v0.0.43), 25.11 (v0.0.44), and 26.05 (v0.0.45) by direct diff of
`src/plugins/data_parser/v0.0.4{2,3,4,5}/parsers.c` across all four
releases. Two purely additive exceptions are folded in directly rather
than kept per-ABI: a new `SLURMDB_JOB_FLAGS` bit and a new `JOB_STATE` flag
bit, both added in 25.11 and harmless no-ops on older releases (see the
comments next to `JOB_STATE_FLAG_BITS`). That validation was done while
this was still Python (against real `sacct --json` output on the mila
cluster, Slurm 25.05.2); the Rust port re-implements the same tables and
the port's e2e/unit tests pin them down again.

## ABI description: repr(C) + offset_of!, and why cffi's rule carried over

The Python version handed cffi a text CDEF and let it compute struct
offsets with standard C layout rules. The Rust version declares each
release's `slurmdb_job_rec_t` as a `#[repr(C)]` struct — which gives the
same System V layout the real headers get from the C compiler — and bakes
the `(json_key, offset, kind)` output tables at compile time with
`offset_of!` (see the `job_fields!` macro). The one property that carried
over 1:1: **a field missing from the mirror silently shifts every later
offset instead of failing to compile** (`offset_of!` can only catch fields
that are named, not fields that were forgotten). The mitigations are the
same rules the abi modules follow: declare the whole struct up to
`work_dir`, not just "the fields I read", and let `tests/mock_e2e.rs` —
which compiles the mock against each release's *real* vendored header — be
the thing that catches drift. The per-release `offsets_match_sysv_amd64_*`
unit tests (hand-computed LP64 offsets) catch gross layout mistakes before
the e2e suite even runs.

## Decode-helper validation

The decode helpers were (originally, and still are) validated by diffing
their output leaf-by-leaf against real `sacct --json` output for the same
real query on the mila cluster (Slurm 25.05.2). That caught one real bug
that still matters for the flat schema these helpers feed today:
`parse_tres()`'s TRES `count` for entries with a negative sentinel value
(e.g. `energy` showing `-2` when unavailable) came out as
`18446744073709551614` — the same bit pattern read as unsigned instead of
signed int64 — instead of `-2`. Fixed by reinterpreting any count >= 2^63
as its two's-complement signed equivalent (`tres_alloc_str`/`tres_req_str`
are written with an unsigned format specifier, but the value is logically
an `INT64`). After that fix, the diffed output matched real `sacct --json`
exactly. In the Rust port the same fix is `u64::parse -> as i64` and
`decode::tests::parse_tres_resolves_and_sign_fixes` pins it.

## Gotchas worth knowing before touching this again

Each of these cost real time to track down and is easy to reintroduce if
this code gets refactored without re-reading the comment that's already
next to the fix:

1. **`undefined symbol: slurm_conf` when loading `auth_munge.so`.**
   `dlopen()` defaults to `RTLD_LOCAL`, so the library's symbols (including
   the global `slurm_conf` that internal plugins reference) never join the
   process's global symbol table — real Slurm binaries get this for free
   because their `DT_NEEDED` deps load with global scope by default. Fix:
   open the *real* library handle with `RTLD_NOW | RTLD_GLOBAL` (see
   `dlopen()` in `src/slurmdb.rs`; the `slurm_api_version()` auto-detect
   probe deliberately stays `RTLD_LOCAL` since it's only used for a
   version number).

2. **`job_cond->db_flags` left at 0 → every query returns zero jobs,
   silently, no error.** `0` is not a wildcard for this field — it's
   `SLURMDB_JOB_FLAG_NONE`, and the mysql query builder turns any
   non-`NOTSET` value into a literal `t1.flags = <value>` filter. Since
   almost every real job has `SLURMDB_JOB_FLAG_START_R` set the moment its
   start RPC lands, `t1.flags = 0` matches almost nothing. `sacct` always
   sets `db_flags = SLURMDB_JOB_FLAG_NOTSET` in `_init_params()` for
   exactly this reason. This is the single most confusing failure mode in
   this codebase — the RPC succeeds, returns a valid empty list, no error
   anywhere, and the window/account/user filters are all red herrings since
   the bug is completely independent of all of them.

3. **`-A` account names need trimming + lowercasing**, matching
   `slurm_addto_char_list()`'s behavior (`src/common/slurm_protocol_defs.c`
   `_add_to_list()`) — the mysql filter is a case-sensitive
   `t1.account='%s'`, so a mixed-case or space-padded `-A` value silently
   matches zero rows under fastsacct's naive `.split(",")` while real
   `sacct` normalizes and matches fine.

4. **`--allusers` needed no code change**, just a required CLI flag —
   `job_cond.userid_list` was already left NULL (in Rust: `JobCond::default()`
   zero-inits it, like `ffi.new` did), which is exactly what `-a` means to
   `slurmdb_jobs_get()` (`sacct` only narrows to the caller's uid when
   `userid_list` is empty AND `-a` wasn't passed).

5. **The `"comment"` field is `derived_es` in the C struct, not
   `admin_comment`/`system_comment`'s sibling you'd expect from the name.**
   `sacct`'s own "Comment" column reads `job->derived_es`
   (`src/sacct/print.c` `PRINT_COMMENT`). The flat-mode `JOB_FIELDS` tables
   use `("comment", derived_es, Str)` — json key != C field name, on
   purpose, so it's discoverable.

6. **Not every version-dependent struct lives in `src/abi/`.**
   `slurmdb_tres_rec_t` (used by `Slurmdb::fetch_tres()`, unrelated to
   `slurmdb_job_rec_t`/`slurmdb_job_cond_t`) was declared directly in the
   binding (originally in `Slurmdb.__init__`'s extra cdef) and assumed
   version-independent — true through 24.11/25.05/25.11, until 26.05
   inserted a `char modifier;` field between `id` and `name`. Checked with
   `offsetof()` on the real struct: here that 1-byte insertion happens to
   land entirely inside the alignment padding `uint32_t id` already needed
   before the 8-byte-aligned `char *name` pointer, so `name`/`type`'s
   offsets come out identical with or without `modifier` declared — this
   particular omission would *not* actually have corrupted TRES resolution.
   That's a coincidence of this specific insertion point, not something to
   rely on, so both variants are declared (`TresRec`/`TresRecWithModifier`)
   and the right one is selected via `Abi::tres_rec_has_modifier`. Moral:
   when regenerating for a future release, diff the *whole*
   `slurm/slurmdb.h`, not just `slurmdb_job_rec_t`/`slurmdb_job_cond_t` —
   anything else the Rust code mirrors (currently
   `slurmdb_tres_rec_t`/`slurmdb_tres_cond_t`/`slurmdb_qos_rec_t`/
   `slurmdb_qos_cond_t`/the `slurm_conf_t` prefix) is fair game to have
   changed too, and don't assume a padding coincidence will save you next
   time.

7. **macOS `dlsym` takes the *plain* C symbol name.** Mach-O object files
   store C symbols with a leading underscore (`nm` shows
   `_slurm_api_version`), which tempts you into prefixing lookup names —
   but dyld's C API does that prefixing itself, so `dlsym(h,
   "_slurm_api_version")` returns not-found while `dlsym(h,
   "slurm_api_version")` resolves. (The old cffi version handled this
   internally via libffi, so this trap only showed up in the Rust port;
   `sym_name()` in `src/slurmdb.rs` documents it.)

8. **errno must be sampled immediately after the FFI call.** The port
   reads errno via `std::io::Error::last_os_error()` right after
   `slurmdb_connection_get()`/`slurmdb_jobs_get()` return NULL — same
   value cffi's `ffi.errno` used to capture at its call boundary. Any libc
   call (even ones the logging path could sneak in) can clobber errno in
   between, so don't move `errno_now()` later.

## Time-handling contract

`parse_time()` accepts only `YYYY-MM-DD[THH:MM[:SS]]` (T or space) and
`now`, always interpreted as UTC, and the process refuses to run unless
the system timezone resolves to UTC (`require_utc_timezone()`, checked
before anything else runs) — Slurm's own `parse_time()` isn't a public
export, so the alternative to this contract would be depending on
undocumented internal symbols.
