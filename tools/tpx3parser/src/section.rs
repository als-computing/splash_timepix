//! TPX3 section discovery.
//!
//! A `.tpx3` file is partitioned into *sections*, each describing one data burst
//! from one chip. Each section begins with an 8-byte header packet whose low 32
//! bits equal `"TPX3"` (`0x33585054`), followed by an 8-byte sequence counter
//! packet (`byte7 == 0x50`).
//!
//! The header packet carries:
//!   - bits 32..39 : chip_id (u8)
//!   - bits 48..63 : section byte count (u16), including the header itself
//!
//! This module discovers section boundaries by scanning for header packets and
//! uses the embedded byte-count field to skip directly to the next header.
//! A magic-scan fallback is used when the size field is zero or inconsistent.

use crate::packet::Tpx3Packet;

const PKT: usize = 8;

/// Byte range `[start, end)` of one chip section's data packets (excluding the
/// two-packet preamble: header + sequence-counter).
#[derive(Clone, Debug)]
pub struct Section {
    /// Byte offset of the first data packet (after header + 0x50 sequence pkt).
    pub start: usize,
    /// Byte offset one past the last data packet in this section.
    pub end: usize,
    /// Chip identifier from the section header.
    pub chip_id: u8,
}

impl Section {
    #[inline]
    pub fn len(&self) -> usize {
        self.end - self.start
    }

    #[inline]
    pub fn packet_count(&self) -> usize {
        self.len() / PKT
    }
}

/// Discover all sections in a memory-mapped `.tpx3` file.
///
/// Returns sections in file order. Sections with zero data packets are omitted.
pub fn discover_sections(data: &[u8]) -> Vec<Section> {
    let n = data.len() / PKT;
    let mut sections = Vec::new();
    let mut i = 0usize;

    while i < n {
        let off = i * PKT;
        let raw = read_u64_le(data, off);
        let pkt = Tpx3Packet::new(raw);

        if !pkt.is_header() {
            i += 1;
            continue;
        }

        let chip_id = pkt.chip_id();
        let section_bytes = pkt.section_bytes() as usize;

        // Determine where data starts: after the header, and optionally after a
        // 0x50 section-sequence-counter packet. Some Luna firmware versions omit
        // the 0x50 packet (e.g. sample21MB.tpx3 has no 0x50 packets at all).
        let next_off = off + PKT;
        let has_seq = next_off + PKT <= data.len() && {
            let b = read_u64_le(data, next_off);
            (b >> 56) & 0xFF == 0x50
        };
        let data_start = if has_seq { off + 2 * PKT } else { off + PKT };
        let next_header = if section_bytes >= 2 * PKT {
            off + section_bytes
        } else {
            0 // field missing / zero — fall back to scan
        };

        // Find section end: either the byte-count field's implied next header,
        // or (fallback) the next magic header found by scanning.
        let section_end = if next_header > data_start && next_header <= data.len() {
            // Validate: the word at next_header should look like a header or EOF.
            if next_header == data.len()
                || Tpx3Packet::new(read_u64_le(data, next_header)).is_header()
            {
                next_header
            } else {
                scan_next_header(data, data_start, n)
            }
        } else {
            scan_next_header(data, data_start, n)
        };

        if section_end > data_start {
            sections.push(Section {
                start: data_start,
                end: section_end,
                chip_id,
            });
        }

        // Advance: jump past the section or just past this header if section is empty.
        let jump = if section_end > off { section_end } else { off + PKT };
        i = jump / PKT;
    }

    sections
}

/// Scan forward from `from_byte` and return the byte offset of the next section
/// header, or `data.len()` if none.
fn scan_next_header(data: &[u8], from_byte: usize, n_packets: usize) -> usize {
    let start_pkt = (from_byte + PKT - 1) / PKT;
    for j in start_pkt..n_packets {
        let off = j * PKT;
        if Tpx3Packet::new(read_u64_le(data, off)).is_header() {
            return off;
        }
    }
    data.len()
}

#[inline]
fn read_u64_le(data: &[u8], off: usize) -> u64 {
    let bytes: [u8; 8] = data[off..off + 8].try_into().unwrap();
    u64::from_le_bytes(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hdr(chip_id: u8, section_bytes: u16) -> u64 {
        Tpx3Packet::HEADER_MAGIC
            | (u64::from(chip_id) << 32)
            | (u64::from(section_bytes) << 48)
    }

    fn seq(idx: u64) -> u64 {
        0x5000_0000_0000_0000 | idx
    }

    fn hit() -> u64 {
        0xB000_0000_0000_0001
    }

    fn make_section(chip_id: u8, n_hits: usize) -> Vec<u8> {
        let total_bytes = (2 + n_hits) * PKT;
        let mut v = Vec::with_capacity(total_bytes);
        v.extend_from_slice(&hdr(chip_id, total_bytes as u16).to_le_bytes());
        v.extend_from_slice(&seq(0).to_le_bytes());
        for _ in 0..n_hits {
            v.extend_from_slice(&hit().to_le_bytes());
        }
        v
    }

    #[test]
    fn single_chip_single_section() {
        let data = make_section(0, 3);
        let sections = discover_sections(&data);
        assert_eq!(sections.len(), 1);
        assert_eq!(sections[0].chip_id, 0);
        assert_eq!(sections[0].packet_count(), 3);
    }

    #[test]
    fn two_sequential_sections() {
        let mut data = make_section(0, 4);
        data.extend_from_slice(&make_section(1, 2));
        let sections = discover_sections(&data);
        assert_eq!(sections.len(), 2);
        assert_eq!(sections[0].chip_id, 0);
        assert_eq!(sections[0].packet_count(), 4);
        assert_eq!(sections[1].chip_id, 1);
        assert_eq!(sections[1].packet_count(), 2);
    }

    #[test]
    fn empty_section_omitted() {
        let data = make_section(0, 0);
        let sections = discover_sections(&data);
        assert_eq!(sections.len(), 0);
    }

    #[test]
    fn fallback_scan_zero_size() {
        // Build header with section_bytes = 0 (forces magic scan fallback).
        let mut data = Vec::new();
        data.extend_from_slice(&hdr(0, 0).to_le_bytes()); // size=0 → scan
        data.extend_from_slice(&seq(0).to_le_bytes());
        data.extend_from_slice(&hit().to_le_bytes());
        data.extend_from_slice(&hit().to_le_bytes());
        // Second section
        let s2_bytes = 3 * PKT;
        data.extend_from_slice(&hdr(1, s2_bytes as u16).to_le_bytes());
        data.extend_from_slice(&seq(1).to_le_bytes());
        data.extend_from_slice(&hit().to_le_bytes());
        let sections = discover_sections(&data);
        assert_eq!(sections.len(), 2);
        assert_eq!(sections[0].packet_count(), 2);
        assert_eq!(sections[1].packet_count(), 1);
    }

    #[test]
    fn no_seq_counter_packet() {
        // Matches sample21MB.tpx3 format: header directly followed by hits, no 0x50.
        let n_hits = 3usize;
        let total_bytes = (1 + n_hits) * PKT; // header + hits, no seq packet
        let mut data = Vec::new();
        data.extend_from_slice(&hdr(0, total_bytes as u16).to_le_bytes());
        for _ in 0..n_hits {
            data.extend_from_slice(&hit().to_le_bytes());
        }
        let sections = discover_sections(&data);
        assert_eq!(sections.len(), 1);
        assert_eq!(sections[0].packet_count(), n_hits);
    }
}
