//! Flat-output decode helpers -- state/flags/qos/group/TRES/exit-code
//! fields that are cheap to resolve from raw `slurmdb_job_rec_t` values (no
//! RPC per job) but not worth leaving as opaque ints for every downstream
//! consumer to re-decode. Ported from sacct's own `data_parser` plugin
//! (src/plugins/data_parser/v0.0.4{2,3,4,5}/parsers.c) and validated
//! field-by-field against real `sacct --json` output on a real cluster
//! (mila, Slurm 25.05.2). Every table/algorithm here was confirmed
//! byte-for-byte identical between Slurm 24.11 (data_parser v0.0.42), 25.05
//! (v0.0.43), 25.11 (v0.0.44), and 26.05 (v0.0.45) by direct diff of
//! parsers.c -- that's why this module is version-independent (unlike
//! `abi/`) -- with two purely additive exceptions folded in directly
//! (a new SLURMDB_JOB_FLAGS bit and a new JOB_STATE flag bit, both added in
//! 25.11; see the comments next to the tables below).

use std::collections::HashMap;

/// NO_VAL/INFINITE sentinels, from slurm/slurm.h.
pub const NO_VAL: u32 = 0xFFFF_FFFE;
pub const INFINITE: u32 = 0xFFFF_FFFF;

// job state -- PARSER_FLAG_ARRAY(JOB_STATE) in parsers.c; base-state values
// from enum job_states, flag-bit values from the JOB_* SLURM_BIT macros,
// both in slurm/slurm.h (public, version-independent header).
const JOB_STATE_BASE: [&str; 12] = [
    "PENDING",
    "RUNNING",
    "SUSPENDED",
    "COMPLETED",
    "CANCELLED",
    "FAILED",
    "TIMEOUT",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
    "OUT_OF_MEMORY",
    // 12 = JOB_END: a real base-state slot, but marked `hidden` in the
    // data_parser table -- real sacct never emits it either.
];
// (bit, name), in the table's declared order (order matters: it's the order
// names get appended to the output array).
const JOB_STATE_FLAG_BITS: [(u32, &str); 16] = [
    (8, "LAUNCH_FAILED"),
    (10, "REQUEUED"),
    (11, "REQUEUE_HOLD"),
    (12, "SPECIAL_EXIT"),
    (13, "RESIZING"),
    (14, "CONFIGURING"),
    (15, "COMPLETING"),
    (16, "STOPPED"),
    (17, "RECONFIG_FAIL"),
    (18, "POWER_UP_NODE"),
    (19, "REVOKED"),
    (20, "REQUEUE_FED"),
    (21, "RESV_DEL_HOLD"),
    (22, "SIGNALING"),
    (23, "STAGE_OUT"),
    (24, "EXPEDITING"), // JOB_EXPEDITING, added in Slurm 25.11
];

pub fn job_state(raw: u32) -> Vec<&'static str> {
    let mut out = Vec::new();
    let base_idx = (raw & 0xFF) as usize;
    if let Some(base) = JOB_STATE_BASE.get(base_idx) {
        out.push(*base);
    }
    for (bit, name) in JOB_STATE_FLAG_BITS {
        if raw & (1 << bit) != 0 {
            out.push(name);
        }
    }
    out
}

/// job flags -- `slurmdb_job_rec_t.flags` (NOT job_cond->flags/db_flags),
/// via PARSER_FLAG_ARRAY(SLURMDB_JOB_FLAGS) in parsers.c; values from
/// slurm/slurmdb.h SLURMDB_JOB_FLAG_*.
pub fn job_flags(raw: u32) -> Vec<&'static str> {
    let mut out = Vec::new();
    if raw == 0x0 {
        out.push("NONE");
    }
    if raw == 0xF {
        out.push("CLEAR_SCHEDULING");
    }
    for (bit, name) in [
        (0, "NOT_SET"),
        (1, "STARTED_ON_SUBMIT"),
        (2, "STARTED_ON_SCHEDULE"),
        (3, "STARTED_ON_BACKFILL"),
        (4, "START_RECEIVED"),
        (5, "JOB_ALTERED"), // SLURMDB_JOB_FLAG_ALTERED, added in Slurm 25.11
    ] {
        if raw & (1 << bit) != 0 {
            out.push(name);
        }
    }
    out
}

/// process exit code -- job->exitcode / job->derived_ec, packed like a Unix
/// wait-status int. Ported from DUMP_FUNC(PROCESS_EXIT_CODE): WIFEXITED is
/// "low 7 bits are 0", WEXITSTATUS is "next 8 bits", WIFSIGNALED/WTERMSIG
/// use the low 7 bits as a signal number. Returns
/// `(return_code, signal)`, each `None` when it doesn't apply (including the
/// WCOREDUMP and still-pending cases, which have neither).
pub fn process_exit_code(raw: u32) -> (Option<u32>, Option<u32>) {
    let low7 = raw & 0x7F;
    if raw == NO_VAL {
        (None, None) // still pending
    } else if low7 == 0 {
        (Some((raw >> 8) & 0xFF), None)
    } else if low7 != 0x7F {
        (None, Some(low7)) // signaled
    } else {
        (None, None) // core-dumped, or an invalid wait status
    }
}

/// gid -> name resolution -- DUMP_FUNC(GROUP_ID): a local NSS lookup
/// (getgrgid), not an RPC. Non-complex mode (what plain `sacct --json`
/// used to run in) falls back to "" on a failed lookup.
pub fn group_name(gid: u32) -> String {
    // SAFETY: single-threaded program; getgrgid's static storage is only
    // read here before any subsequent call overwrites it.
    let entry = unsafe { libc::getgrgid(gid as libc::gid_t) };
    if entry.is_null() {
        return String::new();
    }
    // SAFETY: gr_name is a valid NUL-terminated C string on success.
    let name = unsafe { std::ffi::CStr::from_ptr((*entry).gr_name) };
    name.to_string_lossy().into_owned()
}

/// QOS id -> name entry (from the one-time `slurmdb_qos_get()` fetch).
#[derive(Debug, Clone)]
pub struct QosInfo {
    pub id: u32,
    pub name: String,
}

/// QOS id -> name. Needs a `slurmdb_qos_get()` fetched once per run (see
/// `Slurmdb::qos_by_id`) -- ported from DUMP_FUNC(QOS_ID).
pub fn qos_name(qid: u32, qos_by_id: &HashMap<u32, QosInfo>) -> String {
    if qid == 0 || qid == INFINITE {
        return String::new();
    }
    match qos_by_id.get(&qid) {
        None => "Unknown".to_string(),
        Some(q) => {
            if q.name.is_empty() {
                q.id.to_string()
            } else {
                q.name.clone()
            }
        }
    }
}

/// TRES id -> {type, name} entry (from the one-time `slurmdb_tres_get()`
/// fetch).
#[derive(Debug, Clone, Default)]
pub struct TresInfo {
    pub r#type: String,
    pub name: String,
}

/// One parsed entry of a tres_alloc_str/tres_req_str.
#[derive(Debug, PartialEq, Eq)]
pub struct TresEntry {
    pub r#type: String,
    pub name: String,
    pub id: u64,
    pub count: i64,
}

/// TRES string parsing -- tres_alloc_str/tres_req_str are always
/// "<tres_id>=<count>,<tres_id>=<count>,..." (numeric ids, never names) on
/// the wire; resolving id -> {type, name} needs the one-time
/// `slurmdb_tres_get()` map.
pub fn parse_tres(raw: &str, tres_by_id: &HashMap<u64, TresInfo>) -> Vec<TresEntry> {
    let mut out = Vec::new();
    for pair in raw.split(',') {
        if pair.is_empty() {
            continue;
        }
        let (tid_str, count_str) = pair.split_once('=').unwrap_or((pair, ""));
        let id: u64 = tid_str.parse().unwrap_or_default();
        // Parse as u64 to mirror the wire's unsigned format specifier, then
        // reinterpret per below; accept a plain negative too, just in case.
        let count: i64 = match count_str.parse::<u64>() {
            Ok(c) => {
                // tres_alloc_str/tres_req_str are written with an UNSIGNED
                // format specifier (assoc_mgr_make_tres_str_from_array():
                // "%u=%"PRIu64), but the data_parser dumps a TRES entry's
                // "count" as INT64 (parsers.c PARSER_ARRAY(TRES):
                // add_parse(INT64, count, "count", ...)) -- a sentinel like
                // "unknown/not tracked" (-2, for e.g. energy on nodes
                // without power monitoring) round-trips through the wire as
                // its unsigned 64-bit bit pattern
                // (18446744073709551614), and real sacct reinterprets it
                // back to -2. Confirmed against real sacct --json output --
                // without this, we'd show the raw unsigned value instead.
                c as i64
            }
            Err(_) => count_str.parse::<i64>().unwrap_or_default(),
        };
        let info = tres_by_id.get(&id).cloned().unwrap_or_default();
        out.append(&mut vec![TresEntry {
            r#type: info.r#type,
            name: info.name,
            id,
            count,
        }]);
    }
    out
}

/// Turns a TRES "type" or "name" (e.g. "gres", "gpu:a100") into a JSON-key-
/// safe fragment -- lowercased, non-alnum runs (":", "/", ...) collapsed to
/// a single "_", then trimmed of leading/trailing "_".
pub fn tres_key_part(value: &str) -> String {
    let mut out = String::new();
    for c in value.to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
        } else if !out.ends_with('_') {
            out.push('_');
        }
    }
    out.trim_matches('_').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn job_state_decodes_base_and_flags() {
        assert_eq!(job_state(3), ["COMPLETED"]);
        // COMPLETED | COMPLETING(bit 15) -- e.g. cleanup still in flight.
        assert_eq!(job_state(3 | 1 << 15), ["COMPLETED", "COMPLETING"]);
        // unknown base (12 = JOB_END, hidden), no flags
        assert_eq!(job_state(12), Vec::<&str>::new());
        // 25.11's new EXPEDITING bit is a harmless no-op when unset,
        // decodes when set:
        assert_eq!(job_state(1 | 1 << 24), ["RUNNING", "EXPEDITING"]);
    }

    #[test]
    fn job_flags_decode() {
        assert_eq!(job_flags(0), ["NONE"]);
        assert_eq!(
            job_flags(0xF),
            [
                "CLEAR_SCHEDULING",
                "NOT_SET",
                "STARTED_ON_SUBMIT",
                "STARTED_ON_SCHEDULE",
                "STARTED_ON_BACKFILL"
            ]
        );
        assert_eq!(
            job_flags(1 << 4 | 1 << 5),
            ["START_RECEIVED", "JOB_ALTERED"]
        );
    }

    #[test]
    fn process_exit_code_decode() {
        assert_eq!(process_exit_code(0), (Some(0), None));
        // exit code 2: (2 << 8) | 0
        assert_eq!(process_exit_code(0x200), (Some(2), None));
        // killed by SIGKILL: low7 = 9
        assert_eq!(process_exit_code(9), (None, Some(9)));
        // core dump (low7 == 0x7F): neither
        assert_eq!(process_exit_code(0x7F), (None, None));
        // still pending (NO_VAL): neither
        assert_eq!(process_exit_code(NO_VAL), (None, None));
    }

    #[test]
    fn process_exit_code_of_exited_process() {
        // Real wait-status round trip through libc::wait.
        let pid = unsafe { libc::fork() };
        if pid == 0 {
            unsafe { libc::_exit(7) };
        }
        assert!(pid > 0);
        let mut status: libc::c_int = 0;
        unsafe { libc::waitpid(pid, &mut status, 0) };
        assert_eq!(
            process_exit_code(status as u32),
            (Some(7), None),
            "raw wait status {status:#x}"
        );
    }

    #[test]
    fn parse_tres_resolves_and_sign_fixes() {
        let mut tres = HashMap::new();
        tres.insert(
            2,
            TresInfo {
                r#type: "mem".into(),
                name: String::new(),
            },
        );
        let got = parse_tres("1=4,2=17179869184,2=18446744073709551614", &tres);
        assert_eq!(got[0].id, 1);
        assert_eq!(got[0].r#type, ""); // unknown id -> empty type/name
        assert_eq!(got[0].count, 4);
        assert_eq!(got[1].r#type, "mem");
        assert_eq!(got[1].count, 17179869184);
        // The unsigned-wire-format sentinel comes back as signed -2.
        assert_eq!(got[2].count, -2);
        assert!(parse_tres("", &tres).is_empty());
    }

    #[test]
    fn tres_key_parts() {
        assert_eq!(tres_key_part("gres"), "gres");
        assert_eq!(tres_key_part("gpu:a100"), "gpu_a100");
        assert_eq!(tres_key_part("license/Foo Bar"), "license_foo_bar");
        assert_eq!(tres_key_part("!!"), "");
        assert_eq!(tres_key_part("_cpu_"), "cpu");
    }
}
