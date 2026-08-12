"""
ABI description for Slurm 25.11.x.

See abi/v25_05.py for the general regeneration notes. This file was built by
diffing `slurmdb_job_rec_t` / `slurmdb_job_cond_t` in
`origin/slurm-25.11:slurm/slurmdb.h` against the 25.05 branch: both are
byte-for-byte identical to 25.05 (confirmed via diff) -- CDEF/JOB_FIELDS
below are copied verbatim from abi/v25_05.py.

One real behavioral addition confirmed by diffing data_parser v0.0.44
(25.11) against v0.0.43 (25.05)'s parsers.c, folded into fastsacct.py's
shared decode helpers (not here) since it's additive and harmless for
older releases (the new bit is never set by pre-25.11 libslurmdb.so): `flags`
(SLURMDB_JOB_FLAGS) gained bit 5, SLURMDB_JOB_FLAG_ALTERED -> "JOB_ALTERED".
`state` (JOB_STATE) also gained a new flag bit, JOB_EXPEDITING (bit 24) ->
"EXPEDITING", folded in the same way.

Everything else flat-mode decoding depends on (PROCESS_EXIT_CODE, TRES_STR
dump + its INT64 count quirk) was diffed function-by-function against 25.05
and found identical.
"""

SLURM_ABI_VERSION = "25.11"

# See abi/v25_05.py for what this is. Confirmed via
# `git show origin/slurm-25.11:META` (API_CURRENT=44, API_AGE=0).
SLURM_API_MAJOR = 44

# Unchanged vs. 25.05; SLURM_BIT(offset) == (uint64_t)1 << offset.
JOBCOND_FLAG_DUP = 1 << 0
JOBCOND_FLAG_NO_STEP = 1 << 1
JOBCOND_FLAG_NO_TRUNC = 1 << 2

# See v25_05.py's comment -- job_cond->db_flags must be set to NOTSET, not
# left at 0, or the query silently filters to `t1.flags = 0` server-side.
JOBCOND_DB_FLAG_NOTSET = 1 << 0

CDEF = r"""
/* time_t is `long` on LP64 Linux (our deployment target); cffi has no
 * built-in knowledge of it since it's platform-defined in <time.h>. */
typedef long time_t;

typedef struct xlist list_t;
typedef struct listIterator list_itr_t;
typedef void (*ListDelF) (void *x);

void slurm_init(const char *conf);

void *slurmdb_connection_get(uint16_t *persist_conn_flags);
int slurmdb_connection_close(void **db_conn);

list_t *slurm_list_create(ListDelF f);
void slurm_list_append(list_t *l, void *x);
int slurm_list_count(list_t *l);
list_itr_t *slurm_list_iterator_create(list_t *l);
void *slurm_list_next(list_itr_t *i);
void slurm_list_iterator_destroy(list_itr_t *i);
void slurm_list_destroy(list_t *l);

char *slurm_strerror(int errnum);

/* mirrors slurmdb_job_cond_t, slurm/slurmdb.h (25.11) -- INPUT struct,
 * identical to 25.05 */
typedef struct {
    list_t *acct_list;
    list_t *associd_list;
    list_t *cluster_list;
    list_t *constraint_list;
    uint32_t cpus_max;
    uint32_t cpus_min;
    uint32_t db_flags;
    int32_t exitcode;
    uint32_t flags;
    list_t *format_list;
    list_t *groupid_list;
    list_t *jobname_list;
    uint32_t nodes_max;
    uint32_t nodes_min;
    list_t *partition_list;
    list_t *qos_list;
    list_t *reason_list;
    list_t *resv_list;
    list_t *resvid_list;
    list_t *state_list;
    list_t *step_list;
    uint32_t timelimit_max;
    uint32_t timelimit_min;
    time_t usage_end;
    time_t usage_start;
    char *used_nodes;
    list_t *userid_list;
    list_t *wckey_list;
} slurmdb_job_cond_t;

list_t *slurmdb_jobs_get(void *db_conn, slurmdb_job_cond_t *job_cond);
void slurmdb_destroy_job_rec(void *object);
void slurmdb_destroy_job_cond_members(slurmdb_job_cond_t *job_cond);

/* mirrors slurmdb_job_rec_t, slurm/slurmdb.h (25.11) -- OUTPUT struct,
 * identical to 25.05 */
typedef struct {
    char *account;
    char *admin_comment;
    uint32_t alloc_nodes;
    uint32_t array_job_id;
    uint32_t array_max_tasks;
    uint32_t array_task_id;
    char *array_task_str;
    uint32_t associd;
    char *blockid;
    char *cluster;
    char *constraints;
    char *container;
    uint64_t db_index;
    uint32_t derived_ec;
    char *derived_es;
    uint32_t elapsed;
    time_t eligible;
    time_t end;
    char *env;
    uint32_t exitcode;
    char *extra;
    char *failed_node;
    uint32_t flags;
    void *first_step_ptr;
    uint32_t gid;
    uint32_t het_job_id;
    uint32_t het_job_offset;
    uint32_t jobid;
    char *jobname;
    char *lineage;
    char *licenses;
    char *mcs_label;
    char *nodes;
    char *partition;
    uint32_t priority;
    uint32_t qosid;
    char *qos_req;
    uint32_t req_cpus;
    uint64_t req_mem;
    uint32_t requid;
    uint16_t restart_cnt;
    uint32_t resvid;
    char *resv_name;
    char *resv_req;
    char *script;
    uint16_t segment_size;
    uint32_t show_full;
    time_t start;
    uint32_t state;
    uint32_t state_reason_prev;
    list_t *steps;
    char *std_err;
    char *std_in;
    char *std_out;
    time_t submit;
    char *submit_line;
    uint32_t suspended;
    char *system_comment;
    uint64_t sys_cpu_sec;
    uint64_t sys_cpu_usec;
    uint32_t timelimit;
    uint64_t tot_cpu_sec;
    uint64_t tot_cpu_usec;
    char *tres_alloc_str;
    char *tres_req_str;
    uint32_t uid;
    char *used_gres;
    char *user;
    uint64_t user_cpu_sec;
    uint64_t user_cpu_usec;
    char *wckey;
    uint32_t wckeyid;
    char *work_dir;
} slurmdb_job_rec_t;
"""

# (json_key, c_field, kind) -- see abi/v25_05.py for the "kind" contract.
JOB_FIELDS = [
    ("account", "account", "str"),
    ("admin_comment", "admin_comment", "str"),
    ("alloc_nodes", "alloc_nodes", "u32"),
    ("array_job_id", "array_job_id", "u32"),
    ("array_max_tasks", "array_max_tasks", "u32"),
    ("array_task_id", "array_task_id", "no_val32"),
    ("array_task_str", "array_task_str", "str"),
    ("associd", "associd", "u32"),
    ("blockid", "blockid", "str"),
    ("cluster", "cluster", "str"),
    ("constraints", "constraints", "str"),
    ("container", "container", "str"),
    ("db_index", "db_index", "u64"),
    ("derived_ec", "derived_ec", "u32"),
    # json_key "comment", not "derived_es": this is what sacct itself calls
    # "Comment" (src/sacct/print.c PRINT_COMMENT reads job->derived_es) --
    # naming the output key after the C struct member here would silently
    # hide it from anyone grepping for "comment" in the JSON.
    ("comment", "derived_es", "str"),
    ("time_elapsed", "elapsed", "u32"),
    ("time_eligible", "eligible", "time"),
    ("time_end", "end", "time"),
    ("exitcode", "exitcode", "u32"),
    ("extra", "extra", "str"),
    ("failed_node", "failed_node", "str"),
    ("flags", "flags", "flags"),
    ("gid", "gid", "u32"),
    ("group", "gid", "group"),
    ("het_job_id", "het_job_id", "u32"),
    ("het_job_offset", "het_job_offset", "u32"),
    ("job_id", "jobid", "u32"),
    ("name", "jobname", "str"),
    ("lineage", "lineage", "str"),
    ("licenses", "licenses", "str"),
    ("mcs_label", "mcs_label", "str"),
    ("nodes", "nodes", "str"),
    ("partition", "partition", "str"),
    ("priority", "priority", "u32"),
    ("qos", "qosid", "qos_name"),
    ("qos_req", "qos_req", "str"),
    ("req_cpus", "req_cpus", "u32"),
    ("req_mem", "req_mem", "u64"),
    ("requid", "requid", "u32"),
    ("restart_cnt", "restart_cnt", "u16"),
    ("resvid", "resvid", "u32"),
    ("resv_name", "resv_name", "str"),
    ("resv_req", "resv_req", "str"),
    ("segment_size", "segment_size", "u16"),
    ("show_full", "show_full", "u32"),
    ("time_start", "start", "time"),
    ("state", "state", "job_state"),
    ("state_reason_prev", "state_reason_prev", "u32"),
    ("std_err", "std_err", "str"),
    ("std_in", "std_in", "str"),
    ("std_out", "std_out", "str"),
    ("time_submit", "submit", "time"),
    ("submit_line", "submit_line", "str"),
    ("time_suspended", "suspended", "u32"),
    ("system_comment", "system_comment", "str"),
    ("sys_cpu_sec", "sys_cpu_sec", "u64"),
    ("sys_cpu_usec", "sys_cpu_usec", "u64"),
    ("time_timelimit", "timelimit", "u32"),
    ("tot_cpu_sec", "tot_cpu_sec", "u64"),
    ("tot_cpu_usec", "tot_cpu_usec", "u64"),
    ("tres_alloc_str", "tres_alloc_str", "str"),
    ("tres_req_str", "tres_req_str", "str"),
    ("uid", "uid", "u32"),
    ("used_gres", "used_gres", "str"),
    ("user", "user", "str"),
    ("user_cpu_sec", "user_cpu_sec", "u64"),
    ("user_cpu_usec", "user_cpu_usec", "u64"),
    ("wckey", "wckey", "str"),
    ("wckeyid", "wckeyid", "u32"),
    ("work_dir", "work_dir", "str"),
]
