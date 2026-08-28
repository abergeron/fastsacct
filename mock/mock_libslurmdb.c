/*
 * Mock libslurmdb for local testing of fastsacct.py without a real Slurm
 * install. This single source file is compiled once per
 * mock/include/<version>/slurm/{slurm.h,slurmdb.h} vendored from that
 * release's real headers (see tests/conftest.py) -- each build shares the
 * exact same struct layout fastsacct.py's cffi cdef for that release is
 * meant to describe. This exercises both directions of the ABI:
 *
 *   - WRITE:  fastsacct.py fills in slurmdb_job_cond_t -> we print what we
 *             received, to confirm the Python side wrote it at the right
 *             offsets.
 *   - READ:   we fabricate slurmdb_job_rec_t entries -> fastsacct.py reads
 *             them back, to confirm the Python side reads from the right
 *             offsets.
 *
 * It does NOT prove the vendored struct in abi/vXX_YY.py matches whatever
 * real libslurmdb.so is on a given cluster node -- only that the fastsacct
 * harness itself (arg parsing, list walking, JSON emission) is correct,
 * against a struct layout that (thanks to the per-version headers) really
 * is that release's.
 *
 * Also provides slurmdb_tres_get()/slurmdb_qos_get() (real id/name tables,
 * matching the ids baked into make_job()'s tres_alloc_str/tres_req_str/
 * qosid) so the flat schema's id-resolution path has something real to
 * resolve against. slurmdb_associations_get()/slurmdb_destroy_assoc_rec()
 * are unused stubs that fastsacct.py never calls -- kept only so this file
 * still builds standalone.
 */
#include <slurm/slurm.h>
#include <slurm/slurmdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Opaque in the public headers (`typedef struct xlist list_t;` with no
 * body) -- we're free to pick our own internal representation. */
struct xlist {
    void **items;
    int count;
    int cap;
    ListDelF delf;
};

struct listIterator {
    struct xlist *list;
    int pos;
};

void slurm_init(const char *conf)
{
    (void) conf;
}

slurm_conf_t slurm_conf = {
    .accounting_storage_type = "accounting_storage/mock",
    .accounting_storage_host = "mockdbd.example.com",
    .accounting_storage_port = 6819,
};

/* Real value is `(API_MAJOR << 16) | (API_AGE << 8) | API_REVISION`, packed
 * per auxdir/slurm.m4. Pass -DMOCK_API_MAJOR=NN at build time to fake a
 * given release for testing fastsacct's auto-detection. */
#ifndef MOCK_API_MAJOR
#define MOCK_API_MAJOR 43
#endif

long slurm_api_version(void)
{
    return (long) (MOCK_API_MAJOR << 16);
}

void *slurmdb_connection_get(uint16_t *persist_conn_flags)
{
    if (persist_conn_flags)
        *persist_conn_flags = 0;
    static int dummy;
    return &dummy;
}

int slurmdb_connection_close(void **db_conn)
{
    if (db_conn)
        *db_conn = NULL;
    return 0;
}

list_t *slurm_list_create(ListDelF f)
{
    struct xlist *l = calloc(1, sizeof(*l));
    l->cap = 4;
    l->items = calloc(l->cap, sizeof(void *));
    l->delf = f;
    return (list_t *) l;
}

void slurm_list_append(list_t *lp, void *x)
{
    struct xlist *l = (struct xlist *) lp;
    if (l->count == l->cap) {
        l->cap *= 2;
        l->items = realloc(l->items, l->cap * sizeof(void *));
    }
    l->items[l->count++] = x;
}

int slurm_list_count(list_t *lp)
{
    return ((struct xlist *) lp)->count;
}

list_itr_t *slurm_list_iterator_create(list_t *lp)
{
    struct listIterator *it = calloc(1, sizeof(*it));
    it->list = (struct xlist *) lp;
    it->pos = 0;
    return (list_itr_t *) it;
}

void *slurm_list_next(list_itr_t *ip)
{
    struct listIterator *it = (struct listIterator *) ip;
    if (it->pos >= it->list->count)
        return NULL;
    return it->list->items[it->pos++];
}

void slurm_list_iterator_destroy(list_itr_t *ip)
{
    free(ip);
}

void slurm_list_destroy(list_t *lp)
{
    struct xlist *l = (struct xlist *) lp;
    if (!l)
        return;
    if (l->delf)
        for (int i = 0; i < l->count; i++)
            l->delf(l->items[i]);
    free(l->items);
    free(l);
}

char *slurm_strerror(int errnum)
{
    static char buf[256];
    snprintf(buf, sizeof(buf), "mock error %d", errnum);
    return buf;
}

static void dump_received_job_cond(slurmdb_job_cond_t *job_cond)
{
    fprintf(stderr, "[mock] received job_cond:\n");
    fprintf(stderr, "[mock]   flags=0x%x (DUP=%d NO_STEP=%d NO_TRUNC=%d)\n",
            job_cond->flags,
            !!(job_cond->flags & 0x1),
            !!(job_cond->flags & 0x2),
            !!(job_cond->flags & 0x4));
    fprintf(stderr, "[mock]   usage_start=%ld usage_end=%ld\n",
            (long) job_cond->usage_start, (long) job_cond->usage_end);
    fprintf(stderr, "[mock]   db_flags=0x%x (NOTSET=%d)\n",
            job_cond->db_flags,
            job_cond->db_flags == SLURMDB_JOB_FLAG_NOTSET);
    if (job_cond->db_flags != SLURMDB_JOB_FLAG_NOTSET)
        fprintf(stderr,
                "[mock]   WARNING: db_flags != NOTSET -- real "
                "accounting_storage/mysql would filter on t1.flags here "
                "and likely match nothing (see as_mysql_jobacct_process.c)\n");
    if (job_cond->acct_list) {
        fprintf(stderr, "[mock]   acct_list (%d):",
                slurm_list_count(job_cond->acct_list));
        list_itr_t *itr = slurm_list_iterator_create(job_cond->acct_list);
        char *acct;
        while ((acct = slurm_list_next(itr)))
            fprintf(stderr, " %s", acct);
        slurm_list_iterator_destroy(itr);
        fprintf(stderr, "\n");
    } else {
        fprintf(stderr, "[mock]   acct_list: NULL\n");
    }
}

static slurmdb_job_rec_t *make_job(uint32_t jobid, const char *account,
                                    const char *jobname, time_t start,
                                    time_t end, uint32_t timelimit)
{
    slurmdb_job_rec_t *j = calloc(1, sizeof(*j));
    j->jobid = jobid;
    j->account = strdup(account);
    j->jobname = strdup(jobname);
    j->cluster = strdup("mila");
    j->partition = strdup("main");
    j->nodes = strdup("node[01-02]");
    j->user = strdup("someuser");
    j->uid = 1000;
    j->gid = 1000;
    j->state = 3; /* JOB_COMPLETE, illustrative only */
    j->start = start;
    j->end = end;
    j->submit = start - 60;
    j->eligible = start - 60;
    j->elapsed = (uint32_t) (end - start);
    j->req_cpus = 4;
    j->alloc_nodes = 2;
    /* real wire format is always "<tres_id>=<count>,..." (numeric ids,
     * never names) -- see fastsacct.py's parse_tres() for why. 1=cpu,
     * 2=mem (bytes), 4=node, matching the fake slurmdb_tres_get() table
     * below. */
    j->tres_alloc_str = strdup("1=4,2=17179869184,4=2");
    j->tres_req_str = strdup("1=4,2=17179869184,4=2");
    /* MEM_PER_CPU (bit 63) tagged: 4096 MB per cpu. fastsacct's flat
     * output doesn't split this into per-cpu/per-node fields -- req_mem
     * is emitted raw -- but the bit is still set here to match a real
     * job record's shape. */
    j->req_mem = 0x8000000000000000ULL | 4096;
    j->exitcode = 0;
    j->priority = 100;
    j->qosid = 1;
    j->timelimit = timelimit;
    j->wckeyid = 0;
    j->wckey = strdup("*default");
    j->tot_cpu_sec = 120;
    j->user_cpu_sec = 100;
    j->sys_cpu_sec = 20;
    j->lineage = strdup("/some/lineage/path");
#if MOCK_API_MAJOR == 42
    /* 24.11-only field, removed in 25.05 (superseded by lineage above) --
     * see abi/v24_11.py's docstring. Only compiles/exists in this branch
     * because mock/include/24.11/slurm/slurmdb.h is the real 24.11 header,
     * which still declares it. */
    j->lft = 7;
#else
    /* 25.05+-only fields, didn't exist in 24.11 -- see abi/v24_11.py's
     * docstring. Only compile/exist in this branch because
     * mock/include/{25.05,25.11,26.05}/slurm/slurmdb.h are the real
     * 25.05/25.11/26.05 headers, which declare them. */
    j->resv_req = strdup("normal");
    j->segment_size = 8;
#endif
#if MOCK_API_MAJOR >= 45
    /* 26.05+-only fields -- see abi/v26_05.py's docstring. Only
     * compile/exist in this branch because mock/include/26.05/slurm/
     * slurmdb.h is the real 26.05 header, which declares them. */
    j->exclusive = strdup("NO");
    j->oversubscribe = strdup("NO");
    j->sluid = 0x123456789ULL;
#endif
    /* Last field in the struct in every release -- set explicitly so a
     * wrong field offset anywhere upstream of it (e.g. from building
     * against the wrong abi/vXX_YY.py CDEF) shows up here as garbage
     * instead of going unnoticed. */
    j->work_dir = strdup("/home/someuser");
    return j;
}

list_t *slurmdb_jobs_get(void *db_conn, slurmdb_job_cond_t *job_cond)
{
    (void) db_conn;
    dump_received_job_cond(job_cond);

    list_t *result = slurm_list_create(slurmdb_destroy_job_rec);
    time_t now = job_cond->usage_end ? job_cond->usage_end : 1735689600;

    slurm_list_append(result, make_job(1001, "mila-account", "train_job",
                                        now - 3600, now - 1800, 60));
    slurm_list_append(result, make_job(1002, "mila-account", "eval_job",
                                        now - 1800, now - 900, 60));
    /* No time limit -- exercises fastsacct.py's INFINITE(0xFFFFFFFF)->0
     * mapping for time_timelimit (see tests/conftest.py). */
    slurm_list_append(result, make_job(1003, "other-account", "sweep_job",
                                        now - 7200, now - 6900, INFINITE));
    /* Deliberately nasty job name -- tab, newline, CR, braces, brackets,
     * quotes, backslash -- exercises fastsacct.py's --jsonl framing
     * guarantee (json.dumps escapes every one of these, so a job name
     * like this can never break the one-JSON-value-per-line format; see
     * tests/conftest.py and the "weird chars" case in test_flat_output.py). */
    slurm_list_append(result, make_job(1004, "mila-account",
                                        "weird\tname\nwith\r{braces}[brackets]\"quotes\"\\backslash",
                                        now - 100, now - 50, 60));

    return result;
}

void slurmdb_destroy_job_rec(void *object)
{
    slurmdb_job_rec_t *j = object;
    if (!j)
        return;
    free(j->account);
    free(j->jobname);
    free(j->cluster);
    free(j->partition);
    free(j->nodes);
    free(j->user);
    free(j->tres_alloc_str);
    free(j->tres_req_str);
    free(j->wckey);
    free(j);
}

void slurmdb_destroy_job_cond_members(slurmdb_job_cond_t *job_cond)
{
    if (job_cond->acct_list)
        slurm_list_destroy(job_cond->acct_list);
    job_cond->acct_list = NULL;
}

/* id->name lookups for the flat schema's allocated_/requested_ and qos
 * fields. ids/names below match what make_job() bakes into
 * tres_alloc_str/tres_req_str and qosid above. */
static slurmdb_tres_rec_t *make_tres(uint32_t id, const char *type,
                                      const char *name)
{
    slurmdb_tres_rec_t *t = calloc(1, sizeof(*t));
    t->id = id;
    t->type = strdup(type);
    t->name = strdup(name ? name : "");
    return t;
}

list_t *slurmdb_tres_get(void *db_conn, slurmdb_tres_cond_t *tres_cond)
{
    (void) db_conn;
    fprintf(stderr, "[mock] slurmdb_tres_get(with_deleted=%d)\n",
            tres_cond->with_deleted);
    list_t *result = slurm_list_create(slurmdb_destroy_tres_rec);
    slurm_list_append(result, make_tres(1, "cpu", NULL));
    slurm_list_append(result, make_tres(2, "mem", NULL));
    slurm_list_append(result, make_tres(4, "node", NULL));
    return result;
}

void slurmdb_destroy_tres_rec(void *object)
{
    slurmdb_tres_rec_t *t = object;
    if (!t)
        return;
    free(t->type);
    free(t->name);
    free(t);
}

static slurmdb_qos_rec_t *make_qos(uint32_t id, const char *name)
{
    slurmdb_qos_rec_t *q = calloc(1, sizeof(*q));
    q->id = id;
    q->name = strdup(name);
    return q;
}

list_t *slurmdb_qos_get(void *db_conn, slurmdb_qos_cond_t *qos_cond)
{
    (void) db_conn;
    fprintf(stderr, "[mock] slurmdb_qos_get(flags=0x%x)\n", qos_cond->flags);
    list_t *result = slurm_list_create(slurmdb_destroy_qos_rec);
    slurm_list_append(result, make_qos(1, "normal"));
    return result;
}

void slurmdb_destroy_qos_rec(void *object)
{
    slurmdb_qos_rec_t *q = object;
    if (!q)
        return;
    free(q->name);
    free(q);
}

/* Unused stub (see the file header comment) -- returns an empty list so
 * linking succeeds without touching any slurmdb_assoc_rec_t field, whose
 * layout differs across the vendored slurm/slurmdb.h headers this file
 * gets compiled against. */
list_t *slurmdb_associations_get(void *db_conn, slurmdb_assoc_cond_t *assoc_cond)
{
    (void) db_conn;
    (void) assoc_cond;
    fprintf(stderr, "[mock] slurmdb_associations_get() -- stub, returns empty\n");
    return slurm_list_create(NULL);
}

void slurmdb_destroy_assoc_rec(void *object)
{
    (void) object;
}
