//! ABI description for Slurm 24.11.x.
//!
//! See `src/abi/mod.rs` for the general regeneration notes. This release
//! was built by diffing `slurmdb_job_rec_t` / `slurmdb_job_cond_t` in
//! `origin/slurm-24.11:slurm/slurmdb.h` against the 25.05 branch.
//! Differences vs. 25.05, confirmed via diff (not guessed):
//!
//!   - 24.11 has an extra `uint32_t lft;` field, right before `lineage`
//!     (removed in 25.05 -- `lineage` superseded it as the
//!     association-path representation).
//!   - 24.11 lacks `resv_req` (added in 25.05).
//!   - 24.11 lacks `segment_size` (added in 25.05).
//!
//! Everything else -- field order, `slurmdb_job_cond_t`, the JOBCOND_FLAG_*
//! bit values, the list_t/ListDelF typedefs, and the slurm_list_*/
//! slurmdb_connection_*/slurmdb_jobs_get signatures -- is identical between
//! 24.11 and 25.05.

use crate::abi::Abi;
use core::ffi::{c_char, c_void};

/// mirrors slurmdb_job_rec_t, slurm/slurmdb.h (24.11) -- OUTPUT struct.
/// See the module docs for the 3 field differences vs. 25.05.
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
    pub lft: u32, // 24.11-only, removed in 25.05
    pub lineage: *const c_char,
    pub licenses: *const c_char,
    pub mcs_label: *const c_char,
    pub nodes: *const c_char,
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
    pub script: *const c_char,
    pub show_full: u32,
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
/// contract. Note: no resv_req/segment_size entries (don't exist in 24.11);
/// has an extra "lft" entry that 25.05+ doesn't.
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
    ("lft", lft, U32),
    ("licenses", licenses, Str),
    ("mcs_label", mcs_label, Str),
    ("nodes", nodes, Str),
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
    ("show_full", show_full, U32),
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
    version: "24.11",
    // Confirmed via `git show origin/slurm-24.11:META` (API_CURRENT=42,
    // API_AGE=0).
    api_major: 42,
    job_fields: JOB_FIELDS,
    // `slurmdb_tres_rec_t` is unchanged vs. 25.05 here -- no `modifier`
    // field (that's added in 26.05, see `v26_05`).
    tres_rec_has_modifier: false,
};

#[cfg(test)]
mod tests {
    use super::*;
    use std::mem::offset_of;

    /// Same hand-computed LP64 spot-check as v25_05's test. The extra `lft`
    /// u32 shifts pointer-aligned fields after `jobname` up by 8 vs. 25.05
    /// (200 vs. the padding slot 25.05 leaves unused), while dropping
    /// resv_req (a pointer) and segment_size (a u16) later on pulls the
    /// trailing offsets back -- work_dir lands at the same 512.
    #[test]
    fn offsets_match_sysv_amd64_layout() {
        assert_eq!(offset_of!(JobRec, account), 0);
        assert_eq!(offset_of!(JobRec, db_index), 80);
        assert_eq!(offset_of!(JobRec, jobname), 192);
        assert_eq!(offset_of!(JobRec, lft), 200);
        assert_eq!(offset_of!(JobRec, lineage), 208);
        // 24.11 has no resv_req/segment_size: script follows resv_name.
        assert_eq!(offset_of!(JobRec, resv_name), 296);
        assert_eq!(offset_of!(JobRec, script), 304);
        assert_eq!(offset_of!(JobRec, show_full), 312);
        assert_eq!(offset_of!(JobRec, work_dir), 512);
    }
}
