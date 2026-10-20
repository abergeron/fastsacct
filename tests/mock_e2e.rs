//! End-to-end tests (the Rust port of the old pytest suite): for each
//! supported ABI (24.11, 25.05, 25.11, 26.05), build a mock libslurmdb.so
//! against that release's real vendored headers (mock/include/<version>/),
//! point the fastsacct binary at it, and check the flat JSON output is
//! exactly right -- including the handful of fields that only exist on
//! some releases (lft on 24.11; resv_req/segment_size on 25.05+;
//! exclusive/oversubscribe/sluid on 26.05+).

use fastsacct::abi;
use fastsacct::decode;
use fastsacct::timeparse::parse_time;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Command;
use std::sync::OnceLock;

const VERSIONS: [&str; 4] = ["24.11", "25.05", "25.11", "26.05"];

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn find_cc() -> Option<String> {
    for name in ["cc", "clang", "gcc"] {
        if Command::new(name).arg("--version").output().is_ok() {
            return Some(name.to_string());
        }
    }
    None
}

/// Compile one mock libslurmdb.so per supported ABI (once per test-binary
/// run), each against that release's real vendored headers, and return
/// {abi_version: so_path}. None == skipped (no C compiler found).
fn mock_libs() -> Option<&'static HashMap<&'static str, PathBuf>> {
    static MOCKS: OnceLock<Option<HashMap<&'static str, PathBuf>>> = OnceLock::new();
    MOCKS
        .get_or_init(|| {
            let cc = find_cc()?;
            let out_dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("mock-libs");
            std::fs::create_dir_all(&out_dir).expect("create mock build dir");
            let mut libs = HashMap::new();
            for version in VERSIONS {
                let module = abi::by_version(version).expect("registered ABI");
                let so_path = out_dir.join(format!("libslurmdb_{}.so", version.replace('.', "_")));
                let include_dir = repo_root().join("mock").join("include").join(version);
                assert!(
                    include_dir.is_dir(),
                    "missing vendored headers: {include_dir:?}"
                );
                let status = Command::new(&cc)
                    .arg("-shared")
                    .arg("-fPIC")
                    .arg("-I")
                    .arg(&include_dir)
                    .arg(format!("-DMOCK_API_MAJOR={}", module.api_major))
                    .arg("-o")
                    .arg(&so_path)
                    .arg(repo_root().join("mock").join("mock_libslurmdb.c"))
                    .status()
                    .expect("run C compiler");
                assert!(status.success(), "compiling mock for {version} failed");
                libs.insert(version, so_path);
            }
            Some(libs)
        })
        .as_ref()
}

/// Run fastsacct against `library`, with the required -S/-E/-D/-X/-a/--json
/// flags, under TZ=UTC (fastsacct refuses to run otherwise). Returns raw
/// stdout.
fn run_fastsacct(library: &str, abi: Option<&str>, extra: &[&str]) -> String {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_fastsacct"));
    cmd.env("TZ", "UTC")
        .args([
            "-S",
            "2026-01-01",
            "-E",
            "2026-01-02",
            "-D",
            "-X",
            "-a",
            "--json",
        ])
        .args(["--library", library])
        .args(extra);
    if let Some(abi) = abi {
        cmd.args(["--abi", abi]);
    }
    let out = cmd.output().expect("run fastsacct binary");
    assert!(
        out.status.success(),
        "fastsacct exited {}\nstdout:\n{}\nstderr:\n{}",
        out.status,
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
    String::from_utf8(out.stdout).expect("stdout is UTF-8 JSON")
}

// ---------------------------------------------------------------------------
// Expected output, derived independently from mock_libslurmdb.c's
// make_job() (the actual C values written into the struct) plus
// fastsacct's own (shared, version-independent, separately unit-tested)
// decode helpers. This checks the part that *is* version-specific: that
// each abi module's offsets read those C values back from the right place.
// ---------------------------------------------------------------------------

struct MockJobSpec {
    jobid: u32,
    account: &'static str,
    jobname: &'static str,
    delta: (i64, i64),
    timelimit: u32,
}

const MOCK_NOW: &str = "2026-01-02";

fn mock_job_specs() -> Vec<MockJobSpec> {
    vec![
        MockJobSpec {
            jobid: 1001,
            account: "mila-account",
            jobname: "train_job",
            delta: (3600, 1800),
            timelimit: 60,
        },
        MockJobSpec {
            jobid: 1002,
            account: "mila-account",
            jobname: "eval_job",
            delta: (1800, 900),
            timelimit: 60,
        },
        // No time limit -- exercises the INFINITE-sentinel-to-0 mapping
        // for U32Inf0 fields.
        MockJobSpec {
            jobid: 1003,
            account: "other-account",
            jobname: "sweep_job",
            delta: (7200, 6900),
            timelimit: decode::INFINITE,
        },
        // Deliberately nasty job name (tab, newline, CR, braces, brackets,
        // quotes, backslash) -- exercises the --jsonl one-JSON-value-per-
        // line framing guarantee. Must match mock_libslurmdb.c's
        // slurmdb_jobs_get() exactly.
        MockJobSpec {
            jobid: 1004,
            account: "mila-account",
            jobname: "weird\tname\nwith\r{braces}[brackets]\"quotes\"\\backslash",
            delta: (100, 50),
            timelimit: 60,
        },
    ]
}

fn expected_jobs(version: &str) -> Vec<Value> {
    let now = parse_time(MOCK_NOW).unwrap();
    mock_job_specs()
        .iter()
        .map(|spec| expected_job(spec, now, version))
        .collect()
}

fn expected_job(spec: &MockJobSpec, now: i64, version: &str) -> Value {
    let start = now - spec.delta.0;
    let end = now - spec.delta.1;
    // raw values, keyed by JSON key (the subset make_job() sets; every
    // other field is calloc-zeroed/NULL by the mock)
    let mut raw: Map<String, Value> = Map::new();
    let mut put = |k: &str, v: Value| {
        raw.insert(k.into(), v);
    };
    put("account", json!(spec.account));
    put("job_id", json!(spec.jobid));
    put("name", json!(spec.jobname));
    put("cluster", json!("mila"));
    put("partition", json!("main"));
    put("nodes", json!("node[01-02]"));
    put("user", json!("someuser"));
    put("uid", json!(1000));
    put("gid", json!(1000));
    put("state", json!(3));
    put("time_start", json!(start));
    put("time_end", json!(end));
    put("time_submit", json!(start - 60));
    put("time_eligible", json!(start - 60));
    put("time_elapsed", json!(end - start));
    put("req_cpus", json!(4));
    put("alloc_nodes", json!(2));
    put("tres_alloc_str", json!("1=4,2=17179869184,4=2"));
    put("tres_req_str", json!("1=4,2=17179869184,4=2"));
    // MEM_PER_CPU (bit 63) tagged: 4096 MB per cpu.
    put("req_mem", json!(0x8000_0000_0000_1000u64));
    put("exitcode", json!(0));
    put("priority", json!(100));
    put("qos", json!(1));
    put("time_timelimit", json!(spec.timelimit));
    put("wckeyid", json!(0));
    put("wckey", json!("*default"));
    put("tot_cpu_sec", json!(120));
    put("user_cpu_sec", json!(100));
    put("sys_cpu_sec", json!(20));
    put("lineage", json!("/some/lineage/path"));
    put("work_dir", json!("/home/someuser"));
    match version {
        "24.11" => {
            put("lft", json!(7));
        }
        "25.05" | "25.11" => {
            put("resv_req", json!("normal"));
            put("segment_size", json!(8));
        }
        "26.05" => {
            put("resv_req", json!("normal"));
            put("segment_size", json!(8));
            put("exclusive", json!("NO"));
            put("oversubscribe", json!("NO"));
            put("sluid", json!(0x1_2345_6789u64));
        }
        _ => unreachable!(),
    }

    let module = abi::by_version(version).unwrap();
    let mut out = Map::new();
    for d in module.job_fields {
        let val = raw.get(d.key);
        let v = match d.kind {
            abi::FieldKind::Str => val.cloned().unwrap_or(Value::Null),
            abi::FieldKind::Group => continue, // system-dependent; checked separately
            abi::FieldKind::Flags => Value::from(
                decode::job_flags(0)
                    .into_iter()
                    .map(Value::from)
                    .collect::<Vec<_>>(),
            ),
            abi::FieldKind::JobState => Value::from(
                decode::job_state(3)
                    .into_iter()
                    .map(Value::from)
                    .collect::<Vec<_>>(),
            ),
            abi::FieldKind::QosName => json!("normal"), // mock's qos_get() has only id 1 -> "normal"
            abi::FieldKind::NoVal32 => {
                let ival = val.and_then(Value::as_u64).unwrap_or(0);
                if ival == decode::NO_VAL as u64 || ival == decode::INFINITE as u64 {
                    Value::Null
                } else {
                    json!(ival)
                }
            }
            abi::FieldKind::U32Inf0 => {
                let ival = val.and_then(Value::as_u64).unwrap_or(0);
                json!(if ival == decode::INFINITE as u64 {
                    0
                } else {
                    ival
                })
            }
            abi::FieldKind::U16
            | abi::FieldKind::U32
            | abi::FieldKind::U64
            | abi::FieldKind::Time => val.cloned().unwrap_or_else(|| json!(0)),
        };
        out.insert(d.key.to_string(), v);
    }
    out.insert("allocated_cpu".into(), json!(4));
    out.insert("requested_cpu".into(), json!(4));
    out.insert("allocated_mem".into(), json!(17179869184u64));
    out.insert("requested_mem".into(), json!(17179869184u64));
    out.insert("allocated_node".into(), json!(2));
    out.insert("requested_node".into(), json!(2));
    out.insert("exitcode_return_code".into(), json!(0));
    out.insert("exitcode_signal".into(), Value::Null);
    Value::Object(out)
}

fn version_only_fields(version: &str) -> (Vec<&'static str>, Vec<&'static str>) {
    match version {
        "24.11" => (
            vec!["lft"],
            vec![
                "resv_req",
                "segment_size",
                "exclusive",
                "oversubscribe",
                "sluid",
            ],
        ),
        "25.05" | "25.11" => (
            vec!["resv_req", "segment_size"],
            vec!["lft", "exclusive", "oversubscribe", "sluid"],
        ),
        "26.05" => (
            vec![
                "resv_req",
                "segment_size",
                "exclusive",
                "oversubscribe",
                "sluid",
            ],
            vec!["lft"],
        ),
        _ => unreachable!(),
    }
}

fn assert_jobs_match_expected(jobs: &[Value], version: &str) {
    let expected = expected_jobs(version);
    assert_eq!(jobs.len(), expected.len());
    let (present, absent) = version_only_fields(version);
    for (job, exp) in jobs.iter().zip(&expected) {
        let job = job.as_object().expect("job is an object");
        for key in present.iter() {
            assert!(
                job.contains_key(*key),
                "{version}: {key} missing from output"
            );
        }
        for key in absent.iter() {
            assert!(!job.contains_key(*key), "{version}: {key} should not exist");
        }
        let mut job = job.clone();
        assert!(
            job.remove("group").is_some_and(|g| g.is_string()),
            "{version}: group must be a string"
        );
        let expected = exp.as_object().unwrap();
        assert_eq!(&job, expected, "{version}: job output mismatch");
    }
}

#[test]
fn flat_output() {
    let Some(libs) = mock_libs() else {
        eprintln!("SKIP flat_output: no C compiler (cc/clang/gcc) found on PATH");
        return;
    };
    for version in VERSIONS {
        let out = run_fastsacct(libs[version].to_str().unwrap(), Some(version), &[]);
        let parsed: Value = serde_json::from_str(&out).expect("blob output parses");
        let meta = parsed["meta"].as_object().unwrap();
        assert_eq!(meta["schema_version"], json!(fastsacct::SCHEMA_VERSION));
        assert_eq!(meta["slurm_abi_version"], json!(version));
        let jobs = parsed["jobs"].as_array().unwrap();
        assert_eq!(meta["job_count"].as_i64().unwrap() as usize, jobs.len());
        assert_jobs_match_expected(jobs, version);
    }
}

#[test]
fn autodetect_via_slurm_api_version() {
    let Some(libs) = mock_libs() else {
        eprintln!("SKIP autodetect: no C compiler found on PATH");
        return;
    };
    for version in VERSIONS {
        // No --abi: fastsacct must probe the mock .so's slurm_api_version()
        // and land on the same ABI description as when told explicitly.
        let detected = run_fastsacct(libs[version].to_str().unwrap(), None, &[]);
        let explicit = run_fastsacct(libs[version].to_str().unwrap(), Some(version), &[]);
        assert_eq!(
            serde_json::from_str::<Value>(&detected).unwrap(),
            serde_json::from_str::<Value>(&explicit).unwrap(),
            "{version}: auto-detect disagrees with --abi"
        );
    }
}

#[test]
fn jsonl_output() {
    let Some(libs) = mock_libs() else {
        eprintln!("SKIP jsonl_output: no C compiler found on PATH");
        return;
    };
    for version in VERSIONS {
        let raw = run_fastsacct(libs[version].to_str().unwrap(), Some(version), &["--jsonl"]);

        // No trailing garbage, no blank lines in the middle -- exactly one
        // non-empty line per meta/job value, each independently
        // parseable regardless of what a job name contains (that's the
        // whole point).
        assert!(raw.ends_with('\n'));
        let lines: Vec<&str> = raw[..raw.len() - 1].split('\n').collect();
        assert!(lines.iter().all(|l| !l.is_empty()));

        let meta_line = lines[0];
        let job_lines = &lines[1..];
        let meta = serde_json::from_str::<Value>(meta_line).unwrap()["meta"].clone();
        let meta = meta.as_object().unwrap();
        assert_eq!(
            meta["schema_version"],
            json!(fastsacct::SCHEMA_VERSION_JSONL)
        );
        assert_eq!(meta["slurm_abi_version"], json!(version));
        assert_eq!(
            meta["job_count"].as_i64().unwrap() as usize,
            expected_jobs(version).len()
        );

        let jobs: Vec<Value> = job_lines
            .iter()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_jobs_match_expected(&jobs, version);

        // Cross-check against the default mode's meta and jobs, to make
        // sure --jsonl isn't just internally self-consistent but actually
        // carries the same data as the non-streaming path.
        let blob_raw = run_fastsacct(libs[version].to_str().unwrap(), Some(version), &[]);
        let blob: Value = serde_json::from_str(&blob_raw).unwrap();
        let mut meta_wo = meta.clone();
        meta_wo.remove("schema_version");
        let mut blob_meta = blob["meta"].as_object().unwrap().clone();
        blob_meta.remove("schema_version");
        assert_eq!(meta_wo, blob_meta);
        let jsonl_names: Vec<&Value> = jobs.iter().map(|j| &j["name"]).collect();
        let blob_names: Vec<&Value> = blob["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|j| &j["name"])
            .collect();
        assert_eq!(jsonl_names, blob_names);
        let weird = blob_names.last().unwrap().as_str().unwrap();
        assert!(weird.contains('\t') && weird.contains('\n') && weird.contains('\r'));
    }
}
