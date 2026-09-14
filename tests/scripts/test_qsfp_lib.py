# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import json
import struct
import sys
from types import SimpleNamespace

import pytest

import qsfp
import qsfp_lib
from qsfp_lib import (
    CAGES,
    INVENTORY_FIELDS,
    LANE_FLAG_BYTES,
    OP_INVENTORY,
    OP_READ_PAGE,
    PAGE_INDEX,
    PAGE_LOWER,
    QSFP_MGMT_PAYLOAD_SIZE,
    TAG_DM_APP_FW_VERSION,
    TAG_QSFP_STATUS,
    TELEMETRY_DATA_REG_ADDR,
    TT_SMC_MSG_QSFP_MGMT,
    DiscoveryCage,
    InventoryResult,
    QsfpError,
    QsfpManager,
    decode_applications,
    decode_cable_length,
    decode_discovery_cage,
    decode_discovery_word,
    decode_inventory,
    decode_lane_dom,
    decode_lanes,
    decode_module_dom,
    decode_page,
    decode_page00_advertising,
    decode_status_payload,
    detect_chip,
    media_type_name,
    module_needs_high_power,
    read_discovery,
    structured,
)


def _response_words(status, operation, cage_index, payload, token=1):
    header = struct.pack(
        "<BBBBB", token, operation, cage_index, status, len(payload)
    )
    raw = (header + bytes(3) + payload).ljust(28, b"\x00")
    return [status, *struct.unpack("<7I", raw)]


class FakeBh:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def arc_msg_buf(self, words, timeout=None):
        self.calls.append((list(words), timeout))
        return self.handler(words, timeout)


class FakeChip:
    def __init__(self, telemetry=None, handler=None):
        self.telemetry_base = 0x2000
        self.telemetry = telemetry or {}
        self.bh = FakeBh(handler or (lambda words, timeout: [0] * 8))

    def axi_read32(self, addr):
        if addr == TELEMETRY_DATA_REG_ADDR:
            return self.telemetry_base
        return int(self.telemetry.get((addr - self.telemetry_base) // 4, 0))

    def as_bh(self):
        return self.bh


def test_discovery_cage_codes():
    assert decode_discovery_cage("A", 0x00) == DiscoveryCage(
        "A", 0x00, "expander_absent", False
    )
    assert decode_discovery_cage("B", 0x01) == DiscoveryCage(
        "B", 0x01, "no_module", False
    )
    failed = decode_discovery_cage("C", 0x02)
    assert failed.state == "cmis_read_failed" and failed.present is True
    assert decode_discovery_cage("D", 0x03).state == "bus_stuck"
    present = decode_discovery_cage("A", 0x98)
    assert present == DiscoveryCage(
        "A", 0x98, "present", True, 0x18, "QSFP-DD (CMIS)"
    )
    unexpected = decode_discovery_cage("B", 0x04)
    assert unexpected.state == "unexpected" and unexpected.present is False


def test_discovery_word_byte_order_is_cage_a_in_lsb():
    word = 0x00020198
    result = decode_discovery_word(word, asic_id=3, dm_app_fw_version=0x11)
    assert result.asic_id == 3
    assert result.raw == word
    assert result.dm_app_fw_version == 0x11
    assert [cage.cage for cage in result.cages] == list(CAGES)
    assert result.cages[0].state == "present"
    assert result.cages[0].identifier == 0x18
    assert result.cages[1].state == "no_module"
    assert result.cages[2].state == "cmis_read_failed"
    assert result.cages[3].state == "expander_absent"

    swapped = decode_discovery_word(0x98010200)
    assert swapped.cages[0].state == "expander_absent"
    assert swapped.cages[3].state == "present"


def test_read_discovery_uses_telemetry_tags():
    chip = FakeChip(
        telemetry={TAG_QSFP_STATUS: 0x03020100, TAG_DM_APP_FW_VERSION: 42}
    )
    result = read_discovery(chip, asic_id=1)
    assert result.asic_id == 1
    assert result.raw == 0x03020100
    assert result.dm_app_fw_version == 42
    assert result.cages[0].state == "expander_absent"
    assert result.cages[3].state == "bus_stuck"


def test_status_decode_present_and_absent():
    absent = decode_status_payload("a", 2, b"")
    assert absent.cage == "a"
    assert absent.present is False
    assert absent.status == 2

    pins = 0x00
    payload = bytes([pins, 0x18, 0x50, 1, 3])
    present = decode_status_payload("A", 0, payload)
    assert present.present is True
    assert present.identifier == 0x18
    assert present.identifier_name == "QSFP-DD (CMIS)"
    assert present.revision_string == "5.0"
    assert present.low_power is False
    assert present.module_state_name == "Ready"
    assert present.interrupt_asserted is True

    deasserted = decode_status_payload("A", 0, bytes([0x10, 0x18, 0x40, 0, 1]))
    assert deasserted.interrupt_asserted is False
    assert deasserted.low_power is True
    assert deasserted.module_state_name == "LowPwr"

    qsfp28 = decode_status_payload("A", 0, bytes([0x10, 0x11, 0x07, 0, 0x06]))
    assert qsfp28.identifier_name == "QSFP28"
    assert qsfp28.revision_string == "SFF-8636 Rev 2.5/2.6/2.7"
    assert qsfp28.module_state_name == "n/a"


def test_module_and_lane_dom_scaling():
    temperature = int(25.5 * 256)
    vcc = 33000
    module_payload = struct.pack("<hHBBBB", temperature, vcc, 3, 0, 1, 0)
    module = decode_module_dom("A", 0, module_payload)
    assert module.available is True
    assert module.temperature_c == pytest.approx(25.5)
    assert module.voltage_v == pytest.approx(3.3)
    assert module.module_state_name == "Ready"
    assert module.low_power is False
    assert module.interrupt_asserted is True

    unavailable = decode_module_dom("B", 6, module_payload)
    assert unavailable.available is False

    with pytest.raises(QsfpError):
        decode_module_dom("A", 0, b"short")

    lane_payload = struct.pack("<HHHBB", 500, 10000, 2500, 2, 0x07)
    lane = decode_lane_dom("A", 3, lane_payload)
    assert lane.lane == 3
    assert lane.bias_ma == pytest.approx(2.0)
    assert lane.tx_power_mw == pytest.approx(1.0)
    assert lane.rx_power_mw == pytest.approx(0.25)
    assert lane.tx_bias_supported
    assert lane.tx_power_supported
    assert lane.rx_power_supported

    with pytest.raises(QsfpError):
        decode_lane_dom("A", 0, b"\x00" * 7)


def test_read_page_reassembles_128_bytes_from_20_byte_chunks():
    page = bytes(range(128))
    cage_index = CAGES.index("B")

    def handler(words, timeout):
        assert words[0] == TT_SMC_MSG_QSFP_MGMT
        packed = int(words[1])
        operation = packed & 0xFF
        request_cage = (packed >> 8) & 0xFF
        argument = (packed >> 16) & 0xFF
        assert operation == OP_READ_PAGE
        assert request_cage == cage_index
        assert (argument >> 4) == PAGE_INDEX[0x00]
        block = argument & 0x0F
        start = block * QSFP_MGMT_PAYLOAD_SIZE
        chunk = page[start : start + QSFP_MGMT_PAYLOAD_SIZE]
        return _response_words(0, operation, request_cage, chunk)

    manager = QsfpManager(FakeChip(handler=handler))
    data = manager.read_page("b", 0x00, 128)
    assert data == page
    assert len(manager.chip.bh.calls) == 7
    arguments = [(call[0][1] >> 16) & 0xFF for call in manager.chip.bh.calls]
    assert arguments == [(PAGE_INDEX[0x00] << 4) | block for block in range(7)]
    last_payload_len = 128 - 6 * QSFP_MGMT_PAYLOAD_SIZE
    assert last_payload_len == 8


def test_decode_lanes_page10_page11_and_all_apsel_bytes():
    page_11 = bytearray(16)
    page_11[0] = 0x41
    page_11[1] = 0x32
    page_11[2] = 0x65
    page_11[3] = 0x47
    page_11[4] = 0b0000_0101
    page_11[5] = 0b0000_0010
    page_11[6] = 0b1000_0000
    page_11[7] = 0b0001_0000
    page_11[8] = 0b0000_1000
    page_11[9] = 0b0100_0000

    page_10 = bytearray(25)
    page_10[0] = 0xAA
    page_10[2] = 0x55
    apsel = bytes(range(0xA0, 0xA8))
    page_10[17:25] = apsel

    result = decode_lanes("C", bytes(page_11), bytes(page_10))
    assert result.cage == "C"
    assert result.datapath_states == (
        "Deactivated",
        "Activated",
        "Init",
        "Deinit",
        "TxTurnOn",
        "TxTurnOff",
        "Initialized",
        "Activated",
    )
    assert result.flags["tx_fault"] == (1, 3)
    assert result.flags["tx_los"] == (2,)
    assert result.flags["tx_cdr_lol"] == (8,)
    assert result.flags["tx_adaptive_eq_fault"] == (5,)
    assert result.flags["rx_los"] == (4,)
    assert result.flags["rx_cdr_lol"] == (7,)
    assert result.datapath_deinit == 0xAA
    assert result.tx_disable == 0x55
    assert result.apsel == tuple(apsel)
    assert len(result.apsel) == 8
    assert [name for _, name in LANE_FLAG_BYTES] == list(result.flags)

    with pytest.raises(QsfpError):
        decode_lanes("A", b"\x00" * 9, bytes(page_10))
    with pytest.raises(QsfpError):
        decode_lanes("A", bytes(page_11), b"\x00" * 24)


def _page_handler(pages, seen=None):
    def handler(words, timeout):
        packed = int(words[1])
        argument = (packed >> 16) & 0xFF
        page_index = argument >> 4
        block = argument & 0x0F
        if seen is not None:
            seen.append((page_index, block))
        source = pages.get(page_index, bytes(128))
        start = block * QSFP_MGMT_PAYLOAD_SIZE
        chunk = source[start : start + QSFP_MGMT_PAYLOAD_SIZE]
        return _response_words(0, OP_READ_PAGE, (packed >> 8) & 0xFF, chunk)

    return handler


def test_manager_lanes_reads_page11_then_page10():
    page_11 = bytes([0x44] * 16)
    page_10 = bytes([0] * 17) + bytes(range(8))
    lower = bytes([0x18, 0x50, 0x00]) + bytes(125)
    seen = []
    handler = _page_handler(
        {
            PAGE_INDEX[0x11]: page_11,
            PAGE_INDEX[0x10]: page_10,
            PAGE_INDEX[PAGE_LOWER]: lower,
        },
        seen,
    )

    result = QsfpManager(FakeChip(handler=handler)).lanes("A")
    lane_pages = [item[0] for item in seen if item[0] != PAGE_INDEX[PAGE_LOWER]]
    assert lane_pages[0] == PAGE_INDEX[0x11]
    assert PAGE_INDEX[0x10] in lane_pages[1:]
    assert result.apsel == tuple(range(8))
    assert result.datapath_states == ("Activated",) * 8


def test_lane_pages_are_refused_without_a_bus_transaction():
    # A CMIS flat-memory module (lower byte 2 bit 7) and any SFF-8636 module
    # have no pages 10h/11h at all; saying "I2C failed" would blame the bus.
    flat_cmis = bytes([0x18, 0x40, 0x80]) + bytes(125)
    manager = QsfpManager(
        FakeChip(handler=_page_handler({PAGE_INDEX[PAGE_LOWER]: flat_cmis}))
    )
    with pytest.raises(QsfpError) as raised:
        manager.lanes("B")
    assert raised.value.status == 6
    assert "flat-memory" in str(raised.value)

    lower = bytearray(128)
    lower[0:3] = bytes([0x11, 0x07, 0x00])
    lower[3] = 0x03
    lower[5] = 0xFF
    manager = QsfpManager(
        FakeChip(handler=_page_handler({PAGE_INDEX[PAGE_LOWER]: bytes(lower)}))
    )
    with pytest.raises(QsfpError) as raised:
        manager.read_page("A", 0x11)
    assert "SFF-8636" in str(raised.value)
    assert manager.flat_mem("A") is False

    # The same flags do exist in SFF-8636 lower memory, so lanes reads those.
    result = manager.lanes("A")
    assert result.spec == "SFF-8636"
    assert result.datapath_states == ()
    assert result.flags["rx_los"] == (1, 2)
    assert result.flags["tx_los"] == ()
    assert result.flags["rx_cdr_lol"] == (1, 2, 3, 4)
    assert result.flags["tx_cdr_lol"] == (1, 2, 3, 4)


def test_structured_converts_bytes_and_dataclasses_to_json_safe_values():
    fields = {
        "identifier": bytes([0x18, 0x50]),
        "vendor_name": b"ACME",
        "vendor_oui": bytes([0x00, 0x11, 0x22]),
        "vendor_pn": b"PN",
        "vendor_rev": b"A1",
        "vendor_sn": b"SN",
        "date_code": b"260101",
        "connector": bytes([0x07]),
        "media_type": bytes([0x01]),
    }
    inventory = decode_inventory("A", fields)
    converted = structured(inventory)
    json.dumps(converted)
    assert inventory.connector_name == "LC"
    assert inventory.media_type_name == "Optical MMF"
    assert inventory.needs_high_power is True
    assert converted["vendor_oui"] == "00:11:22"
    assert converted["raw_fields"]["identifier"] == "1850"
    assert converted["raw_fields"]["vendor_name"] == b"ACME".hex()
    assert isinstance(inventory, InventoryResult)
    assert structured(b"\xde\xad") == "dead"
    assert structured(("x", b"\x01")) == ["x", "01"]


def test_detect_chip_indexes_pyluwen_list(monkeypatch):
    chips = ["chip0", "chip1"]
    monkeypatch.setattr(qsfp_lib.pyluwen, "detect_chips", lambda: chips)
    assert detect_chip(1) == "chip1"
    with pytest.raises(ValueError):
        detect_chip(2)


def test_cli_json_discover_and_status(monkeypatch, capsys):
    discovery = decode_discovery_word(0x00020198, asic_id=0)
    monkeypatch.setattr(qsfp, "detect_chip", lambda asic_id: SimpleNamespace())
    monkeypatch.setattr(qsfp, "read_discovery", lambda chip, asic_id: discovery)

    qsfp.main(["discover", "--json"])
    discover_payload = json.loads(capsys.readouterr().out)
    assert discover_payload["raw"] == 0x00020198
    assert discover_payload["cages"][0]["state"] == "present"
    assert discover_payload["cages"][0]["identifier"] == 0x18

    absent = decode_status_payload("A", 2, b"")

    class FakeManager:
        def __init__(self, chip):
            self.chip = chip

        def status(self, cage):
            assert cage == "A"
            return absent

    monkeypatch.setattr(qsfp, "QsfpManager", FakeManager)
    qsfp.main(["status", "A", "--json"])
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["cage"] == "A"
    assert status_payload["present"] is False
    assert status_payload["status"] == 2


def test_cli_json_dom(monkeypatch, capsys):
    module_payload = struct.pack("<hHBBBB", int(10 * 256), 12000, 3, 0, 0, 0)
    lane_payload = struct.pack("<HHHBB", 0, 0, 0, 1, 0)

    class FakeManager:
        def __init__(self, chip):
            pass

        def dom(self, cage):
            return (
                decode_module_dom(cage, 0, module_payload),
                (decode_lane_dom(cage, 0, lane_payload),),
            )

    monkeypatch.setattr(qsfp, "detect_chip", lambda asic_id: object())
    monkeypatch.setattr(qsfp, "QsfpManager", FakeManager)
    qsfp.main(["dom", "A", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["module"]["temperature_c"] == pytest.approx(10.0)
    assert payload["lanes"][0]["lane"] == 0


def test_pyluwen_stub_is_importable_when_missing():
    assert "pyluwen" in sys.modules
    assert hasattr(qsfp_lib, "pyluwen")


def test_connector_and_media_type_names():
    fields = {
        "identifier": bytes([0x18, 0x40]),
        "vendor_name": b"CISCO",
        "vendor_oui": bytes([0x00, 0x00, 0x00]),
        "vendor_pn": b"T-DH8CNT-NCI",
        "vendor_rev": b"A",
        "vendor_sn": b"INL1",
        "date_code": b"260101",
        "connector": bytes([0x0C]),
        "media_type": bytes([0x18]),
        "applications": bytes([0x51, 0x1D, 0x00, 0x00]),
    }
    info = decode_inventory("A", fields)
    assert info.connector_name == "MPO 1x12"
    assert "identifier" in info.media_type_name
    assert info.needs_high_power is True
    assert info.applications[0].apsel == 1
    assert info.applications[0].host_name == "800GAUI-8-S C2M"
    assert "400GBASE-FR4" in info.applications[0].media_name
    assert media_type_name(0x02) == "Optical SMF"
    assert module_needs_high_power(0x03, 0x0C) is False
    assert module_needs_high_power(0x18, 0x23) is False


def test_decode_applications_skips_empty_slots():
    apps = decode_applications(bytes([0x49, 0x01, 0xFF, 0xFF, 0x00, 0x00]), 0x03)
    assert len(apps) == 1
    assert apps[0].host_name == "800GBASE-CR8"
    assert apps[0].media_name == "Copper cable"


def test_page00_advertising_length_power_and_attenuation():
    assert decode_cable_length(0x00) is None
    assert decode_cable_length(0x0F) == pytest.approx(1.5)
    assert decode_cable_length(0x45) == pytest.approx(5.0)
    assert decode_cable_length(0xFF) == pytest.approx(6300.0)

    page00 = bytearray(85)
    page00[72] = 0xE0
    page00[73] = 68
    page00[74] = 0x0F
    page00[76:80] = bytes([4, 4, 6, 12])
    page00[84] = 0x0A
    advertising = decode_page00_advertising(bytes(page00))
    assert advertising["power_class"] == 7
    assert advertising["power_class_name"] == "Power class 8"
    assert advertising["max_power_w"] == pytest.approx(17.0)
    assert advertising["cable_length_m"] == pytest.approx(1.5)
    assert advertising["attenuation_db"] == (4, 4, 6, 12)
    assert advertising["media_technology_name"] == "Copper cable unequalized"

    fields = {
        "identifier": bytes([0x1E, 0x50]),
        "vendor_name": b"FS",
        "vendor_oui": bytes([0x64, 0x9D, 0x99]),
        "vendor_pn": b"QSFP-400G-PC015",
        "vendor_rev": b"A0",
        "vendor_sn": b"SN",
        "date_code": b"260312",
        "connector": bytes([0x23]),
        "media_type": bytes([0x1E]),
        "applications": bytes([0x48, 0x01]),
        "page00": bytes(page00),
    }
    info = decode_inventory("C", fields)
    assert info.spec == "CMIS"
    assert info.attenuation_ghz == qsfp_lib.CMIS_ATTENUATION_GHZ
    assert info.cable_length_m == pytest.approx(1.5)
    assert info.max_power_w == pytest.approx(17.0)
    assert info.attenuation_db == (4, 4, 6, 12)
    apps = decode_applications(bytes([0x49, 0x01, 0xFF, 0xFF, 0x00, 0x00]), 0x03)
    assert len(apps) == 1
    assert apps[0].host_name == "800GBASE-CR8"
    assert apps[0].media_name == "Copper cable"


def test_sff8636_qsfp28_dac_matches_ethtool_layout():
    page00 = bytearray(92)
    page00[1] = 0x00
    page00[2] = 0x23
    page00[3] = 0x80
    page00[18] = 2
    page00[19] = 0xA0
    page00[20:23] = b"CCI"
    page00[37:40] = bytes([0x6C, 0xDD, 0xEF])
    page00[40:53] = b"187-D014-1.3M"
    page00[58:62] = bytes([5, 7, 8, 15])
    page00[64] = 0x0B
    page00[68:81] = b"CCI-223190267"
    page00[84:90] = b"221122"
    info = qsfp_lib.decode_sff8636_inventory("A", 0x11, 0x07, bytes(page00))
    assert info.spec == "SFF-8636"
    assert info.identifier_name == "QSFP28"
    assert info.revision_string == "SFF-8636 Rev 2.5/2.6/2.7"
    assert info.connector_name == "No separable connector"
    assert info.vendor_name == "CCI"
    assert info.vendor_oui == "6c:dd:ef"
    assert info.vendor_pn == "187-D014-1.3M"
    assert info.vendor_sn == "CCI-223190267"
    assert info.date_code == "221122"
    assert info.max_power_w == pytest.approx(1.5)
    assert info.cable_length_m == pytest.approx(2.0)
    assert info.attenuation_db == (5, 7, 8, 15)
    assert info.attenuation_ghz == qsfp_lib.SFF8636_ATTENUATION_GHZ
    assert info.media_technology_name == "Copper cable unequalized"
    assert info.transceiver_type == "100GBASE-CR4 or 25GBASE-CR CA-L"
    assert info.needs_high_power is False
    assert info.applications == ()
    assert info.wavelength_nm is None
    assert info.link_lengths == ()


def test_sff8636_optic_reports_wavelength_and_reach_not_copper_fields():
    # Bytes 186-189 are attenuation on a DAC but wavelength on a transceiver,
    # and byte 146 is a copper length only on a cable assembly.
    page00 = bytearray(92)
    page00[1] = 0xDC
    page00[2] = 0x0C
    page00[3] = 0x80
    page00[15] = 35
    page00[18] = 50
    page00[19] = 0x00
    page00[58:60] = (850 * 20).to_bytes(2, "big")
    page00[64] = 0x02
    info = qsfp_lib.decode_sff8636_inventory("A", 0x11, 0x07, bytes(page00))
    assert info.transceiver_type == "100GBASE-SR4 or 25GBASE-SR"
    assert info.max_power_w == pytest.approx(3.5)
    assert info.wavelength_nm == pytest.approx(850.0)
    assert info.link_lengths == (("OM3", 70.0),)
    assert info.cable_length_m is None
    assert info.attenuation_db == ()
    assert info.needs_high_power is True


def test_copper_media_id_resolved_from_host_when_media_class_is_bogus():
    # QSFP-DD DACs copy the SFF-8024 identifier into byte 128, so the media
    # class is unusable and the copper table has to come from the host ID.
    apps = decode_applications(bytes([0x49, 0x01, 0x1D, 0x01]), 0x18)
    assert [app.media_name for app in apps] == ["Copper cable", "Copper cable"]
    assert qsfp_lib.module_needs_high_power(0x18, None, apps) is False

    optical = decode_applications(bytes([0x52, 0x00, 0x50, 0x1C]), 0x18)
    assert optical[1].media_name == "400GBASE-DR4"
    assert qsfp_lib.module_needs_high_power(0x18, None, optical) is True


def test_infiniband_slots_inherit_the_copper_class_of_the_whole_module():
    # QSFP112 DACs advertise BASE-CR and InfiniBand rates together. IB runs on
    # either medium, so those slots must not fall out of the copper table.
    apps = decode_applications(
        bytes([0x48, 0x01, 0x1C, 0x01, 0x1A, 0x01, 0x32, 0x01, 0x31, 0x01, 0x2C, 0x01]),
        0x1E,
    )
    assert [app.host_name for app in apps][3:] == ["IB NDR", "IB HDR", "IB SDR"]
    assert {app.media_name for app in apps} == {"Copper cable"}
    assert qsfp_lib.module_needs_high_power(0x1E, 0x23, apps) is False


def test_effective_media_class_prefers_a_real_cmis_class():
    assert qsfp_lib.effective_media_class(0x02, [0x48]) == 0x02
    assert qsfp_lib.effective_media_class(0x18, [0x48]) == 0x03
    assert qsfp_lib.effective_media_class(0x18, [0x52]) is None
    assert qsfp_lib.effective_media_class(0x18, [0x2C]) is None


def test_inventory_reports_partial_data_when_upper_page_is_unreadable():
    # A flat-memory module can refuse page 00h; lower memory still answers.
    def handler(words, timeout):
        packed = int(words[1])
        field = (packed >> 16) & 0xFF
        cage_index = (packed >> 8) & 0xFF
        if field == INVENTORY_FIELDS["identifier"]:
            return _response_words(0, OP_INVENTORY, cage_index, bytes([0x1E, 0x50]))
        if field == INVENTORY_FIELDS["applications"]:
            return _response_words(
                0, OP_INVENTORY, cage_index, bytes([0x48, 0x01, 0x1C, 0x01])
            )
        return _response_words(3, OP_INVENTORY, cage_index, b"")

    info = QsfpManager(FakeChip(handler=handler)).inventory("A")
    assert info.identifier_name == "QSFP+ w/ CMIS"
    assert info.connector is None
    assert info.connector_name == "unavailable"
    assert info.media_type_name == "unavailable"
    assert info.vendor_pn == ""
    assert info.applications[0].host_name == "400GBASE-CR4"
    assert info.needs_high_power is False


def test_page_lower_uses_offset_zero_and_its_own_index():
    seen = []

    def handler(words, timeout):
        packed = int(words[1])
        argument = (packed >> 16) & 0xFF
        seen.append(argument >> 4)
        return _response_words(0, OP_READ_PAGE, 0, bytes(QSFP_MGMT_PAYLOAD_SIZE))

    result = QsfpManager(FakeChip(handler=handler)).page("A", PAGE_LOWER, 20)
    assert seen == [PAGE_INDEX[PAGE_LOWER]]
    assert result.offset == 0
    assert decode_page("A", 0x11, b"\x00").offset == 128


def test_inventory_tolerates_old_firmware_without_applications():
    def handler(words, timeout):
        packed = int(words[1])
        field = (packed >> 16) & 0xFF
        cage_index = (packed >> 8) & 0xFF
        payloads = {
            0: bytes([0x18, 0x40]),
            1: b"ACME",
            2: bytes([0, 1, 2]),
            3: b"PN",
            4: b"A1",
            5: b"SN",
            6: b"260101",
            7: bytes([0x23]),
            8: bytes([0x03]),
        }
        if field == INVENTORY_FIELDS["applications"]:
            return _response_words(1, OP_INVENTORY, cage_index, b"")
        return _response_words(0, OP_INVENTORY, cage_index, payloads[field])

    info = QsfpManager(FakeChip(handler=handler)).inventory("B")
    assert info.cage == "B"
    assert info.connector_name == "No separable connector"
    assert info.media_type_name == "Passive copper"
    assert info.applications == ()
    assert info.needs_high_power is False


def test_cli_info_prints_decoded_codes(monkeypatch, capsys):
    fields = {
        "identifier": bytes([0x18, 0x40]),
        "vendor_name": b"FS",
        "vendor_oui": bytes([0x00, 0x00, 0x00]),
        "vendor_pn": b"QDD-800G-PC005",
        "vendor_rev": b"A",
        "vendor_sn": b"C1",
        "date_code": b"250101",
        "connector": bytes([0x23]),
        "media_type": bytes([0x03]),
        "applications": bytes([0x49, 0x00]),
    }

    class FakeManager:
        def __init__(self, chip):
            pass

        def inventory(self, cage):
            return decode_inventory(cage, fields)

        def status(self, cage):
            return SimpleNamespace(low_power=True)

    monkeypatch.setattr(qsfp, "detect_chip", lambda asic_id: object())
    monkeypatch.setattr(qsfp, "QsfpManager", FakeManager)
    qsfp.main(["info", "B"])
    out = capsys.readouterr().out
    assert "No separable connector" in out
    assert "Passive copper" in out
    assert "800GBASE-CR8" in out
    assert "Power  " not in out


def test_cli_info_power_line_follows_the_lpmode_pin(capsys):
    fields = {
        "identifier": bytes([0x18, 0x40]),
        "connector": bytes([0x0C]),
        "media_type": bytes([0x18]),
        "applications": bytes([0x0B, 0x09]),
    }
    info = decode_inventory("A", fields)
    assert info.needs_high_power is True

    qsfp._print_info(info, low_power=True)
    assert "power A high" in capsys.readouterr().out

    qsfp._print_info(info, low_power=False)
    assert "LPMODE deasserted, lasers enabled" in capsys.readouterr().out
