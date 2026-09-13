#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Read QSFP-DD cage discovery status over PCIe (no JTAG probe required).

The DMC's qsfp_discover() packs per-cage state into a 32-bit word that the DMC
forwards to the SMC via the dmStaticInfo message; the SMC publishes it as
telemetry TAG_QSFP_STATUS. This reads that telemetry word straight out of the
SMC telemetry table over PCIe using pyluwen's axi_read32.

Usage: python3 scripts/qsfp_telem.py [--asic-id N] [--raw]
"""
import argparse
import sys

import pyluwen

# RESET_UNIT_SCRATCH_RAM_REG_ADDR(12): publishes &telemetry[0] (see status_reg.h /
# telemetry.c). Telemetry values are indexed by tag (TELEM_OFFSET(tag) == tag).
TELEMETRY_DATA_REG_ADDR = 0x80030430
TAG_QSFP_STATUS = 81
TAG_DM_APP_FW_VERSION = 26  # used only as a sanity check that the reader is aligned

CAGES = ("A", "B", "C", "D")

# CMIS SFF-8024 identifier byte -> human name (matches qsfp.c cmis_identifier_str).
CMIS_IDS = {
    0x0C: "QSFP",
    0x0D: "QSFP+",
    0x11: "QSFP28",
    0x18: "QSFP-DD (CMIS)",
    0x19: "OSFP (CMIS)",
    0x1E: "QSFP+ w/ CMIS",
    0x00: "unspecified",
    0xFF: "unspecified",
}


def read_tag(chip, tag):
    base = chip.axi_read32(TELEMETRY_DATA_REG_ADDR)
    return chip.axi_read32(base + tag * 4)


def decode_cage(byte):
    if byte == 0x00:
        return "expander absent (cage unpopulated?)"
    if byte == 0x01:
        return "expander ok, no module seated"
    if byte == 0x02:
        return "module seated, CMIS read failed"
    if byte & 0x80:
        cid = byte & 0x7F
        return f"module present, id=0x{cid:02x} ({CMIS_IDS.get(cid, 'unknown')})"
    return f"unexpected 0x{byte:02x}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asic-id", type=int, default=0, help="chip index (default 0)")
    ap.add_argument("--raw", action="store_true", help="print only the raw hex word")
    args = ap.parse_args()

    chips = pyluwen.detect_chips()
    if args.asic_id >= len(chips):
        sys.exit(f"asic-id {args.asic_id} out of range (found {len(chips)} chips)")
    chip = chips[args.asic_id]

    word = read_tag(chip, TAG_QSFP_STATUS)
    if args.raw:
        print(f"0x{word:08x}")
        return

    dm = read_tag(chip, TAG_DM_APP_FW_VERSION)
    if dm == 0:
        print(
            "warning: DM app fw version telemetry reads 0 - DMC may not have sent "
            "static info yet, or firmware predates TAG_QSFP_STATUS.",
            file=sys.stderr,
        )

    print(f"ASIC {args.asic_id}: TAG_QSFP_STATUS = 0x{word:08x}")
    for i, name in enumerate(CAGES):
        byte = (word >> (8 * i)) & 0xFF
        print(f"  QSFP {name}: {decode_cage(byte)}")


if __name__ == "__main__":
    main()
