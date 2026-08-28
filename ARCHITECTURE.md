# Architecture notes

Implementation rationale, validation methodology, and gotchas that are
too detailed for the README but worth keeping for whoever next touches
this code.

## Organization

```
fastsacct.py       CLI, argument parsing, cffi/RPC plumbing, Slurmdb class,
                   the flat-mode formatter (_job_to_dict), and the shared
                   decode helpers it uses (job_state, job_flags, qos_name,
                   group_name, parse_tres, process_exit_code) — these
                   helpers are version-independent (unlike abi/*.py); see
                   ARCHITECTURE.md for how that's confirmed
abi/v26_05.py      Slurm 26.05 ABI: struct layouts (CDEF), JOBCOND_FLAG_*
                   values, flat-mode JOB_FIELDS
abi/v25_11.py      Same, for Slurm 25.11
abi/v25_05.py      Same, for Slurm 25.05
abi/v24_11.py      Same, for Slurm 24.11
mock/              A fake libslurmdb.so, compiled once per supported ABI
                   against that release's real vendored slurm/slurm.h +
                   slurmdb.h (mock/include/<version>/), for local testing
                   without a real cluster
tests/             pytest suite that builds the three mock/ variants and
                   runs fastsacct.py against each — `uv run pytest`
```

Adding a new Slurm release's ABI module: see the docstring at the top of
`abi/v25_05.py` — across 22.05→26.05, `slurmdb_job_rec_t`/
`slurmdb_job_cond_t` have only ever grown by appending fields, so it's a
diff-and-copy job, not a rewrite. Bump `SLURM_ABI_VERSION`/
`SLURM_API_MAJOR`, add fields to `CDEF`, and if you want them in the
output, to `JOB_FIELDS`.

The decode helpers listed above have been validated field-by-field
against real `sacct --json` output on a production cluster (mila, Slurm
25.05.2) — see ARCHITECTURE.md for the methodology and the one bug it
caught. ARCHITECTURE.md also lists implementation gotchas worth reading
before touching `Slurmdb`, `job_cond` construction, or the decode
helpers.

## Decode helpers are version-independent

`fastsacct.py`'s shared decode helpers (`job_state`, `job_flags`,
`qos_name`, `group_name`, `parse_tres`, `process_exit_code`) live outside
`abi/*.py`, unlike everything else that varies per Slurm release, because
they were confirmed byte-for-byte identical between Slurm 24.11
(data_parser v0.0.42), 25.05 (v0.0.43), 25.11 (v0.0.44), and 26.05
(v0.0.45) by direct diff of `src/plugins/data_parser/v0.0.4{2,3,4,5}/
parsers.c` across all four releases. Two purely additive exceptions are
folded in directly rather than kept per-ABI: a new `SLURMDB_JOB_FLAGS`
bit and a new `JOB_STATE` flag bit, both added in 25.11 and harmless
no-ops on older releases (see the comments next to
`_JOB_STATE_FLAG_BITS`).

## Decode-helper validation

fastsacct.py's decode helpers were validated by diffing their output
leaf-by-leaf against real `sacct --json` output for the same real query
on the mila cluster (Slurm 25.05.2). That caught one real bug that still
matters for the flat schema these helpers feed today: `parse_tres()`'s
TRES `count` for entries with a negative sentinel value (e.g. `energy`
showing `-2` when unavailable) came out as `18446744073709551614` — the
same bit pattern read as unsigned instead of signed int64 — instead of
`-2`. Fixed by reinterpreting any count ≥ 2⁶³ as its two's-complement
signed equivalent (`tres_alloc_str`/`tres_req_str` are written with an
unsigned format specifier, but the value is logically an `INT64`). After
that fix, the diffed output matched real `sacct --json` exactly.

## Gotchas worth knowing before touching this again

Each of these cost real time to track down and is easy to reintroduce if
this code gets refactored without re-reading the comment that's already
next to the fix:

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
   exactly this reason. This is the single most confusing failure mode in
   this codebase — the RPC succeeds, returns a valid empty list, no error
   anywhere, and the window/account/user filters are all red herrings
   since the bug is completely independent of all of them.

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

6. **Not every version-dependent struct lives in `abi/*.py`.**
   `slurmdb_tres_rec_t` (used by `Slurmdb.fetch_tres()`, unrelated to
   `slurmdb_job_rec_t`/`slurmdb_job_cond_t`) was declared directly in
   `Slurmdb.__init__` and assumed version-independent — true through
   24.11/25.05/25.11, until 26.05 inserted a `char modifier;` field between
   `id` and `name`. Checked with `offsetof()` on the real struct (see
   `/tmp/offcheck.c`-style probe, System V AMD64 ABI): here that 1-byte
   insertion happens to land entirely inside the alignment padding
   `uint32_t id` already needed before the 8-byte-aligned `char *name`
   pointer, so `name`/`type`'s offsets come out identical with or without
   `modifier` declared — this particular omission would *not* actually
   have corrupted TRES resolution. That's a coincidence of this specific
   insertion point, not something to rely on: a struct's cdef should
   describe its host, not "the fields I've personally verified matter",
   so the field is declared anyway, gated on a new
   `SLURMDB_TRES_REC_HAS_MODIFIER` flag in each `abi/vXX_YY.py` (see the
   comment next to `slurmdb_tres_rec_t` in `Slurmdb.__init__`) so it's
   still correct if a future release adds a field that *isn't* padding-
   absorbed. Moral: when regenerating for a future release, diff the
   *whole* `slurm/slurmdb.h`, not just `slurmdb_job_rec_t`/
   `slurmdb_job_cond_t` — anything else fastsacct.py declares its own cdef
   for (currently `slurmdb_tres_rec_t`/`slurmdb_tres_cond_t`/
   `slurmdb_qos_rec_t`/`slurmdb_qos_cond_t`/the `slurm_conf_t` prefix) is
   fair game to have changed too, and don't assume a padding coincidence
   will save you next time.
