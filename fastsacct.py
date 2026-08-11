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

Output is a flat JSON schema (plain scalars, not sacct's nested
{"set":...,"infinite":...,"number":...} OpenAPI shape) -- see
`meta.source` in the output to distinguish it from real `sacct --json`.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

import cffi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import full_format
from abi import v24_11, v25_05, v25_11

SCHEMA_VERSION = "fastsacct-flat-v1"
# --full's schema is meant to be sacct --json's actual nested schema, not
# a fastsacct-invented one -- versioned separately since its shape tracks
# upstream's data_parser output, not this flat schema's own evolution.
FULL_SCHEMA_VERSION = "sacct-json-compat-v1"

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
# ---------------------------------------------------------------------------
_ISO_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"(?:[T ](?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?)?$"
)


def parse_time(value):
    if value.lower() == "now":
        return int(datetime.datetime.now().timestamp())
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
        "--full",
        action="store_true",
        default=False,
        help="emit sacct --json's exact nested schema (state/reason/flags "
        "as decoded strings, NO_VAL-aware {set,infinite,number} wrapping, "
        "uid/gid/qos name resolution, TRES parsing, stdio %%-expansion) "
        "instead of the default flat schema. Slower: two extra one-time "
        "RPCs (TRES + QOS lists) plus per-job Python-side formatting -- "
        "see README for the flat-vs-full tradeoff. Default: flat.",
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


# ---------------------------------------------------------------------------
# libslurmdb binding
# ---------------------------------------------------------------------------
class SlurmdbError(RuntimeError):
    pass


class Slurmdb:
    def __init__(self, library_path, abi, debug=False, full=False):
        self.abi = abi
        self.debug = debug
        self.full = full
        self.ffi = cffi.FFI()
        self.ffi.cdef(abi.CDEF)
        if full and abi.ASSOC_CDEF:
            self.ffi.cdef(abi.ASSOC_CDEF)
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

            /* Needed only for --full: resolving TRES/QOS ids to names.
             * Both structs transcribed in full (not just a read prefix)
             * since we ffi.new() the *_cond_t ones ourselves and pass them
             * to the library -- under-declaring one of those would size
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
        if full:
            self.tres_by_id = self.fetch_tres()
            self.qos_by_id = self.fetch_qos()
            self.assoc_list = abi.fetch_assoc_list(self.ffi, self.lib, self.conn)
            self._debug(
                f"--full: fetched {len(self.tres_by_id)} TRES, "
                f"{len(self.qos_by_id)} QOS, "
                f"{len(self.assoc_list) if self.assoc_list else 0} associations "
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
        """id -> {"type", "name"}, for --full's TRES string resolution.
        Mirrors slurmdb_helpers.c's own `slurmdb_tres_cond_t cond = {
        .with_deleted = 1 }`."""
        cond = self.ffi.new("slurmdb_tres_cond_t *")
        cond.with_deleted = 1
        result = self.lib.slurmdb_tres_get(self.conn, cond)
        return {
            int(t.id): {"type": self._str(t.type), "name": self._str(t.name)}
            for t in self._iter_list(result, "slurmdb_tres_rec_t")
        }

    def fetch_qos(self):
        """id -> {"id", "name"}, for --full's QOS name resolution. Mirrors
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
        now = int(time.time())
        itr = self.lib.slurm_list_iterator_create(result)
        try:
            while True:
                job_ptr = self.lib.slurm_list_next(itr)
                if job_ptr == self.ffi.NULL:
                    break
                job = self.ffi.cast("slurmdb_job_rec_t *", job_ptr)
                jobs.append(
                    self._job_to_full_dict(job, now)
                    if self.full
                    else self._job_to_dict(job)
                )
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
            else:
                out[json_key] = int(val)
        return out

    def _job_to_full_dict(self, job, now):
        """Replicates the exact nested JSON shape plain `sacct --json`
        produces for a JOB record -- see full_format.py's module docstring
        and abi/*.py's JOB_ASSOC_ID/stdio_node comments for what's shared
        vs. per-ABI-version. Field-by-field correspondence to
        src/plugins/data_parser/v0.0.43/parsers.c's PARSER_ARRAY(JOB)
        (7498-7579); fields marked `add_skip()` there (db_index, env,
        first_step_ptr, show_full, uid, user, wckeyid) are intentionally
        absent here too."""
        ffi = self.ffi
        ff = full_format

        def s(v):
            return ffi.string(v).decode(errors="replace") if v != ffi.NULL else ""

        out = {}

        def setp(path, value):
            ff.setpath(out, path, value)

        setp("account", s(job.account))
        setp("comment/administrator", s(job.admin_comment))
        setp("allocation_nodes", int(job.alloc_nodes))
        setp("array/job_id", int(job.array_job_id))
        setp("array/limits/max/running/tasks", int(job.array_max_tasks))
        setp("array/task_id", ff.no_val(int(job.array_task_id), 32))
        setp("array/task", s(job.array_task_str))
        setp(
            "association",
            self.abi.job_assoc(job, ffi, getattr(self, "assoc_list", None)),
        )
        setp("block", s(job.blockid))
        setp("cluster", s(job.cluster))
        setp("constraints", s(job.constraints))
        setp("container", s(job.container))
        setp("derived_exit_code", ff.process_exit_code(int(job.derived_ec)))
        setp("comment/job", s(job.derived_es))
        setp("time/elapsed", int(job.elapsed))
        setp("time/eligible", int(job.eligible))
        setp("time/end", int(job.end))
        setp("exit_code", ff.process_exit_code(int(job.exitcode)))
        setp("extra", s(job.extra))
        setp("failed_node", s(job.failed_node))
        setp("flags", ff.job_flags(int(job.flags)))
        setp("group", ff.group_name(int(job.gid)))
        setp("het/job_id", int(job.het_job_id))
        setp("het/job_offset", ff.no_val(int(job.het_job_offset), 32))
        setp("job_id", int(job.jobid))
        setp("name", s(job.jobname))
        setp("licenses", s(job.licenses))
        setp("mcs/label", s(job.mcs_label))
        setp("nodes", s(job.nodes))
        setp("partition", s(job.partition))
        setp("hold", ff.hold(int(job.priority)))
        setp("priority", ff.no_val(int(job.priority), 32))
        setp("qos", ff.qos_name(int(job.qosid), self.qos_by_id))
        setp("qosreq", s(job.qos_req))
        setp("required/CPUs", int(job.req_cpus))
        setp("required/memory_per_cpu", ff.mem_per_cpu(int(job.req_mem)))
        setp("required/memory_per_node", ff.mem_per_node(int(job.req_mem)))
        setp("kill_request_user", ff.user_name(int(job.requid)))
        setp("restart_cnt", int(job.restart_cnt))
        setp("reservation/id", int(job.resvid))
        setp("reservation/name", s(job.resv_name))
        if hasattr(job, "resv_req"):  # 25.05 only
            setp("reservation/requested", s(job.resv_req))
        setp(
            "time/planned",
            ff.planned_time(int(job.eligible), int(job.start), int(job.end), now),
        )
        setp("script", s(job.script))
        if hasattr(job, "segment_size"):  # 25.05 only
            setp("segment_size", int(job.segment_size))
        node = self.abi.stdio_node(job, ffi)
        stdio_ctx = {
            "jobid": int(job.jobid),
            "array_job_id": int(job.array_job_id),
            "array_task_id": int(job.array_task_id),
            "jobname": s(job.jobname),
            "user": s(job.user),
            "node": node,
        }
        setp("stdin_expanded", ff.expand_stdio(s(job.std_in), stdio_ctx))
        setp("stdout_expanded", ff.expand_stdio(s(job.std_out), stdio_ctx))
        setp("stderr_expanded", ff.expand_stdio(s(job.std_err), stdio_ctx))
        setp("stdout", s(job.std_out))
        setp("stderr", s(job.std_err))
        setp("stdin", s(job.std_in))
        setp("time/start", int(job.start))
        setp("state/current", ff.job_state(int(job.state)))
        setp("state/reason", ff.job_state_reason(int(job.state_reason_prev)))
        # fastsacct always sets JOBCOND_FLAG_NO_STEP (mirrors sacct -X, a
        # required flag -- see module docstring), so the server never
        # populates job->steps for any query fastsacct can issue.
        setp("steps", [])
        setp("time/submission", int(job.submit))
        setp("submit_line", s(job.submit_line))
        setp("time/suspended", int(job.suspended))
        setp("comment/system", s(job.system_comment))
        setp("time/system/seconds", int(job.sys_cpu_sec))
        setp("time/system/microseconds", int(job.sys_cpu_usec))
        setp("time/limit", ff.no_val(int(job.timelimit), 32))
        setp("time/total/seconds", int(job.tot_cpu_sec))
        setp("time/total/microseconds", int(job.tot_cpu_usec))
        setp("tres/allocated", ff.parse_tres(s(job.tres_alloc_str), self.tres_by_id))
        setp("tres/requested", ff.parse_tres(s(job.tres_req_str), self.tres_by_id))
        setp("used_gres", s(job.used_gres))
        setp("user", ff.job_user(s(job.user), int(job.uid)))
        setp("time/user/seconds", int(job.user_cpu_sec))
        setp("time/user/microseconds", int(job.user_cpu_usec))
        setp("wckey", ff.wckey_tag(s(job.wckey)))
        setp("working_directory", s(job.work_dir))
        return out


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
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

    db = Slurmdb(args.library, abi, debug=args.debug, full=args.full)
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
            "schema_version": FULL_SCHEMA_VERSION if args.full else SCHEMA_VERSION,
            "slurm_abi_version": abi.SLURM_ABI_VERSION,
        },
        "jobs": jobs,
    }
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
