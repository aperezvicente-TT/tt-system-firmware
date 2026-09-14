#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Discover and manage P150A QSFP-DD cages through PCIe."""

import argparse
from datetime import datetime
import json
import sys
import time
from typing import Callable, Optional, Sequence

from qsfp_lib import (
    CAGES,
    MEDIA_CLASS_PASSIVE_COPPER,
    MODULE_STATE_READY,
    PAGE_LOWER,
    QsfpError,
    QsfpManager,
    SFF8636_IDS,
    detect_chip,
    read_discovery,
    read_telemetry_tag,
    structured,
    TAG_QSFP_STATUS,
)


def _manager(asic_id: int) -> QsfpManager:
    return QsfpManager(detect_chip(asic_id))


def _add_watch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--watch",
        nargs="?",
        const=1.0,
        type=float,
        metavar="SEC",
        help="repeat every SEC seconds (default 1.0)",
    )


def _add_json_watch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    _add_watch(parser)


def _repeat(action: Callable[[], None], interval: Optional[float]) -> None:
    if interval is not None and interval <= 0:
        raise ValueError("--watch interval must be > 0")
    while True:
        action()
        if interval is None:
            return
        time.sleep(interval)


def _emit_json(value) -> None:
    print(json.dumps(structured(value), sort_keys=True), flush=True)


def _print_discovery(result, raw: bool = False) -> None:
    if raw:
        print(f"0x{result.raw:08x}", flush=True)
        return
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{stamp}  ASIC {result.asic_id}: TAG_QSFP_STATUS = 0x{result.raw:08x}")
    descriptions = {
        "expander_absent": "expander absent (cage unpopulated?)",
        "no_module": "expander ok, no module seated",
        "cmis_read_failed": "module seated, CMIS read failed",
        "bus_stuck": "MCU_I2C0 stuck low (discovery aborted)",
    }
    for cage in result.cages:
        if cage.state == "present":
            detail = (
                f"module present, id=0x{cage.identifier:02x} "
                f"({cage.identifier_name})"
            )
        elif cage.state == "unexpected":
            detail = f"unexpected 0x{cage.raw:02x}"
        else:
            detail = descriptions[cage.state]
        print(f"  QSFP {cage.cage}: {detail}")
    print(flush=True)


def _print_status(state) -> None:
    if not state.present:
        print(f"QSFP {state.cage}: empty")
        return
    print(
        f"QSFP {state.cage}: id=0x{state.identifier:02x} "
        f"({state.identifier_name}), {state.revision_string}, "
        f"power={'low' if state.low_power else 'high'}, "
        f"state={state.module_state_name}, "
        f"IntL={'asserted' if state.interrupt_asserted else 'deasserted'}"
    )


def _code(value, name: str) -> str:
    return name if value is None else f"0x{value:02x} ({name})"


def _print_info(info, low_power: Optional[bool] = None) -> None:
    print(f"QSFP {info.cage}")
    print(
        f"  {'Identifier':40s} : 0x{info.identifier:02x} "
        f"({info.identifier_name})"
    )
    spec = getattr(info, "spec", "CMIS")
    if spec == "CMIS":
        print(f"  {'Revision Compliance':40s} : CMIS {info.revision_string}")
    else:
        print(f"  {'Revision Compliance':40s} : {info.revision_string}")
    print(f"  {'Connector':40s} : {_code(info.connector, info.connector_name)}")
    if spec == "CMIS":
        print(f"  {'Media type':40s} : {_code(info.media_type, info.media_type_name)}")
    elif info.transceiver_type:
        print(f"  {'Transceiver type':40s} : {info.transceiver_type}")
    print(f"  {'Vendor name':40s} : {info.vendor_name}")
    print(f"  {'Vendor OUI':40s} : {info.vendor_oui}")
    print(f"  {'Vendor PN':40s} : {info.vendor_pn}")
    print(f"  {'Vendor rev':40s} : {info.vendor_rev}")
    print(f"  {'Vendor SN':40s} : {info.vendor_sn}")
    print(f"  {'Date code':40s} : {info.date_code}")
    if info.power_class is not None:
        power = info.power_class_name
        if info.spec == "CMIS" and info.max_power_w is not None:
            power = f"{power}, {info.max_power_w:.2f} W max"
        print(f"  {'Power class':40s} : {power}")
    if info.cable_length_m is not None:
        length = info.cable_length_m
        printed = f"{length:.1f} m" if length < 10 else f"{length:.0f} m"
        print(f"  {'Cable length':40s} : {printed}")
    if info.wavelength_nm:
        print(f"  {'Wavelength':40s} : {info.wavelength_nm:g} nm")
    for name, metres in info.link_lengths:
        printed = f"{metres / 1000:g} km" if metres >= 1000 else f"{metres:g} m"
        print(f"  {f'Link length ({name})':40s} : {printed}")
    if info.attenuation_db:
        bands = " / ".join(
            f"{ghz:g} GHz" for ghz in info.attenuation_ghz
        )
        values = " / ".join(f"{db} dB" for db in info.attenuation_db)
        print(f"  {'Attenuation':40s} : {values} at {bands}")
    if info.media_technology is not None:
        print(
            f"  {'Media technology':40s} : "
            f"0x{info.media_technology:02x} ({info.media_technology_name})"
        )
    if info.applications:
        for application in info.applications:
            print(
                f"  {f'AppSel {application.apsel}':40s} : "
                f"host 0x{application.host_id:02x} ({application.host_name}), "
                f"media 0x{application.media_id:02x} ({application.media_name})"
            )
    elif spec == "CMIS":
        print(f"  {'Applications':40s} : unavailable")
    if info.needs_high_power:
        if low_power is False:
            detail = "optical/active; LPMODE deasserted, lasers enabled"
        else:
            detail = (
                "optical/active; lasers need "
                f"`power {info.cage} high` (not enabled automatically)"
            )
        print(f"  {'Power':40s} : {detail}")


def _print_dom(manager: QsfpManager, cage: str, json_output: bool = False) -> None:
    module, lanes = manager.dom(cage)
    if json_output:
        _emit_json({"module": module, "lanes": lanes})
        return
    print(f"QSFP {module.cage}")
    print(f"  {'Module state':40s} : {module.module_state_name}")
    print(f"  {'Power mode':40s} : {'low' if module.low_power else 'high'}")
    print(
        f"  {'Interrupt':40s} : "
        f"{'asserted' if module.interrupt_asserted else 'deasserted'}"
    )
    if module.available:
        print(f"  {'Module temperature':40s} : {module.temperature_c:.2f} degrees C")
        print(f"  {'Module voltage':40s} : {module.voltage_v:.4f} V")
    else:
        detail = (
            "not implemented (passive copper cable)"
            if manager.media_class(cage) == MEDIA_CLASS_PASSIVE_COPPER
            else "unavailable (module reports no monitor)"
        )
        print(f"  {'Module temperature':40s} : {detail}")
        print(f"  {'Module voltage':40s} : {detail}")
    if module.low_power or not lanes:
        if manager.media_class(cage) == MEDIA_CLASS_PASSIVE_COPPER:
            print("  Lane monitors not implemented (passive copper cable)")
        elif module.low_power:
            print("  Lane monitors unavailable while hardware LPMODE is asserted")
        else:
            print("  Lane monitors unavailable")
        return
    for lane in lanes:
        number = lane.lane + 1
        if lane.tx_bias_supported:
            print(
                f"  Laser tx bias current (Lane {number})      : "
                f"{lane.bias_ma:.3f} mA"
            )
        if lane.tx_power_supported:
            print(
                f"  Transmit avg power (Lane {number})        : "
                f"{lane.tx_power_mw:.4f} mW"
            )
        if lane.rx_power_supported:
            print(
                f"  Receive avg power (Lane {number})         : "
                f"{lane.rx_power_mw:.4f} mW"
            )
        if lane.flags == 0:
            print(f"  Lane {number} monitors                     : unsupported")


def _print_lanes(result) -> None:
    labels = {
        "tx_fault": "TX fault",
        "tx_los": "TX LOS",
        "tx_cdr_lol": "TX CDR LOL",
        "tx_adaptive_eq_fault": "TX adaptive eq fault",
        "rx_los": "RX LOS",
        "rx_cdr_lol": "RX CDR LOL",
    }
    print(f"QSFP {result.cage}")
    for lane, state in enumerate(result.datapath_states, 1):
        print(f"  Lane {lane} datapath                     : {state}")
    for name in labels:
        if name not in result.flags:
            continue
        lanes = ", ".join(str(lane) for lane in result.flags[name])
        print(f"  {labels[name]:40s} : {lanes if lanes else 'none'}")
    if result.spec == "SFF-8636":
        print(f"  {'TxDisable (86)':40s} : 0x{result.tx_disable:02x}")
        return
    print(f"  {'DataPathDeinit (10h:128)':40s} : 0x{result.datapath_deinit:02x}")
    print(f"  {'TxDisable (10h:130)':40s} : 0x{result.tx_disable:02x}")
    print(
        f"  {'ApSel per lane (10h:145-152)':40s} : "
        f"{' '.join(f'{value:02x}' for value in result.apsel)}"
    )


def _page_arg(value: str):
    if value.lower() in ("lo", "lower"):
        return PAGE_LOWER
    return int(value, 16)


def _print_page(result) -> None:
    if result.page == PAGE_LOWER:
        print(f"QSFP {result.cage} CMIS lower memory")
    else:
        print(f"QSFP {result.cage} CMIS page {result.page:02x}h upper memory")
    for index in range(0, len(result.data), 16):
        row = result.data[index : index + 16]
        text_row = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in row)
        print(
            f"  {result.offset + index:3d}: "
            f"{' '.join(f'{byte:02x}' for byte in row):<47s}  {text_row}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asic-id", type=int, default=0, help="chip index (default 0)")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="show firmware cage discovery telemetry")
    discover.add_argument("--raw", action="store_true", help="print only the raw word")
    _add_json_watch(discover)

    status = sub.add_parser("status", help="show one cage or all cages")
    status.add_argument("cage", nargs="?", choices=CAGES)
    _add_json_watch(status)

    info = sub.add_parser("info", help="show CMIS inventory")
    info.add_argument("cage", choices=CAGES)
    _add_json_watch(info)

    power = sub.add_parser("power", help="set hardware LPMODE")
    power.add_argument("cage", choices=CAGES)
    power.add_argument("mode", choices=("low", "high"))
    power.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait for CMIS Ready after enabling high power",
    )

    reset = sub.add_parser("reset", help="pulse module ResetL")
    reset.add_argument("cage", choices=CAGES)

    dom = sub.add_parser("dom", help="show module and lane monitoring")
    dom.add_argument("cage", choices=CAGES)
    _add_json_watch(dom)

    lanes = sub.add_parser("lanes", help="show datapath state and lane faults")
    lanes.add_argument("cage", choices=CAGES)
    _add_json_watch(lanes)

    page = sub.add_parser("page", help="hex dump CMIS memory")
    page.add_argument("cage", choices=CAGES)
    page.add_argument(
        "page",
        type=_page_arg,
        help="hex page 00, 01, 02, 10, 11, or 'lower' for bytes 0-127",
    )
    page.add_argument("--length", type=int, default=128)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        watch = getattr(args, "watch", None)
        if watch is not None and watch <= 0:
            parser.error("--watch interval must be > 0")
        chip = detect_chip(args.asic_id)
        manager = QsfpManager(chip)

        if args.command == "discover":
            def show_discovery():
                result = read_discovery(chip, args.asic_id)
                if args.json:
                    _emit_json(result)
                else:
                    _print_discovery(result, args.raw)

            _repeat(show_discovery, args.watch)
        elif args.command == "status":
            cages = args.cage or CAGES

            def show_status():
                results = tuple(manager.status(cage) for cage in cages)
                if args.json:
                    _emit_json(results[0] if args.cage else results)
                else:
                    if args.watch is not None:
                        print(datetime.now().strftime("%H:%M:%S"))
                    for result in results:
                        _print_status(result)

            _repeat(show_status, args.watch)
        elif args.command == "info":
            def show_info():
                result = manager.inventory(args.cage)
                if args.json:
                    _emit_json(result)
                else:
                    _print_info(result, manager.status(args.cage).low_power)

            _repeat(show_info, args.watch)
        elif args.command == "power":
            if args.timeout < 0:
                parser.error("--timeout must be >= 0")
            manager.set_power(args.cage, args.mode)
            state = (
                manager.wait_ready(args.cage, args.timeout)
                if args.mode == "high"
                else manager.status(args.cage)
            )
            if (
                args.mode == "high"
                and state.identifier not in SFF8636_IDS
                and state.module_state != MODULE_STATE_READY
            ):
                print(
                    f"QSFP {args.cage}: still {state.module_state_name} after "
                    f"{args.timeout:g}s; LPMODE stays deasserted",
                    file=sys.stderr,
                )
            _print_status(state)
        elif args.command == "reset":
            manager.reset(args.cage)
            print(f"QSFP {args.cage}: reset pulse complete")
        elif args.command == "dom":
            _repeat(lambda: _print_dom(manager, args.cage, args.json), args.watch)
        elif args.command == "lanes":
            def show_lanes():
                result = manager.lanes(args.cage)
                _emit_json(result) if args.json else _print_lanes(result)

            _repeat(show_lanes, args.watch)
        elif args.command == "page":
            _print_page(manager.page(args.cage, args.page, args.length))
    except (QsfpError, OSError, RuntimeError, ValueError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        pass


def telem_main(argv: Optional[Sequence[str]] = None) -> None:
    """Compatibility entry point for the original qsfp_telem.py interface."""
    parser = argparse.ArgumentParser(
        description="Read QSFP-DD cage discovery status over PCIe "
        "(no JTAG probe required)."
    )
    parser.add_argument("--asic-id", type=int, default=0, help="chip index (default 0)")
    parser.add_argument("--raw", action="store_true", help="print only the raw hex word")
    parser.add_argument("--watch", nargs="?", const=1.0, type=float, metavar="SEC")
    args = parser.parse_args(argv)
    if args.watch is not None and args.watch <= 0:
        parser.error("--watch interval must be > 0")
    try:
        chip = detect_chip(args.asic_id)
        result = read_discovery(
            chip, args.asic_id, include_dm_version=not args.raw
        )
        if result.dm_app_fw_version == 0 and not args.raw:
            print(
                "warning: DM app fw version telemetry reads 0 - DMC may not have sent "
                "static info yet, or firmware predates TAG_QSFP_STATUS.",
                file=sys.stderr,
            )
        _print_discovery(result, args.raw)
        if args.watch is None:
            return
        last = result.raw
        while True:
            time.sleep(args.watch)
            word = read_telemetry_tag(chip, TAG_QSFP_STATUS)
            if word != last or args.raw:
                last = word
                _print_discovery(
                    read_discovery(chip, args.asic_id, include_dm_version=False),
                    args.raw,
                )
            else:
                stamp = datetime.now().strftime("%H:%M:%S")
                print(
                    f"{stamp}  ASIC {args.asic_id}: TAG_QSFP_STATUS = "
                    f"0x{word:08x} (unchanged)",
                    flush=True,
                )
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        pass


def mgmt_main(argv: Optional[Sequence[str]] = None) -> None:
    """Compatibility entry point for the original qsfp_mgmt.py interface."""
    parser = argparse.ArgumentParser(
        description="Manage P150A QSFP-DD cages through PCIe -> SMC -> DMC."
    )
    parser.add_argument("--asic-id", type=int, default=0)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="show all cages")
    _add_watch(listing)
    info = sub.add_parser("info", help="show CMIS inventory")
    info.add_argument("cage", choices=CAGES)
    power = sub.add_parser("power", help="set hardware LPMODE")
    power.add_argument("cage", choices=CAGES)
    power.add_argument("mode", choices=("low", "high"))
    power.add_argument("--timeout", type=float, default=15.0)
    reset = sub.add_parser("reset", help="pulse module ResetL")
    reset.add_argument("cage", choices=CAGES)
    dom = sub.add_parser("dom", help="show module/lane monitoring")
    dom.add_argument("cage", choices=CAGES)
    _add_watch(dom)
    lanes = sub.add_parser("lanes", help="show CMIS datapath state and lane faults")
    lanes.add_argument("cage", choices=CAGES)
    _add_watch(lanes)
    page = sub.add_parser("page", help="hex dump CMIS memory")
    page.add_argument("cage", choices=CAGES)
    page.add_argument("page", type=_page_arg)
    page.add_argument("--length", type=int, default=128)
    args = parser.parse_args(argv)

    try:
        manager = _manager(args.asic_id)
        if args.command == "list":
            def show_list():
                print(datetime.now().strftime("%H:%M:%S"))
                for cage in CAGES:
                    _print_status(manager.status(cage))

            _repeat(show_list, args.watch)
        elif args.command == "info":
            _print_info(
                manager.inventory(args.cage), manager.status(args.cage).low_power
            )
        elif args.command == "power":
            manager.set_power(args.cage, args.mode)
            state = (
                manager.wait_ready(args.cage, args.timeout)
                if args.mode == "high"
                else manager.status(args.cage)
            )
            if (
                args.mode == "high"
                and state.identifier not in SFF8636_IDS
                and state.module_state != MODULE_STATE_READY
            ):
                print(
                    f"QSFP {args.cage}: still {state.module_state_name} after "
                    f"{args.timeout:g}s; LPMODE stays deasserted",
                    file=sys.stderr,
                )
            _print_status(state)
        elif args.command == "reset":
            manager.reset(args.cage)
            print(f"QSFP {args.cage}: reset pulse complete")
        elif args.command == "dom":
            def show_dom():
                print(datetime.now().strftime("%H:%M:%S"))
                _print_dom(manager, args.cage)

            _repeat(show_dom, args.watch)
        elif args.command == "lanes":
            def show_lanes():
                print(datetime.now().strftime("%H:%M:%S"))
                _print_lanes(manager.lanes(args.cage))

            _repeat(show_lanes, args.watch)
        elif args.command == "page":
            _print_page(manager.page(args.cage, args.page, args.length))
    except (QsfpError, OSError, RuntimeError, ValueError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
