"""
fastsacct -- a fast, narrow replacement for:

    sacct -A <accounts> --starttime <t> --endtime <t> --duplicates -X --allusers --json

It calls libslurmdb.so directly (the same slurmdb_jobs_get() sacct itself
calls) via cffi, and skips Slurm's data_parser plugin entirely -- that
plugin, not the RPC, is what makes `sacct --json` slow (see README.md).

Only -A/--accounts, -S/--starttime, -E/--endtime are actually implemented.
-D/--duplicates, -X/--allocations, -a/--allusers, --json must ALSO be
passed (this tool's behavior always matches what those four mean in real
sacct: no dedup, job-level only, all users, JSON out) -- but they don't
change what we do, we just require them so an invocation that would
behave differently under plain sacct is rejected rather than silently
mishandled. Any other sacct flag is a hard error: this is a narrow
drop-in for one invocation shape, not a general sacct replacement.

Output is a flat JSON schema: one flat object per job, mostly plain
scalars (not sacct --json's nested {"set":...,"infinite":...,"number":...}
OpenAPI shape) with a handful of fields decoded to strings/lists or
expanded into sibling fields where that's cheap to do without an RPC per
job -- see README.md's "Output schema" section for the full list.
"""

import argparse
import datetime
import grp
import json
import os
import re
import subprocess
import sys

import cffi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from abi import v24_11, v25_05, v25_11

SCHEMA_VERSION = "fastsacct-flat-v1"

# One entry per Slurm release we've built an abi/vXX_YY.py module for.
# Add new releases here as they're built (see abi/v25_05.py's docstring
# for how to build one).
ABI_REGISTRY = {
    v25_11.SLURM_ABI_VERSION: v25_11,
    v25_05.SLURM_ABI_VERSION: v25_05,
    v24_11.SLURM_ABI_VERSION: v24_11,
}
ABI_BY_API_MAJOR = {m.SLURM_API_MAJOR: m for m in ABI_REGISTRY.values()}

# Documentation only, not used for dispatch: API_CURRENT (== API major,
# since API_AGE has been 0 for every release below) per Slurm release,
# gathered from each branch's META file. Lets an unimplemented-version
# error name the release instead of just a bare number.
API_MAJOR_RELEASE_HINTS = {
    38: "22.05",
    39: "23.02",
    40: "23.11",
    41: "24.05",
    42: "24.11",
    43: "25.05",
    44: "25.11",
    45: "26.05",
}


# ---------------------------------------------------------------------------
# ABI auto-detection.
#
# Primary: call the real libslurmdb.so's own `slurm_api_version()` (public,
# slurm/slurm.h) and decode the API_CURRENT it was built with -- this comes
# straight from the exact .so we're about to use, no subprocess needed.
#
# Fallback: if that probe fails for some reason, shell out to
# `sacct --version` (e.g. "slurm 24.11.7") and match on release string
# instead. Slower and one step removed from the actual .so we'll load, but
# still self-contained -- no need for the user to tell us anything.
# ---------------------------------------------------------------------------
def _api_major_via_libslurmdb(library_path):
    boot_ffi = cffi.FFI()
    boot_ffi.cdef("long slurm_api_version(void);")
    lib = boot_ffi.dlopen(library_path)
    raw = lib.slurm_api_version()
    return (raw >> 16) & 0xFF


def _abi_version_via_sacct():
    out = subprocess.run(
        ["sacct", "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    m = re.search(r"(\d+)\.(\d+)\.\d+", out)
    if not m:
        raise RuntimeError(f"couldn't parse `sacct --version` output: {out!r}")
    return f"{m[1]}.{m[2]}"


def detect_abi(library_path):
    try:
        api_major = _api_major_via_libslurmdb(library_path)
    except Exception as lib_exc:
        try:
            version_str = _abi_version_via_sacct()
        except Exception as sacct_exc:
            raise SlurmdbError(
                f"couldn't auto-detect the Slurm ABI: probing {library_path!r} via "
                f"slurm_api_version() failed ({lib_exc}), and the `sacct --version` "
                f"fallback also failed ({sacct_exc}). Pass --abi explicitly; "
                f"implemented: {', '.join(sorted(ABI_REGISTRY))}"
            ) from sacct_exc
        abi = ABI_REGISTRY.get(version_str)
        if abi is None:
            raise SlurmdbError(
                f"`sacct --version` reports Slurm {version_str}, but no abi/ module "
                f"is implemented for it. Implemented: {', '.join(sorted(ABI_REGISTRY))}. "
                "Pass --abi to override, or build a new abi/vXX_YY.py module "
                "(see abi/v25_05.py docstring)."
            )
        print(
            f"fastsacct: auto-detected Slurm {abi.SLURM_ABI_VERSION} via `sacct --version` "
            f"(slurm_api_version() probe failed: {lib_exc})",
            file=sys.stderr,
        )
        return abi

    abi = ABI_BY_API_MAJOR.get(api_major)
    if abi is None:
        hint = API_MAJOR_RELEASE_HINTS.get(api_major, "unknown release")
        raise SlurmdbError(
            f"{library_path} reports Slurm API major={api_major} ({hint}) via "
            "slurm_api_version(), but no abi/ module is implemented for it. "
            f"Implemented: {', '.join(f'{m.SLURM_ABI_VERSION} (API major {m.SLURM_API_MAJOR})' for m in ABI_REGISTRY.values())}. "
            "Pass --abi to override, or build a new abi/vXX_YY.py module "
            "(see abi/v25_05.py docstring)."
        )
    print(
        f"fastsacct: auto-detected Slurm {abi.SLURM_ABI_VERSION} "
        f"(API major {api_major}) via slurm_api_version()",
        file=sys.stderr,
    )
    return abi


# ---------------------------------------------------------------------------
# Time parsing: intentionally only a subset of Slurm's parse_time() grammar
# (see src/common/parse_time.c) -- ISO 8601 and "now". Slurm's own
# parse_time()/slurmdb_job_cond_def_start_end() aren't in the installed
# public headers (slurm/slurm.h, slurm/slurmdb.h), so calling them via cffi
# would mean depending on undocumented internal symbols; we'd rather fail
# loudly on an unsupported format than silently drift from an unversioned
# internal function.
#
# -S/-E values carry no timezone of their own (unlike --debug's usage_start/
# usage_end log line, which is always UTC regardless of system tz), so they
# are always interpreted as UTC -- never the process's local timezone. That
# is only actually correct if the system timezone *is* UTC, which
# require_utc_timezone() enforces at startup.
# ---------------------------------------------------------------------------
_ISO_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"(?:[T ](?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?)?$"
)


def require_utc_timezone():
    """
    Refuse to run under a non-UTC system timezone. parse_time() below and
    the --debug usage_start/usage_end log line both treat naive/epoch
    timestamps as UTC; if the process's actual local timezone (TZ env var,
    or /etc/localtime when TZ is unset) isn't UTC, -S/-E would silently be
    interpreted in the wrong timezone instead of erroring.
    """
    offset = datetime.datetime.now().astimezone().utcoffset()
    if offset != datetime.timedelta(0):
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        h, m = divmod(abs(total_minutes), 60)
        raise SlurmdbError(
            f"fastsacct requires the system timezone to be UTC, but the "
            f"resolved local timezone offset is UTC{sign}{h:02d}:{m:02d} "
            f"(TZ={os.environ.get('TZ')!r}). Set TZ=UTC (or configure the "
            "system timezone as UTC) before running fastsacct."
        )


def parse_time(value):
    if value.lower() == "now":
        return int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    m = _ISO_RE.match(value)
    if not m:
        raise argparse.ArgumentTypeError(
            f"unsupported time format: {value!r} "
            "(fastsacct only supports YYYY-MM-DD[THH:MM[:SS]] or 'now', "
            "not Slurm's full relative-time grammar)"
        )
    dt = datetime.datetime(
        int(m["y"]),
        int(m["m"]),
        int(m["d"]),
        int(m["H"] or 0),
        int(m["M"] or 0),
        int(m["S"] or 0),
        tzinfo=datetime.timezone.utc,
    )
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Argument parsing: only -A/-S/-E carry data; -D/-X/--json are required
# markers. Anything else is a hard error (argparse's default behavior for
# unknown flags, which we rely on rather than reimplement).
# ---------------------------------------------------------------------------
def build_argparser():
    p = argparse.ArgumentParser(
        prog="fastsacct",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-A",
        "--accounts",
        required=False,
        help="comma-separated account list (sacct -A)",
        default="",
    )
    p.add_argument(
        "-S",
        "--starttime",
        required=True,
        type=parse_time,
        help="sacct -S; YYYY-MM-DD[THH:MM[:SS]] or 'now'",
    )
    p.add_argument(
        "-E",
        "--endtime",
        required=True,
        type=parse_time,
        help="sacct -E; YYYY-MM-DD[THH:MM[:SS]] or 'now'",
    )
    p.add_argument(
        "-D",
        "--duplicates",
        action="store_true",
        default=False,
        help="required marker flag, must be passed (see module docstring)",
    )
    p.add_argument(
        "-X",
        "--allocations",
        action="store_true",
        default=False,
        help="required marker flag, must be passed (see module docstring)",
    )
    p.add_argument(
        "-a",
        "--allusers",
        action="store_true",
        default=False,
        help="required marker flag, must be passed (see module docstring)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="required marker flag, must be passed (see module docstring)",
    )
    p.add_argument(
        "--library",
        default="libslurmdb.so",
        help="path to libslurmdb.so (default: libslurmdb.so, "
        "resolved via the normal dynamic linker search path)",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="print job_cond fields and the raw slurmdb_jobs_get() result "
        "count to stderr before any Python-side processing",
    )
    p.add_argument(
        "--abi",
        choices=sorted(ABI_REGISTRY),
        default=None,
        help="Slurm release this cluster's libslurmdb.so was built "
        "from. Default: auto-detect (see detect_abi()). "
        f"Available: {', '.join(sorted(ABI_REGISTRY))}",
    )
    return p


def parse_args(argv):
    args = build_argparser().parse_args(argv)
    missing = [
        name
        for name, present in (
            ("-D/--duplicates", args.duplicates),
            ("-X/--allocations", args.allocations),
            ("-a/--allusers", args.allusers),
            ("--json", args.json),
        )
        if not present
    ]
    if missing:
        build_argparser().error(
            "fastsacct always behaves as if -D -X -a --json were given "
            "(no dedup, job-level only, all users, JSON output) -- pass "
            "them explicitly so that's clear at the call site. "
            f"Missing: {', '.join(missing)}"
        )
    if args.starttime > args.endtime:
        build_argparser().error("start time is after end time")
    return args


class SlurmdbError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Flat-output decode helpers -- state/flags/qos/group/TRES/exit-code fields
# that are cheap to resolve from raw job_rec_t values (no RPC per job) but
# not worth leaving as opaque ints for every downstream consumer to
# re-decode. Ported from sacct's own `data_parser` plugin
# (src/plugins/data_parser/v0.0.4{2,3,4}/parsers.c) and validated
# field-by-field against real `sacct --json` output on a real cluster
# (mila, Slurm 25.05.2). Every table/algorithm here was
# confirmed byte-for-byte identical between Slurm 24.11 (data_parser
# v0.0.42), 25.05 (v0.0.43), and 25.11 (v0.0.44) by direct diff of
# parsers.c -- that's why this is version-independent
# (unlike abi/*.py) -- with two purely additive exceptions folded in
# directly below (a new SLURMDB_JOB_FLAGS bit and a new JOB_STATE flag
# bit, both added in 25.11; see the comments next to
# `_JOB_STATE_FLAG_BITS`).
# ---------------------------------------------------------------------------

# NO_VAL/INFINITE sentinels, from slurm/slurm.h.
NO_VAL, INFINITE = 0xFFFFFFFE, 0xFFFFFFFF

# job state -- PARSER_FLAG_ARRAY(JOB_STATE) in parsers.c; base-state values
# from enum job_states, flag-bit values from the JOB_* SLURM_BIT macros,
# both in slurm/slurm.h (public, version-independent header).
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


# job flags -- slurmdb_job_rec_t.flags (NOT job_cond->flags/db_flags), via
# PARSER_FLAG_ARRAY(SLURMDB_JOB_FLAGS) in parsers.c; values from
# slurm/slurmdb.h SLURMDB_JOB_FLAG_*.
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


# process exit code -- job->exitcode / job->derived_ec, packed like a Unix
# wait-status int. Ported from DUMP_FUNC(PROCESS_EXIT_CODE): WIFEXITED is
# "low 7 bits are 0", WEXITSTATUS is "next 8 bits", WIFSIGNALED/WTERMSIG use
# the low 7 bits as a signal number. Returns the plain return code / signal
# number, `None` for whichever doesn't apply (including the WCOREDUMP and
# still-pending cases, which have neither).
def process_exit_code(raw):
    low7 = raw & 0x7F
    if raw == NO_VAL:
        rc, sig = None, None  # still pending
    elif low7 == 0:
        rc, sig = (raw >> 8) & 0xFF, None
    elif low7 != 0x7F:
        rc, sig = None, low7  # signaled
    else:
        rc, sig = None, None  # core-dumped, or an invalid wait status
    return {"return_code": rc, "signal": sig}


# gid -> name resolution -- DUMP_FUNC(GROUP_ID): a local NSS lookup
# (getgrgid), not an RPC. Non-complex mode (what plain `sacct --json` used
# to run in) falls back to "" on a failed lookup.
def group_name(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return ""


# QOS id -> name. Needs a slurmdb_qos_get() fetched once per run (see
# Slurmdb.qos_by_id below) -- ported from DUMP_FUNC(QOS_ID).
def qos_name(qid, qos_by_id):
    if not qid or qid == INFINITE:
        return ""
    q = qos_by_id.get(qid)
    if q is None:
        return "Unknown"
    return q["name"] or str(q["id"])


# TRES string parsing -- tres_alloc_str/tres_req_str are always
# "<tres_id>=<count>,<tres_id>=<count>,..." (numeric ids, never names) on
# the wire; resolving id -> {type, name} needs a slurmdb_tres_get() fetched
# once per run (see Slurmdb.tres_by_id below).
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
        # sacct --json output -- without this, we'd show the raw unsigned
        # value instead.
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
# Turns a TRES "type" or "name" (e.g. "gres", "gpu:a100") into a JSON-key-
# safe fragment for _tres_flat_fields below -- lowercased, non-alnum runs
# (":", "/", ...) collapsed to a single "_".
# ---------------------------------------------------------------------------
def _tres_key_part(value):
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") if value else ""


# ---------------------------------------------------------------------------
# libslurmdb binding
# ---------------------------------------------------------------------------


class Slurmdb:
    def __init__(self, library_path, abi, debug=False):
        self.abi = abi
        self.debug = debug
        self.ffi = cffi.FFI()
        self.ffi.cdef(abi.CDEF)
        # Diagnostic-only: slurm_conf is the same global struct auth/accounting
        # plugins reference (see the RTLD_GLOBAL note below) -- reading its
        # first few fields after slurm_init() tells us which accounting
        # storage host/port/type our process actually resolved, so we can
        # compare against what `sacct` connects to on the same box. Only the
        # prefix up to accounting_storage_type is declared (matches the
        # "declare fields in order up to the last one we care about" rule
        # used in abi/*.py) -- not tied to a specific Slurm release's ABI
        # since this prefix has been stable across 24.11/25.05.
        self.ffi.cdef(
            """
            typedef struct {
                time_t last_update;
                char *accounting_storage_tres;
                uint16_t accounting_storage_enforce;
                char *accounting_storage_backup_host;
                char *accounting_storage_ext_host;
                char *accounting_storage_host;
                char *accounting_storage_params;
                char *accounting_storage_pass;
                uint16_t accounting_storage_port;
                char *accounting_storage_type;
            } slurm_conf_t;
            extern slurm_conf_t slurm_conf;

            /* TRES id->name resolution feeds _tres_flat_fields's
             * allocated_/requested_ fields; QOS id->name resolution feeds
             * the "qos" field. Both structs transcribed in full (not just a
             * read prefix) since we ffi.new() the *_cond_t ones ourselves
             * and pass them to the library -- under-declaring one of those
             * would size
             * our allocation smaller than what the real function expects
             * to read/write, corrupting adjacent heap memory. Confirmed
             * against slurm/slurmdb.h (25.05); version-independent for the
             * fields used here. */
            typedef struct {
                uint64_t alloc_secs;
                uint32_t rec_count;
                uint64_t count;
                uint32_t id;
                char *name;
                char *type;
            } slurmdb_tres_rec_t;
            typedef struct {
                uint64_t count;
                list_t *format_list;
                list_t *id_list;
                list_t *name_list;
                list_t *type_list;
                uint16_t with_deleted;
            } slurmdb_tres_cond_t;
            list_t *slurmdb_tres_get(void *db_conn, slurmdb_tres_cond_t *tres_cond);
            void slurmdb_destroy_tres_rec(void *object);

            typedef struct {
                time_t blocked_until;
                char *description;
                uint32_t id;
                int32_t flags;
                uint32_t grace_time;
                uint32_t grp_jobs_accrue;
                uint32_t grp_jobs;
                uint32_t grp_submit_jobs;
                char *grp_tres;
                uint64_t *grp_tres_ctld;
                char *grp_tres_mins;
                uint64_t *grp_tres_mins_ctld;
                char *grp_tres_run_mins;
                uint64_t *grp_tres_run_mins_ctld;
                uint32_t grp_wall;
                double limit_factor;
                uint32_t max_jobs_pa;
                uint32_t max_jobs_pu;
                uint32_t max_jobs_accrue_pa;
                uint32_t max_jobs_accrue_pu;
                uint32_t max_submit_jobs_pa;
                uint32_t max_submit_jobs_pu;
                char *max_tres_mins_pj;
                uint64_t *max_tres_mins_pj_ctld;
                char *max_tres_pa;
                uint64_t *max_tres_pa_ctld;
                char *max_tres_pj;
                uint64_t *max_tres_pj_ctld;
                char *max_tres_pn;
                uint64_t *max_tres_pn_ctld;
                char *max_tres_pu;
                uint64_t *max_tres_pu_ctld;
                char *max_tres_run_mins_pa;
                uint64_t *max_tres_run_mins_pa_ctld;
                char *max_tres_run_mins_pu;
                uint64_t *max_tres_run_mins_pu_ctld;
                uint32_t max_wall_pj;
                uint32_t min_prio_thresh;
                char *min_tres_pj;
                uint64_t *min_tres_pj_ctld;
                char *name;
                void *preempt_bitstr;
                list_t *preempt_list;
                uint16_t preempt_mode;
                uint32_t preempt_exempt_time;
                uint32_t priority;
                uint64_t *relative_tres_cnt;
                void *usage;
                double usage_factor;
                double usage_thres;
            } slurmdb_qos_rec_t;
            typedef struct {
                list_t *description_list;
                uint16_t flags;
                list_t *id_list;
                list_t *format_list;
                list_t *name_list;
                uint16_t preempt_mode;
            } slurmdb_qos_cond_t;
            list_t *slurmdb_qos_get(void *db_conn, slurmdb_qos_cond_t *qos_cond);
            void slurmdb_destroy_qos_rec(void *object);
            """
        )
        # RTLD_GLOBAL: slurm_init()/slurmdb_connection_get() below make Slurm's
        # plugin loader dlopen() auth/accounting_storage plugins (e.g.
        # auth_munge.so), which reference globals like slurm_conf that they
        # expect to already be resolvable in the process's symbol table --
        # exactly how they'd see it inside slurmd/sacct, which pull this
        # library in via DT_NEEDED (global scope by default). cffi's dlopen()
        # defaults to RTLD_LOCAL, which walls those symbols off and makes the
        # plugin load fail with "undefined symbol: slurm_conf".
        self.lib = self.ffi.dlopen(
            library_path, self.ffi.RTLD_NOW | self.ffi.RTLD_GLOBAL
        )
        self.lib.slurm_init(self.ffi.NULL)
        if debug:
            sc = self.lib.slurm_conf

            def _s(v):
                return self.ffi.string(v).decode() if v != self.ffi.NULL else None

            print(
                "fastsacct: debug: slurm_conf after slurm_init(): "
                f"accounting_storage_type={_s(sc.accounting_storage_type)}, "
                f"accounting_storage_host={_s(sc.accounting_storage_host)}, "
                f"accounting_storage_backup_host={_s(sc.accounting_storage_backup_host)}, "
                f"accounting_storage_port={int(sc.accounting_storage_port)}",
                file=sys.stderr,
            )
        flags = self.ffi.new("uint16_t *")
        self.conn = self.lib.slurmdb_connection_get(flags)
        if self.conn == self.ffi.NULL:
            raise SlurmdbError(self._strerror())
        # Two one-time RPCs: flat expands tres_alloc_str/tres_req_str into
        # allocated_*/requested_* fields (see _tres_flat_fields) and qosid
        # into a qos name field, which need these same id->name maps.
        self.tres_by_id = self.fetch_tres()
        self.qos_by_id = self.fetch_qos()
        self._debug(
            f"fetched {len(self.tres_by_id)} TRES, {len(self.qos_by_id)} QOS "
            "for id resolution"
        )

    def _debug(self, msg):
        if self.debug:
            print(f"fastsacct: debug: {msg}", file=sys.stderr)

    def _strerror(self):
        msg = self.lib.slurm_strerror(self.ffi.errno)
        return (
            self.ffi.string(msg).decode() if msg != self.ffi.NULL else "unknown error"
        )

    def _str(self, v):
        return self.ffi.string(v).decode() if v != self.ffi.NULL else ""

    def _iter_list(self, result, ctype):
        """Yield each element of a slurm list_t* as `ctype *`, then destroy
        the list. Caller is responsible for freeing each element via the
        matching slurmdb_destroy_*_rec if the library doesn't already own
        that memory -- for the two read-only lookups that use this
        (fetch_tres/fetch_qos) we only copy scalars out and destroy via
        slurm_list_destroy(), matching how jobs_get() already handles
        slurmdb_jobs_get()'s result list."""
        if result == self.ffi.NULL:
            return
        itr = self.lib.slurm_list_iterator_create(result)
        try:
            while True:
                ptr = self.lib.slurm_list_next(itr)
                if ptr == self.ffi.NULL:
                    break
                yield self.ffi.cast(f"{ctype} *", ptr)
        finally:
            self.lib.slurm_list_iterator_destroy(itr)
            self.lib.slurm_list_destroy(result)

    def fetch_tres(self):
        """id -> {"type", "name"}, for TRES string resolution (see
        _tres_flat_fields). Mirrors slurmdb_helpers.c's own
        `slurmdb_tres_cond_t cond = { .with_deleted = 1 }`."""
        cond = self.ffi.new("slurmdb_tres_cond_t *")
        cond.with_deleted = 1
        result = self.lib.slurmdb_tres_get(self.conn, cond)
        return {
            int(t.id): {"type": self._str(t.type), "name": self._str(t.name)}
            for t in self._iter_list(result, "slurmdb_tres_rec_t")
        }

    def fetch_qos(self):
        """id -> {"id", "name"}, for the "qos" field's name resolution. Mirrors
        slurmdb_helpers.c's `slurmdb_qos_cond_t cond = { .flags =
        QOS_COND_FLAG_WITH_DELETED }` -- without it, a job whose QOS was
        later deleted would resolve to "Unknown" here but to its real name
        under real sacct, which always asks for deleted QOS too."""
        cond = self.ffi.new("slurmdb_qos_cond_t *")
        cond.flags = 1  # QOS_COND_FLAG_WITH_DELETED = SLURM_BIT(0)
        result = self.lib.slurmdb_qos_get(self.conn, cond)
        return {
            int(q.id): {"id": int(q.id), "name": self._str(q.name)}
            for q in self._iter_list(result, "slurmdb_qos_rec_t")
        }

    def close(self):
        conn_holder = self.ffi.new("void **", self.conn)
        self.lib.slurmdb_connection_close(conn_holder)

    def _str_list(self, values):
        lst = self.lib.slurm_list_create(self.ffi.NULL)
        # keep the char[] buffers alive for the lifetime of this call by
        # stashing them on the returned list object
        bufs = []
        for v in values:
            buf = self.ffi.new("char[]", v.encode())
            bufs.append(buf)
            self.lib.slurm_list_append(lst, buf)
        return lst, bufs

    def jobs_get(self, accounts, start, end):
        job_cond = self.ffi.new("slurmdb_job_cond_t *")
        # job_cond.userid_list is left NULL (ffi.new zero-inits the struct),
        # which is exactly what --allusers means to slurmdb_jobs_get(): sacct
        # itself only narrows to the caller's uid when userid_list is empty
        # AND -a wasn't passed (src/sacct/options.c ~1034-1042). We require
        # -a/--allusers at the CLI level (see parse_args) rather than filter
        # here, so there's nothing else to do for it.
        if accounts:
            acct_list, _keepalive = self._str_list(accounts)
            job_cond.acct_list = acct_list
        job_cond.usage_start = start
        job_cond.usage_end = end
        job_cond.flags = (
            self.abi.JOBCOND_FLAG_DUP
            | self.abi.JOBCOND_FLAG_NO_STEP
            | self.abi.JOBCOND_FLAG_NO_TRUNC
        )
        # job_cond->db_flags is a DIFFERENT sentinel from job_cond->flags
        # above: 0 is not a wildcard here, it's SLURMDB_JOB_FLAG_NONE, which
        # the accounting_storage/mysql query builder turns into a literal
        # `t1.flags = 0` filter -- matching almost no real job, since e.g.
        # SLURMDB_JOB_FLAG_START_R gets set the moment a job's start RPC
        # lands (as_mysql_job.c). sacct always sets this to NOTSET in
        # _init_params() (src/sacct/options.c) so that clause is skipped
        # entirely; leaving it unset here is what was causing every query to
        # silently return zero jobs regardless of window/account/user.
        job_cond.db_flags = self.abi.JOBCOND_DB_FLAG_NOTSET

        self._debug(
            f"job_cond before slurmdb_jobs_get(): "
            f"usage_start={int(job_cond.usage_start)} "
            f"({datetime.datetime.fromtimestamp(int(job_cond.usage_start), datetime.timezone.utc).isoformat()} UTC), "
            f"usage_end={int(job_cond.usage_end)} "
            f"({datetime.datetime.fromtimestamp(int(job_cond.usage_end), datetime.timezone.utc).isoformat()} UTC), "
            f"flags=0x{int(job_cond.flags):x}, "
            f"db_flags=0x{int(job_cond.db_flags):x}, "
            f"acct_list={'set (' + str(len(accounts)) + ' entries)' if accounts else 'NULL'}, "
            f"userid_list=NULL, cluster_list=NULL"
        )

        result = self.lib.slurmdb_jobs_get(self.conn, job_cond)

        self._debug(
            f"slurmdb_jobs_get() returned "
            f"{'NULL' if result == self.ffi.NULL else self.lib.slurm_list_count(result)} "
            f"(errno={self.ffi.errno})"
        )

        self.lib.slurmdb_destroy_job_cond_members(job_cond)

        if result == self.ffi.NULL:
            raise SlurmdbError(self._strerror())

        jobs = []
        itr = self.lib.slurm_list_iterator_create(result)
        try:
            while True:
                job_ptr = self.lib.slurm_list_next(itr)
                if job_ptr == self.ffi.NULL:
                    break
                job = self.ffi.cast("slurmdb_job_rec_t *", job_ptr)
                jobs.append(self._job_to_dict(job))
        finally:
            self.lib.slurm_list_iterator_destroy(itr)
            self.lib.slurm_list_destroy(result)

        return jobs

    def _job_to_dict(self, job):
        out = {}
        for json_key, c_field, kind in self.abi.JOB_FIELDS:
            val = getattr(job, c_field)
            if kind == "str":
                out[json_key] = (
                    self.ffi.string(val).decode(errors="replace")
                    if val != self.ffi.NULL
                    else None
                )
            elif kind == "group":
                # A local NSS lookup (getgrgid), not an RPC -- cheap enough
                # to resolve even though every other flat field is left raw.
                out[json_key] = group_name(int(val))
            elif kind == "flags":
                # PARSER_FLAG_ARRAY(SLURMDB_JOB_FLAGS) decoding -- pure bit
                # twiddling against a fixed table, no RPC.
                out[json_key] = job_flags(int(val))
            elif kind == "job_state":
                # Base state name + any flag bits -- pure bit twiddling
                # against a fixed table, no RPC.
                out[json_key] = job_state(int(val))
            elif kind == "qos_name":
                # Dict lookup against the one-time slurmdb_qos_get() fetch,
                # no RPC per job -- replaces the raw qosid rather than
                # sitting alongside it.
                out[json_key] = qos_name(int(val), self.qos_by_id)
            elif kind == "no_val32":
                # NO_VAL/INFINITE sentinel collapsed to a plain null.
                ival = int(val)
                out[json_key] = None if ival in (NO_VAL, INFINITE) else ival
            elif kind == "u32_inf0":
                # INFINITE sentinel collapsed to 0 -- for a u32 field where
                # INFINITE means "no limit" (e.g. time_timelimit for a job
                # with no time limit), not "unset"/NO_VAL.
                ival = int(val)
                out[json_key] = 0 if ival == INFINITE else ival
            else:
                out[json_key] = int(val)
        out.update(self._tres_flat_fields("allocated", out.get("tres_alloc_str")))
        out.update(self._tres_flat_fields("requested", out.get("tres_req_str")))
        out.update(self._exit_code_flat_fields("exitcode", out["exitcode"]))
        return out

    def _exit_code_flat_fields(self, prefix, raw):
        """Expand a wait-status-packed exitcode like job->exitcode into
        "<prefix>_return_code"/"<prefix>_signal" via process_exit_code's
        WIFEXITED/WIFSIGNALED decoding."""
        decoded = process_exit_code(raw)
        return {
            f"{prefix}_return_code": decoded["return_code"],
            f"{prefix}_signal": decoded["signal"],
        }

    def _tres_flat_fields(self, prefix, raw):
        """Expand a tres_alloc_str/tres_req_str like "1=4,2=17179869184,
        1001=2" into {"<prefix>_cpu": 4, "<prefix>_mem": 17179869184,
        "<prefix>_gres_gpu": 2, ...}, reusing parse_tres for the
        id->{type,name} resolution and its int64 sign-fix (a count like
        18446744073709551614 is really -2, see parse_tres).

        A GRES name optionally carries a model/type suffix after a colon
        (e.g. "gpu:a100" -- the "a100" is which GPU model, not always
        present). That suffix is kept out of the key -- "gres_gpu" stays
        "gres_gpu" whether or not a model is reported -- and surfaced
        instead as a separate "<prefix>_<key>_type" field, so consumers
        can group by GRES name without the key varying per model."""
        fields = {}
        for entry in parse_tres(raw or "", self.tres_by_id):
            type_key = _tres_key_part(entry["type"]) or f"id_{entry['id']}"
            name, _, gres_type = entry["name"].partition(":")
            name_key = _tres_key_part(name)
            key = f"{type_key}_{name_key}" if name_key else type_key
            fields[f"{prefix}_{key}"] = entry["count"]
            if gres_type:
                fields[f"{prefix}_{key}_type"] = gres_type
        return fields


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        require_utc_timezone()
    except SlurmdbError as exc:
        print(f"fastsacct: {exc}", file=sys.stderr)
        return 1

    # sacct -A runs each entry through slurm_addto_char_list(), which trims
    # whitespace and lower-cases it (src/common/slurm_protocol_defs.c
    # _add_to_list()) before it ever reaches the accounting_storage/mysql
    # plugin's case-sensitive `t1.account='%s'` filter. Match that here, or
    # a mixed-case/space-padded -A value silently matches zero rows instead
    # of erroring or matching like real sacct would.
    accounts = [a.strip().lower() for a in args.accounts.split(",") if a.strip()]

    try:
        abi = ABI_REGISTRY[args.abi] if args.abi else detect_abi(args.library)
    except SlurmdbError as exc:
        print(f"fastsacct: {exc}", file=sys.stderr)
        return 1

    db = Slurmdb(args.library, abi, debug=args.debug)
    try:
        jobs = db.jobs_get(accounts, args.starttime, args.endtime)
    except SlurmdbError as exc:
        print(f"fastsacct: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    output = {
        "meta": {
            "source": "fastsacct",
            "schema_version": SCHEMA_VERSION,
            "slurm_abi_version": abi.SLURM_ABI_VERSION,
        },
        "jobs": jobs,
    }
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
