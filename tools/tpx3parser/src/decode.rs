//! Parallel TPX3 hit and TDC decoder.
//!
//! Each section is decoded independently in parallel with rayon. The resulting
//! hit arrays are then merged and sorted by (toa, x, y) to match the row order
//! emitted by ASI tpx3dump --disable-clustering.
//!
//! # SPIDR rollover
//!
//! SPIDR is a 16-bit counter (0..65535). At 40 MHz it wraps every ~1.638 ms,
//! but paired with the 14-bit ToA this gives a combined 30-bit coarse field
//! (25 ns resolution) that wraps every ~26.8 s. Long acquisitions cross this
//! boundary. We detect wraps per-section using a large-jump threshold:
//! a decrease in coarse value larger than 2^29 ticks (roughly half the range)
//! indicates a genuine wrap rather than out-of-order data within a section.
//!
//! Note: hits within a section are NOT globally monotone (packets from the
//! same SPIDR tick can arrive in sub-tick order). We apply wrap detection using
//! consecutive packet comparison inside each section, tracking an epoch counter.
//! The section's epoch is inherited from the previous section of the same chip.

use crate::packet::Tpx3Packet;
use crate::section::Section;
use rayon::prelude::*;

const PKT: usize = 8;
/// Threshold for detecting SPIDR hit-coarse rollover: ~half the 30-bit range.
const ROLLOVER_THRESHOLD: u64 = 1 << 29;
/// Threshold for detecting TDC ts35 rollover: ~half the 35-bit range.
const TDC_ROLLOVER_THRESHOLD: u64 = 1u64 << 34;
/// One TDC ts35 epoch in 0.1 ps: 2^35 × 31250.
const TDC_EPOCH_01PS: u64 = (1u64 << 35) * 31250;

/// Structure-of-arrays for decoded pixel hits.
#[derive(Default, Debug)]
pub struct HitBatch {
    /// Time-of-arrival in 0.1 ps units (Luna native; globally monotone after sort).
    pub toa: Vec<u64>,
    pub x: Vec<u16>,
    pub y: Vec<u16>,
    /// Time-over-threshold in units of 25 ps (= raw × 25).
    pub tot: Vec<u64>,
}

impl HitBatch {
    pub fn with_capacity(n: usize) -> Self {
        Self {
            toa: Vec::with_capacity(n),
            x: Vec::with_capacity(n),
            y: Vec::with_capacity(n),
            tot: Vec::with_capacity(n),
        }
    }

    pub fn push(&mut self, toa: u64, x: u16, y: u16, tot: u64) {
        self.toa.push(toa);
        self.x.push(x);
        self.y.push(y);
        self.tot.push(tot);
    }

    pub fn len(&self) -> usize {
        self.toa.len()
    }

    pub fn is_empty(&self) -> bool {
        self.toa.is_empty()
    }

    /// Sort in-place by (toa, x, y) ascending — matches tpx3dump row order.
    pub fn sort_by_toa_x_y(&mut self) {
        let n = self.toa.len();
        let mut idx: Vec<usize> = (0..n).collect();
        idx.sort_unstable_by(|&a, &b| {
            self.toa[a]
                .cmp(&self.toa[b])
                .then(self.x[a].cmp(&self.x[b]))
                .then(self.y[a].cmp(&self.y[b]))
        });
        apply_permutation(&mut self.toa, &idx);
        apply_permutation(&mut self.x, &idx);
        apply_permutation(&mut self.y, &idx);
        apply_permutation(&mut self.tot, &idx);
    }
}

/// Structure-of-arrays for TDC events.
#[derive(Default, Debug)]
pub struct TdcBatch {
    pub tdc_type: Vec<u8>,
    pub trigger_count: Vec<u16>,
    pub timestamp: Vec<u64>,
}

impl TdcBatch {
    pub fn with_capacity(n: usize) -> Self {
        Self {
            tdc_type: Vec::with_capacity(n),
            trigger_count: Vec::with_capacity(n),
            timestamp: Vec::with_capacity(n),
        }
    }

    pub fn push(&mut self, tdc_type: u8, trigger_count: u16, timestamp: u64) {
        self.tdc_type.push(tdc_type);
        self.trigger_count.push(trigger_count);
        self.timestamp.push(timestamp);
    }

    pub fn len(&self) -> usize {
        self.tdc_type.len()
    }

    pub fn is_empty(&self) -> bool {
        self.tdc_type.is_empty()
    }
}

/// Result of decoding a single section (intra-section epoch already applied).
struct RawSectionResult {
    hits: HitBatch,
    tdcs: TdcBatch,
    /// Raw 30-bit coarse of the FIRST hit in this section (before any epoch).
    first_raw_coarse: Option<u64>,
    /// Monotone coarse at end of section: `epoch_end * 2^30 + last_raw_coarse`.
    last_adj_coarse: Option<u64>,
    /// Raw TDC ts35 of the first TDC event in this section.
    first_raw_tdc_ts35: Option<u64>,
    /// Monotone TDC ts35 at end of section: `tdc_epoch_end * 2^35 + last_raw_ts35`.
    last_adj_tdc_ts35: Option<u64>,
}

/// Decode all sections in parallel, apply SPIDR rollover epochs, sort hits.
pub fn decode_all(data: &[u8], sections: &[Section]) -> (HitBatch, TdcBatch) {
    if sections.is_empty() {
        return (HitBatch::default(), TdcBatch::default());
    }

    // Group section indices by chip_id (sequential processing per chip for epoch).
    let max_chip = sections.iter().map(|s| s.chip_id).max().unwrap_or(0) as usize;
    let mut by_chip: Vec<Vec<usize>> = vec![Vec::new(); max_chip + 1];
    for (i, s) in sections.iter().enumerate() {
        by_chip[s.chip_id as usize].push(i);
    }

    // Phase 1: decode each section in parallel (intra-section epoch applied, cross-section not yet).
    let raw_results: Vec<RawSectionResult> = sections
        .par_iter()
        .map(|sec| decode_section(data, sec))
        .collect();

    // Phase 2: sequential per-chip pass — compute cross-section epochs and apply them.
    //
    // Each section's hits have intra-section epoch embedded via decode_section.
    // Here we detect cross-section rollovers by comparing:
    //   prev_last_adj_coarse = epoch_end * 2^30 + last_raw_coarse   (from prev section)
    //   cur_first_raw_coarse = first raw coarse of current section
    //
    // If prev_last_adj_coarse > cur_first_raw_coarse + ROLLOVER_THRESHOLD, a cross-section
    // rollover occurred and we increment the cross-section epoch.
    //
    // The cross-section epoch adds `cross_epoch * 2^30 * 16 * 15625` to every hit toa.
    let mut final_hits = HitBatch::default();
    let mut final_tdcs = TdcBatch::default();

    for chip_sections in &by_chip {
        if chip_sections.is_empty() {
            continue;
        }
        let mut cross_epoch: u64 = 0;
        let mut prev_last_adj: Option<u64> = None;
        let mut cross_tdc_epoch: u64 = 0;
        let mut prev_last_tdc_adj: Option<u64> = None;

        for &sec_idx in chip_sections {
            let res = &raw_results[sec_idx];

            // Detect cross-section hit rollover at the boundary.
            if let (Some(prev_adj), Some(cur_first)) = (prev_last_adj, res.first_raw_coarse) {
                if prev_adj > cur_first + ROLLOVER_THRESHOLD {
                    cross_epoch += 1;
                }
            }
            // Detect cross-section TDC rollover at the boundary.
            if let (Some(prev_tdc), Some(cur_tdc)) = (prev_last_tdc_adj, res.first_raw_tdc_ts35) {
                if prev_tdc > cur_tdc + TDC_ROLLOVER_THRESHOLD {
                    cross_tdc_epoch += 1;
                }
            }

            let cross_epoch_add_01ps = cross_epoch * (1u64 << 30) * 16 * 15625;
            let cross_tdc_epoch_add_01ps = cross_tdc_epoch * TDC_EPOCH_01PS;

            // Add cross-section epoch on top of intra-section epoch already in toa values.
            for &t in &res.hits.toa {
                final_hits.toa.push(t + cross_epoch_add_01ps);
            }
            final_hits.x.extend_from_slice(&res.hits.x);
            final_hits.y.extend_from_slice(&res.hits.y);
            final_hits.tot.extend_from_slice(&res.hits.tot);

            final_tdcs.tdc_type.extend_from_slice(&res.tdcs.tdc_type);
            final_tdcs.trigger_count.extend_from_slice(&res.tdcs.trigger_count);
            for &ts in &res.tdcs.timestamp {
                final_tdcs.timestamp.push(ts + cross_tdc_epoch_add_01ps);
            }

            // Only update the "last seen" coarse when this section actually had hits/TDC.
            // Empty sections (0x6F/0x6A phase-only) must not reset the rollover reference,
            // otherwise a genuine rollover that spans an empty-section gap would be missed.
            if res.last_adj_coarse.is_some() {
                prev_last_adj = res.last_adj_coarse;
            }
            if res.last_adj_tdc_ts35.is_some() {
                prev_last_tdc_adj = res.last_adj_tdc_ts35;
            }
        }
    }

    final_hits.sort_by_toa_x_y();
    (final_hits, final_tdcs)
}

/// Decode a single section. Intra-section SPIDR rollover epochs are already
/// applied to the stored toa values. Cross-section epochs are applied later
/// in decode_all.
///
/// Returns:
///   - hits.toa: intra-section epoch included (relative to SPIDR=0 for this section)
///   - first_raw_coarse: first raw 30-bit coarse seen (used for cross-section wrap detection)
///   - last_adj_coarse: epoch_end * 2^30 + last_raw_coarse (monotone reference for next section)
fn decode_section(data: &[u8], section: &Section) -> RawSectionResult {
    let sec_data = &data[section.start..section.end];
    let n = sec_data.len() / PKT;
    let mut hits = HitBatch::with_capacity(n);
    let mut tdcs = TdcBatch::with_capacity(0);

    // Hit rollover tracking.
    let mut last_raw_coarse: Option<u64> = None;
    let mut first_raw_coarse: Option<u64> = None;
    let mut hit_epoch: u64 = 0;

    // TDC rollover tracking (intra-section).
    let mut last_raw_ts35: Option<u64> = None;
    let mut first_raw_tdc_ts35: Option<u64> = None;
    let mut tdc_epoch: u64 = 0;

    for i in 0..n {
        let off = i * PKT;
        let bytes: [u8; 8] = sec_data[off..off + 8].try_into().unwrap();
        let pkt = Tpx3Packet::from_le_bytes(bytes);

        if pkt.is_hit() {
            let spidr = u64::from(pkt.spidr());
            let toa14 = u64::from(pkt.toa14());
            let ftoa = u64::from(pkt.ftoa());
            let coarse = (spidr << 14) | toa14;

            // Detect SPIDR rollover within this section.
            if let Some(prev_c) = last_raw_coarse {
                if prev_c > coarse + ROLLOVER_THRESHOLD {
                    hit_epoch += 1;
                }
            } else {
                first_raw_coarse = Some(coarse);
            }
            last_raw_coarse = Some(coarse);

            let epoch_ticks = hit_epoch * (1u64 << 30);
            let toa_01ps = ((epoch_ticks + coarse) * 16 - ftoa) * 15625;
            let tot = u64::from(pkt.tot_raw()) * 25;
            let (x, y) = pkt.pixel_coordinates();
            hits.push(toa_01ps, x, y, tot);
        } else if pkt.is_tdc() {
            let ts35 = pkt.tdc_ts35();

            // Detect TDC ts35 rollover within this section.
            if let Some(prev_ts) = last_raw_ts35 {
                if prev_ts > ts35 + TDC_ROLLOVER_THRESHOLD {
                    tdc_epoch += 1;
                }
            } else {
                first_raw_tdc_ts35 = Some(ts35);
            }
            last_raw_ts35 = Some(ts35);

            let fine = u64::from(pkt.tdc_fine());
            let fine_safe = if fine == 0 { 1 } else { fine };
            let epoch_ts = tdc_epoch * TDC_EPOCH_01PS;
            let tdc_01ps = epoch_ts + ts35 * 31250 + ((fine_safe - 1) * 31250) / 12;

            tdcs.push(pkt.tdc_type_h5(), pkt.tdc_trigger_count(), tdc_01ps);
        }
    }

    let last_adj_coarse = last_raw_coarse.map(|c| hit_epoch * (1u64 << 30) + c);
    let last_adj_tdc_ts35 = last_raw_ts35.map(|ts| tdc_epoch * (1u64 << 35) + ts);

    RawSectionResult {
        hits,
        tdcs,
        first_raw_coarse,
        last_adj_coarse,
        first_raw_tdc_ts35,
        last_adj_tdc_ts35,
    }
}

/// Apply a permutation in-place without allocating.
fn apply_permutation<T: Copy>(v: &mut Vec<T>, idx: &[usize]) {
    let orig: Vec<T> = idx.iter().map(|&i| v[i]).collect();
    v.copy_from_slice(&orig);
}

/// Packet-type statistics collected during decoding (for --stats).
#[derive(Default, Debug)]
pub struct PacketStats {
    pub headers: u64,
    pub hits: u64,
    pub tdcs: u64,
    pub seq_counter: u64,
    pub other: u64,
}

impl PacketStats {
    pub fn total_packets(&self) -> u64 {
        self.headers + self.hits + self.tdcs + self.seq_counter + self.other
    }
}

/// Walk every packet in the file and count packet types.
pub fn collect_stats(data: &[u8]) -> PacketStats {
    let n = data.len() / PKT;
    let mut stats = PacketStats::default();
    for i in 0..n {
        let off = i * PKT;
        let bytes: [u8; 8] = data[off..off + 8].try_into().unwrap();
        let pkt = Tpx3Packet::from_le_bytes(bytes);
        if pkt.is_header() {
            stats.headers += 1;
        } else if pkt.is_hit() {
            stats.hits += 1;
        } else if pkt.is_tdc() {
            stats.tdcs += 1;
        } else if pkt.byte7() == 0x50 {
            stats.seq_counter += 1;
        } else {
            stats.other += 1;
        }
    }
    stats
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::section::discover_sections;

    fn make_hit_packet(spidr: u16, toa14: u16, ftoa: u8, tot: u16, x: u16, y: u16) -> u64 {
        // Reconstruct addr from (x, y) — inverse of pixel_coordinates.
        // For simplicity use addr = 0 and test hit counts instead.
        let _ = (x, y);
        let addr: u64 = 0;
        let raw = 0xB000_0000_0000_0000u64
            | (addr << 44)
            | (u64::from(tot) << 20)
            | (u64::from(ftoa) << 16)
            | (u64::from(toa14) << 30)
            | u64::from(spidr);
        raw
    }

    fn make_tdc_packet(trigger: u16, ts35: u64, fine: u8) -> u64 {
        0x6E00_0000_0000_0000u64
            | (u64::from(trigger) << 44)
            | (ts35 << 9)
            | (u64::from(fine) << 5)
    }

    fn make_hdr_seq(chip: u8, n_data_pkts: usize) -> Vec<u8> {
        let total = (2 + n_data_pkts) * 8;
        let mut v = Vec::new();
        v.extend_from_slice(
            &(crate::packet::Tpx3Packet::HEADER_MAGIC
                | (u64::from(chip) << 32)
                | ((total as u64) << 48))
                .to_le_bytes(),
        );
        v.extend_from_slice(&0x5000_0000_0000_0000u64.to_le_bytes());
        v
    }

    #[test]
    fn decode_hits_and_tdcs() {
        let mut data = make_hdr_seq(0, 3);
        data.extend_from_slice(&make_hit_packet(10, 100, 2, 5, 0, 0).to_le_bytes());
        data.extend_from_slice(&make_tdc_packet(1, 50000, 3).to_le_bytes());
        data.extend_from_slice(&make_hit_packet(20, 200, 0, 10, 0, 0).to_le_bytes());

        let sections = discover_sections(&data);
        let (hits, tdcs) = decode_all(&data, &sections);

        assert_eq!(hits.len(), 2);
        assert_eq!(tdcs.len(), 1);
        assert_eq!(tdcs.trigger_count[0], 1);
    }

    #[test]
    fn hits_sorted_by_toa() {
        let mut data = make_hdr_seq(0, 2);
        // Hit 2 has higher spidr so larger toa
        data.extend_from_slice(&make_hit_packet(20, 0, 0, 5, 0, 0).to_le_bytes());
        data.extend_from_slice(&make_hit_packet(10, 0, 0, 5, 0, 0).to_le_bytes());

        let sections = discover_sections(&data);
        let (hits, _) = decode_all(&data, &sections);
        assert_eq!(hits.len(), 2);
        assert!(hits.toa[0] <= hits.toa[1], "hits should be sorted by toa");
    }

    #[test]
    fn no_tdc_still_decodes_hits() {
        // Matches the sample21MB.tpx3 case: chip0, zero TDC packets.
        let mut data = make_hdr_seq(0, 2);
        data.extend_from_slice(&make_hit_packet(5, 50, 1, 8, 0, 0).to_le_bytes());
        data.extend_from_slice(&make_hit_packet(3, 30, 0, 4, 0, 0).to_le_bytes());
        let sections = discover_sections(&data);
        let (hits, tdcs) = decode_all(&data, &sections);
        assert_eq!(hits.len(), 2);
        assert!(tdcs.is_empty());
    }

    #[test]
    fn rollover_across_empty_section() {
        // Simulates a SPIDR rollover that spans an empty section (no hit packets).
        // Section A ends just before rollover (high coarse), empty section, section B starts just after.
        // Without the "don't reset prev_last_adj on empty section" fix, the rollover would be missed.
        let near_max: u16 = u16::MAX; // spidr near maximum
        let near_max_toa14: u16 = 0x3FFE; // just below max toa14
        let coarse_a: u64 = (u64::from(near_max) << 14) | u64::from(near_max_toa14);
        // section B starts at low coarse
        let low_spidr: u16 = 1;
        let low_toa14: u16 = 10;
        let coarse_b: u64 = (u64::from(low_spidr) << 14) | u64::from(low_toa14);
        assert!(coarse_a > coarse_b + ROLLOVER_THRESHOLD, "test setup requires a rollover");

        let mut data = Vec::new();

        // Section A: one hit near end of range
        let n_pkts_a = 1usize;
        let size_a = (1 + 1 + n_pkts_a) * 8;
        data.extend_from_slice(
            &(crate::packet::Tpx3Packet::HEADER_MAGIC
                | (u64::from(size_a as u16) << 48))
                .to_le_bytes(),
        );
        data.extend_from_slice(&0x5000_0000_0000_0000u64.to_le_bytes());
        data.extend_from_slice(
            &make_hit_packet(near_max, near_max_toa14, 0, 5, 0, 0).to_le_bytes(),
        );

        // Empty section: header + seq only, no data
        let size_empty = 2 * 8;
        data.extend_from_slice(
            &(crate::packet::Tpx3Packet::HEADER_MAGIC
                | (u64::from(size_empty as u16) << 48))
                .to_le_bytes(),
        );
        data.extend_from_slice(&0x5000_0000_0000_0001u64.to_le_bytes());

        // Section B: one hit just after rollover
        let n_pkts_b = 1usize;
        let size_b = (1 + 1 + n_pkts_b) * 8;
        data.extend_from_slice(
            &(crate::packet::Tpx3Packet::HEADER_MAGIC
                | (u64::from(size_b as u16) << 48))
                .to_le_bytes(),
        );
        data.extend_from_slice(&0x5000_0000_0000_0002u64.to_le_bytes());
        data.extend_from_slice(
            &make_hit_packet(low_spidr, low_toa14, 0, 5, 0, 0).to_le_bytes(),
        );

        let sections = discover_sections(&data);
        assert_eq!(sections.len(), 2, "empty section should be omitted");

        let (hits, _) = decode_all(&data, &sections);
        assert_eq!(hits.len(), 2);

        // Hit B must have toa > hit A due to rollover epoch
        let toa_a = (coarse_a * 16) * 15625; // no ftoa, no epoch
        let toa_b_no_epoch = (coarse_b * 16) * 15625;
        let epoch_01ps = (1u64 << 30) * 16 * 15625;
        let toa_b_with_epoch = toa_b_no_epoch + epoch_01ps;
        // After sort by (toa, x, y): B should come after A
        assert!(hits.toa[0] < hits.toa[1], "B must sort after A when rollover is detected");
        assert_eq!(hits.toa[0], toa_a, "hit A toa mismatch");
        assert_eq!(hits.toa[1], toa_b_with_epoch, "hit B toa should include rollover epoch");
    }
}
