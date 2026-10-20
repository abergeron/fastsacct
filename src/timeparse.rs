//! Time parsing and timezone enforcement -- the intentionally-partial port
//! of Slurm's `parse_time()` grammar (src/common/parse_time.c): ISO 8601
//! and "now" only. Slurm's own `parse_time()`/
//! `slurmdb_job_cond_def_start_end()` aren't in the installed public
//! headers (slurm/slurm.h, slurm/slurmdb.h), so calling them via FFI would
//! mean depending on undocumented internal symbols; we'd rather fail loudly
//! on an unsupported format than silently drift from an unversioned
//! internal function.
//!
//! -S/-E values carry no timezone of their own (unlike --debug's
//! usage_start/usage_end log line, which is always UTC regardless of system
//! tz), so they are always interpreted as UTC -- never the process's local
//! timezone. That is only actually correct if the system timezone *is* UTC,
//! which [`require_utc_timezone`] enforces at startup.

use std::time::{SystemTime, UNIX_EPOCH};

/// Error carrying the message the CLI should surface verbatim.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TimeError(pub String);

impl std::fmt::Display for TimeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for TimeError {}

fn bad_format(value: &str) -> TimeError {
    TimeError(format!(
        "unsupported time format: {value:?} \
         (fastsacct only supports YYYY-MM-DD[THH:MM[:SS]] or 'now', \
         not Slurm's full relative-time grammar)"
    ))
}

/// `^\d{2}$` parsed to u32, else None.
fn two_digits(s: &str) -> Option<u32> {
    let b = s.as_bytes();
    if b.len() != 2 || !b.iter().all(u8::is_ascii_digit) {
        return None;
    }
    Some((b[0] - b'0') as u32 * 10 + (b[1] - b'0') as u32)
}

/// Parses `YYYY-MM-DD[THH:MM[:SS]]` (T or space separator) or `now` (case
/// insensitive), always as UTC. Mirrors the old argparse `type=` callback:
/// callers map the error to a CLI usage error (exit code 2).
pub fn parse_time(value: &str) -> Result<i64, TimeError> {
    if value.eq_ignore_ascii_case("now") {
        return Ok(SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before Unix epoch")
            .as_secs() as i64);
    }
    // ^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})
    //   (?:[T ](?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?)?$
    let err = || bad_format(value);
    let b = value.as_bytes();
    let digits = |r: std::ops::Range<usize>| -> Option<u32> {
        if b.len() < r.end || !b[r.clone()].iter().all(u8::is_ascii_digit) {
            return None;
        }
        std::str::from_utf8(&b[r]).ok()?.parse().ok()
    };
    if b.len() < 10 || b[4] != b'-' || b[7] != b'-' {
        return Err(err());
    }
    let (Some(y), Some(m), Some(d)) = (digits(0..4), digits(5..7), digits(8..10)) else {
        return Err(err());
    };
    let rest = &value[10..];
    let (h, mi, s) = if rest.is_empty() {
        (0, 0, 0)
    } else {
        if !matches!(rest.as_bytes()[0], b'T' | b' ') {
            return Err(err());
        }
        let mut it = rest[1..].split(':');
        let (Some(hh), Some(mm)) = (
            it.next().and_then(two_digits),
            it.next().and_then(two_digits),
        ) else {
            return Err(err());
        };
        let ss = match it.next() {
            Some(v) => two_digits(v).ok_or_else(err)?,
            None => 0,
        };
        if it.next().is_some() {
            return Err(err());
        }
        (hh, mm, ss)
    };
    if !(1..=12).contains(&m)
        || !(1..=days_in_month(y as i64, m)).contains(&d)
        || h > 23
        || mi > 59
        || s > 59
    {
        return Err(err());
    }
    Ok(days_from_civil(y as i64, m as i64, d as i64) * 86400
        + (h as i64) * 3600
        + (mi as i64) * 60
        + s as i64)
}

/// Refuse to run under a non-UTC system timezone. [`parse_time`] and the
/// --debug usage_start/usage_end log line both treat naive/epoch timestamps
/// as UTC; if the process's actual local timezone (TZ env var, or
/// /etc/localtime when TZ is unset) isn't UTC, -S/-E would silently be
/// interpreted in the wrong timezone instead of erroring.
pub fn require_utc_timezone() -> Result<(), String> {
    let offset_min = local_utc_offset_minutes();
    if offset_min == 0 {
        return Ok(());
    }
    let sign = if offset_min >= 0 { '+' } else { '-' };
    let (h, m) = (offset_min.abs() / 60, offset_min.abs() % 60);
    let tz_env = match std::env::var("TZ") {
        Ok(v) => format!("TZ='{v}'"),
        Err(_) => "TZ=None".to_string(),
    };
    Err(format!(
        "fastsacct requires the system timezone to be UTC, but the resolved \
         local timezone offset is UTC{sign}{h:02}:{m:02} ({tz_env}). Set \
         TZ=UTC (or configure the system timezone as UTC) before running \
         fastsacct."
    ))
}

/// The resolved local timezone's offset from UTC, in minutes (TZ env var,
/// or /etc/localtime when TZ is unset) at the current instant.
pub fn local_utc_offset_minutes() -> i32 {
    // SAFETY: time() and localtime_r() only read/clobber their own args.
    unsafe {
        let t = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&t, &mut tm).is_null() {
            // Can't resolve the local zone -- return a nonzero sentinel so
            // require_utc_timezone() errors out rather than silently
            // guessing the offset is 0.
            return i32::MIN;
        }
        (tm.tm_gmtoff / 60) as i32
    }
}

/// Formats an epoch-seconds timestamp as `YYYY-MM-DDTHH:MM:SS+00:00`
/// (Python's `datetime.fromtimestamp(ts, timezone.utc).isoformat()`, used
/// in --debug log lines).
pub fn format_utc_iso(epoch: i64) -> String {
    let days = epoch.div_euclid(86400);
    let secs = epoch.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    let (h, mi, s) = (secs / 3600, secs / 60 % 60, secs % 60);
    format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}+00:00")
}

/// Days since the Unix epoch for a proleptic-Gregorian civil date (Howard
/// Hinnant's days_from_civil).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146097 + doe - 719468
}

/// Inverse of [`days_from_civil`].
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn days_in_month(y: i64, m: u32) -> u32 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_supported_formats() {
        assert_eq!(parse_time("2026-01-01"), Ok(1767225600));
        assert_eq!(parse_time("2026-01-01T00:00:00"), Ok(1767225600));
        assert_eq!(parse_time("2026-01-01 12:30"), Ok(1767270600));
        assert_eq!(parse_time("2026-01-01T12:30:45"), Ok(1767270645));
        assert_eq!(parse_time("2024-02-29"), Ok(1709164800)); // leap day
    }

    #[test]
    fn now_is_epoch_approximately_now() {
        let now = parse_time("now").unwrap();
        let real = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        assert!((now - real).abs() <= 2);
    }

    #[test]
    fn rejects_everything_else() {
        for bad in [
            "",
            "n",
            "now+1hour",
            "2026-1-1",
            "2026-13-01",
            "2026-02-30",
            "2026-02-29", // 2026 is not a leap year
            "2026-01-01T24:00",
            "2026-01-01T12:60",
            "2026-01-01T12:00:60",
            "2026-01-01_12:00",
            "2026-01-01T12",
            "2026-01-01T12:00:00Z",
            "1767225600",
            "2026-01-0",
        ] {
            assert!(parse_time(bad).is_err(), "should reject {bad:?}");
        }
        assert!(parse_time("2024-02-29").is_ok());
    }

    #[test]
    fn iso_formatting() {
        assert_eq!(format_utc_iso(1767225600), "2026-01-01T00:00:00+00:00");
        assert_eq!(format_utc_iso(0), "1970-01-01T00:00:00+00:00");
        assert_eq!(format_utc_iso(951782400), "2000-02-29T00:00:00+00:00");
        assert_eq!(format_utc_iso(1767270645), "2026-01-01T12:30:45+00:00");
    }

    #[test]
    fn utc_check_matches_environment() {
        // The check must agree with the resolved offset either way (dev
        // machines may or may not run under TZ=UTC).
        let offset = local_utc_offset_minutes();
        assert_eq!(require_utc_timezone().is_ok(), offset == 0);
    }
}
