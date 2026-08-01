//! HDF5 writer producing output compatible with ASI tpx3dump --disable-clustering.
//!
//! Dataset layout (measured from ASI output):
//!
//! /PixelHits — compound, itemsize 40, chunk 15000, no compression, maxshape unlimited
//!   offset 0:  toa    u64
//!   offset 8:  cid    i64   (always -1 when clustering disabled)
//!   offset 16: tot    u64
//!   offset 24: dtoa   i32   (always -1)
//!   offset 28: x      u16
//!   offset 30: y      u16
//!   (2 bytes implicit padding to reach 32)
//!   offset 32: tof    i64   (always -1)
//!   itemsize 40.
//!
//! /TDCEvents — compound, itemsize 16, chunk 15000, maxshape unlimited
//!   offset 0:  tdc_type      u8
//!   offset 1:  (1 byte pad)
//!   offset 2:  trigger_count u16
//!   offset 4:  (4 bytes pad)
//!   offset 8:  timestamp     u64
//!   itemsize 16.
//!
//! /ExposureTimeBoundaries — uint64, chunk 15000, maxshape unlimited

use crate::decode::{HitBatch, TdcBatch};
use anyhow::{Context, Result};
use hdf5_metno as hdf5;
use std::path::Path;

const CHUNK: usize = 15000;

/// Returns the chunk size to use for a dataset of length `n`.
/// HDF5 does not allow chunk dimensions to exceed data dimensions for fixed-size datasets.
#[inline]
fn chunk_for(n: usize) -> usize {
    if n == 0 { 1 } else { n.min(CHUNK) }
}

pub fn write_h5(
    path: &Path,
    hits: &HitBatch,
    tdcs: &TdcBatch,
    exposure_boundaries: &[u64],
) -> Result<()> {
    let file = hdf5::File::create(path)
        .with_context(|| format!("creating HDF5 file {}", path.display()))?;

    write_pixel_hits(&file, hits).context("writing PixelHits")?;
    write_tdc_events(&file, tdcs).context("writing TDCEvents")?;
    write_exposure_boundaries(&file, exposure_boundaries)
        .context("writing ExposureTimeBoundaries")?;

    Ok(())
}

// ── /PixelHits ────────────────────────────────────────────────────────────────

/// Packed on-disk representation that matches ASI's compound type.
#[repr(C)]
#[derive(hdf5::H5Type)]
struct PixelHitRecord {
    toa: u64,
    cid: i64,
    tot: u64,
    dtoa: i32,
    x: u16,
    y: u16,
    // 2 bytes of implicit C-struct padding are inserted here by the compiler
    // to align `tof` to 8 bytes. hdf5-metno's derive will pick this up if the
    // struct is #[repr(C)]. We need to ensure the resulting compound type has
    // itemsize == 40. The layout is:
    //   0: toa(8) 8: cid(8) 16: tot(8) 24: dtoa(4) 28: x(2) 30: y(2) [pad2] 32: tof(8) = 40
    tof: i64,
}

fn write_pixel_hits(file: &hdf5::File, hits: &HitBatch) -> Result<()> {
    let n = hits.len();
    let mut records: Vec<PixelHitRecord> = Vec::with_capacity(n);
    for i in 0..n {
        records.push(PixelHitRecord {
            toa: hits.toa[i],
            cid: -1,
            tot: hits.tot[i],
            dtoa: -1,
            x: hits.x[i],
            y: hits.y[i],
            tof: -1,
        });
    }

    let ds = file
        .new_dataset::<PixelHitRecord>()
        .chunk([chunk_for(n)])
        .shape([n])
        .create("PixelHits")
        .context("creating PixelHits dataset")?;
    ds.write(&records).context("writing PixelHits data")?;
    Ok(())
}

// ── /TDCEvents ────────────────────────────────────────────────────────────────

/// Packed on-disk representation matching ASI's TDCEvents compound type.
/// itemsize = 16, offsets: tdc_type@0, trigger_count@2, timestamp@8.
#[repr(C)]
#[derive(hdf5::H5Type)]
struct TdcEventRecord {
    tdc_type: u8,
    _pad1: u8,
    trigger_count: u16,
    _pad2: u32,
    timestamp: u64,
}

fn write_tdc_events(file: &hdf5::File, tdcs: &TdcBatch) -> Result<()> {
    let n = tdcs.len();
    let mut records: Vec<TdcEventRecord> = Vec::with_capacity(n);
    for i in 0..n {
        records.push(TdcEventRecord {
            tdc_type: tdcs.tdc_type[i],
            _pad1: 0,
            trigger_count: tdcs.trigger_count[i],
            _pad2: 0,
            timestamp: tdcs.timestamp[i],
        });
    }

    let ds = file
        .new_dataset::<TdcEventRecord>()
        .chunk([chunk_for(n)])
        .shape([n])
        .create("TDCEvents")
        .context("creating TDCEvents dataset")?;
    ds.write(&records).context("writing TDCEvents data")?;
    Ok(())
}

// ── /ExposureTimeBoundaries ───────────────────────────────────────────────────

fn write_exposure_boundaries(file: &hdf5::File, boundaries: &[u64]) -> Result<()> {
    let n = boundaries.len();
    let ds = file
        .new_dataset::<u64>()
        .chunk([chunk_for(n)])
        .shape([n])
        .create("ExposureTimeBoundaries")
        .context("creating ExposureTimeBoundaries dataset")?;
    ds.write(boundaries).context("writing ExposureTimeBoundaries data")?;
    Ok(())
}
