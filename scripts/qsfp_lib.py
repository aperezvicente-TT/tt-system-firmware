#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Shared host-side access and decoding for P150A QSFP-DD cages."""

from dataclasses import asdict, dataclass
import struct
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pyluwen


TELEMETRY_DATA_REG_ADDR = 0x80030430
TAG_DM_APP_FW_VERSION = 26
TAG_QSFP_STATUS = 81

TT_SMC_MSG_QSFP_MGMT = 0xBF
QSFP_MGMT_PAYLOAD_SIZE = 20
QSFP_MGMT_SMC_TIMEOUT_SECONDS = 5.0
PYLUWEN_TIMEOUT_SECONDS = 6.0

OP_STATUS = 0
OP_INVENTORY = 1
OP_SET_POWER = 2
OP_RESET = 3
OP_DOM_MODULE = 4
OP_DOM_LANE = 5
OP_READ_PAGE = 6

POWER_LOW = 0
POWER_HIGH = 1
MODULE_STATE_READY = 3
CAGES = "ABCD"

PAGE_LOWER = "lower"
PAGE_INDEX = {0x00: 0, 0x01: 1, 0x02: 2, 0x10: 3, 0x11: 4, PAGE_LOWER: 5}

INVENTORY_FIELDS = {
    "identifier": 0,
    "vendor_name": 1,
    "vendor_oui": 2,
    "vendor_pn": 3,
    "vendor_rev": 4,
    "vendor_sn": 5,
    "date_code": 6,
    "connector": 7,
    "media_type": 8,
    "applications": 9,
}

ERRORS = {
    0: "success",
    1: "invalid argument",
    2: "module absent",
    3: "I2C transaction failed",
    4: "operation timed out",
    5: "manager busy",
    6: "data unavailable",
    7: "module is in low power",
    8: "protocol error",
}

OP_NAMES = {
    OP_STATUS: "status",
    OP_INVENTORY: "inventory",
    OP_SET_POWER: "set power",
    OP_RESET: "reset",
    OP_DOM_MODULE: "module DOM",
    OP_DOM_LANE: "lane DOM",
    OP_READ_PAGE: "page read",
}

CMIS_IDS = {
    0x00: "unspecified",
    0x0C: "QSFP",
    0x0D: "QSFP+",
    0x11: "QSFP28",
    0x18: "QSFP-DD (CMIS)",
    0x19: "OSFP (CMIS)",
    0x1E: "QSFP+ w/ CMIS",
    0xFF: "unspecified",
}

# SFF-8024 Table 4-3 (CMIS page 00h byte 203).
CONNECTOR_TYPES = {
    0x00: "Unknown or unspecified",
    0x01: "SC",
    0x07: "LC",
    0x0B: "Optical pigtail",
    0x0C: "MPO 1x12",
    0x0D: "MPO 2x16",
    0x0E: "MPO 1x16",
    0x21: "Copper pigtail",
    0x22: "RJ45",
    0x23: "No separable connector",
    0x24: "MXC 2x16",
    0x25: "CS optical",
    0x26: "SN optical",
    0x27: "MPO 2x12",
    0x28: "MPO 1x16",
}

# CMIS page 00h byte 128 media class (not SFF-8024 Identifier).
MEDIA_TYPES = {
    0x00: "Undefined",
    0x01: "Optical MMF",
    0x02: "Optical SMF",
    0x03: "Passive copper",
    0x04: "Active cable",
    0x05: "BASE-T",
}

OPTICAL_CONNECTORS = frozenset(
    {0x01, 0x07, 0x08, 0x09, 0x0B, 0x0C, 0x0D, 0x0E, 0x24, 0x25, 0x26, 0x27, 0x28}
)
COPPER_CONNECTORS = frozenset({0x21, 0x22, 0x23})

# SFF-8024 Table 4-5, QSFP-DD-relevant subset.
HOST_INTERFACE_IDS = {
    0x00: "Undefined",
    0x0B: "CAUI-4 C2M",
    0x0C: "100GAUI-4 C2M",
    0x0D: "100GAUI-2 C2M",
    0x0E: "200GAUI-8 C2M",
    0x0F: "200GAUI-4 C2M",
    0x10: "400GAUI-16 C2M",
    0x11: "400GAUI-8 C2M",
    0x13: "10GBASE-CX4",
    0x17: "40GBASE-CR4",
    0x18: "50GBASE-CR",
    0x19: "100GBASE-CR10",
    0x1A: "100GBASE-CR4",
    0x1B: "100GBASE-CR2",
    0x1C: "200GBASE-CR4",
    0x1D: "400GBASE-CR8",
    0x1E: "200GBASE-CR1",
    0x1F: "400GBASE-CR2",
    0x2C: "IB SDR",
    0x2D: "IB DDR",
    0x2E: "IB QDR",
    0x2F: "IB FDR",
    0x30: "IB EDR",
    0x31: "IB HDR",
    0x32: "IB NDR",
    0x46: "100GBASE-CR1",
    0x47: "200GBASE-CR2",
    0x48: "400GBASE-CR4",
    0x49: "800GBASE-CR8",
    0x4B: "100GAUI-1-S C2M",
    0x4C: "100GAUI-1-L C2M",
    0x4D: "200GAUI-2-S C2M",
    0x4E: "200GAUI-2-L C2M",
    0x4F: "400GAUI-4-S C2M",
    0x50: "400GAUI-4-L C2M",
    0x51: "800GAUI-8-S C2M",
    0x52: "800GAUI-8-L C2M",
    0x57: "800GBASE-CR4",
    0xFF: "Unused",
}

# SFF-8024 Table 4-6 (MMF) and 4-7 (SMF). Codes overlap; Media Type selects.
MMF_MEDIA_IDS = {
    0x00: "Undefined",
    0x09: "100GBASE-SR4",
    0x0C: "100GBASE-SR2",
    0x0D: "100GBASE-SR1",
    0x0E: "200GBASE-SR4",
    0x10: "400GBASE-SR8",
    0x11: "400GBASE-SR4",
    0x12: "800GBASE-SR8",
    0x1A: "400GBASE-SR4.2",
    0x1D: "100GBASE-VR1",
    0x1F: "400GBASE-VR4",
    0x20: "800GBASE-VR8",
}

# SFF-8024 Table 4-8 (passive copper) and 4-9 (active cable assemblies).
PASSIVE_COPPER_MEDIA_IDS = {
    0x00: "Undefined",
    0x01: "Copper cable",
    0x02: "Passive loopback module",
    0xBF: "Passive loopback module",
}

ACTIVE_CABLE_MEDIA_IDS = {
    0x00: "Undefined",
    0x01: "Active cable, BER < 1e-12",
    0x02: "Active cable, BER < 5e-5",
    0x03: "Active cable, BER < 2.6e-4",
    0x04: "Active cable, BER < 1e-6",
    0xBF: "Active loopback module",
}

# Host electrical IDs that imply a copper cable assembly (SFF-8024 Table 4-5).
# InfiniBand and Fibre Channel rates are deliberately absent: they run over
# either medium, so they say nothing about this module.
COPPER_HOST_IDS = frozenset(
    set(range(0x13, 0x20)) | {0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x57, 0x58}
)

# Chip-to-module AUI variants, which only a powered transceiver advertises.
AUI_HOST_IDS = frozenset(
    {0x06, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11}
    | {0x41, 0x42, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F, 0x50, 0x51, 0x52, 0x55, 0x56}
)

MEDIA_CLASS_PASSIVE_COPPER = 0x03

# Flat_mem: CMIS lower byte 2 bit 7, SFF-8636 lower byte 2 bit 2.
CMIS_STATUS_FLAT_MEM = 0x80
SFF8636_STATUS_FLAT_MEM = 0x04


def effective_media_class(
    media_type: Optional[int], host_ids: Sequence[int]
) -> Optional[int]:
    """Resolve the media class to use when decoding media interface IDs.

    Many modules copy the SFF-8024 identifier into page 00h byte 128 instead of
    a CMIS media class. The advertised host electrical IDs then decide it: a
    BASE-CR rate only exists on a copper assembly, an AUI only on a transceiver.
    """
    if media_type in MEDIA_TYPES and media_type != 0x00:
        return media_type
    if any(host_id in COPPER_HOST_IDS for host_id in host_ids):
        return MEDIA_CLASS_PASSIVE_COPPER
    if any(host_id in AUI_HOST_IDS for host_id in host_ids):
        return None
    return None

SMF_MEDIA_IDS = {
    0x00: "Undefined",
    0x14: "100GBASE-DR",
    0x15: "100GBASE-FR1",
    0x16: "100GBASE-LR1",
    0x17: "200GBASE-DR4",
    0x18: "200GBASE-FR4",
    0x19: "200GBASE-LR4",
    0x1A: "400GBASE-FR8",
    0x1B: "400GBASE-LR8",
    0x1C: "400GBASE-DR4",
    0x1D: "400GBASE-FR4",
    0x1E: "400G-LR4-10",
    0x55: "400GBASE-DR4-2",
    0x56: "800GBASE-DR8",
    0x57: "800GBASE-DR8-2",
    0x7A: "800GBASE-FR4-500",
    0x7B: "800GBASE-FR4",
    0x7C: "800GBASE-LR4",
}

POWER_CLASS_NAMES = {
    0: "Power class 1",
    1: "Power class 2",
    2: "Power class 3",
    3: "Power class 4",
    4: "Power class 5",
    5: "Power class 6",
    6: "Power class 7",
    7: "Power class 8",
}

# CMIS Table 8-24, page 00h byte 212.
MEDIA_TECHNOLOGY = {
    0x00: "850 nm VCSEL",
    0x01: "1310 nm VCSEL",
    0x02: "1550 nm VCSEL",
    0x03: "1310 nm FP",
    0x04: "1310 nm DFB",
    0x05: "1550 nm DFB",
    0x06: "1310 nm EML",
    0x07: "1550 nm EML",
    0x08: "Other",
    0x09: "1490 nm DFB",
    0x0A: "Copper cable unequalized",
    0x0B: "Copper cable passive equalized",
    0x0C: "Copper cable, near and far end limiting active equalizers",
    0x0D: "Copper cable, far end limiting active equalizers",
    0x0E: "Copper cable, near end limiting active equalizers",
    0x0F: "Copper cable, linear active equalizers",
}

SFF8636_IDS = frozenset({0x0C, 0x0D, 0x11})

SFF8636_REVS = {
    0x00: "unspecified",
    0x01: "SFF-8436 Rev 4.8/4.9",
    0x03: "SFF-8636 Rev 1.3",
    0x04: "SFF-8636 Rev 1.4",
    0x05: "SFF-8636 Rev 1.5",
    0x06: "SFF-8636 Rev 2.0",
    0x07: "SFF-8636 Rev 2.5/2.6/2.7",
    0x08: "SFF-8636 Rev 2.8–2.10",
}

# SFF-8636 byte 129 bits 7-6, printed the way ethtool names them.
SFF8636_POWER_CLASS_W = {0: 1.5, 1: 2.0, 2: 2.5, 3: 3.5}

# SFF-8636 byte 147 bits 7-4.
SFF8636_DEVICE_TECH = {
    0x0: "850 nm VCSEL",
    0x1: "1310 nm VCSEL",
    0x2: "1550 nm VCSEL",
    0x3: "1310 nm FP",
    0x4: "1310 nm DFB",
    0x5: "1550 nm DFB",
    0x6: "1310 nm EML",
    0x7: "1550 nm EML",
    0x8: "Other / copper",
    0x9: "1490 nm DFB",
    0xA: "Copper cable unequalized",
    0xB: "Copper cable passive equalized",
    0xC: "Copper cable, far and near end limiting active equalizers",
    0xD: "Copper cable, far end limiting active equalizers",
    0xE: "Copper cable, near end limiting active equalizers",
    0xF: "Copper cable, linear active equalizers",
}

# SFF-8636 byte 131 bits 0-6; bit 7 defers to byte 192.
SFF8636_ETHERNET_COMPLIANCE = {
    0: "40G Active Cable (XLPPI)",
    1: "40GBASE-LR4",
    2: "40GBASE-SR4",
    3: "40GBASE-CR4",
    4: "10GBASE-SR",
    5: "10GBASE-LR",
    6: "10GBASE-LRM",
}

# SFF-8024 Table 4-4, byte 192.
SFF8636_EXTENDED_COMPLIANCE = {
    0x00: "unspecified",
    0x01: "100G AOC, BER < 5e-5",
    0x02: "100GBASE-SR4 or 25GBASE-SR",
    0x03: "100GBASE-LR4 or 25GBASE-LR",
    0x04: "100GBASE-ER4 or 25GBASE-ER",
    0x05: "100GBASE-SR10",
    0x06: "100G CWDM4",
    0x07: "100G PSM4 parallel SMF",
    0x08: "100G ACC, BER < 5e-5",
    0x0B: "100GBASE-CR4 or 25GBASE-CR CA-L",
    0x0C: "25GBASE-CR CA-S",
    0x0D: "25GBASE-CR CA-N",
    0x10: "40GBASE-ER4",
    0x11: "4x10GBASE-SR",
    0x12: "40G PSM4 parallel SMF",
    0x16: "10GBASE-T with SFI",
    0x17: "100G CLR4",
    0x18: "100G AOC, BER < 1e-12",
    0x19: "100G ACC, BER < 1e-12",
    0x1F: "40G SWDM4",
    0x20: "100G SWDM4",
    0x21: "100G PAM4 BiDi",
    0x25: "100GBASE-DR",
    0x26: "100GBASE-FR1",
    0x27: "100GBASE-LR1",
}

# SFF-8636 bytes 142-146. Byte 146 is copper length for a cable assembly; its
# optical meaning is not consistent across spec revisions, so it is only used
# for copper here.
SFF8636_LINK_LENGTHS = ((142, "SMF", 1000.0), (143, "OM3", 2.0), (144, "OM2", 1.0),
                        (145, "OM1", 1.0))

SFF8636_ATTENUATION_GHZ = (2.5, 5.0, 7.0, 12.9)
CMIS_ATTENUATION_GHZ = (5.0, 7.0, 12.9, 25.8)

MODULE_STATES = {
    0: "reserved",
    1: "LowPwr",
    2: "PwrUp",
    3: "Ready",
    4: "PwrDn",
    5: "Fault",
}

DATAPATH_STATES = {
    1: "Deactivated",
    2: "Init",
    3: "Deinit",
    4: "Activated",
    5: "TxTurnOn",
    6: "TxTurnOff",
    7: "Initialized",
}

# SFF-8636 lower memory flag bytes: one nibble per direction, lanes 1-4.
SFF8636_LANE_FLAGS = (
    (3, 0, "rx_los"),
    (3, 4, "tx_los"),
    (4, 0, "tx_fault"),
    (4, 4, "tx_adaptive_eq_fault"),
    (5, 0, "rx_cdr_lol"),
    (5, 4, "tx_cdr_lol"),
)

LANE_FLAG_BYTES = (
    (4, "tx_fault"),
    (5, "tx_los"),
    (6, "tx_cdr_lol"),
    (7, "tx_adaptive_eq_fault"),
    (8, "rx_los"),
    (9, "rx_cdr_lol"),
)


def _code_name(table: Dict[int, str], value: int) -> str:
    return table.get(value, f"unknown 0x{value:02x}")


def connector_name(value: int) -> str:
    return _code_name(CONNECTOR_TYPES, value)


def media_type_name(value: int, identifier: Optional[int] = None) -> str:
    if identifier is not None and value == identifier:
        return (
            f"SFF-8024 identifier ({_code_name(CMIS_IDS, value)}), "
            "not a CMIS media class"
        )
    return _code_name(MEDIA_TYPES, value)


def host_interface_name(value: int) -> str:
    return _code_name(HOST_INTERFACE_IDS, value)


MEDIA_ID_TABLES = {
    0x01: MMF_MEDIA_IDS,
    0x02: SMF_MEDIA_IDS,
    0x03: PASSIVE_COPPER_MEDIA_IDS,
    0x04: ACTIVE_CABLE_MEDIA_IDS,
}


def media_interface_name(media_class: Optional[int], value: int) -> str:
    """Name a Module Media Interface ID for an already-resolved media class."""
    table = MEDIA_ID_TABLES.get(media_class)
    if table is not None:
        return _code_name(table, value)
    if media_class == 0x05:
        return f"0x{value:02x}"
    if value in (0x00, 0xFF):
        return "Undefined"
    smf = SMF_MEDIA_IDS.get(value)
    mmf = MMF_MEDIA_IDS.get(value)
    if smf and mmf and smf != mmf:
        return f"SMF {smf} / MMF {mmf}"
    return smf or mmf or f"unknown 0x{value:02x}"


def module_needs_high_power(
    media_type: Optional[int],
    connector: Optional[int],
    applications: Sequence["Application"] = (),
) -> bool:
    host_ids = [application.host_id for application in applications]
    media_class = effective_media_class(media_type, host_ids)
    if media_class == MEDIA_CLASS_PASSIVE_COPPER:
        return False
    if media_class in (0x01, 0x02, 0x04):
        return True
    if connector in COPPER_CONNECTORS:
        return False
    if connector in OPTICAL_CONNECTORS:
        return True
    return any(host_id in AUI_HOST_IDS for host_id in host_ids)


def structured(value: Any) -> Any:
    """Convert result dataclasses and bytes into JSON-safe values."""
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: structured(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [structured(item) for item in value]
    return value


@dataclass(frozen=True)
class DiscoveryCage:
    cage: str
    raw: int
    state: str
    present: bool
    identifier: Optional[int] = None
    identifier_name: Optional[str] = None


@dataclass(frozen=True)
class DiscoveryResult:
    asic_id: int
    raw: int
    cages: Tuple[DiscoveryCage, ...]
    dm_app_fw_version: Optional[int] = None


@dataclass(frozen=True)
class StatusResult:
    cage: str
    status: int
    present: bool
    pins: int
    identifier: int
    identifier_name: str
    revision: int
    revision_string: str
    low_power: bool
    module_state: int
    module_state_name: str
    interrupt_asserted: bool


@dataclass(frozen=True)
class Application:
    apsel: int
    host_id: int
    media_id: int
    host_name: str
    media_name: str


@dataclass(frozen=True)
class InventoryResult:
    cage: str
    identifier: int
    identifier_name: str
    revision: int
    revision_string: str
    connector: Optional[int]
    connector_name: str
    media_type: Optional[int]
    media_type_name: str
    vendor_name: str
    vendor_oui: str
    vendor_pn: str
    vendor_rev: str
    vendor_sn: str
    date_code: str
    power_class: Optional[int]
    power_class_name: str
    max_power_w: Optional[float]
    cable_length_m: Optional[float]
    attenuation_db: Tuple[int, ...]
    media_technology: Optional[int]
    media_technology_name: str
    attenuation_ghz: Tuple[float, ...]
    spec: str
    transceiver_type: str
    applications: Tuple[Application, ...]
    needs_high_power: bool
    raw_fields: Dict[str, bytes]
    wavelength_nm: Optional[float] = None
    link_lengths: Tuple[Tuple[str, float], ...] = ()


@dataclass(frozen=True)
class ModuleDomResult:
    cage: str
    available: bool
    temperature_c: float
    voltage_v: float
    module_state: int
    module_state_name: str
    low_power: bool
    interrupt_asserted: bool


@dataclass(frozen=True)
class LaneDomResult:
    cage: str
    lane: int
    bias_ma: float
    tx_power_mw: float
    rx_power_mw: float
    tx_bias_supported: bool
    tx_power_supported: bool
    rx_power_supported: bool
    flags: int


@dataclass(frozen=True)
class LanesResult:
    cage: str
    datapath_states: Tuple[str, ...]
    flags: Dict[str, Tuple[int, ...]]
    datapath_deinit: int
    tx_disable: int
    apsel: Tuple[int, ...]
    spec: str = "CMIS"


@dataclass(frozen=True)
class PageResult:
    cage: str
    page: Any
    offset: int
    data: bytes


class QsfpError(RuntimeError):
    def __init__(self, status: int, operation: Any):
        name = OP_NAMES.get(operation, operation)
        super().__init__(f"{name}: {ERRORS.get(status, f'error {status}')}")
        self.status = status
        self.operation = operation


def detect_chip(asic_id: int):
    chips = pyluwen.detect_chips()
    if asic_id < 0 or asic_id >= len(chips):
        raise ValueError(f"asic-id {asic_id} out of range (found {len(chips)} chips)")
    return chips[asic_id]


def read_telemetry_tag(chip, tag: int) -> int:
    base = chip.axi_read32(TELEMETRY_DATA_REG_ADDR)
    return int(chip.axi_read32(base + tag * 4))


def decode_discovery_cage(cage: str, value: int) -> DiscoveryCage:
    if value == 0x00:
        return DiscoveryCage(cage, value, "expander_absent", False)
    if value == 0x01:
        return DiscoveryCage(cage, value, "no_module", False)
    if value == 0x02:
        return DiscoveryCage(cage, value, "cmis_read_failed", True)
    if value == 0x03:
        return DiscoveryCage(cage, value, "bus_stuck", False)
    if value & 0x80:
        identifier = value & 0x7F
        return DiscoveryCage(
            cage,
            value,
            "present",
            True,
            identifier,
            CMIS_IDS.get(identifier, "unknown"),
        )
    return DiscoveryCage(cage, value, "unexpected", False)


def decode_discovery_word(
    word: int, asic_id: int = 0, dm_app_fw_version: Optional[int] = None
) -> DiscoveryResult:
    cages = tuple(
        decode_discovery_cage(cage, (word >> (index * 8)) & 0xFF)
        for index, cage in enumerate(CAGES)
    )
    return DiscoveryResult(asic_id, word & 0xFFFFFFFF, cages, dm_app_fw_version)


def read_discovery(chip, asic_id: int = 0, include_dm_version: bool = True):
    word = read_telemetry_tag(chip, TAG_QSFP_STATUS)
    dm_version = (
        read_telemetry_tag(chip, TAG_DM_APP_FW_VERSION)
        if include_dm_version
        else None
    )
    return decode_discovery_word(word, asic_id, dm_version)


def _ascii(data: bytes) -> str:
    return data.decode("ascii", errors="replace").split("\x00", 1)[0].strip()


def decode_status_payload(cage: str, status: int, data: bytes) -> StatusResult:
    values = list(data[:5]) + [0] * max(0, 5 - len(data))
    identifier, revision = values[1], values[2]
    module_state = values[4]
    if identifier in SFF8636_IDS:
        revision_string = SFF8636_REVS.get(revision, f"0x{revision:02x}")
        module_state_name = "n/a"
    else:
        revision_string = f"{revision >> 4}.{revision & 0x0f}"
        module_state_name = MODULE_STATES.get(module_state, str(module_state))
    return StatusResult(
        cage=cage,
        status=status,
        present=status != 2,
        pins=values[0],
        identifier=identifier,
        identifier_name=CMIS_IDS.get(identifier, "unknown"),
        revision=revision,
        revision_string=revision_string,
        low_power=values[3] == POWER_LOW,
        module_state=module_state,
        module_state_name=module_state_name,
        interrupt_asserted=not bool(values[0] & 0x10),
    )


def decode_applications(
    data: bytes, media_type: Optional[int]
) -> Tuple[Application, ...]:
    slots = [
        (index + 1, data[2 * index], data[2 * index + 1])
        for index in range(len(data) // 2)
        if not (data[2 * index] in (0x00, 0xFF) and data[2 * index + 1] in (0x00, 0xFF))
    ]
    # The media class belongs to the module, not to one descriptor, so resolve
    # it across every advertised application before naming any of them.
    media_class = effective_media_class(media_type, [host for _, host, _ in slots])
    return tuple(
        Application(
            apsel=apsel,
            host_id=host_id,
            media_id=media_id,
            host_name=host_interface_name(host_id),
            media_name=media_interface_name(media_class, media_id),
        )
        for apsel, host_id, media_id in slots
    )


def decode_cable_length(value: int) -> Optional[float]:
    """CMIS page 00h byte 202: multiplier in bits 7-6, base meters in bits 5-0."""
    if value in (0x00, 0xFF):
        return None if value == 0x00 else 6300.0
    multipliers = {0: 0.1, 1: 1.0, 2: 10.0, 3: 100.0}
    return (value & 0x3F) * multipliers[value >> 6]


def decode_page00_advertising(page00: bytes) -> Dict[str, Any]:
    """Static fields from CMIS upper page 00h (bytes 200-212)."""
    if len(page00) < 85:
        return {}
    power_class = page00[72] >> 5
    max_raw = page00[73]
    technology = page00[84] if len(page00) > 84 else None
    attenuation = tuple(int(byte) for byte in page00[76:80])
    return {
        "power_class": power_class,
        "power_class_name": POWER_CLASS_NAMES[power_class],
        "max_power_w": max_raw * 0.25 if max_raw else None,
        "cable_length_m": decode_cable_length(page00[74]),
        "attenuation_db": attenuation if any(attenuation) else (),
        "media_technology": technology,
        "media_technology_name": (
            "unavailable"
            if technology is None
            else _code_name(MEDIA_TECHNOLOGY, technology)
        ),
    }


def _byte(fields: Dict[str, bytes], name: str) -> Optional[int]:
    data = fields.get(name) or b""
    return data[0] if data else None


def sff8636_transceiver_type(page00: bytes) -> str:
    """Byte 131 compliance bits; bit 7 means "see byte 192" (SFF-8024 4-4)."""
    if len(page00) < 4:
        return ""
    code = page00[3]
    names = [
        name for bit, name in SFF8636_ETHERNET_COMPLIANCE.items() if code & (1 << bit)
    ]
    if code & 0x80 and len(page00) > 64:
        extended = page00[64]
        names.append(
            SFF8636_EXTENDED_COMPLIANCE.get(extended, f"extended 0x{extended:02x}")
        )
    return ", ".join(names)


def decode_sff8636_inventory(
    cage: str, identifier: int, revision: int, page00: bytes
) -> InventoryResult:
    connector = page00[2] if len(page00) > 2 else None
    ext_id = page00[1] if len(page00) > 1 else 0
    power_index = (ext_id >> 6) & 0x03
    max_power_w = SFF8636_POWER_CLASS_W[power_index]
    tech_byte = page00[19] if len(page00) > 19 else 0
    tech_code = tech_byte >> 4
    copper = tech_code >= 0xA
    length = None
    attn = ()
    wavelength = None
    link_lengths = ()
    if copper:
        length = page00[18] if len(page00) > 18 and page00[18] else None
        attn = tuple(int(byte) for byte in page00[58:62]) if len(page00) >= 62 else ()
        if not any(attn):
            attn = ()
    elif len(page00) >= 60:
        # Bytes 186-187 hold the nominal wavelength in 0.05 nm units.
        wavelength = int.from_bytes(page00[58:60], "big") / 20.0 or None
        link_lengths = tuple(
            (name, page00[offset - 128] * unit)
            for offset, name, unit in SFF8636_LINK_LENGTHS
            if page00[offset - 128]
        )
    return InventoryResult(
        cage=cage,
        identifier=identifier,
        identifier_name=CMIS_IDS.get(identifier, "unknown"),
        revision=revision,
        revision_string=SFF8636_REVS.get(revision, f"0x{revision:02x}"),
        connector=connector,
        connector_name=(
            "unavailable" if connector is None else connector_name(connector)
        ),
        media_type=identifier,
        media_type_name="SFF-8636 (not CMIS)",
        vendor_name=_ascii(page00[20:36]),
        vendor_oui=":".join(f"{byte:02x}" for byte in page00[37:40]),
        vendor_pn=_ascii(page00[40:56]),
        vendor_rev=_ascii(page00[56:58]),
        vendor_sn=_ascii(page00[68:84]),
        date_code=_ascii(page00[84:92]),
        power_class=power_index,
        power_class_name=f"{max_power_w:g} W max",
        max_power_w=max_power_w,
        cable_length_m=float(length) if length else None,
        attenuation_db=attn,
        media_technology=tech_byte,
        media_technology_name=_code_name(SFF8636_DEVICE_TECH, tech_code),
        attenuation_ghz=SFF8636_ATTENUATION_GHZ,
        spec="SFF-8636",
        transceiver_type=sff8636_transceiver_type(page00),
        applications=(),
        needs_high_power=not copper,
        raw_fields={"page00": page00, "identifier": bytes([identifier, revision])},
        wavelength_nm=wavelength,
        link_lengths=link_lengths,
    )


def decode_inventory(cage: str, fields: Dict[str, bytes]) -> InventoryResult:
    identifier_data = fields["identifier"]
    identifier = identifier_data[0]
    revision = identifier_data[1]
    connector = _byte(fields, "connector")
    media_type = _byte(fields, "media_type")
    applications = decode_applications(fields.get("applications", b""), media_type)
    advertising = decode_page00_advertising(fields.get("page00", b""))
    return InventoryResult(
        cage=cage,
        identifier=identifier,
        identifier_name=CMIS_IDS.get(identifier, "unknown"),
        revision=revision,
        revision_string=f"{revision >> 4}.{revision & 0x0f}",
        connector=connector,
        connector_name=(
            "unavailable" if connector is None else connector_name(connector)
        ),
        media_type=media_type,
        media_type_name=(
            "unavailable"
            if media_type is None
            else media_type_name(media_type, identifier)
        ),
        vendor_name=_ascii(fields.get("vendor_name", b"")),
        vendor_oui=":".join(
            f"{byte:02x}" for byte in fields.get("vendor_oui", b"")
        ),
        vendor_pn=_ascii(fields.get("vendor_pn", b"")),
        vendor_rev=_ascii(fields.get("vendor_rev", b"")),
        vendor_sn=_ascii(fields.get("vendor_sn", b"")),
        date_code=_ascii(fields.get("date_code", b"")),
        power_class=advertising.get("power_class"),
        power_class_name=advertising.get("power_class_name", "unavailable"),
        max_power_w=advertising.get("max_power_w"),
        cable_length_m=advertising.get("cable_length_m"),
        attenuation_db=advertising.get("attenuation_db", ()),
        media_technology=advertising.get("media_technology"),
        media_technology_name=advertising.get(
            "media_technology_name", "unavailable"
        ),
        attenuation_ghz=CMIS_ATTENUATION_GHZ,
        spec="CMIS",
        transceiver_type="",
        applications=applications,
        needs_high_power=module_needs_high_power(
            media_type, connector, applications
        ),
        raw_fields=fields,
    )


def decode_module_dom(cage: str, status: int, data: bytes) -> ModuleDomResult:
    if len(data) != 8:
        raise QsfpError(8, "DOM module response")
    temperature, vcc, state, low_power, interrupt, _ = struct.unpack(
        "<hHBBBB", data
    )
    return ModuleDomResult(
        cage=cage,
        available=status == 0,
        temperature_c=temperature / 256.0,
        voltage_v=vcc / 10000.0,
        module_state=state,
        module_state_name=MODULE_STATES.get(state, str(state)),
        low_power=bool(low_power),
        interrupt_asserted=bool(interrupt),
    )


def decode_sff8636_module_dom(
    cage: str, lower: bytes, state: StatusResult
) -> ModuleDomResult:
    """Module monitors from SFF-8636 lower memory (CMIS puts them at 14-17)."""
    if len(lower) < 28:
        raise QsfpError(8, "lower memory response")
    temperature = int.from_bytes(lower[22:24], "big", signed=True)
    vcc = int.from_bytes(lower[26:28], "big")
    # No CMIS state machine here; byte 2 bit 0 is the one readiness bit.
    data_not_ready = bool(lower[2] & 0x01)
    return ModuleDomResult(
        cage=cage,
        available=bool(temperature or vcc),
        temperature_c=temperature / 256.0,
        voltage_v=vcc / 10000.0,
        module_state=state.module_state,
        module_state_name="Data not ready" if data_not_ready else "Ready",
        low_power=state.low_power,
        interrupt_asserted=state.interrupt_asserted,
    )


def decode_sff8636_lane_dom(
    cage: str, lane: int, lower: bytes, tx_power_supported: bool
) -> LaneDomResult:
    """Channel monitors from SFF-8636 lower memory bytes 34-57 (4 channels)."""
    if lane >= 4 or len(lower) < 58:
        raise QsfpError(6, OP_DOM_LANE)
    rx_power = int.from_bytes(lower[34 + lane * 2 : 36 + lane * 2], "big")
    bias = int.from_bytes(lower[42 + lane * 2 : 44 + lane * 2], "big")
    tx_power = int.from_bytes(lower[50 + lane * 2 : 52 + lane * 2], "big")
    return LaneDomResult(
        cage=cage,
        lane=lane,
        bias_ma=bias * 0.002,
        tx_power_mw=tx_power * 0.0001,
        rx_power_mw=rx_power * 0.0001,
        tx_bias_supported=True,
        tx_power_supported=tx_power_supported,
        rx_power_supported=True,
        flags=0x05 | (0x02 if tx_power_supported else 0x00),
    )


def decode_lane_dom(cage: str, lane: int, data: bytes) -> LaneDomResult:
    if len(data) != 8:
        raise QsfpError(8, "DOM lane response")
    bias, tx_power, rx_power, multiplier, flags = struct.unpack("<HHHBB", data)
    return LaneDomResult(
        cage=cage,
        lane=lane,
        bias_ma=bias * 0.002 * multiplier,
        tx_power_mw=tx_power * 0.0001,
        rx_power_mw=rx_power * 0.0001,
        tx_bias_supported=bool(flags & 0x01),
        tx_power_supported=bool(flags & 0x02),
        rx_power_supported=bool(flags & 0x04),
        flags=flags,
    )


def decode_lanes(cage: str, page_11: bytes, page_10: bytes) -> LanesResult:
    if len(page_11) < 10 or len(page_10) < 25:
        raise QsfpError(8, "lane page response")
    states = tuple(
        DATAPATH_STATES.get(
            (page_11[lane // 2] >> (4 * (lane % 2))) & 0x0F,
            f"0x{(page_11[lane // 2] >> (4 * (lane % 2))) & 0x0F:x}",
        )
        for lane in range(8)
    )
    flags = {
        name: tuple(lane + 1 for lane in range(8) if page_11[offset] & (1 << lane))
        for offset, name in LANE_FLAG_BYTES
    }
    return LanesResult(
        cage=cage,
        datapath_states=states,
        flags=flags,
        datapath_deinit=page_10[0],
        tx_disable=page_10[2],
        apsel=tuple(page_10[17:25]),
    )


def decode_sff8636_lanes(cage: str, lower: bytes) -> LanesResult:
    """Lane flags from SFF-8636 lower memory; there is no datapath state machine."""
    if len(lower) < 87:
        raise QsfpError(8, "lower memory response")
    flags = {
        name: tuple(
            lane + 1 for lane in range(4) if lower[offset] & (1 << (shift + lane))
        )
        for offset, shift, name in SFF8636_LANE_FLAGS
    }
    return LanesResult(
        cage=cage,
        datapath_states=(),
        flags=flags,
        datapath_deinit=0,
        tx_disable=lower[86] & 0x0F,
        apsel=(),
        spec="SFF-8636",
    )


def decode_page(cage: str, page, data: bytes) -> PageResult:
    offset = 0 if page == PAGE_LOWER else 128
    return PageResult(cage=cage, page=page, offset=offset, data=data)


class QsfpManager:
    def __init__(self, chip):
        self.chip = chip
        self._media_class: Dict[str, Optional[int]] = {}
        self._lower_head: Dict[str, bytes] = {}

    @staticmethod
    def _cage_index(cage: str) -> int:
        try:
            return CAGES.index(cage.upper())
        except (AttributeError, ValueError) as error:
            raise QsfpError(1, "cage") from error

    def request(
        self,
        operation: int,
        cage: str,
        argument: int = 0,
        allow: Sequence[int] = (),
    ) -> Tuple[int, bytes]:
        cage_index = self._cage_index(cage)
        if not 0 <= operation <= OP_READ_PAGE or not 0 <= argument <= 0xFF:
            raise QsfpError(1, operation)
        packed = operation | (cage_index << 8) | (argument << 16)
        words = self.chip.as_bh().arc_msg_buf(
            [TT_SMC_MSG_QSFP_MGMT, packed, 0, 0, 0, 0, 0, 0],
            timeout=PYLUWEN_TIMEOUT_SECONDS,
        )
        if len(words) < 8:
            raise QsfpError(8, "response length")
        status = int(words[0])
        raw = struct.pack("<7I", *(int(word) for word in words[1:8]))
        if status and not any(raw):
            raise QsfpError(status, operation)
        token, response_op, response_cage, wire_status, length = struct.unpack_from(
            "<BBBBB", raw
        )
        if (
            token == 0
            or response_op != operation
            or response_cage != cage_index
            or wire_status != status
            or length > QSFP_MGMT_PAYLOAD_SIZE
        ):
            raise QsfpError(8, "response")
        if status and status not in allow:
            raise QsfpError(status, operation)
        return status, raw[8 : 8 + length]

    def status(self, cage: str) -> StatusResult:
        status, data = self.request(OP_STATUS, cage, allow=(2,))
        return decode_status_payload(cage.upper(), status, data)

    def inventory(self, cage: str) -> InventoryResult:
        status, ident = self.request(
            OP_INVENTORY, cage, INVENTORY_FIELDS["identifier"], allow=(1, 3, 6)
        )
        if status != 0 or len(ident) < 2:
            raise QsfpError(3, OP_INVENTORY)
        if ident[0] in SFF8636_IDS:
            page00 = self.read_page(cage, 0x00, 92)
            return decode_sff8636_inventory(cage.upper(), ident[0], ident[1], page00)
        fields = {"identifier": ident}
        for name, selector in INVENTORY_FIELDS.items():
            if name == "identifier":
                continue
            status, data = self.request(OP_INVENTORY, cage, selector, allow=(1, 3, 6))
            if status == 0:
                fields[name] = data
        try:
            fields["page00"] = self.read_page(cage, 0x00, 85)
        except QsfpError:
            pass
        return decode_inventory(cage.upper(), fields)

    def media_class(self, cage: str) -> Optional[int]:
        """Resolve the module's media class from the two cheapest fields."""
        key = cage.upper()
        if key not in self._media_class:
            status, ident = self.request(
                OP_INVENTORY, cage, INVENTORY_FIELDS["identifier"], allow=(1, 3, 6)
            )
            if status == 0 and ident and ident[0] in SFF8636_IDS:
                try:
                    page00 = self.read_page(cage, 0x00, 20)
                    tech = page00[19] >> 4 if len(page00) > 19 else 0
                except QsfpError:
                    tech = 0xA
                self._media_class[key] = (
                    MEDIA_CLASS_PASSIVE_COPPER if tech >= 0xA else None
                )
                return self._media_class[key]
            media_type = None
            host_ids: List[int] = []
            status, data = self.request(
                OP_INVENTORY, cage, INVENTORY_FIELDS["media_type"], allow=(1, 3, 6)
            )
            if status == 0 and data:
                media_type = data[0]
            status, data = self.request(
                OP_INVENTORY, cage, INVENTORY_FIELDS["applications"], allow=(1, 3, 6)
            )
            if status == 0:
                host_ids = [
                    application.host_id
                    for application in decode_applications(data, media_type)
                ]
            self._media_class[key] = effective_media_class(media_type, host_ids)
        return self._media_class[key]

    def set_power(self, cage: str, mode: str) -> None:
        if mode not in ("low", "high"):
            raise QsfpError(1, OP_SET_POWER)
        self.request(OP_SET_POWER, cage, POWER_LOW if mode == "low" else POWER_HIGH)

    def reset(self, cage: str) -> None:
        self.request(OP_RESET, cage)

    def sff8636(self, cage: str) -> bool:
        head = self.lower_head(cage)
        return bool(head) and head[0] in SFF8636_IDS

    def dom_module(self, cage: str) -> ModuleDomResult:
        if self.sff8636(cage):
            return decode_sff8636_module_dom(
                cage.upper(), self.read_page(cage, PAGE_LOWER), self.status(cage)
            )
        status, data = self.request(OP_DOM_MODULE, cage, allow=(6,))
        return decode_module_dom(cage.upper(), status, data)

    def dom_lane(self, cage: str, lane: int) -> LaneDomResult:
        if not 0 <= lane < 8:
            raise QsfpError(1, OP_DOM_LANE)
        _, data = self.request(OP_DOM_LANE, cage, lane)
        return decode_lane_dom(cage.upper(), lane, data)

    def dom(self, cage: str) -> Tuple[ModuleDomResult, Tuple[LaneDomResult, ...]]:
        # SFF-8636 keeps every monitor in lower memory; CMIS uses page 11h.
        if self.sff8636(cage):
            lower = self.read_page(cage, PAGE_LOWER)
            module = decode_sff8636_module_dom(cage.upper(), lower, self.status(cage))
            if self.media_class(cage) == MEDIA_CLASS_PASSIVE_COPPER:
                return module, ()
            page00 = self.read_page(cage, 0x00, 93)
            # Page 00h byte 220 bit 2: transmitter power measurement implemented.
            tx_power = bool(len(page00) > 92 and page00[92] & 0x04)
            return module, tuple(
                decode_sff8636_lane_dom(cage.upper(), lane, lower, tx_power)
                for lane in range(4)
            )
        module = self.dom_module(cage)
        lanes = ()
        if not module.low_power and self.media_class(cage) != MEDIA_CLASS_PASSIVE_COPPER:
            lanes = tuple(self.dom_lane(cage, lane) for lane in range(8))
        return module, lanes

    def lower_head(self, cage: str) -> bytes:
        """First block of lower memory: identifier, revision, status."""
        key = cage.upper()
        if key not in self._lower_head:
            status, data = self.request(
                OP_READ_PAGE,
                cage,
                PAGE_INDEX[PAGE_LOWER] << 4,
                allow=(1, 2, 3, 6),
            )
            self._lower_head[key] = data if status == 0 else b""
        return self._lower_head[key]

    def flat_mem(self, cage: str) -> bool:
        """Module implements page 00h and nothing else.

        The bit moved between specs: CMIS puts Flat_mem in lower byte 2 bit 7,
        SFF-8636 in bit 2. Neither spec makes a module set it, and the QSFP28
        DACs here leave byte 2 clear.
        """
        head = self.lower_head(cage)
        if len(head) < 3:
            return False
        mask = (
            SFF8636_STATUS_FLAT_MEM
            if head[0] in SFF8636_IDS
            else CMIS_STATUS_FLAT_MEM
        )
        return bool(head[2] & mask)

    def read_page(self, cage: str, page, length: int = 128) -> bytes:
        if page not in PAGE_INDEX or not 0 <= length <= 128:
            raise QsfpError(1, OP_READ_PAGE)
        if page in (0x10, 0x11):
            head = self.lower_head(cage)
            if head and head[0] in SFF8636_IDS:
                raise QsfpError(
                    6, f"page {page:02x}h (SFF-8636 module has no CMIS lane pages)"
                )
        if page not in (0x00, PAGE_LOWER) and self.flat_mem(cage):
            raise QsfpError(
                6, f"page {page:02x}h (flat-memory module implements page 00h only)"
            )
        data = bytearray()
        for block in range((length + QSFP_MGMT_PAYLOAD_SIZE - 1) // QSFP_MGMT_PAYLOAD_SIZE):
            _, chunk = self.request(
                OP_READ_PAGE, cage, (PAGE_INDEX[page] << 4) | block
            )
            data.extend(chunk)
        return bytes(data[:length])

    def page(self, cage: str, page, length: int = 128) -> PageResult:
        return decode_page(cage.upper(), page, self.read_page(cage, page, length))

    def lanes(self, cage: str) -> LanesResult:
        if self.sff8636(cage):
            return decode_sff8636_lanes(cage.upper(), self.read_page(cage, PAGE_LOWER))
        page_11 = self.read_page(cage, 0x11, 16)
        # Page-relative bytes 17..24 are CMIS absolute offsets 145..152.
        page_10 = self.read_page(cage, 0x10, 25)
        return decode_lanes(cage.upper(), page_11, page_10)

    def wait_ready(self, cage: str, timeout: float) -> StatusResult:
        deadline = time.monotonic() + timeout
        while True:
            state = self.status(cage)
            if state.identifier in SFF8636_IDS:
                return state
            if state.module_state == MODULE_STATE_READY:
                return state
            if time.monotonic() >= deadline:
                return state
            time.sleep(0.25)
