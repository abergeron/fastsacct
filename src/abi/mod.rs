//! Per-Slurm-release ABI descriptions (the Rust port of the old `abi/*.py`
//! packages).
//!
//! Each `vXX_YY` module mirrors the *exact* field order of
//! `slurmdb_job_rec_t` as declared in that release's real
//! `slurm/slurmdb.h`, as a `#[repr(C)]` struct used purely to compute
//! per-field offsets (`std::mem::offset_of!`). The emitted output is then
//! driven by a shared, version-independent table of
//! `(json_key, offset, kind)` entries — the direct equivalent of the
//! Python version's `(json_key, c_field, kind)` `JOB_FIELDS` lists.
//!
//! Why this shape: `#[repr(C)]` gives Rust the same System V C layout rules
//! the real headers get from the C compiler, and `offset_of!` bakes the
//! offsets in at compile time — but, exactly like cffi's ABI mode did, a
//! *missing* struct field silently shifts every later offset instead of
//! failing to compile. So every field between the start of the struct and
//! the last one we read MUST be present, in order. (We declare the whole
//! struct anyway, so there is no "read prefix" to get wrong.)
//!
//! Regenerating for a new Slurm release:
//!   1. diff the new release's `slurm/slurmdb.h` `slurmdb_job_rec_t`
//!      against the newest `src/abi/vXX_YY.rs` here.
//!   2. Across 22.05 -> 26.05 this struct has only ever grown by appending
//!      fields at the end (verified by diffing all release branches) -- so
//!      in practice this is a short copy/paste-and-diff job, not a
//!      rewrite. Bump `SLURM_ABI_VERSION`/`api_major`, add the new fields
//!      to the `#[repr(C)]` struct at their real positions and, if you want
//!      them in the output, to `JOB_FIELDS` -- position matters in the
//!      struct, order (output order) in the table.
//!   3. Register the module in [`ABIS`] below.

pub mod v24_11;
pub mod v25_05;
pub mod v25_11;
pub mod v26_05;

/// How one flat-output field is converted from raw struct bytes to JSON --
/// the same "kind" contract the Python `_job_to_dict()` dispatch used.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FieldKind {
    /// `char *`, NULL-safe -> string or null.
    Str,
    /// `uint16_t` -> number.
    U16,
    /// `uint32_t` -> number.
    U32,
    /// `uint64_t` -> number.
    U64,
    /// `time_t` -> number.
    Time,
    /// `uint32_t` gid -> group name via a local `getgrgid` NSS lookup.
    Group,
    /// `uint32_t` -> decoded `SLURMDB_JOB_FLAGS` name list.
    Flags,
    /// `uint32_t` -> decoded `JOB_STATE` base+flags name list.
    JobState,
    /// `uint32_t` qosid -> name via the one-time `slurmdb_qos_get()` map.
    QosName,
    /// `uint32_t`, NO_VAL/INFINITE sentinels collapsed to null ("unset") --
    /// mirrors what sacct's data_parser emits for a UINT32_NO_VAL field.
    NoVal32,
    /// `uint32_t`, INFINITE ("no limit") collapsed to 0, NO_VAL ("unset" --
    /// e.g. timelimit's "Partition_Limit" state) collapsed to null.
    U32Inf0NullNoVal,
}

/// One flat-output field: JSON key, byte offset into `slurmdb_job_rec_t`,
/// and how to convert it.
#[derive(Clone, Copy, Debug)]
pub struct FieldDesc {
    pub key: &'static str,
    pub offset: usize,
    pub kind: FieldKind,
}

/// Everything version-dependent that the rest of the program needs.
#[derive(Debug)]
pub struct Abi {
    /// Release string, e.g. "25.05" -- also the `--abi` value and the
    /// `slurm_abi_version` meta field.
    pub version: &'static str,
    /// `slurm_api_version()`'s API_CURRENT major for this release.
    pub api_major: u32,
    /// The `(json_key, offset, kind)` output table (Python's JOB_FIELDS).
    pub job_fields: &'static [FieldDesc],
    /// Whether this release's `slurmdb_tres_rec_t` has the `char modifier;`
    /// field 26.05 inserted between `id` and `name`.
    pub tres_rec_has_modifier: bool,
}

// JOBCOND_FLAG_* / SLURM_BIT(n) values, from slurm/slurmdb.h -- identical
// across every release we support (SLURM_BIT(offset) == (uint64_t)1 <<
// offset, confirmed in slurm/slurm.h).
pub const JOBCOND_FLAG_DUP: u32 = 1 << 0;
pub const JOBCOND_FLAG_NO_STEP: u32 = 1 << 1;
pub const JOBCOND_FLAG_NO_TRUNC: u32 = 1 << 2;

/// `job_cond->db_flags` sentinel (a DIFFERENT field from `job_cond->flags`
/// above). The accounting_storage/mysql query builder treats db_flags as
/// "filter on these bits" UNLESS it's exactly NOTSET -- 0 is NOT a wildcard
/// here, it's SLURMDB_JOB_FLAG_NONE, which builds `t1.flags = 0` and matches
/// almost no real job (e.g. as_mysql_job.c sets SLURMDB_JOB_FLAG_START_R the
/// moment a job's start RPC lands). sacct always sets db_flags = NOTSET in
/// _init_params() (src/sacct/options.c) for exactly this reason.
pub const JOBCOND_DB_FLAG_NOTSET: u32 = 1 << 0;

/// One entry per Slurm release we have an ABI module for, newest first
/// (order only affects the wording of "implemented: ..." error messages).
static ABIS: [&Abi; 4] = [&v26_05::ABI, &v25_11::ABI, &v25_05::ABI, &v24_11::ABI];

pub fn all() -> &'static [&'static Abi] {
    &ABIS
}

pub fn by_api_major(api_major: u32) -> Option<&'static Abi> {
    ABIS.iter().copied().find(|abi| abi.api_major == api_major)
}

pub fn by_version(version: &str) -> Option<&'static Abi> {
    ABIS.iter().copied().find(|abi| abi.version == version)
}

/// Documentation only, not used for dispatch: API_CURRENT (== API major,
/// since API_AGE has been 0 for every release below) per Slurm release,
/// gathered from each branch's META file. Lets an unimplemented-version
/// error name the release instead of just a bare number.
pub fn release_hint(api_major: u32) -> &'static str {
    match api_major {
        38 => "22.05",
        39 => "23.02",
        40 => "23.11",
        41 => "24.05",
        42 => "24.11",
        43 => "25.05",
        44 => "25.11",
        45 => "26.05",
        _ => "unknown release",
    }
}

/// "26.05 (API major 45), 25.11 (API major 44), ..." for error messages.
pub fn implemented_with_api_major() -> String {
    ABIS.iter()
        .map(|abi| format!("{} (API major {})", abi.version, abi.api_major))
        .collect::<Vec<_>>()
        .join(", ")
}

/// "24.11, 25.05, 25.11, 26.05" (sorted, like the old --abi choices list).
pub fn implemented_versions_sorted() -> String {
    let mut versions: Vec<&str> = ABIS.iter().map(|abi| abi.version).collect();
    versions.sort_unstable();
    versions.join(", ")
}

/// Builds a `JOB_FIELDS` table for one release's `JobRec`: each entry is
/// `(json_key, struct field, FieldKind variant)`, offsets computed at
/// compile time with `offset_of!` -- the direct equivalent of the Python
/// version's `(json_key, c_field, kind)` lists.
#[macro_export]
macro_rules! job_fields {
    ($rec:ty, $(($key:literal, $field:ident, $kind:ident)),* $(,)?) => {
        &[$($crate::abi::FieldDesc {
            key: $key,
            offset: ::core::mem::offset_of!($rec, $field),
            kind: $crate::abi::FieldKind::$kind,
        }),*]
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_lookup_round_trips() {
        for &abi in all() {
            assert!(std::ptr::eq(abi, by_api_major(abi.api_major).unwrap()));
            assert!(std::ptr::eq(abi, by_version(abi.version).unwrap()));
            assert!(!abi.job_fields.is_empty());
        }
        assert!(by_api_major(99).is_none());
        assert!(by_version("6.66").is_none());
    }
}
