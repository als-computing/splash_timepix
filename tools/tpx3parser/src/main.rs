//! tpx3parser — ASI TimePix3 file parser
//!
//! Reads .tpx3 files and writes HDF5 output with the same dataset schema as
//! ASI tpx3dump process --disable-clustering.
//!
//! Usage mirrors tpx3dump for easy side-by-side comparison:
//!   tpx3parser -i input.tpx3 -o output.h5
//!   tpx3parser -i input.tpx3 -o output.h5 --stats
//!   tpx3parser -i input.tpx3 -o output.h5 --threads 4
//!   tpx3parser -i input.tpx3 --stats          (stats only, no output)

mod decode;
mod packet;
mod section;
mod writer;

use anyhow::{Context, Result};
use clap::Parser;
use memmap2::Mmap;
use std::fs::File;
use std::path::PathBuf;
use std::time::Instant;

#[derive(Parser, Debug)]
#[command(
    name = "tpx3parser",
    about = "ASI TimePix3 .tpx3 → HDF5 parser (raw hits + TDC events, no clustering)"
)]
struct Cli {
    /// Input .tpx3 file.
    #[arg(short = 'i', long)]
    input: PathBuf,

    /// Output .h5 file. Omit to do a dry-run (stats only).
    #[arg(short = 'o', long)]
    output: Option<PathBuf>,

    /// Print packet-type statistics to stdout.
    #[arg(long)]
    stats: bool,

    /// Number of rayon worker threads (default: all logical CPUs).
    #[arg(long)]
    threads: Option<usize>,

    /// Exposure interval in milliseconds for ExposureTimeBoundaries.
    ///
    /// The sorted hit+TDC stream is split at multiples of this duration,
    /// producing one cumulative-count boundary per interval — identical to
    /// `tpx3dump --time-bin-interval`. Defaults to 500 ms, which is also
    /// `tpx3dump`'s default.
    #[arg(long, value_name = "MS", default_value = "500")]
    exposure_interval_ms: f64,

    /// Suppress progress messages.
    #[arg(short, long)]
    quiet: bool,
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    if let Some(n) = cli.threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .context("building rayon thread pool")?;
    }

    let t0 = Instant::now();
    let file = File::open(&cli.input)
        .with_context(|| format!("opening {}", cli.input.display()))?;
    let mmap = unsafe { Mmap::map(&file) }
        .with_context(|| format!("memory-mapping {}", cli.input.display()))?;
    let data: &[u8] = &mmap;

    if !cli.quiet {
        eprintln!(
            "Opened {} ({:.1} MB, {} packets)",
            cli.input.display(),
            data.len() as f64 / 1e6,
            data.len() / 8
        );
    }

    if cli.stats {
        let stats = decode::collect_stats(data);
        println!("Packet statistics for {}:", cli.input.display());
        println!("  Total packets   : {}", stats.total_packets());
        println!("  Section headers : {}", stats.headers);
        println!("  Hit packets     : {}", stats.hits);
        println!("  TDC packets     : {}", stats.tdcs);
        println!("  Seq-counter (0x50): {}", stats.seq_counter);
        println!("  Other           : {}", stats.other);
    }

    if cli.output.is_none() {
        if !cli.quiet {
            eprintln!("No output file specified; exiting after stats.");
        }
        return Ok(());
    }
    let output = cli.output.as_ref().unwrap();

    // Discover sections.
    let t1 = Instant::now();
    let sections = section::discover_sections(data);
    if !cli.quiet {
        eprintln!(
            "Discovered {} sections in {:.1} ms",
            sections.len(),
            t1.elapsed().as_secs_f64() * 1000.0
        );
    }

    // Decode hits and TDC events.
    let t2 = Instant::now();
    let (hits, tdcs) = decode::decode_all(data, &sections);
    if !cli.quiet {
        eprintln!(
            "Decoded {} hits + {} TDC events in {:.1} ms",
            hits.len(),
            tdcs.len(),
            t2.elapsed().as_secs_f64() * 1000.0
        );
    }

    // Derive ExposureTimeBoundaries (defaults to 500 ms, same as tpx3dump).
    let etb = derive_etb_by_interval(&hits, &tdcs, cli.exposure_interval_ms);

    // Write HDF5.
    let t3 = Instant::now();
    writer::write_h5(output, &hits, &tdcs, &etb)
        .with_context(|| format!("writing {}", output.display()))?;
    if !cli.quiet {
        eprintln!(
            "Wrote {} in {:.1} ms",
            output.display(),
            t3.elapsed().as_secs_f64() * 1000.0
        );
    }

    let elapsed = t0.elapsed().as_secs_f64();
    let mpps = hits.len() as f64 / elapsed / 1e6;
    if !cli.quiet {
        eprintln!(
            "Total: {:.2} s  |  {:.1} M hits/s",
            elapsed, mpps
        );
    }

    Ok(())
}

/// Compute ExposureTimeBoundaries by splitting the sorted event stream at fixed
/// time intervals.
///
/// For each interval boundary at t = interval_ms, 2*interval_ms, ...:
///   ETB[i] = number of sorted hits + sorted TDC events with timestamp < boundary_01ps
///
/// Hits are already sorted by (toa, x, y). TDC events are in file order (ASI tpx3dump
/// writes them in the order they were decoded, which is approximately time-ordered).
///
/// The final boundary is always appended as n_hits + n_tdcs.
fn derive_etb_by_interval(
    hits: &decode::HitBatch,
    tdcs: &decode::TdcBatch,
    interval_ms: f64,
) -> Vec<u64> {
    if hits.is_empty() && tdcs.is_empty() {
        return vec![0];
    }

    // interval in 0.1 ps units: 1 ms = 1e-3 s / 1e-13 s/unit = 1e10 units/ms
    let interval_01ps: u64 = (interval_ms * 1e10).round() as u64;
    if interval_01ps == 0 {
        return vec![hits.len() as u64 + tdcs.len() as u64];
    }

    // tpx3dump bins from absolute t = 0, not from the first event.
    // Boundary k covers events with timestamp in [0, k * interval).
    let last_toa = hits.toa.last().copied().unwrap_or(0);
    let last_tdc = tdcs.timestamp.last().copied().unwrap_or(0);
    let t_end = last_toa.max(last_tdc);

    // Number of full interval boundaries; any events past the last boundary
    // are captured by the sentinel appended below.
    let n_intervals = (t_end / interval_01ps) as usize;
    let mut boundaries = Vec::with_capacity(n_intervals + 1);

    let mut hit_idx = 0usize;
    let mut tdc_idx = 0usize;
    let n_hits = hits.toa.len();
    let n_tdcs = tdcs.timestamp.len();

    for k in 1..=n_intervals {
        let boundary = k as u64 * interval_01ps;
        while hit_idx < n_hits && hits.toa[hit_idx] < boundary {
            hit_idx += 1;
        }
        while tdc_idx < n_tdcs && tdcs.timestamp[tdc_idx] < boundary {
            tdc_idx += 1;
        }
        boundaries.push(hit_idx as u64 + tdc_idx as u64);
    }

    // tpx3dump always appends the total count as a final sentinel entry.
    boundaries.push(n_hits as u64 + n_tdcs as u64);

    boundaries
}

