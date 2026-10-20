//! fastsacct -- a fast, narrow replacement for:
//!
//! ```text
//! sacct -A <accounts> --starttime <t> --endtime <t> --duplicates -X --allusers --json
//! ```
//!
//! It calls libslurmdb.so directly (the same `slurmdb_jobs_get()` sacct
//! itself calls) via dlopen/dlsym, and skips Slurm's data_parser plugin
//! entirely -- that plugin, not the RPC, is what makes `sacct --json` slow
//! (see README.md).
//!
//! Only `-A`/`--accounts`, `-S`/`--starttime`, `-E`/`--endtime` are
//! actually implemented. `-D`/`--duplicates`, `-X`/`--allocations`,
//! `-a`/`--allusers`, `--json` must ALSO be passed (this tool's behavior
//! always matches what those four mean in real sacct: no dedup, job-level
//! only, all users, JSON out) -- but they don't change what we do, we just
//! require them so an invocation that would behave differently under plain
//! sacct is rejected rather than silently mishandled. Any other sacct flag
//! is a hard error: this is a narrow drop-in for one invocation shape, not
//! a general sacct replacement.
//!
//! Output is a flat JSON schema: one flat object per job, mostly plain
//! scalars (not sacct --json's nested `{"set":...,"infinite":...,"number":
//! ...}` OpenAPI shape) with a handful of fields decoded to strings/lists
//! or expanded into sibling fields where that's cheap to do without an RPC
//! per job -- see README.md's "Output schema" section for the full list.
//!
//! Two output modes, chosen at the CLI (`--jsonl`): the default is one
//! `{"meta": ..., "jobs": [...]}` JSON blob; `--jsonl` instead streams
//! newline-delimited JSON -- a `{"meta": ...}` line, then one job object
//! per line, each written and flushed as soon as it's decoded. Both modes
//! put a `job_count` in the meta: `slurm_list_count()` is O(1) over the C
//! `list_t` `slurmdb_jobs_get()` returns, so the total is known the instant
//! the RPC returns, with no job decoded into memory -- a downstream reader
//! can detect a truncated/lost-tail file just from the header, without a
//! trailing count chunk and without holding the whole result set in memory.
//! `slurmdb_jobs_get()` is a single RPC that returns every job in one shot,
//! so there's no way to start emitting before the whole result set exists
//! in memory either way -- `--jsonl`'s win is letting a downstream reader
//! start *processing* jobs (one parse per line) without waiting for the
//! whole run to finish or holding the whole response in memory, which a
//! single top-level JSON array can't offer without both ends speaking an
//! incremental parser.

pub mod abi;
pub mod decode;
pub mod slurmdb;
pub mod timeparse;

/// Flat single-blob mode's schema id.
pub const SCHEMA_VERSION: &str = "fastsacct-flat-v1";
/// Streaming --jsonl mode's schema id.
pub const SCHEMA_VERSION_JSONL: &str = "fastsacct-jsonl-v1";
