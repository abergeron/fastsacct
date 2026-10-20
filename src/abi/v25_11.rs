//! ABI description for Slurm 25.11.x.
//!
//! See `src/abi/mod.rs` for the general regeneration notes. This release
//! was built by diffing `slurmdb_job_rec_t` / `slurmdb_job_cond_t` in
//! `origin/slurm-25.11:slurm/slurmdb.h` against the 25.05 branch: both are
//! byte-for-byte identical to 25.05 (confirmed via diff) -- so this module
//! literally reuses 25.05's `JobRec`/`JOB_FIELDS` rather than copying them.
//!
//! One real behavioral addition confirmed by diffing data_parser v0.0.44
//! (25.11) against v0.0.43 (25.05)'s parsers.c, folded into the shared
//! decode helpers (not here) since it's additive and harmless for older
//! releases (the new bit is never set by pre-25.11 libslurmdb.so): `flags`
//! (SLURMDB_JOB_FLAGS) gained bit 5, SLURMDB_JOB_FLAG_ALTERED ->
//! "JOB_ALTERED". `state` (JOB_STATE) also gained a new flag bit,
//! JOB_EXPEDITING (bit 24) -> "EXPEDITING", folded in the same way.
//!
//! Everything else flat-mode decoding depends on (PROCESS_EXIT_CODE,
//! TRES_STR dump + its INT64 count quirk) was diffed function-by-function
//! against 25.05 and found identical.

use crate::abi::Abi;
use crate::abi::v25_05::JOB_FIELDS;

pub static ABI: Abi = Abi {
    version: "25.11",
    // Confirmed via `git show origin/slurm-25.11:META` (API_CURRENT=44,
    // API_AGE=0).
    api_major: 44,
    job_fields: JOB_FIELDS,
    // `slurmdb_tres_rec_t` is unchanged vs. 25.05 -- no `modifier` field
    // (that's added in 26.05, see `v26_05`).
    tres_rec_has_modifier: false,
};
