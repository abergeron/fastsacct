//! CLI + main flow: argument parsing (sacct-shaped, narrow by design),
//! ABI auto-detection, output-mode dispatch. The heavy lifting lives in
//! the `fastsacct` library crate.

use clap::Parser;
use fastsacct::abi::{self, Abi};
use fastsacct::slurmdb::Slurmdb;
use fastsacct::timeparse::{parse_time, require_utc_timezone};
use fastsacct::{SCHEMA_VERSION, SCHEMA_VERSION_JSONL};
use serde_json::{Map, Value};
use std::io::Write;
use std::process::ExitCode;

/// Required marker flags, checked after parsing (see the lib docs): their
/// presence is validated, their meaning is always assumed.
#[derive(Parser)]
#[command(
    name = "fastsacct",
    about = "a fast, narrow replacement for `sacct -A <accounts> -S <start> -E <end> -D -X -a --json` \
             that calls slurmdb_jobs_get() directly, bypassing Slurm's data_parser plugin \
             (see the crate docs / README.md)"
)]
struct Args {
    /// comma-separated account list (sacct -A)
    #[arg(short = 'A', long = "accounts", default_value = "")]
    accounts: String,

    /// sacct -S; YYYY-MM-DD[THH:MM[:SS]] or 'now'
    #[arg(short = 'S', long = "starttime")]
    starttime: String,

    /// sacct -E; YYYY-MM-DD[THH:MM[:SS]] or 'now'
    #[arg(short = 'E', long = "endtime")]
    endtime: String,

    /// required marker flag, must be passed (see crate docs)
    #[arg(short = 'D', long = "duplicates")]
    duplicates: bool,

    /// required marker flag, must be passed (see crate docs)
    #[arg(short = 'X', long = "allocations")]
    allocations: bool,

    /// required marker flag, must be passed (see crate docs)
    #[arg(short = 'a', long = "allusers")]
    allusers: bool,

    /// required marker flag, must be passed (see crate docs)
    #[arg(long = "json")]
    json: bool,

    /// stream newline-delimited JSON instead of one JSON blob: first line
    /// is {"meta": ...}, one job object per line after that, each written
    /// and flushed as soon as that job is ready. The meta line carries
    /// job_count (the total number of job lines to follow), so a reader can
    /// detect a truncated/lost-tail file from the header alone. Every line
    /// is a single JSON value with no embedded literal newlines -- serde
    /// escaping handles job names containing tabs, newlines, braces, or
    /// anything else short of a NUL byte -- so a plain readline() +
    /// JSON-parse loop on the other end is safe.
    #[arg(long = "jsonl")]
    jsonl: bool,

    /// path to libslurmdb.so (default: libslurmdb.so, resolved via the
    /// normal dynamic linker search path)
    #[arg(long = "library", default_value = "libslurmdb.so")]
    library: String,

    /// print job_cond fields and the raw slurmdb_jobs_get() result count to
    /// stderr before any further processing
    #[arg(long = "debug")]
    debug: bool,

    /// Slurm release this cluster's libslurmdb.so was built from.
    /// Default: auto-detect (see detect_abi()).
    #[arg(long = "abi", value_parser = abi_choices())]
    abi: Option<String>,
}

fn abi_choices() -> clap::builder::PossibleValuesParser {
    clap::builder::PossibleValuesParser::new(fastsacct::abi::all().iter().map(|a| a.version))
}

/// argparse-style CLI error: usage-ish message on stderr, exit code 2.
fn cli_error(msg: &str) -> ! {
    eprintln!("usage: fastsacct -A <accounts> -S <start> -E <end> -D -X -a --json [...]");
    eprintln!("fastsacct: error: {msg}");
    std::process::exit(2)
}

fn runtime_error(msg: &str) -> ExitCode {
    eprintln!("fastsacct: {msg}");
    ExitCode::FAILURE
}

/// ABI auto-detection.
///
/// Primary: call the real libslurmdb.so's own `slurm_api_version()` (public,
/// slurm/slurm.h) and decode the API_CURRENT it was built with -- this comes
/// straight from the exact .so we're about to use.
fn detect_abi(library_path: &str) -> Result<&'static Abi, String> {
    match fastsacct::slurmdb::probe_api_major(library_path) {
        Ok(api_major) => match abi::by_api_major(api_major) {
            Some(abi) => {
                eprintln!(
                    "fastsacct: auto-detected Slurm {} (API major {}) via slurm_api_version()",
                    abi.version, api_major
                );
                Ok(abi)
            }
            None => Err(format!(
                "{library_path} reports Slurm API major={api_major} ({}) via \
                 slurm_api_version(), but no ABI module is implemented for it. \
                 Implemented: {}. Pass --abi to override, or add a new \
                 src/abi/vXX_YY.rs module (see src/abi/mod.rs docs).",
                abi::release_hint(api_major),
                abi::implemented_with_api_major(),
            )),
        },
        Err(lib_exc) => Err(format!(
            "couldn't auto-detect the Slurm ABI: probing {library_path:?} via \
                 slurm_api_version() failed ({lib_exc}); implemented: {}",
            abi::implemented_versions_sorted(),
        )),
    }
}

fn main() -> ExitCode {
    let args = Args::parse();

    // -S/-E parse first (argparse did this via type=, before the rest).
    let (Ok(starttime), Ok(endtime)) = (parse_time(&args.starttime), parse_time(&args.endtime))
    else {
        // surface whichever failed, same message parse_time built
        for raw in [&args.starttime, &args.endtime] {
            if let Err(e) = parse_time(raw) {
                cli_error(&format!("invalid --starttime/--endtime value: {e}"));
            }
        }
        unreachable!();
    };

    let mut missing = Vec::new();
    for (name, present) in [
        ("-D/--duplicates", args.duplicates),
        ("-X/--allocations", args.allocations),
        ("-a/--allusers", args.allusers),
        ("--json", args.json),
    ] {
        if !present {
            missing.push(name);
        }
    }
    if !missing.is_empty() {
        cli_error(&format!(
            "fastsacct always behaves as if -D -X -a --json were given (no \
             dedup, job-level only, all users, JSON output) -- pass them \
             explicitly so that's clear at the call site. Missing: {}",
            missing.join(", ")
        ));
    }
    if starttime > endtime {
        cli_error("start time is after end time");
    }

    if let Err(msg) = require_utc_timezone() {
        return runtime_error(&msg);
    }

    // sacct -A runs each entry through slurm_addto_char_list(), which trims
    // whitespace and lower-cases it (src/common/slurm_protocol_defs.c
    // _add_to_list()) before it ever reaches the accounting_storage/mysql
    // plugin's case-sensitive `t1.account='%s'` filter. Match that here, or
    // a mixed-case/space-padded -A value silently matches zero rows instead
    // of erroring or matching like real sacct would.
    let accounts: Vec<String> = args
        .accounts
        .split(',')
        .map(|a| a.trim())
        .filter(|a| !a.is_empty())
        .map(|a| a.to_lowercase())
        .collect();

    let abi = match &args.abi {
        Some(v) => abi::by_version(v).expect("clap validated --abi against the registry"),
        None => match detect_abi(&args.library) {
            Ok(abi) => abi,
            Err(e) => return runtime_error(&e),
        },
    };

    let mut meta = Map::new();
    meta.insert("source".into(), Value::from("fastsacct"));
    meta.insert(
        "schema_version".into(),
        Value::from(if args.jsonl {
            SCHEMA_VERSION_JSONL
        } else {
            SCHEMA_VERSION
        }),
    );
    meta.insert("slurm_abi_version".into(), Value::from(abi.version));

    let mut db = match Slurmdb::new(&args.library, abi, args.debug) {
        Ok(db) => db,
        Err(e) => return runtime_error(&e.to_string()),
    };

    let (job_count, list) = match db.jobs_get(&accounts, starttime, endtime) {
        Ok(v) => v,
        Err(e) => {
            db.close();
            return runtime_error(&e.to_string());
        }
    };
    meta.insert("job_count".into(), Value::from(job_count));

    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let result = if args.jsonl {
        // One job per line, flushed as it's decoded -- see --jsonl's help
        // text for why a downstream reader doing readline() + JSON-parse is
        // safe against whatever ends up in a job name. JSON string escaping
        // (used for every line, meta and job alike) never emits a literal
        // control character inside a string -- every char < 0x20 is
        // \-escaped -- so no job's JSON text can ever contain a literal
        // newline that would corrupt this format's one-value-per-line
        // framing. job_count comes from slurm_list_count() (O(1) over the C
        // list) so the header carries the total up front, letting a reader
        // detect a truncated/lost-tail file without a trailing count chunk
        // and without holding all jobs in memory.
        let mut write_line = |v: &Value| -> Result<(), Box<dyn std::error::Error>> {
            serde_json::to_writer(&mut out, v)?;
            out.write_all(b"\n")?;
            out.flush()?;
            Ok(())
        };
        write_line(&Value::Object({
            let mut line = Map::new();
            line.insert("meta".into(), Value::Object(meta.clone()));
            line
        }))
        .and_then(|()| {
            db.stream_jobs(list, |job| {
                // Flush per job: a downstream reader can process each line
                // as it arrives.
                write_line(job)
            })
        })
    } else {
        let jobs = db.collect_jobs(list);
        let mut blob = Map::new();
        blob.insert("meta".into(), Value::Object(meta));
        blob.insert("jobs".into(), Value::Array(jobs));
        let write_blob = || -> Result<(), Box<dyn std::error::Error>> {
            serde_json::to_writer(&mut out, &Value::Object(blob))?;
            out.write_all(b"\n")?;
            out.flush()?;
            Ok(())
        };
        write_blob()
    };

    db.close();

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => runtime_error(&format!("writing output: {e}")),
    }
}
