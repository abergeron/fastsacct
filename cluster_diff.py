#!/usr/bin/env python3
"""
Diff fastsacct's flat JSON output against real `sacct --json` output for
the same query -- the Rust port's version of the original decode-helper
validation done by hand on mila (see ARCHITECTURE.md
"Decode-helper validation").

Usage:
    cluster_diff.py fastsacct.json sacct.json [--dump-keys] [--verbose]

The sacct --json key mapping below was built against the real v0.0.4x
(data_parser) schema as observed on Slurm 25.11 (nested `time`, `tres`,
`comment`, `required`, `reservation`, `state`, wrapped
{set,infinite,number} scalars). --dump-keys prints the sacct job keys of
the first job when a release's shape differs and the mapping needs new
candidates.

Hard comparisons (mismatch => exit 1): every fastsacct field with a
semantically identical sacct counterpart -- strings, state/flags lists,
qos/group names, the time scalars, timelimit (INFINITE->0), array/het
sentinel wrapping, the decoded exit-code pair (both sides derive it from
the same wait status, so comparing it validates the raw field too),
req_mem's MEM_PER_CPU (bit-63) split, the TRES id->count strings
(rebuilt from sacct's structured entries with the wire's unsigned count
format), the allocated_*/requested_* key-rule expansion, and
state_reason_prev (decoded via the reason table parsed from the vendored
slurm source, when available).

Fields fastsacct emits but sacct --json does not carry are listed as
skipped (db_index, requid, show_full, lineage, uid/gid/wckeyid numeric
ids -- sacct shows the name-resolved forms instead).
"""

import json
import re
import sys

NO_VAL = 0xFFFFFFFE
INFINITE = 0xFFFFFFFF
MEM_PER_CPU = 0x8000_0000_0000_0000

VERBOSE = False


# ---------------------------------------------------------------------------
# sacct --json wrappers
# ---------------------------------------------------------------------------
def unwrap(v):
    """Plain value passthrough; {set,infinite,number}/{string}/{...} wrappers
    -> (kind, val) with kind in {v, unset, inf}; plain 0/int -> (v, int)."""
    if isinstance(v, dict):
        if "set" in v and "infinite" in v:
            if v.get("infinite"):
                return ("inf", v.get("number"))
            if not v.get("set"):
                return ("unset", v.get("number"))
            return ("v", v.get("number"))
        for k in ("number", "string", "id"):
            if k in v:
                return unwrap(v[k])
        return ("dict", {k: unwrap(x) for k, x in v.items()})
    return ("v", v)


def dig(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class Diff:
    def __init__(self):
        self.mismatches = []  # (field, job_id, fa, sa)
        self.checked = {}
        self.infos = set()

    def eq(self, field, jid, fa_v, sa_v):
        self.checked[field] = self.checked.get(field, 0) + 1
        if fa_v != sa_v:
            self.mismatches.append((field, jid, fa_v, sa_v))

    def eq_sentinel(self, field, jid, fa_v, sa_v, sentinels=(NO_VAL, INFINITE)):
        """sacct normalizes an unset field to 0 / '' (and a NULL char* to '')
        where fastsacct keeps the raw sentinel / null."""
        self.checked[field] = self.checked.get(field, 0) + 1
        if fa_v != sa_v and not (sa_v in (0, "") and (fa_v in sentinels or fa_v is None)):
            self.mismatches.append((field, jid, fa_v, sa_v))

    def eq_wrapped(self, field, jid, fa_v, wrapped, sentinels=(NO_VAL, INFINITE),
                   null_on_unset=False, zero_on_inf=False, null_no_val_inf_zero=False):
        """Compare fastsacct's raw value against sacct's {set,infinite,number}
        wrapper. null_on_unset: fastsacct collapses NO_VAL/INFINITE to null
        for this field (NoVal32). zero_on_inf: fastsacct maps INFINITE to 0
        (i.e. timelimit). null_no_val_inf_zero: fastsacct maps NO_VAL to
        null and INFINITE to 0 (U32Inf0NullNoVal, i.e. priority). Otherwise
        the raw sentinel is emitted."""
        self.checked[field] = self.checked.get(field, 0) + 1
        kind, num = unwrap(wrapped)
        if kind == "v":
            ok = fa_v == num
        elif kind == "inf":
            if zero_on_inf or null_no_val_inf_zero:
                ok = fa_v == 0
            else:
                ok = fa_v is None if null_on_unset else fa_v in sentinels
        else:  # unset (NO_VAL)
            if null_no_val_inf_zero:
                ok = fa_v is None
            else:
                ok = fa_v is None if null_on_unset else fa_v in sentinels
        if not ok:
            self.mismatches.append((field, jid, fa_v, wrapped))


# ---------------------------------------------------------------------------
# job state reason table, parsed from the vendored slurm source when present
# ---------------------------------------------------------------------------
def load_reason_table(root="slurm"):
    """num -> display string, mirroring slurm_job_state_reason_str():
    parse `enum job_state_reason` from slurm.h (implicit + explicit values,
    non-WAIT members included) and the jsra[] .str table from
    job_state_reason.c."""
    try:
        hdr = open(f"{root}/slurm/slurm.h").read()
        src = open(f"{root}/src/common/job_state_reason.c").read()
    except OSError:
        return None
    i = hdr.find("enum job_state_reason")
    if i < 0:
        return None
    body = re.sub(r"/\*.*?\*/", "", hdr[hdr.index("{", i) + 1:hdr.index("}", i)], flags=re.S)
    enums, val = {}, -1
    for tok in body.split(","):
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(\S+))?\s*$", tok)
        if not m:
            continue
        val = int(m.group(2), 0) if m.group(2) else val + 1
        enums[m.group(1)] = val
    table = {}
    for m in re.finditer(r"\[\s*([A-Z0-9_]+)\s*\]\s*=\s*\{[^}]*?\.str\s*=\s*\"([^\"]+)\"", src, re.S):
        if m[1] in enums:
            table[enums[m[1]]] = m[2]
    return table or None


REASON_TABLE = load_reason_table()
REASON_NUM = {v: k for k, v in (REASON_TABLE or {}).items()}


# ---------------------------------------------------------------------------
# TRES: same key rules as src/slurmdb.rs tres_flat_fields / parse_tres
# ---------------------------------------------------------------------------
def tres_key_part(value):
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")


def expand_tres(entries, prefix):
    out = {}
    for e in entries or []:
        type_key = tres_key_part(e.get("type", "")) or f"id_{e.get('id')}"
        name, _, gres_type = (e.get("name") or "").partition(":")
        name_key = tres_key_part(name)
        key = f"{type_key}_{name_key}" if name_key else type_key
        out[f"{prefix}_{key}"] = e.get("count")
        if gres_type:
            out[f"{prefix}_{key}_type"] = gres_type
    return out


def rebuild_tres_str(entries):
    # wire format is "<id>=<count>,..." written with a UNSIGNED count
    # format specifier (a -2 energy sentinel shows as 18446744073709551614)
    return ",".join(f"{e['id']}={e['count'] % 2**64}" for e in (entries or []))


# ---------------------------------------------------------------------------
# mapping: fastsacct key -> sacct dotted path (plain or wrapped scalar)
# ---------------------------------------------------------------------------
DIRECT = {
    "account": "account",
    "admin_comment": "comment.administrator",
    "system_comment": "comment.system",
    "comment": "comment.job",
    "alloc_nodes": "allocation_nodes",
    "array_task_str": "array.task",
    "associd": "association.id",
    "blockid": "block",
    "cluster": "cluster",
    "constraints": "constraints",
    "container": "container",
    "extra": "extra",
    "failed_node": "failed_node",
    "licenses": "licenses",
    "mcs_label": "mcs.label",
    "name": "name",
    "nodes": "nodes",
    "partition": "partition",
    "qos_req": "qosreq",
    "req_cpus": "required.CPUs",
    "restart_cnt": "restart_cnt",
    "resvid": "reservation.id",
    "resv_name": "reservation.name",
    "resv_req": "reservation.requested",
    "segment_size": "segment_size",
    "std_err": "stderr",
    "std_in": "stdin",
    "std_out": "stdout",
    "submit_line": "submit_line",
    "used_gres": "used_gres",
    "user": "user",
    "work_dir": "working_directory",
    "het_job_id": "het.job_id",
    "array_job_id": "array.job_id",
    "array_max_tasks": "array.limits.max.running.tasks",
    "time_elapsed": "time.elapsed",
    "time_eligible": "time.eligible",
    "time_end": "time.end",
    "time_start": "time.start",
    "time_submit": "time.submission",
    "time_suspended": "time.suspended",
    "sys_cpu_sec": "time.system.seconds",
    "sys_cpu_usec": "time.system.microseconds",
    "tot_cpu_sec": "time.total.seconds",
    "tot_cpu_usec": "time.total.microseconds",
    "user_cpu_sec": "time.user.seconds",
    "user_cpu_usec": "time.user.microseconds",
}

# wrapped sacct scalars -> per-field fastsacct sentinel policy
# (array_task_id/het_job_offset are NoVal32 -> null; priority is
# U32Inf0NullNoVal -> NO_VAL null, INFINITE 0)
WRAPPED = {
    "priority": ("priority", {"null_no_val_inf_zero": True}),
    "array_task_id": ("array.task_id", {"null_on_unset": True}),
    "het_job_offset": ("het.job_offset", {"null_on_unset": True}),
}

# fastsacct fields with no sacct --json counterpart (name-resolved or absent)
SKIPPED = {
    "db_index", "requid", "show_full", "lineage",
    "uid", "gid", "wckeyid",  # sacct shows user/group name strings instead
    "derived_ec",  # sacct gives the decoded form only (compare via it below)
    "exitcode",    # raw wait status: validated transitively via the decoded pair
}


def compare_exit_code(d: Diff, fa, sa, jid):
    w = dig(sa, "exit_code.return_code")
    fa_v = fa.get("exitcode_return_code")
    if w is None:
        return
    kind, num = unwrap(w)
    d.checked["exitcode_return_code"] = d.checked.get("exitcode_return_code", 0) + 1
    if kind == "v" and num is not None and fa_v != num:
        d.mismatches.append(("exitcode_return_code", jid, fa_v, w))
    if kind in ("unset",) and fa_v is not None:
        d.mismatches.append(("exitcode_return_code", jid, fa_v, w))
    w = dig(sa, "exit_code.signal.id")
    fa_v = fa.get("exitcode_signal")
    if w is None:
        return
    kind, num = unwrap(w)
    d.checked["exitcode_signal"] = d.checked.get("exitcode_signal", 0) + 1
    # a signal id of 0 / unset means "no signal" -> fastsacct emits null
    if kind == "v" and (num or 0) > 0 and fa_v != num:
        d.mismatches.append(("exitcode_signal", jid, fa_v, w))
    if (kind in ("unset", "inf") or num in (None, 0)) and fa_v is not None:
        d.mismatches.append(("exitcode_signal", jid, fa_v, w))


def compare_all(fa_jobs, sa_jobs):
    d = Diff()
    sa_by_id = {}
    for j in sa_jobs:
        jid = j.get("job_id")
        if isinstance(jid, dict):
            jid = unwrap(jid)[1]
        sa_by_id[jid] = j

    if len(fa_jobs) != len(sa_by_id):
        print(f"note: fastsacct {len(fa_jobs)} jobs, sacct {len(sa_by_id)} unique ids")

    for fa in fa_jobs:
        jid = fa["job_id"]
        s = sa_by_id.get(jid)
        if s is None:
            d.mismatches.append(("<presence>", jid, "present", "MISSING from sacct"))
            continue

        # --- sacct job identity/name sanity (jobid+name+account must agree)
        active = bool(set(fa.get("state") or []) &
                      {"RUNNING", "COMPLETING", "CONFIGURING", "RESIZING", "PENDING"})
        for f, p in DIRECT.items():
            sa_v = dig(s, p)
            if sa_v is None:
                d.infos.add(f"{f}: sacct path {p!r} absent (skipped)")
                continue
            if isinstance(sa_v, dict) and ("set" in sa_v or "string" in sa_v):
                d.eq_wrapped(f, jid, fa.get(f), sa_v)
            elif f == "time_elapsed" and active and fa.get(f) != sa_v:
                # elapsed is computed at query time for not-yet-finished jobs;
                # the two dumps are seconds/minutes apart
                d.checked[f] = d.checked.get(f, 0) + 1
                d.infos.add(f"time_elapsed: live value drifts for active job "
                            f"{jid} (fa={fa.get(f)} sacct={sa_v})")
            else:
                # sacct normalizes some unset sentinels to 0/''
                d.eq_sentinel(f, jid, fa.get(f), sa_v)

        for f, (p, opts) in WRAPPED.items():
            sa_v = dig(s, p)
            if sa_v is None:
                d.infos.add(f"{f}: sacct path {p!r} absent (skipped)")
                continue
            d.eq_wrapped(f, jid, fa.get(f), sa_v, **opts)

        # --- lists (same table + order both sides)
        sa_state = dig(s, "state")
        if isinstance(sa_state, dict):
            d.eq("state", jid, fa.get("state"), sa_state.get("current"))
            reason = sa_state.get("reason")
            fa_r = fa.get("state_reason_prev")
            if reason is not None:
                d.checked["state_reason_prev"] = d.checked.get("state_reason_prev", 0) + 1
                if REASON_NUM is not None and reason in REASON_NUM:
                    if fa_r != REASON_NUM[reason]:
                        d.mismatches.append(("state_reason_prev", jid, fa_r, reason))
                elif reason == "None":
                    if fa_r not in (0, None):
                        d.mismatches.append(("state_reason_prev", jid, fa_r, reason))
                else:
                    d.infos.add(f"state_reason_prev: no table, kept pair fa={fa_r} sacct={reason!r}")
        d.eq("flags", jid, fa.get("flags"), (dig(s, "flags") or []))

        # --- names (NULL char* -> null in fastsacct, '' in sacct)
        d.eq_sentinel("qos", jid, fa.get("qos"), dig(s, "qos"))
        d.eq_sentinel("group", jid, fa.get("group"), dig(s, "group"))
        d.eq_sentinel("wckey", jid, fa.get("wckey"), (unwrap(dig(s, "wckey.wckey"))[1]))

        # --- time_timelimit: sacct wraps; fastsacct maps INFINITE -> 0
        # and NO_VAL (sacct's "Partition_Limit") -> null
        lim = dig(s, "time.limit")
        if lim is not None:
            d.eq_wrapped("time_timelimit", jid, fa.get("time_timelimit"), lim,
                         null_no_val_inf_zero=True)

        # --- req_mem: MEM_PER_CPU bit-63 tag vs sacct's split fields
        fa_mem = fa.get("req_mem")
        mpc = dig(s, "required.memory_per_cpu")
        mpn = dig(s, "required.memory_per_node")
        d.checked["req_mem"] = d.checked.get("req_mem", 0) + 1
        ok = False
        c_pc, n_pc = unwrap(mpc) if mpc else ("unset", None)
        c_pn, n_pn = unwrap(mpn) if mpn else ("unset", None)
        if c_pc == "v" and n_pc:
            ok = fa_mem == (MEM_PER_CPU | n_pc)
        elif c_pn == "v" and n_pn:
            ok = fa_mem == n_pn
        else:  # unset -> raw sentinel
            ok = fa_mem in (NO_VAL, INFINITE, 0)
        if not ok:
            d.mismatches.append(("req_mem", jid, fa_mem, (mpc, mpn)))

        # --- TRES strings + expansion
        for fa_str, fa_prefix, sa_key in (
            ("tres_alloc_str", "allocated", "tres.allocated"),
            ("tres_req_str", "requested", "tres.requested"),
        ):
            entries = dig(s, sa_key)
            if entries is None:
                d.infos.add(f"{fa_str}: sacct path {sa_key!r} absent (skipped)")
                continue
            d.eq(fa_str, jid, fa.get(fa_str) or "", rebuild_tres_str(entries))
            sa_exp = expand_tres(entries, fa_prefix)
            fa_exp = {k: v for k, v in fa.items() if k.startswith(fa_prefix + "_")}
            d.eq(f"{fa_prefix} TRES expansion", jid, fa_exp, sa_exp)

        # --- exit codes (decoded pair validates the raw field transitively)
        compare_exit_code(d, fa, s, jid)

        # --- derived exit code decoded form: informational only. sacct
        # drops the 0x80 core-dumped bit from the raw wait status.
        dec = dig(s, "derived_exit_code")
        if isinstance(dec, dict):
            kind, rc = unwrap(dec.get("return_code", {}))
            skind, sig = unwrap(dec.get("signal", {}).get("id", {}))
            base = (rc or 0) << 8 if kind == "v" else 0
            if skind == "v" and (sig or 0) > 0:
                base |= sig
            if kind == "v" or (skind == "v" and (sig or 0) > 0):
                allowed = {base, base | 0x80, NO_VAL, INFINITE}
                if fa.get("derived_ec") not in allowed:
                    d.infos.add(f"derived_ec: fa={fa.get('derived_ec')} "
                                f"sacct-decoded={base} (manual check)")

    return d


def main(argv):
    global VERBOSE
    VERBOSE = "--verbose" in argv
    dump_keys = "--dump-keys" in argv
    argv = [a for a in argv if a not in ("--verbose", "--dump-keys")]
    if len(argv) != 2:
        print(__doc__)
        return 2
    fa = json.load(open(argv[0]))
    sa = json.load(open(argv[1]))

    sa_jobs = sa.get("jobs") or []
    if dump_keys:
        if sa_jobs:
            print("sacct job keys:", sorted(sa_jobs[0].keys()))
        return 0

    fa_jobs = fa["jobs"]
    d = compare_all(fa_jobs, sa_jobs)

    if VERBOSE or d.infos:
        for i in sorted(d.infos):
            print("info:", i)

    by_field = {}
    for field, jid, f_v, s_v in d.mismatches:
        by_field.setdefault(field, []).append((jid, f_v, s_v))
    for field, items in sorted(by_field.items()):
        print(f"MISMATCH {field}: {len(items)} job(s), e.g. job {items[0][0]}: fastsacct={items[0][1]!r} sacct={items[0][2]!r}")
        if VERBOSE:
            for jid, f_v, s_v in items[:10]:
                print(f"    job {jid}: fastsacct={f_v!r} sacct={s_v!r}")

    total_checks = sum(d.checked.values())
    print(f"checked {total_checks} field comparisons over {len(fa_jobs)} jobs; "
          f"skipped fields (no sacct counterpart): {', '.join(sorted(SKIPPED))}")
    if d.mismatches:
        print(f"FAIL: {len(d.mismatches)} mismatches across {len(by_field)} field(s)")
        return 1
    print(f"OK: {len(fa_jobs)} jobs match sacct --json on every comparable field")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
