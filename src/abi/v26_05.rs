//! ABI description for Slurm 26.05.x.
//!
//! See `src/abi/mod.rs` for the general regeneration notes. This release
//! was built by diffing `origin/slurm-26.05:slurm/slurmdb.h` against the
//! 25.11 branch (25.11 itself is byte-for-byte identical to 25.05, see
//! `v25_11`):
//!
//!   - `slurmdb_job_cond_t` is unchanged.
//!   - `slurmdb_job_rec_t` gained three fields, all purely additive (no
//!     removals, no reordering of existing fields):
//!       - `char *exclusive;` -- inserted between `env` and `exitcode`.
//!       - `char *oversubscribe;` -- inserted between `nodes` and
//!         `partition`.
//!       - `uint64_t sluid;` -- inserted between `show_full` and `start`.
//!         They are spliced into `JobRec` below at their real struct
//!         positions (offsets of every field after each insertion point
//!         shift accordingly, so position matters, not just presence) and
//!         into `JOB_FIELDS`.
//!
//! One thing this file does NOT mention: `slurmdb_tres_rec_t` (used by
//! `Slurmdb::fetch_tres()` for the flat schema's allocated_*/requested_*
//! resolution) also changed in 26.05 -- a `char modifier;` field was
//! inserted between `id` and `name`. That struct is version-independent
//! scaffolding for a helper RPC, not part of the job ABI this file
//! describes, so it's declared in `crate::slurmdb` instead -- see
//! `Abi::tres_rec_has_modifier` (true here, the only release so far) and
//! ARCHITECTURE.md gotcha #6.
//!
//! Everything else flat-mode decoding depends on (SLURMDB_JOB_FLAGS,
//! JOB_STATE, PROCESS_EXIT_CODE, TRES_STR dump + its INT64 count quirk) was
//! diffed function-by-function against 25.11's data_parser (v0.0.44 ->
//! v0.0.45 parsers.c) and found identical -- no new flag/state bits.

use crate::abi::Abi;
use core::ffi::{c_char, c_void};

/// mirrors slurmdb_job_rec_t, slurm/slurmdb.h (26.05) -- OUTPUT struct.
/// See the module docs for the 3 fields added vs. 25.05/25.11 (exclusive,
/// oversubscribe, sluid).
#[repr(C)]
#[allow(dead_code)]
pub(crate) struct JobRec {
    pub account: *const c_char,
    pub admin_comment: *const c_char,
    pub alloc_nodes: u32,
    pub array_job_id: u32,
    pub array_max_tasks: u32,
    pub array_task_id: u32,
    pub array_task_str: *const c_char,
    pub associd: u32,
    pub blockid: *const c_char,
    pub cluster: *const c_char,
    pub constraints: *const c_char,
    pub container: *const c_char,
    pub db_index: u64,
    pub derived_ec: u32,
    pub derived_es: *const c_char,
    pub elapsed: u32,
    pub eligible: i64,
    pub end: i64,
    pub env: *const c_char,
    pub exclusive: *const c_char, // new in 26.05
    pub exitcode: u32,
    pub extra: *const c_char,
    pub failed_node: *const c_char,
    pub flags: u32,
    pub first_step_ptr: *const c_void,
    pub gid: u32,
    pub het_job_id: u32,
    pub het_job_offset: u32,
    pub jobid: u32,
    pub jobname: *const c_char,
    pub lineage: *const c_char,
    pub licenses: *const c_char,
    pub mcs_label: *const c_char,
    pub nodes: *const c_char,
    pub oversubscribe: *const c_char, // new in 26.05
    pub partition: *const c_char,
    pub priority: u32,
    pub qosid: u32,
    pub qos_req: *const c_char,
    pub req_cpus: u32,
    pub req_mem: u64,
    pub requid: u32,
    pub restart_cnt: u16,
    pub resvid: u32,
    pub resv_name: *const c_char,
    pub resv_req: *const c_char,
    pub script: *const c_char,
    pub segment_size: u16,
    pub show_full: u32,
    pub sluid: u64, // new in 26.05
    pub start: i64,
    pub state: u32,
    pub state_reason_prev: u32,
    pub steps: *const c_void, // list_t *
    pub std_err: *const c_char,
    pub std_in: *const c_char,
    pub std_out: *const c_char,
    pub submit: i64,
    pub submit_line: *const c_char,
    pub suspended: u32,
    pub system_comment: *const c_char,
    pub sys_cpu_sec: u64,
    pub sys_cpu_usec: u64,
    pub timelimit: u32,
    pub tot_cpu_sec: u64,
    pub tot_cpu_usec: u64,
    pub tres_alloc_str: *const c_char,
    pub tres_req_str: *const c_char,
    pub uid: u32,
    pub used_gres: *const c_char,
    pub user: *const c_char,
    pub user_cpu_sec: u64,
    pub user_cpu_usec: u64,
    pub wckey: *const c_char,
    pub wckeyid: u32,
    pub work_dir: *const c_char,
}

/// (json_key, offset, kind) -- see `v25_05`'s JOB_FIELDS for the kind
/// contract. Note: has 3 extra entries vs. 25.05/25.11 (exclusive,
/// oversubscribe, sluid) that don't exist in those releases -- see the
/// module docs.
pub static JOB_FIELDS: &[crate::abi::FieldDesc] = crate::job_fields!(
    JobRec,
    ("account", account, Str),
    ("admin_comment", admin_comment, Str),
    ("alloc_nodes", alloc_nodes, U32),
    ("array_job_id", array_job_id, U32),
    ("array_max_tasks", array_max_tasks, U32),
    ("array_task_id", array_task_id, NoVal32),
    ("array_task_str", array_task_str, Str),
    ("associd", associd, U32),
    ("blockid", blockid, Str),
    ("cluster", cluster, Str),
    ("constraints", constraints, Str),
    ("container", container, Str),
    ("db_index", db_index, U64),
    ("derived_ec", derived_ec, U32),
    // See v25_05's comment -- sacct calls this field "Comment"
    // (job->derived_es in src/sacct/print.c), not "derived_es".
    ("comment", derived_es, Str),
    ("time_elapsed", elapsed, U32),
    ("time_eligible", eligible, Time),
    ("time_end", end, Time),
    ("exclusive", exclusive, Str),
    ("exitcode", exitcode, U32),
    ("extra", extra, Str),
    ("failed_node", failed_node, Str),
    ("flags", flags, Flags),
    ("gid", gid, U32),
    ("group", gid, Group),
    ("het_job_id", het_job_id, U32),
    ("het_job_offset", het_job_offset, U32),
    ("job_id", jobid, U32),
    ("name", jobname, Str),
    ("lineage", lineage, Str),
    ("licenses", licenses, Str),
    ("mcs_label", mcs_label, Str),
    ("nodes", nodes, Str),
    ("oversubscribe", oversubscribe, Str),
    ("partition", partition, Str),
    ("priority", priority, U32),
    ("qos", qosid, QosName),
    ("qos_req", qos_req, Str),
    ("req_cpus", req_cpus, U32),
    ("req_mem", req_mem, U64),
    ("requid", requid, U32),
    ("restart_cnt", restart_cnt, U16),
    ("resvid", resvid, U32),
    ("resv_name", resv_name, Str),
    ("resv_req", resv_req, Str),
    ("segment_size", segment_size, U16),
    ("show_full", show_full, U32),
    ("sluid", sluid, U64),
    ("time_start", start, Time),
    ("state", state, JobState),
    ("state_reason_prev", state_reason_prev, U32),
    ("std_err", std_err, Str),
    ("std_in", std_in, Str),
    ("std_out", std_out, Str),
    ("time_submit", submit, Time),
    ("submit_line", submit_line, Str),
    ("time_suspended", suspended, U32),
    ("system_comment", system_comment, Str),
    ("sys_cpu_sec", sys_cpu_sec, U64),
    ("sys_cpu_usec", sys_cpu_usec, U64),
    ("time_timelimit", timelimit, U32Inf0),
    ("tot_cpu_sec", tot_cpu_sec, U64),
    ("tot_cpu_usec", tot_cpu_usec, U64),
    ("tres_alloc_str", tres_alloc_str, Str),
    ("tres_req_str", tres_req_str, Str),
    ("uid", uid, U32),
    ("used_gres", used_gres, Str),
    ("user", user, Str),
    ("user_cpu_sec", user_cpu_sec, U64),
    ("user_cpu_usec", user_cpu_usec, U64),
    ("wckey", wckey, Str),
    ("wckeyid", wckeyid, U32),
    ("work_dir", work_dir, Str),
);

pub static ABI: Abi = Abi {
    version: "26.05",
    // Confirmed via `git show origin/slurm-26.05:META` (API_CURRENT=45,
    // API_AGE=0).
    api_major: 45,
    job_fields: JOB_FIELDS,
    // `slurmdb_tres_rec_t` gained `char modifier;` between `id` and `name`
    // in 26.05 -- see the module docs and ARCHITECTURE.md gotcha #6. On
    // this ABI the 1-byte insertion happens to land inside padding `id`
    // already needed, so it doesn't move `name`/`type`'s offsets, but the
    // flag is honored anyway (crate::slurmdb::TresRecWithModifier) since
    // that's a coincidence of this specific insertion point, not something
    // to rely on.
    tres_rec_has_modifier: true,
};

#[cfg(test)]
mod tests {
    use super::*;
    use std::mem::offset_of;

    /// Spot-check the three insertion points against hand-computed LP64
    /// offsets: each new field shifts everything after it vs. the 25.05
    /// test values (exclusive +8 before exitcode, oversubscribe another +8
    /// before partition, sluid another +8 before start).
    #[test]
    fn offsets_match_sysv_amd64_layout() {
        assert_eq!(offset_of!(JobRec, env), 128);
        assert_eq!(offset_of!(JobRec, exclusive), 136);
        assert_eq!(offset_of!(JobRec, exitcode), 144);
        assert_eq!(offset_of!(JobRec, nodes), 232);
        assert_eq!(offset_of!(JobRec, oversubscribe), 240);
        assert_eq!(offset_of!(JobRec, partition), 248);
        assert_eq!(offset_of!(JobRec, show_full), 332);
        assert_eq!(offset_of!(JobRec, sluid), 336);
        assert_eq!(offset_of!(JobRec, start), 344);
        assert_eq!(offset_of!(JobRec, work_dir), 536);
    }
}
