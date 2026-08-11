"""
Rendering logic that replicates the exact JSON shape plain `sacct --json`
produces for JOB records (src/plugins/data_parser/v0.0.4{2,3}/parsers.c),
for use by fastsacct.py's --full output mode.

Every table/algorithm in this module was confirmed byte-for-byte identical
between Slurm 24.11 (data_parser v0.0.42), 25.05 (v0.0.43), and 25.11
(v0.0.44) by direct diff of parsers.c -- that's why this module is
version-independent (unlike abi/*.py) -- with two purely additive
exceptions folded in directly below (a new SLURMDB_JOB_FLAGS bit and a new
JOB_STATE flag bit, both added in 25.11; see the comments next to
`_JOB_STATE_FLAG_BITS` and `JOB_STATE_REASON`). The one piece of JOB
rendering that *does* differ in a way that needs real per-version code --
JOB_ASSOC_ID, which needs an extra slurmdb_associations_get() RPC on 24.11
but not on 25.05/25.11 -- lives in abi/v24_11.py / abi/v25_05.py /
abi/v25_11.py instead, next to the rest of each release's ABI-specific
code.

`sacct --json` (no `=complex` suffix) always renders in "non-complex" mode:
nullable numerics are the verbose {"set","infinite","number"} struct, not a
bare number/"Infinity"/null. Everything here targets that mode -- it's the
only one plain `sacct --json` ever uses.
"""

import grp
import pwd
import re
import signal
import time

# NO_VAL/INFINITE sentinels, from slurm/slurm.h.
NO_VAL16, INFINITE16 = 0xFFFE, 0xFFFF
NO_VAL, INFINITE = 0xFFFFFFFE, 0xFFFFFFFF
NO_VAL64, INFINITE64 = 0xFFFFFFFFFFFFFFFE, 0xFFFFFFFFFFFFFFFF

_SENTINELS = {
    16: (NO_VAL16, INFINITE16),
    32: (NO_VAL, INFINITE),
    64: (NO_VAL64, INFINITE64),
}


def no_val(value, width=32):
    """Port of DUMP_FUNC(UINT{16,32,64}_NO_VAL) / DUMP_FUNC(INT64_NO_VAL):
    identical shape and logic across all integer widths, just different
    sentinel constants."""
    no, inf = _SENTINELS[width]
    if value == inf:
        return {"set": False, "infinite": True, "number": 0}
    if value == no:
        return {"set": False, "infinite": False, "number": 0}
    return {"set": True, "infinite": False, "number": value}


def hold(priority):
    """DUMP_FUNC(HOLD): a job is "held" iff priority is literally 0."""
    return priority == 0


# ---------------------------------------------------------------------------
# job state -- PARSER_FLAG_ARRAY(JOB_STATE) in parsers.c; base-state values
# from enum job_states, flag-bit values from the JOB_* SLURM_BIT macros,
# both in slurm/slurm.h (public, version-independent header).
# ---------------------------------------------------------------------------
_JOB_STATE_BASE = {
    0: "PENDING",
    1: "RUNNING",
    2: "SUSPENDED",
    3: "COMPLETED",
    4: "CANCELLED",
    5: "FAILED",
    6: "TIMEOUT",
    7: "NODE_FAIL",
    8: "PREEMPTED",
    9: "BOOT_FAIL",
    10: "DEADLINE",
    11: "OUT_OF_MEMORY",
    # 12 = JOB_END: a real base-state slot, but marked `hidden` in the
    # data_parser table -- real sacct never emits it either.
}
# (bit, name), in the table's declared order (order matters: it's the order
# names get appended to the output array).
_JOB_STATE_FLAG_BITS = [
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
    (24, "EXPEDITING"),  # JOB_EXPEDITING, added in Slurm 25.11
]


def job_state(raw):
    out = []
    base = _JOB_STATE_BASE.get(raw & 0xFF)
    if base:
        out.append(base)
    for bit, name in _JOB_STATE_FLAG_BITS:
        if raw & (1 << bit):
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# job state reason -- job_state_reason_string() in src/common/job_state_reason.c,
# indexed by `enum job_state_reason` (slurm/slurm.h). Version-independent:
# shared common code, not part of either data_parser plugin. A handful of
# enum slots (34, 76, 77, 221) have no table entry and fall through to the
# same "InvalidReason" default as any out-of-range value. Slot 17 used to be
# one of these (`DEFUNCT_WAIT_17`, a reserved-for-reuse placeholder in
# 24.11/25.05) but Slurm 25.11 filled it in as WAIT_NVIDIA_IMEX_CHANNELS --
# not a renumbering, just that one placeholder gaining a real name, so it's
# safe to list unconditionally here: older libslurmdb.so builds never set
# reason code 17 on a real job.
# ---------------------------------------------------------------------------
JOB_STATE_REASON = {
    0: "None",
    1: "Priority",
    2: "Dependency",
    3: "Resources",
    4: "PartitionNodeLimit",
    5: "PartitionTimeLimit",
    6: "PartitionDown",
    7: "PartitionInactive",
    8: "JobHeldAdmin",
    9: "BeginTime",
    10: "Licenses",
    11: "AssociationJobLimit",
    12: "AssociationResourceLimit",
    13: "AssociationTimeLimit",
    14: "Reservation",
    15: "ReqNodeNotAvail",
    16: "JobHeldUser",
    17: "NvidiaImexChannels",  # WAIT_NVIDIA_IMEX_CHANNELS, added in Slurm 25.11
    18: "SchedDefer",
    19: "PartitionDown",
    20: "NodeDown",
    21: "BadConstraints",
    22: "SystemFailure",
    23: "JobLaunchFailure",
    24: "NonZeroExitCode",
    25: "TimeLimit",
    26: "InactiveLimit",
    27: "InvalidAccount",
    28: "InvalidQOS",
    29: "QOSUsageThreshold",
    30: "QOSJobLimit",
    31: "QOSResourceLimit",
    32: "QOSTimeLimit",
    33: "RaisedSignal",
    35: "Cleaning",
    36: "Prolog",
    37: "QOSNotAllowed",
    38: "AccountNotAllowed",
    39: "DependencyNeverSatisfied",
    40: "QOSGrpCpuLimit",
    41: "QOSGrpCPUMinutesLimit",
    42: "QOSGrpCPURunMinutesLimit",
    43: "QOSGrpJobsLimit",
    44: "QOSGrpMemLimit",
    45: "QOSGrpNodeLimit",
    46: "QOSGrpSubmitJobsLimit",
    47: "QOSGrpWallLimit",
    48: "QOSMaxCpuPerJobLimit",
    49: "QOSMaxCpuMinutesPerJobLimit",
    50: "QOSMaxNodePerJobLimit",
    51: "QOSMaxWallDurationPerJobLimit",
    52: "QOSMaxCpuPerUserLimit",
    53: "QOSMaxJobsPerUserLimit",
    54: "QOSMaxNodePerUserLimit",
    55: "QOSMaxSubmitJobPerUserLimit",
    56: "QOSMinCpuNotSatisfied",
    57: "AssocGrpCpuLimit",
    58: "AssocGrpCPUMinutesLimit",
    59: "AssocGrpCPURunMinutesLimit",
    60: "AssocGrpJobsLimit",
    61: "AssocGrpMemLimit",
    62: "AssocGrpNodeLimit",
    63: "AssocGrpSubmitJobsLimit",
    64: "AssocGrpWallLimit",
    65: "AssocMaxJobsLimit",
    66: "AssocMaxCpuPerJobLimit",
    67: "AssocMaxCpuMinutesPerJobLimit",
    68: "AssocMaxNodePerJobLimit",
    69: "AssocMaxWallDurationPerJobLimit",
    70: "AssocMaxSubmitJobLimit",
    71: "JobHoldMaxRequeue",
    72: "JobArrayTaskLimit",
    73: "BurstBufferResources",
    74: "BurstBufferStageIn",
    75: "BurstBufferOperation",
    78: "AssocGrpUnknown",
    79: "AssocGrpUnknownMinutes",
    80: "AssocGrpUnknownRunMinutes",
    81: "AssocMaxUnknownPerJob",
    82: "AssocMaxUnknownPerNode",
    83: "AssocMaxUnknownMinutesPerJob",
    84: "AssocMaxCpuPerNode",
    85: "AssocGrpMemMinutes",
    86: "AssocGrpMemRunMinutes",
    87: "AssocMaxMemPerJob",
    88: "AssocMaxMemPerNode",
    89: "AssocMaxMemMinutesPerJob",
    90: "AssocGrpNodeMinutes",
    91: "AssocGrpNodeRunMinutes",
    92: "AssocMaxNodeMinutesPerJob",
    93: "AssocGrpEnergy",
    94: "AssocGrpEnergyMinutes",
    95: "AssocGrpEnergyRunMinutes",
    96: "AssocMaxEnergyPerJob",
    97: "AssocMaxEnergyPerNode",
    98: "AssocMaxEnergyMinutesPerJob",
    99: "AssocGrpGRES",
    100: "AssocGrpGRESMinutes",
    101: "AssocGrpGRESRunMinutes",
    102: "AssocMaxGRESPerJob",
    103: "AssocMaxGRESPerNode",
    104: "AssocMaxGRESMinutesPerJob",
    105: "AssocGrpLicense",
    106: "AssocGrpLicenseMinutes",
    107: "AssocGrpLicenseRunMinutes",
    108: "AssocMaxLicensePerJob",
    109: "AssocMaxLicenseMinutesPerJob",
    110: "AssocGrpBB",
    111: "AssocGrpBBMinutes",
    112: "AssocGrpBBRunMinutes",
    113: "AssocMaxBBPerJob",
    114: "AssocMaxBBPerNode",
    115: "AssocMaxBBMinutesPerJob",
    116: "QOSGrpUnknown",
    117: "QOSGrpUnknownMinutes",
    118: "QOSGrpUnknownRunMinutes",
    119: "QOSMaxUnknownPerJob",
    120: "QOSMaxUnknownPerNode",
    121: "QOSMaxUnknownPerUser",
    122: "QOSMaxUnknownMinutesPerJob",
    123: "QOSMinUnknown",
    124: "QOSMaxCpuPerNode",
    125: "QOSGrpMemoryMinutes",
    126: "QOSGrpMemoryRunMinutes",
    127: "QOSMaxMemoryMinutesPerJob",
    128: "QOSMaxMemoryPerJob",
    129: "QOSMaxMemoryPerNode",
    130: "QOSMaxMemoryPerUser",
    131: "QOSMinMemory",
    132: "QOSGrpEnergy",
    133: "QOSGrpEnergyMinutes",
    134: "QOSGrpEnergyRunMinutes",
    135: "QOSMaxEnergyPerJob",
    136: "QOSMaxEnergyPerNode",
    137: "QOSMaxEnergyPerUser",
    138: "QOSMaxEnergyMinutesPerJob",
    139: "QOSMinEnergy",
    140: "QOSGrpNodeMinutes",
    141: "QOSGrpNodeRunMinutes",
    142: "QOSMaxNodeMinutesPerJob",
    143: "QOSMinNode",
    144: "QOSGrpGRES",
    145: "QOSGrpGRESMinutes",
    146: "QOSGrpGRESRunMinutes",
    147: "QOSMaxGRESPerJob",
    148: "QOSMaxGRESPerNode",
    149: "QOSMaxGRESPerUser",
    150: "QOSMaxGRESMinutesPerJob",
    151: "QOSMinGRES",
    152: "QOSGrpLicense",
    153: "QOSGrpLicenseMinutes",
    154: "QOSGrpLicenseRunMinutes",
    155: "QOSMaxLicensePerJob",
    156: "QOSMaxLicensePerUser",
    157: "QOSMaxLicenseMinutesPerJob",
    158: "QOSMinLicense",
    159: "QOSGrpBB",
    160: "QOSGrpBBMinutes",
    161: "QOSGrpBBRunMinutes",
    162: "QOSMaxBBPerJob",
    163: "QOSMaxBBPerNode",
    164: "QOSMaxBBPerUser",
    165: "AssocMaxBBMinutesPerJob",  # sic: upstream table quirk, preserved for fidelity
    166: "QOSMinBB",
    167: "DeadLine",
    168: "MaxBBPerAccount",
    169: "MaxCpuPerAccount",
    170: "MaxEnergyPerAccount",
    171: "MaxGRESPerAccount",
    172: "MaxNodePerAccount",
    173: "MaxLicensePerAccount",
    174: "MaxMemoryPerAccount",
    175: "MaxUnknownPerAccount",
    176: "MaxJobsPerAccount",
    177: "MaxSubmitJobsPerAccount",
    178: "PartitionConfig",
    179: "AccountingPolicy",
    180: "FedJobLock",
    181: "OutOfMemory",
    182: "MaxMemPerLimit",
    183: "AssocGrpBilling",
    184: "AssocGrpBillingMinutes",
    185: "AssocGrpBillingRunMinutes",
    186: "AssocMaxBillingPerJob",
    187: "AssocMaxBillingPerNode",
    188: "AssocMaxBillingMinutesPerJob",
    189: "QOSGrpBilling",
    190: "QOSGrpBillingMinutes",
    191: "QOSGrpBillingRunMinutes",
    192: "QOSMaxBillingPerJob",
    193: "QOSMaxBillingPerNode",
    194: "QOSMaxBillingPerUser",
    195: "QOSMaxBillingMinutesPerJob",
    196: "MaxBillingPerAccount",
    197: "QOSMinBilling",
    198: "ReservationDeleted",
    199: "ReservationInvalid",
    200: "Constraints",
    201: "MaxBBRunMinsPerAccount",
    202: "MaxBillingRunMinsPerAccount",
    203: "MaxCpuRunMinsPerAccount",
    204: "MaxEnergyRunMinsPerAccount",
    205: "MaxGRESRunMinsPerAccount",
    206: "MaxNodeRunMinsPerAccount",
    207: "MaxLicenseRunMinsPerAccount",
    208: "MaxMemoryRunMinsPerAccount",
    209: "MaxUnknownRunMinsPerAccount",
    210: "MaxBBRunMinsPerUser",
    211: "MaxBillingRunMinsPerUser",
    212: "MaxCpuRunMinsPerUser",
    213: "MaxEnergyRunMinsPerUser",
    214: "MaxGRESRunMinsPerUser",
    215: "MaxNodeRunMinsPerUser",
    216: "MaxLicenseRunMinsPerUser",
    217: "MaxMemoryRunMinsPerUser",
    218: "MaxUnknownRunMinsPerUser",
    219: "MaxPoweredUpNodes",
    220: "MpiPortsBusy",
}


def job_state_reason(raw):
    return JOB_STATE_REASON.get(raw, "InvalidReason")


# ---------------------------------------------------------------------------
# job flags -- slurmdb_job_rec_t.flags (NOT job_cond->flags/db_flags), via
# PARSER_FLAG_ARRAY(SLURMDB_JOB_FLAGS) in parsers.c; values from
# slurm/slurmdb.h SLURMDB_JOB_FLAG_*.
# ---------------------------------------------------------------------------
def job_flags(raw):
    out = []
    if raw == 0x0:
        out.append("NONE")
    if raw == 0xF:
        out.append("CLEAR_SCHEDULING")
    for bit, name in (
        (0, "NOT_SET"),
        (1, "STARTED_ON_SUBMIT"),
        (2, "STARTED_ON_SCHEDULE"),
        (3, "STARTED_ON_BACKFILL"),
        (4, "START_RECEIVED"),
        (5, "JOB_ALTERED"),  # SLURMDB_JOB_FLAG_ALTERED, added in Slurm 25.11
    ):
        if raw & (1 << bit):
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# process exit code -- job->exitcode / job->derived_ec, packed like a Unix
# wait-status int. Ported from DUMP_FUNC(PROCESS_EXIT_CODE): WIFEXITED is
# "low 7 bits are 0", WEXITSTATUS is "next 8 bits", WIFSIGNALED/WTERMSIG use
# the low 7 bits as a signal number, WCOREDUMP is bit 0x80. Real glibc's
# WIFSIGNALED excludes the (nonexistent) signal 127 -- we don't bother
# replicating that pathological case since it can't occur for a real exit
# status.
# ---------------------------------------------------------------------------
def _signal_name(sig):
    if sig == NO_VAL16:
        return ""
    try:
        name = signal.Signals(sig).name
        return name[3:] if name.startswith("SIG") else name
    except ValueError:
        return str(sig)


def process_exit_code(raw):
    low7 = raw & 0x7F
    if raw == NO_VAL:
        status, rc, sig = "PENDING", NO_VAL, NO_VAL16
    elif low7 == 0:
        rc = (raw >> 8) & 0xFF
        status, sig = ("SUCCESS" if rc == 0 else "ERROR"), NO_VAL16
    elif low7 != 0x7F:
        status, rc, sig = "SIGNALED", NO_VAL, low7
    elif raw & 0x80:
        status, rc, sig = "CORE_DUMPED", NO_VAL, NO_VAL16
    else:
        status, rc, sig = "INVALID", raw, NO_VAL16
    return {
        "status": [status],
        "return_code": no_val(rc, 32),
        "signal": {"id": no_val(sig, 16), "name": _signal_name(sig)},
    }


# ---------------------------------------------------------------------------
# uid/gid -> name resolution -- DUMP_FUNC(USER_ID)/DUMP_FUNC(GROUP_ID): a
# local NSS lookup (getpwuid/getgrgid), not an RPC. Non-complex mode (what
# plain `sacct --json` uses) falls back to "" on a failed lookup for BOTH;
# uid 0 is special-cased to "root" without a lookup (matches the C code,
# also matches virtually every real NSS config, but the C special-case is
# unconditional so we mirror it unconditionally too).
# ---------------------------------------------------------------------------
def user_name(uid):
    if uid == 0:
        return "root"
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return ""


def group_name(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return ""


def job_user(user_field, uid):
    """DUMP_FUNC(JOB_USER): prefer the server-supplied job.user string;
    only fall back to a local uid lookup if that's empty, and -- unlike
    plain USER_ID above -- fall back to JSON null (not "") if the lookup
    also fails."""
    if user_field:
        return user_field
    if uid == 0:
        return "root"
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# QOS id -> name. Needs a slurmdb_qos_get() fetched once per run (see
# Slurmdb.qos_by_id in fastsacct.py) -- ported from DUMP_FUNC(QOS_ID).
# ---------------------------------------------------------------------------
def qos_name(qid, qos_by_id):
    if not qid or qid == INFINITE:
        return ""
    q = qos_by_id.get(qid)
    if q is None:
        return "Unknown"
    return q["name"] or str(q["id"])


# ---------------------------------------------------------------------------
# TRES string parsing -- tres_alloc_str/tres_req_str are always
# "<tres_id>=<count>,<tres_id>=<count>,..." (numeric ids, never names) on
# the wire; resolving id -> {type, name} needs a slurmdb_tres_get() fetched
# once per run (see Slurmdb.tres_by_id in fastsacct.py).
# ---------------------------------------------------------------------------
def parse_tres(raw, tres_by_id):
    if not raw:
        return []
    out = []
    for pair in raw.split(","):
        if not pair:
            continue
        tid_str, _, count_str = pair.partition("=")
        tid = int(tid_str)
        count = int(count_str)
        # tres_alloc_str/tres_req_str are written with an UNSIGNED format
        # specifier (assoc_mgr_make_tres_str_from_array(): "%u=%"PRIu64),
        # but the data_parser dumps a TRES entry's "count" as INT64
        # (parsers.c PARSER_ARRAY(TRES): add_parse(INT64, count, "count",
        # ...)) -- a sentinel like "unknown/not tracked" (-2, for e.g.
        # energy on nodes without power monitoring) round-trips through the
        # wire as its unsigned 64-bit bit pattern (18446744073709551614),
        # and real sacct reinterprets it back to -2. Confirmed against real
        # sacct --json output (see fastsacct/sample.json job 4's "energy"
        # TRES) -- without this, we'd show the raw unsigned value instead.
        if count >= 2**63:
            count -= 2**64
        info = tres_by_id.get(tid, {})
        out.append(
            {
                "type": info.get("type", ""),
                "name": info.get("name", ""),
                "id": tid,
                "count": count,
            }
        )
    return out


# ---------------------------------------------------------------------------
# JOB_PLANNED_TIME ("time/planned"): time required to start after becoming
# eligible. `now` is only used for the still-pending branch -- pass a
# consistent value (e.g. one int(time.time()) per fastsacct run) rather
# than calling time.time() per-job, so a single run's output is internally
# consistent.
# ---------------------------------------------------------------------------
def planned_time(eligible, start, end, now):
    if not eligible or eligible == INFINITE:
        diff = 0
    elif start == NO_VAL and end:
        diff = end - eligible
    elif start:
        diff = start - eligible
    else:
        diff = now - eligible
    return no_val(diff, 64)


# ---------------------------------------------------------------------------
# stdin/stdout/stderr %-expansion -- ported from expand_stdio_fields() /
# src/common/print_fields.c. `ctx` must have: jobid, array_job_id,
# array_task_id, jobname, user, node (see abi/*.py for how `node` differs
# between releases).
# ---------------------------------------------------------------------------
def expand_stdio(template, ctx):
    if not template:
        return template
    out = []
    escaped = False
    i, n = 0, len(template)
    while i < n:
        c = template[i]
        if c == "\\":
            escaped = True
            i += 1
            continue
        if escaped:
            out.append(c)
            i += 1
            continue
        if c != "%":
            out.append(c)
            i += 1
            continue
        i += 1
        pad = ""
        while i < n and template[i].isdigit():
            pad += template[i]
            i += 1
        if i >= n:
            out.append("%" + pad)
            break
        code = template[i]
        i += 1
        out.append(_expand_code(code, pad, ctx))
    return "".join(out)


def _numfmt(value, pad):
    s = str(value)
    return s.zfill(int(pad)) if pad else s


def _expand_code(code, pad, ctx):
    if code == "%":
        return "%"
    if code == "A":
        return _numfmt(ctx["array_job_id"] or ctx["jobid"], pad)
    if code == "a":
        return _numfmt(ctx["array_task_id"], pad)
    if code == "b":
        return _numfmt(ctx["array_task_id"] % 10, pad)
    if code in ("J", "j"):
        # %J would append ".<stepid>" if first_step_id != SLURM_BATCH_SCRIPT;
        # fastsacct always sets JOBCOND_FLAG_NO_STEP, so no step is ever
        # fetched and first_step_id is always SLURM_BATCH_SCRIPT -- no
        # suffix, matching real sacct's behavior under the same flag.
        return _numfmt(ctx["jobid"], pad)
    if code == "N":
        return ctx["node"]
    if code == "n":
        return "0"
    if code == "s":
        return "batch"
    if code == "t":
        return "0"
    if code == "u":
        return ctx["user"]
    if code == "x":
        return ctx["jobname"]
    return "%" + pad + code


# ---------------------------------------------------------------------------
# Best-effort Slurm hostlist "first node" extractor (e.g. "node[01-05,10]"
# -> "node01", "nodeA,nodeB[1-2]" -> "nodeA"). Covers the common
# single-bracket-range syntax %N needs; does not implement Slurm's full
# hostlist grammar (multi-dimensional/nested ranges).
# ---------------------------------------------------------------------------
def hostlist_first(nodespec):
    if not nodespec:
        return ""
    depth = 0
    for i, c in enumerate(nodespec):
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == "," and depth == 0:
            nodespec = nodespec[:i]
            break
    m = re.match(r"^(.*?)\[([^\]]*)\]$", nodespec)
    if not m:
        return nodespec
    prefix, ranges = m.groups()
    first_range = ranges.split(",")[0]
    return prefix + first_range.split("-")[0]


# ---------------------------------------------------------------------------
# req_mem overload: DUMP_FUNC(MEM_PER_CPUS)/DUMP_FUNC(MEM_PER_NODE). The top
# bit of job->req_mem (uint64_t) is a MEM_PER_CPU marker (slurm/slurm.h);
# whichever of the two interpretations doesn't apply reports NO_VAL64 (i.e.
# {"set": false, ...}), not 0 or omitted.
# ---------------------------------------------------------------------------
_MEM_PER_CPU = 0x8000000000000000


def mem_per_cpu(raw):
    cpu_mem = (raw & ~_MEM_PER_CPU) if (raw & _MEM_PER_CPU) else NO_VAL64
    return no_val(cpu_mem, 64)


def mem_per_node(raw):
    node_mem = raw if not (raw & _MEM_PER_CPU) else NO_VAL64
    return no_val(node_mem, 64)


# ---------------------------------------------------------------------------
# wckey -- DUMP_FUNC(WCKEY_TAG): a leading '*' on the raw string marks
# "assigned by default" and is stripped rather than kept in `wckey`.
# ---------------------------------------------------------------------------
def wckey_tag(raw):
    if not raw:
        return {"wckey": "", "flags": []}
    if raw.startswith("*"):
        return {"wckey": raw[1:], "flags": ["ASSIGNED_DEFAULT"]}
    return {"wckey": raw, "flags": []}


def setpath(d, path, value):
    """"/"-delimited dict nesting -- ported from data_define_dict_path():
    plain strtok_r(path, "/"), each token whitespace-trimmed, walking/
    creating one nested dict level per token. No array indices, no
    wildcards, no escaping."""
    parts = [p.strip() for p in path.split("/")]
    node = d
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
