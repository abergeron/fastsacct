//! libslurmdb binding: dlopen/dlsym plumbing, connection lifecycle, the
//! one-time TRES/QOS id->name fetches, job_cond construction, and the
//! C-list -> JSON decode (the Rust port of the old `Slurmdb` class).

use crate::abi::{Abi, FieldKind};
use crate::decode::{self, QosInfo, TresInfo};
use crate::timeparse::format_utc_iso;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int, c_void};

/// Error mirroring the old Python `SlurmdbError`: a message to surface
/// verbatim on stderr, exit code 1.
#[derive(Debug)]
pub struct SlurmdbError(pub String);

impl std::fmt::Display for SlurmdbError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for SlurmdbError {}

impl From<String> for SlurmdbError {
    fn from(msg: String) -> Self {
        SlurmdbError(msg)
    }
}

/// `typedef struct xlist list_t;` -- opaque in the public headers.
#[repr(C)]
pub struct ListT {
    _opaque: [u8; 0],
}

/// `typedef struct listIterator list_itr_t;` -- opaque.
#[repr(C)]
pub struct ListItr {
    _opaque: [u8; 0],
}

/// `typedef void (*ListDelF)(void *x);`
type ListDelF = Option<unsafe extern "C" fn(*mut c_void)>;

// ---------------------------------------------------------------------------
// Struct mirrors of slurm/slurmdb.h pieces other than slurmdb_job_rec_t
// (which lives per-release in crate::abi). Like the abi modules, these MUST
// mirror the real headers field-for-field in order: the cond structs are
// ffi-allocated by us and passed TO the library, the rec structs we read
// FROM the library. Confirmed against slurm/slurmdb.h (25.05);
// version-independent for the fields used here -- EXCEPT
// slurmdb_tres_rec_t, which gained `char modifier;` between `id` and
// `name` in 26.05 (see Abi::tres_rec_has_modifier and ARCHITECTURE.md
// gotcha #6: on that ABI the insertion happens to land inside existing
// padding so offsets don't move, but that's a coincidence of this specific
// insertion point, not something to rely on -- the modifier variant is
// declared and selected anyway).
// ---------------------------------------------------------------------------

/// mirrors slurmdb_job_cond_t -- INPUT struct. Identical across every
/// release we support (22.05 -> 26.05, verified by header diff).
#[repr(C)]
#[derive(Default)]
pub struct JobCond {
    pub acct_list: *mut ListT,
    pub associd_list: *mut ListT,
    pub cluster_list: *mut ListT,
    pub constraint_list: *mut ListT,
    pub cpus_max: u32,
    pub cpus_min: u32,
    pub db_flags: u32,
    pub exitcode: i32,
    pub flags: u32,
    pub format_list: *mut ListT,
    pub groupid_list: *mut ListT,
    pub jobname_list: *mut ListT,
    pub nodes_max: u32,
    pub nodes_min: u32,
    pub partition_list: *mut ListT,
    pub qos_list: *mut ListT,
    pub reason_list: *mut ListT,
    pub resv_list: *mut ListT,
    pub resvid_list: *mut ListT,
    pub state_list: *mut ListT,
    pub step_list: *mut ListT,
    pub timelimit_max: u32,
    pub timelimit_min: u32,
    pub usage_end: i64,
    pub usage_start: i64,
    pub used_nodes: *mut c_char,
    pub userid_list: *mut ListT,
    pub wckey_list: *mut ListT,
}

/// Diagnostic-only: the `slurm_conf` global is the same struct
/// auth/accounting plugins reference (see the RTLD_GLOBAL note below) --
/// reading its first few fields after slurm_init() tells us which
/// accounting storage host/port/type our process actually resolved, so we
/// can compare against what `sacct` connects to on the same box. Only the
/// prefix up to accounting_storage_type is declared (the "declare fields in
/// order up to the last one we care about" rule) -- not tied to a specific
/// Slurm release's ABI since this prefix has been stable 24.11..26.05.
#[repr(C)]
pub struct SlurmConfPrefix {
    pub last_update: i64,
    pub accounting_storage_tres: *mut c_char,
    pub accounting_storage_enforce: u16,
    pub accounting_storage_backup_host: *mut c_char,
    pub accounting_storage_ext_host: *mut c_char,
    pub accounting_storage_host: *mut c_char,
    pub accounting_storage_params: *mut c_char,
    pub accounting_storage_pass: *mut c_char,
    pub accounting_storage_port: u16,
    pub accounting_storage_type: *mut c_char,
}

/// mirrors slurmdb_tres_rec_t (pre-26.05).
#[repr(C)]
struct TresRec {
    alloc_secs: u64,
    rec_count: u32,
    count: u64,
    id: u32,
    name: *mut c_char,
    tres_type: *mut c_char,
}

/// mirrors slurmdb_tres_rec_t with the 26.05 `char modifier;`.
#[repr(C)]
struct TresRecWithModifier {
    alloc_secs: u64,
    rec_count: u32,
    count: u64,
    id: u32,
    modifier: i8,
    name: *mut c_char,
    tres_type: *mut c_char,
}

/// mirrors slurmdb_tres_cond_t.
#[repr(C)]
#[derive(Default)]
pub(crate) struct TresCond {
    count: u64,
    format_list: *mut ListT,
    id_list: *mut ListT,
    name_list: *mut ListT,
    type_list: *mut ListT,
    with_deleted: u16,
}

/// mirrors slurmdb_qos_rec_t -- declared in full (not just the
/// id/name prefix we read) so this is an honest transcription of the real
/// struct, not "the fields that happen to matter today".
#[repr(C)]
struct QosRec {
    blocked_until: i64,
    description: *mut c_char,
    id: u32,
    flags: i32,
    grace_time: u32,
    grp_jobs_accrue: u32,
    grp_jobs: u32,
    grp_submit_jobs: u32,
    grp_tres: *mut c_char,
    grp_tres_ctld: *mut u64,
    grp_tres_mins: *mut c_char,
    grp_tres_mins_ctld: *mut u64,
    grp_tres_run_mins: *mut c_char,
    grp_tres_run_mins_ctld: *mut u64,
    grp_wall: u32,
    limit_factor: f64,
    max_jobs_pa: u32,
    max_jobs_pu: u32,
    max_jobs_accrue_pa: u32,
    max_jobs_accrue_pu: u32,
    max_submit_jobs_pa: u32,
    max_submit_jobs_pu: u32,
    max_tres_mins_pj: *mut c_char,
    max_tres_mins_pj_ctld: *mut u64,
    max_tres_pa: *mut c_char,
    max_tres_pa_ctld: *mut u64,
    max_tres_pj: *mut c_char,
    max_tres_pj_ctld: *mut u64,
    max_tres_pn: *mut c_char,
    max_tres_pn_ctld: *mut u64,
    max_tres_pu: *mut c_char,
    max_tres_pu_ctld: *mut u64,
    max_tres_run_mins_pa: *mut c_char,
    max_tres_run_mins_pa_ctld: *mut u64,
    max_tres_run_mins_pu: *mut c_char,
    max_tres_run_mins_pu_ctld: *mut u64,
    max_wall_pj: u32,
    min_prio_thresh: u32,
    min_tres_pj: *mut c_char,
    min_tres_pj_ctld: *mut u64,
    name: *mut c_char,
    preempt_bitstr: *mut c_void,
    preempt_list: *mut ListT,
    preempt_mode: u16,
    preempt_exempt_time: u32,
    priority: u32,
    relative_tres_cnt: *mut u64,
    usage: *mut c_void,
    usage_factor: f64,
    usage_thres: f64,
}

/// mirrors slurmdb_qos_cond_t.
#[repr(C)]
#[derive(Default)]
pub(crate) struct QosCond {
    description_list: *mut ListT,
    flags: u16,
    id_list: *mut ListT,
    format_list: *mut ListT,
    name_list: *mut ListT,
    preempt_mode: u16,
}

// ---------------------------------------------------------------------------
// dlopen()/dlsym() plumbing
// ---------------------------------------------------------------------------

fn dlerror_string() -> String {
    // SAFETY: dlerror() returns a thread-local C string or NULL.
    unsafe {
        let p = libc::dlerror();
        if p.is_null() {
            "unknown dl error".to_string()
        } else {
            CStr::from_ptr(p).to_string_lossy().into_owned()
        }
    }
}

/// dlsym()'s name argument. (Mach-O object files prefix C symbols with an
/// underscore -- visible in `nm` -- but the dl* C API takes the plain C
/// name and dyld does the prefixing itself; verified against the mock .so:
/// dlsym(h, "_slurm_api_version") fails while dlsym(h,
/// "slurm_api_version") resolves.)
fn sym_name(name: &str) -> CString {
    CString::new(name).expect("symbol name has no NUL")
}

/// Fetch one symbol and transmute it to a fn pointer type `T`.
fn sym<T: Copy>(handle: *mut c_void, name: &str) -> Result<T, SlurmdbError> {
    let cname = sym_name(name);
    // SAFETY: dlsym on a live handle; we check for NULL and size-check the
    // fn-pointer type before transmuting.
    let ptr = unsafe { libc::dlsym(handle, cname.as_ptr()) };
    if ptr.is_null() {
        return Err(SlurmdbError(format!(
            "undefined symbol: {name} ({})",
            dlerror_string()
        )));
    }
    assert_eq!(
        std::mem::size_of::<T>(),
        std::mem::size_of::<*mut c_void>(),
        "dlsym symbol fn-pointer size mismatch for {name}"
    );
    // SAFETY: a pointer read from dlsym, same size (asserted above).
    Ok(unsafe { std::mem::transmute_copy::<*mut c_void, T>(&ptr) })
}

/// Raw function pointers loaded out of libslurm.so/libslurmdb.so.
macro_rules! decl_syms {
    ($($field:ident: $name:literal, $ty:ty,)*) => {
        pub(crate) struct Lib {
            #[allow(dead_code, reason = "kept so we can dlclose one day; never closed today, mirroring cffi")]
            handle: *mut c_void,
            $(pub(crate) $field: $ty,)*
        }

        impl Lib {
            fn load(handle: *mut c_void) -> Result<Lib, SlurmdbError> {
                Ok(Lib {
                    handle,
                    $($field: sym(handle, $name)?,)*
                })
            }
        }
    };
}

type FnSlurmInit = unsafe extern "C" fn(conf: *const c_char);
type FnConnGet = unsafe extern "C" fn(persist_conn_flags: *mut u16) -> *mut c_void;
type FnConnClose = unsafe extern "C" fn(db_conn: *mut *mut c_void) -> c_int;
type FnListCreate = unsafe extern "C" fn(f: ListDelF) -> *mut ListT;
type FnListAppend = unsafe extern "C" fn(l: *mut ListT, x: *mut c_void);
type FnListCount = unsafe extern "C" fn(l: *mut ListT) -> c_int;
type FnIterCreate = unsafe extern "C" fn(l: *mut ListT) -> *mut ListItr;
type FnListNext = unsafe extern "C" fn(i: *mut ListItr) -> *mut c_void;
type FnIterDestroy = unsafe extern "C" fn(i: *mut ListItr);
type FnListDestroy = unsafe extern "C" fn(l: *mut ListT);
type FnStrerror = unsafe extern "C" fn(errnum: c_int) -> *mut c_char;
type FnJobsGet = unsafe extern "C" fn(db_conn: *mut c_void, job_cond: *mut JobCond) -> *mut ListT;
type FnDestroyJobCondMembers = unsafe extern "C" fn(job_cond: *mut JobCond);
type FnTresGet = unsafe extern "C" fn(db_conn: *mut c_void, cond: *mut TresCond) -> *mut ListT;
type FnQosGet = unsafe extern "C" fn(db_conn: *mut c_void, cond: *mut QosCond) -> *mut ListT;
type FnSlurmApiVersion = unsafe extern "C" fn() -> libc::c_long;

decl_syms! {
    slurm_init: "slurm_init", FnSlurmInit,
    slurmdb_connection_get: "slurmdb_connection_get", FnConnGet,
    slurmdb_connection_close: "slurmdb_connection_close", FnConnClose,
    slurm_list_create: "slurm_list_create", FnListCreate,
    slurm_list_append: "slurm_list_append", FnListAppend,
    slurm_list_count: "slurm_list_count", FnListCount,
    slurm_list_iterator_create: "slurm_list_iterator_create", FnIterCreate,
    slurm_list_next: "slurm_list_next", FnListNext,
    slurm_list_iterator_destroy: "slurm_list_iterator_destroy", FnIterDestroy,
    slurm_list_destroy: "slurm_list_destroy", FnListDestroy,
    slurm_strerror: "slurm_strerror", FnStrerror,
    slurmdb_jobs_get: "slurmdb_jobs_get", FnJobsGet,
    slurmdb_destroy_job_cond_members: "slurmdb_destroy_job_cond_members", FnDestroyJobCondMembers,
    slurmdb_tres_get: "slurmdb_tres_get", FnTresGet,
    slurmdb_qos_get: "slurmdb_qos_get", FnQosGet,
}

/// dlopen() wrapper. `global` selects RTLD_GLOBAL vs RTLD_LOCAL.
///
/// RTLD_GLOBAL matters: slurm_init()/slurmdb_connection_get() below make
/// Slurm's plugin loader dlopen() auth/accounting_storage plugins (e.g.
/// auth_munge.so), which reference globals like slurm_conf that they expect
/// to already be resolvable in the process's symbol table -- exactly how
/// they'd see it inside slurmd/sacct, which pull this library in via
/// DT_NEEDED (global scope by default). RTLD_LOCAL (the default, and what
/// the ABI-probe open uses) walls those symbols off and makes the plugin
/// load fail with "undefined symbol: slurm_conf".
fn dlopen(path: &str, global: bool) -> Result<*mut c_void, SlurmdbError> {
    let cpath = CString::new(path)
        .map_err(|_| SlurmdbError(format!("library path {path:?} contains a NUL byte")))?;
    let mode = libc::RTLD_NOW
        | if global {
            libc::RTLD_GLOBAL
        } else {
            libc::RTLD_LOCAL
        };
    // SAFETY: dlopen with a valid C string.
    let handle = unsafe { libc::dlopen(cpath.as_ptr(), mode) };
    if handle.is_null() {
        return Err(SlurmdbError(format!(
            "couldn't dlopen {path:?}: {}",
            dlerror_string()
        )));
    }
    Ok(handle)
}

/// Current process errno -- mirrors cffi's `ffi.errno` (the errno as of the
/// most recent FFI call). Call this immediately after the call you care
/// about; libc calls in between can clobber it.
fn errno_now() -> c_int {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

/// Probe a library's own `slurm_api_version()` and decode the API major it
/// was built with -- comes straight from the exact .so we're about to use,
/// no subprocess needed. Opens RTLD_LOCAL: this handle is only used for a
/// version number, never for connection/plugin work (see dlopen()).
pub fn probe_api_major(path: &str) -> Result<u32, String> {
    let handle = dlopen(path, false).map_err(|e| e.0)?;
    let f: FnSlurmApiVersion = sym(handle, "slurm_api_version").map_err(|e| e.0)?;
    // SAFETY: slurm_api_version() takes no args and just returns a packed
    // version long.
    let raw = unsafe { f() };
    Ok(((raw >> 16) & 0xFF) as u32)
}

// ---------------------------------------------------------------------------
// The binding proper
// ---------------------------------------------------------------------------

pub struct Slurmdb {
    abi: &'static Abi,
    debug: bool,
    lib: Lib,
    conn: *mut c_void,
    /// TRES id->name resolution feeds `tres_flat_fields`'
    /// allocated_/requested_ fields (one-time slurmdb_tres_get()).
    pub tres_by_id: HashMap<u64, TresInfo>,
    /// QOS id->name resolution feeds the "qos" field (one-time
    /// slurmdb_qos_get()).
    pub qos_by_id: HashMap<u32, QosInfo>,
}

fn cstr_to_lossy<'a>(ptr: *const c_char) -> Option<&'a CStr> {
    if ptr.is_null() {
        None
    } else {
        // SAFETY: non-NULL caller contract -- valid NUL-terminated string.
        Some(unsafe { CStr::from_ptr(ptr) })
    }
}

fn cstr_to_string(ptr: *const c_char) -> String {
    cstr_to_lossy(ptr)
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default()
}

impl Slurmdb {
    pub fn new(
        library_path: &str,
        abi: &'static Abi,
        debug: bool,
    ) -> Result<Slurmdb, SlurmdbError> {
        // The main open: RTLD_GLOBAL, see dlopen()'s doc comment.
        let handle = dlopen(library_path, true)?;
        let lib = Lib::load(handle)?;

        // SAFETY: slurm_init(NULL) = load config from the default locations.
        unsafe { (lib.slurm_init)(std::ptr::null()) };

        if debug {
            // Diagnostic-only, see SlurmConfPrefix's doc comment. The
            // symbol is dlsym'd lazily here and optional: real
            // libslurm.so always has it; a minimal test double might not.
            let conf = sym_name("slurm_conf");
            // SAFETY: dlsym on a live handle.
            let conf_ptr = unsafe { libc::dlsym(handle, conf.as_ptr()) };
            if conf_ptr.is_null() {
                eprintln!("fastsacct: debug: slurm_conf after slurm_init(): (symbol not found)");
            } else {
                // SAFETY: slurm_conf is a live global of the loaded lib; we
                // only read the declared prefix.
                let sc = unsafe { &*(conf_ptr as *const SlurmConfPrefix) };
                let s = |p: *mut c_char| match cstr_to_lossy(p) {
                    Some(v) => v.to_string_lossy().into_owned(),
                    None => "None".to_string(),
                };
                eprintln!(
                    "fastsacct: debug: slurm_conf after slurm_init(): \
                     accounting_storage_type={}, accounting_storage_host={}, \
                     accounting_storage_backup_host={}, \
                     accounting_storage_port={}",
                    s(sc.accounting_storage_type),
                    s(sc.accounting_storage_host),
                    s(sc.accounting_storage_backup_host),
                    sc.accounting_storage_port,
                );
            }
        }

        let mut persist_conn_flags: u16 = 0;
        // SAFETY: slurmdb_connection_get takes a uint16_t* out-param.
        let conn = unsafe { (lib.slurmdb_connection_get)(&mut persist_conn_flags) };
        let errno = errno_now();
        if conn.is_null() {
            return Err(Slurmdb::strerror_with(&lib, errno));
        }

        let mut db = Slurmdb {
            abi,
            debug,
            lib,
            conn,
            tres_by_id: HashMap::new(),
            qos_by_id: HashMap::new(),
        };
        // Two one-time RPCs: flat mode expands tres_alloc_str/tres_req_str
        // into allocated_*/requested_* fields and qosid into a qos name
        // field, which need these same id->name maps.
        let tres_by_id = db.fetch_tres();
        let qos_by_id = db.fetch_qos();
        db.tres_by_id = tres_by_id;
        db.qos_by_id = qos_by_id;
        db.debug_log(&format!(
            "fetched {} TRES, {} QOS for id resolution",
            db.tres_by_id.len(),
            db.qos_by_id.len()
        ));
        Ok(db)
    }

    fn debug_log(&self, msg: &str) {
        if self.debug {
            eprintln!("fastsacct: debug: {msg}");
        }
    }

    fn strerror_with(lib: &Lib, errno: c_int) -> SlurmdbError {
        // SAFETY: slurm_strerror returns a static C string or NULL.
        let msg = unsafe { (lib.slurm_strerror)(errno) };
        SlurmdbError(if msg.is_null() {
            "unknown error".to_string()
        } else {
            // SAFETY: non-NULL => valid C string.
            unsafe { CStr::from_ptr(msg) }
                .to_string_lossy()
                .into_owned()
        })
    }

    fn strerror(&self, errno: c_int) -> SlurmdbError {
        Self::strerror_with(&self.lib, errno)
    }

    /// Walk a slurm list_t*, run `f` per element, then destroy the list.
    /// Caller is responsible for the elements' lifetime matching the list's
    /// ListDelF -- the real library creates the lists returned by
    /// slurmdb_jobs_get()/slurmdb_tres_get()/slurmdb_qos_get() with the
    /// matching destroy_*_rec already wired, so list destroy frees them
    /// after `f` has copied everything it needs out (same assumption the
    /// Python _iter_list made).
    fn list_foreach_try<E>(
        &self,
        result: *mut ListT,
        mut f: impl FnMut(*mut c_void) -> Result<(), E>,
    ) -> Result<(), E> {
        if result.is_null() {
            return Ok(());
        }
        // SAFETY: opaque list iterators, used strictly per Slurm's own
        // list API contract (create/next/destroy, then destroy the list).
        unsafe {
            let itr = (self.lib.slurm_list_iterator_create)(result);
            let mut outcome = Ok(());
            loop {
                let ptr = (self.lib.slurm_list_next)(itr);
                if ptr.is_null() {
                    break;
                }
                if let Err(e) = f(ptr) {
                    outcome = Err(e);
                    break;
                }
            }
            (self.lib.slurm_list_iterator_destroy)(itr);
            (self.lib.slurm_list_destroy)(result);
            outcome
        }
    }

    /// id -> {type, name}, for TRES string resolution. Mirrors
    /// slurmdb_helpers.c's own `slurmdb_tres_cond_t cond = { .with_deleted
    /// = 1 }`.
    fn fetch_tres(&self) -> HashMap<u64, TresInfo> {
        let cond = TresCond {
            with_deleted: 1,
            ..Default::default()
        };
        let mut cond = cond;
        // SAFETY: opaque handle + a fully declared cond struct.
        let result = unsafe { (self.lib.slurmdb_tres_get)(self.conn, &mut cond) };
        let mut map = HashMap::new();
        let has_modifier = self.abi.tres_rec_has_modifier;
        // No NULL check: like the Python version, a NULL result (TRES
        // tracking off on this cluster) just yields an empty map -- the
        // job decode then leaves types/names empty, it's not a hard error.
        let _ = self.list_foreach_try(result, |ptr| {
            // SAFETY: ptr is a slurmdb_tres_rec_t*, layout selected per
            // the release flag (see TresRecWithModifier's doc comment).
            let (id, name, tres_type) = unsafe {
                if has_modifier {
                    let t = &*(ptr as *const TresRecWithModifier);
                    (t.id, t.name, t.tres_type)
                } else {
                    let t = &*(ptr as *const TresRec);
                    (t.id, t.name, t.tres_type)
                }
            };
            map.insert(
                u64::from(id),
                TresInfo {
                    r#type: cstr_to_string(tres_type),
                    name: cstr_to_string(name),
                },
            );
            Ok::<(), std::convert::Infallible>(())
        });
        map
    }

    /// id -> {id, name}, for the "qos" field's name resolution. Mirrors
    /// slurmdb_helpers.c's `slurmdb_qos_cond_t cond = { .flags =
    /// QOS_COND_FLAG_WITH_DELETED }` -- without it, a job whose QOS was
    /// later deleted would resolve to "Unknown" here but to its real name
    /// under real sacct, which always asks for deleted QOS too.
    fn fetch_qos(&self) -> HashMap<u32, QosInfo> {
        let mut cond = QosCond {
            flags: 1, // QOS_COND_FLAG_WITH_DELETED = SLURM_BIT(0)
            ..Default::default()
        };
        // SAFETY: opaque handle + fully declared cond struct.
        let result = unsafe { (self.lib.slurmdb_qos_get)(self.conn, &mut cond) };
        let mut map = HashMap::new();
        let _ = self.list_foreach_try(result, |ptr| {
            // SAFETY: ptr is a slurmdb_qos_rec_t* matching QosRec above.
            let (id, name) = unsafe {
                let q = &*(ptr as *const QosRec);
                (q.id, q.name)
            };
            map.insert(
                id,
                QosInfo {
                    id,
                    name: cstr_to_string(name),
                },
            );
            Ok::<(), std::convert::Infallible>(())
        });
        map
    }

    pub fn close(&mut self) {
        let mut conn = self.conn;
        self.conn = std::ptr::null_mut();
        // SAFETY: slurmdb_connection_close(NULLs out the in/out pointer).
        unsafe { (self.lib.slurmdb_connection_close)(&mut conn) };
    }

    /// Build the acct_list sacct would build -- the caller normalizes
    /// entries first (see main). The list is handed to the library via
    /// job_cond and freed by slurmdb_destroy_job_cond_members(); the
    /// strdup'd buffers use libc::free as the list's ListDelF so they go
    /// with it.
    fn str_list(&self, values: &[String]) -> Result<*mut ListT, SlurmdbError> {
        // SAFETY: slurm_list_create with libc::free as ListDelF; each
        // element is a strdup'd buffer.
        unsafe {
            let delf: ListDelF = Some(libc::free);
            let lst = (self.lib.slurm_list_create)(delf);
            for v in values {
                let buf = libc::strdup(CString::new(v.as_str()).unwrap_or_default().as_ptr());
                (self.lib.slurm_list_append)(lst, buf.cast());
            }
            Ok(lst)
        }
    }

    /// Fill + run a slurmdb_jobs_get() query.
    ///
    /// Returns the raw result list pointer (caller decodes it via
    /// `stream_jobs`, which also destroys it). slurm_list_count() is O(1)
    /// over the C list_t (a node counter), so the job count is known the
    /// instant the RPC returns -- no record needs to be decoded just to
    /// count them; a caller can put it in a --jsonl header before touching
    /// any job. slurmdb_jobs_get() itself is a single RPC -- the whole
    /// result list already exists in C memory by the time we get here, so
    /// there's no earlier point at which real streaming from the source
    /// could start. What we *can* stream is the C-list -> JSON decode.
    pub fn jobs_get(
        &self,
        accounts: &[String],
        start: i64,
        end: i64,
    ) -> Result<(i64, *mut ListT), SlurmdbError> {
        let mut job_cond = JobCond::default();
        // job_cond.userid_list is left NULL (Default zero-inits the struct),
        // which is exactly what --allusers means to slurmdb_jobs_get():
        // sacct itself only narrows to the caller's uid when userid_list is
        // empty AND -a wasn't passed (src/sacct/options.c ~1034-1042). We
        // require -a/--allusers at the CLI level rather than filter here,
        // so there's nothing else to do for it.
        if !accounts.is_empty() {
            job_cond.acct_list = self.str_list(accounts)?;
        }
        job_cond.usage_start = start;
        job_cond.usage_end = end;
        job_cond.flags = crate::abi::JOBCOND_FLAG_DUP
            | crate::abi::JOBCOND_FLAG_NO_STEP
            | crate::abi::JOBCOND_FLAG_NO_TRUNC;
        // job_cond->db_flags is a DIFFERENT sentinel from job_cond->flags
        // above: 0 is not a wildcard here, it's SLURMDB_JOB_FLAG_NONE, which
        // the accounting_storage/mysql query builder turns into a literal
        // `t1.flags = 0` filter -- matching almost no real job, since e.g.
        // SLURMDB_JOB_FLAG_START_R gets set the moment a job's start RPC
        // lands (as_mysql_job.c). sacct always sets this to NOTSET in
        // _init_params() (src/sacct/options.c) so that clause is skipped
        // entirely; leaving it unset here is what (in the original Python
        // version) caused every query to silently return zero jobs
        // regardless of window/account/user.
        job_cond.db_flags = crate::abi::JOBCOND_DB_FLAG_NOTSET;

        self.debug_log(&format!(
            "job_cond before slurmdb_jobs_get(): \
             usage_start={} ({} UTC), usage_end={} ({} UTC), \
             flags=0x{:x}, db_flags=0x{:x}, \
             acct_list={}, userid_list=NULL, cluster_list=NULL",
            job_cond.usage_start,
            format_utc_iso(job_cond.usage_start),
            job_cond.usage_end,
            format_utc_iso(job_cond.usage_end),
            job_cond.flags,
            job_cond.db_flags,
            if accounts.is_empty() {
                "NULL".to_string()
            } else {
                format!("set ({} entries)", accounts.len())
            },
        ));

        // SAFETY: job_cond is a fully mirrored struct; acct_list (if set)
        // was built by the library's own slurm_list_create.
        let result = unsafe { (self.lib.slurmdb_jobs_get)(self.conn, &mut job_cond) };
        let errno = errno_now();

        self.debug_log(&format!(
            "slurmdb_jobs_get() returned {} (errno={errno})",
            if result.is_null() {
                "NULL".to_string()
            } else {
                // SAFETY: result is a live list.
                unsafe { (self.lib.slurm_list_count)(result) }.to_string()
            },
        ));

        // SAFETY: slurmdb_destroy_job_cond_members frees the cond's owned
        // members (acct_list and its now-freed strdup'd strings).
        unsafe { (self.lib.slurmdb_destroy_job_cond_members)(&mut job_cond) };

        if result.is_null() {
            return Err(self.strerror(errno));
        }

        // O(1) count, see the method doc comment.
        // SAFETY: result is a live list.
        let count = unsafe { (self.lib.slurm_list_count)(result) };
        Ok((count.max(0) as i64, result))
    }

    /// Decode + emit each job record as a JSON value, streaming: `emit`
    /// sees every job as soon as it's decoded, without a full
    /// intermediate collection. Destroys the C list when done (including on
    /// emit error, after processing stops).
    pub fn stream_jobs<E>(
        &self,
        list: *mut ListT,
        mut emit: impl FnMut(&Value) -> Result<(), E>,
    ) -> Result<(), E> {
        self.list_foreach_try(list, |ptr| {
            let job = self.job_to_value(ptr.cast_const());
            emit(&job)
        })
    }

    /// Decode every job record into one big Vec (the non-streaming
    /// default-mode path).
    pub fn collect_jobs(&self, list: *mut ListT) -> Vec<Value> {
        let mut out = Vec::new();
        let _infallible: Result<(), std::convert::Infallible> =
            self.list_foreach_try(list, |ptr| {
                out.push(self.job_to_value(ptr.cast_const()));
                Ok(())
            });
        out
    }

    /// Turn one slurmdb_job_rec_t into the flat JSON object -- the port of
    /// the old `_job_to_dict`, driven by this release's field table.
    fn job_to_value(&self, job: *const c_void) -> Value {
        let base = job.cast::<u8>();
        let mut out = Map::new();
        for d in self.abi.job_fields {
            // SAFETY: every offset in the table came from offset_of! on a
            // struct mirror of the real release's layout, and job points at
            // a real slurmdb_job_rec_t of that release. read_unaligned: the
            // compiler already guarantees alignment, this is just the
            // always-valid form through a byte-cast pointer.
            let val = unsafe {
                match d.kind {
                    FieldKind::Str => {
                        let p = (base.add(d.offset) as *const *const c_char).read_unaligned();
                        match cstr_to_lossy(p) {
                            None => Value::Null,
                            Some(s) => Value::from(s.to_string_lossy().into_owned()),
                        }
                    }
                    FieldKind::U16 => Value::from(u64::from(
                        (base.add(d.offset) as *const u16).read_unaligned(),
                    )),
                    FieldKind::U32 => Value::from(u64::from(
                        (base.add(d.offset) as *const u32).read_unaligned(),
                    )),
                    FieldKind::U64 => {
                        Value::from((base.add(d.offset) as *const u64).read_unaligned())
                    }
                    FieldKind::Time => {
                        Value::from((base.add(d.offset) as *const i64).read_unaligned())
                    }
                    FieldKind::Group => {
                        // A local NSS lookup (getgrgid), not an RPC -- cheap
                        // enough to resolve even though every other flat
                        // field is left raw.
                        let gid = (base.add(d.offset) as *const u32).read_unaligned();
                        Value::from(decode::group_name(gid))
                    }
                    FieldKind::Flags => {
                        // PARSER_FLAG_ARRAY(SLURMDB_JOB_FLAGS) decoding --
                        // pure bit twiddling against a fixed table, no RPC.
                        let raw = (base.add(d.offset) as *const u32).read_unaligned();
                        Value::from(
                            decode::job_flags(raw)
                                .into_iter()
                                .map(Value::from)
                                .collect::<Vec<_>>(),
                        )
                    }
                    FieldKind::JobState => {
                        // Base state name + any flag bits -- pure bit
                        // twiddling against a fixed table, no RPC.
                        let raw = (base.add(d.offset) as *const u32).read_unaligned();
                        Value::from(
                            decode::job_state(raw)
                                .into_iter()
                                .map(Value::from)
                                .collect::<Vec<_>>(),
                        )
                    }
                    FieldKind::QosName => {
                        // HashMap lookup against the one-time
                        // slurmdb_qos_get() fetch, no RPC per job --
                        // replaces the raw qosid rather than sitting
                        // alongside it.
                        let raw = (base.add(d.offset) as *const u32).read_unaligned();
                        Value::from(decode::qos_name(raw, &self.qos_by_id))
                    }
                    FieldKind::NoVal32 => {
                        // NO_VAL/INFINITE sentinel collapsed to a plain null.
                        let ival = (base.add(d.offset) as *const u32).read_unaligned();
                        if ival == decode::NO_VAL || ival == decode::INFINITE {
                            Value::Null
                        } else {
                            Value::from(u64::from(ival))
                        }
                    }
                    FieldKind::U32Inf0NullNoVal => {
                        // INFINITE collapsed to 0 -- for a u32 field where
                        // INFINITE means "no limit" (e.g. time_timelimit
                        // for a job with an explicitly unlimited limit),
                        // not "unset". NO_VAL is a distinct third state
                        // ("never set -- the limit is whatever the
                        // partition imposes", printed as "Partition_Limit"
                        // by sacct, truncated to "Partition+" at the
                        // default column width) and collapses to null,
                        // like NoVal32.
                        let ival = (base.add(d.offset) as *const u32).read_unaligned();
                        if ival == decode::NO_VAL {
                            Value::Null
                        } else {
                            Value::from(u64::from(if ival == decode::INFINITE {
                                0
                            } else {
                                ival
                            }))
                        }
                    }
                }
            };
            out.insert(d.key.to_string(), val);
        }

        let tres_alloc = out
            .get("tres_alloc_str")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let tres_req = out
            .get("tres_req_str")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        for (k, v) in self.tres_flat_fields("allocated", &tres_alloc) {
            out.insert(k, v);
        }
        for (k, v) in self.tres_flat_fields("requested", &tres_req) {
            out.insert(k, v);
        }
        // Expand the wait-status-packed exitcode into
        // exitcode_return_code/exitcode_signal via process_exit_code's
        // WIFEXITED/WIFSIGNALED decoding.
        let exitcode = out.get("exitcode").and_then(Value::as_u64).unwrap_or(0) as u32;
        let (return_code, signal) = decode::process_exit_code(exitcode);
        out.insert(
            "exitcode_return_code".to_string(),
            return_code.map_or(Value::Null, |v| Value::from(u64::from(v))),
        );
        out.insert(
            "exitcode_signal".to_string(),
            signal.map_or(Value::Null, |v| Value::from(u64::from(v))),
        );
        Value::Object(out)
    }

    /// Expand a tres_alloc_str/tres_req_str like "1=4,2=17179869184,1001=2"
    /// into {"allocated_cpu": 4, "allocated_mem": 17179869184,
    /// "allocated_gres_gpu": 2, ...}, reusing parse_tres for the
    /// id->{type,name} resolution and its int64 sign-fix (a count like
    /// 18446744073709551614 is really -2, see parse_tres).
    ///
    /// A GRES name optionally carries a model/type suffix after a colon
    /// (e.g. "gpu:a100" -- the "a100" is which GPU model, not always
    /// present). That suffix is kept out of the key -- "gres_gpu" stays
    /// "gres_gpu" whether or not a model is reported -- and surfaced
    /// instead as a separate "<prefix>_<key>_type" field, so consumers can
    /// group by GRES name without the key varying per model.
    fn tres_flat_fields(&self, prefix: &str, raw: &str) -> Vec<(String, Value)> {
        let mut fields = Vec::new();
        for entry in decode::parse_tres(raw, &self.tres_by_id) {
            let type_key = if decode::tres_key_part(&entry.r#type).is_empty() {
                format!("id_{}", entry.id)
            } else {
                decode::tres_key_part(&entry.r#type)
            };
            let (name, gres_type) = match entry.name.split_once(':') {
                Some((n, t)) => (n, t),
                None => (entry.name.as_str(), ""),
            };
            let name_key = decode::tres_key_part(name);
            let key = if name_key.is_empty() {
                type_key
            } else {
                format!("{type_key}_{name_key}")
            };
            fields.push((format!("{prefix}_{key}"), Value::from(entry.count)));
            if !gres_type.is_empty() {
                fields.push((format!("{prefix}_{key}_type"), Value::from(gres_type)));
            }
        }
        fields
    }
}

impl Drop for Slurmdb {
    fn drop(&mut self) {
        if !self.conn.is_null() {
            self.close();
        }
    }
}
